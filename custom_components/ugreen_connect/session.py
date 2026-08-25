"""Charging sessions: how much a port has delivered since the current device was plugged in.

A session runs from the moment something is attached to a port until it is taken off
again, and its total stays on show afterwards -- so the answer to "how much did that
get?" is still there when you come back to look. Plugging the next thing in starts over.

Deliberately free of Home Assistant imports so the rules can be tested on their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# How long a port must read empty before its session is called finished. The charger
# briefly reports "none" mid-renegotiation, and without this every such blip would
# reset the counter.
UNPLUG_DEBOUNCE = 30.0

# Longest span between two readings that is still treated as continuous. The cloud
# drops out for minutes at a time, and carrying the last known power across such a
# hole would invent energy that was never delivered. Under-counting a real gap is
# the safer error, so the interval is simply dropped.
MAX_GAP = 60.0

# Below this the reading is the 0.1 A measurement quantum rather than charge going
# anywhere: a full phone left plugged in reports 0.1 A, which at 9 V looks like 0.9 W
# and would invent close to a whole battery over a night. The threshold is on current
# rather than power because the quantum is fixed while the port voltage is not.
IDLE_CURRENT = 0.15


def _plugged(values: dict) -> bool:
    """Whether anything is attached: an empty port reads 0 V and no protocol."""
    return values.get("protocol", "none") != "none" or (values.get("voltage") or 0.0) > 0.5


def _swapped(before: str, now: str) -> bool:
    """Whether a different device answered, rather than the same one re-negotiating."""
    return now != "none" and before not in ("none", now)


@dataclass
class Session:
    """One device's stay on one port."""

    energy_wh: float = 0.0
    # False until a reading shows something attached, so a socket nobody has used
    # does not read as a session in progress.
    active: bool = False
    protocol: str = "none"
    started_at: float | None = None
    ended_at: float | None = None
    peak_w: float = 0.0
    # Sampling state, not part of what a restart carries over.
    last_ts: float | None = None
    last_power: float = 0.0
    empty_since: float | None = None
    pending_restore: bool = field(default=False, repr=False)

    @property
    def duration(self) -> float:
        """Seconds the device has been on the port, or was on it."""
        until = self.ended_at if self.ended_at is not None else self.last_ts
        if self.started_at is None or until is None:
            return 0.0
        return until - self.started_at

    @property
    def average_w(self) -> float:
        span = self.duration
        return self.energy_wh * 3600 / span if span > 0 else 0.0

    def as_dict(self) -> dict:
        """The part worth carrying across a restart -- live sampling state is not."""
        return {
            "energy_wh": self.energy_wh,
            "active": self.active,
            "protocol": self.protocol,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "peak_w": self.peak_w,
        }


class SessionTracker:
    """Accumulates per-port charging sessions from successive readings."""

    def __init__(self, max_gap: float = MAX_GAP) -> None:
        self._max_gap = max_gap
        self._sessions: dict[tuple[str, str], Session] = {}

    def session(self, key: str, port: str) -> Session | None:
        return self._sessions.get((key, port))

    def restore(self, key: str, port: str, saved: dict) -> None:
        """Take back a session saved before a restart.

        ``last_ts`` is deliberately left unset: nothing is known about what the port
        did while Home Assistant was down, so the first reading after this only
        re-establishes the baseline rather than integrating across the outage.
        """
        self._sessions[(key, port)] = Session(
            energy_wh=saved.get("energy_wh") or 0.0,
            active=bool(saved.get("active")),
            protocol=saved.get("protocol") or "none",
            started_at=saved.get("started_at"),
            ended_at=saved.get("ended_at"),
            peak_w=saved.get("peak_w") or 0.0,
            pending_restore=True,
        )

    def update(self, now: float, key: str, ports: dict[str, dict]) -> None:
        """Fold one poll's readings into each port's session.

        Call this only for polls that actually returned data: a failed poll must leave
        every session exactly as it was, or an outage would read as an unplug.
        """
        for port, values in ports.items():
            state = self._sessions.setdefault((key, port), Session())
            plugged = _plugged(values)
            protocol = values.get("protocol", "none")

            if state.pending_restore:
                state.pending_restore = False
                if not plugged:
                    # Whatever was charging left while Home Assistant was down.
                    self._finish(now, state)
                    continue
                if _swapped(state.protocol, protocol):
                    state = self._restart(key, port)

            if not plugged:
                self._empty(now, state)
                continue

            # A new session either when the previous device's total has been sitting
            # finished, or when the port came back mid-debounce speaking a different
            # protocol -- that is a swap, not a blip. A session that has never begun
            # is neither: it is simply about to.
            finished = state.started_at is not None and not state.active
            swapped = state.empty_since is not None and _swapped(state.protocol, protocol)
            if finished or swapped:
                state = self._restart(key, port)
            self._advance(now, state, protocol, values)

    def _restart(self, key: str, port: str) -> Session:
        state = Session()
        self._sessions[(key, port)] = state
        return state

    def _advance(self, now: float, state: Session, protocol: str, values: dict) -> None:
        """Charge one more reading's worth of energy into a running session."""
        state.protocol = protocol
        state.active = True
        state.empty_since = None
        state.ended_at = None
        if state.started_at is None:
            state.started_at = now

        power = values.get("power") or 0.0
        state.peak_w = max(state.peak_w, power)
        if (values.get("current") or 0.0) < IDLE_CURRENT:
            power = 0.0

        if state.last_ts is not None:
            span = now - state.last_ts
            if 0 < span <= self._max_gap:
                state.energy_wh += (state.last_power + power) / 2 * span / 3600
        state.last_ts = now
        state.last_power = power

    def _empty(self, now: float, state: Session) -> None:
        """An empty reading: note when it started, and end the session if it holds."""
        if state.empty_since is None:
            state.empty_since = now
            if state.started_at is not None:
                state.ended_at = now
        elif now - state.empty_since >= UNPLUG_DEBOUNCE:
            state.active = False

    def _finish(self, now: float, state: Session) -> None:
        state.active = False
        if state.ended_at is None:
            state.ended_at = now


def charge_mah(energy_wh: float, nominal_v: float, efficiency: float) -> float:
    """Watt-hours out of the port, as charge into a battery -- an estimate, not a reading.

    The charger measures at its own connector, where a PD device may be taking 5, 9 or
    28 V; the cell behind it sits near 3.85 V and the converter in between loses some of
    what arrives. Both of those are assumptions, which is why every label says "about".
    """
    if nominal_v <= 0:
        return 0.0
    return energy_wh / nominal_v * 1000 * efficiency
