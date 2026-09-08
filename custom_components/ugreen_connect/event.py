"""Event platform: a port's charging starting and finishing.

The session sensors already say how much a bout delivered, but only while
someone is looking. What people actually want is to be told -- "the laptop is
done" -- and for that an automation needs a moment to fire on, not a number to
watch.

These are event entities rather than plain bus events so that the automation
editor offers them as device triggers, with the bout's figures carried along:
a notification can say how much went in and how long it took without reading
anything back.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import UgreenConfigEntry
from .const import X783_PORTS
from .coordinator import UgreenCoordinator, device_key
from .entity import UgreenPortEntity
from .session import Session

STARTED = "started"
ENDED = "ended"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UgreenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add_new_devices() -> None:
        new: list[EventEntity] = []
        for device in coordinator.data.get("devices", []):
            key = device_key(device)
            if key is None or not (coordinator.data.get("power") or {}).get(key):
                continue
            for port in X783_PORTS:
                if (key, port) in known:
                    continue
                known.add((key, port))
                new.append(UgreenChargingEvent(coordinator, key, port))
        if new:
            async_add_entities(new)

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class UgreenChargingEvent(UgreenPortEntity, EventEntity):
    """Fires when a bout of charging starts on this port, and when it ends."""

    _attr_event_types = [STARTED, ENDED]
    _attr_translation_key = "charging"
    _attr_icon = "mdi:battery-charging"

    def __init__(self, coordinator: UgreenCoordinator, key: str, port: str) -> None:
        super().__init__(coordinator, key)
        self._port = port
        self._attr_unique_id = f"{key}_{port}_charging_event"
        # What the last poll saw, so a change can be recognised as one.
        self._started_at: float | None = None
        self._active = False
        # The first update after a start only records where things stand. A
        # session restored across a restart has been running for hours and its
        # start is old news; announcing it would wake the house at boot.
        self._primed = False

    @property
    def _session(self) -> Session | None:
        return self.coordinator.sessions.session(self._key, self._port)

    def _details(self, session: Session) -> dict[str, Any]:
        return {
            "energy_wh": round(session.energy_wh, 3),
            "duration": round(session.duration),
            "peak_power": session.peak_w,
            "protocol": session.protocol,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        session = self._session
        if session is None:
            super()._handle_coordinator_update()
            return

        if not self._primed:
            self._primed = True
        elif session.started_at is not None and session.started_at != self._started_at:
            # A bout that was not running last time, or a different one: the
            # tracker starts a new Session rather than reusing the old.
            self._trigger_event(STARTED, self._details(session))
        elif self._active and not session.active:
            self._trigger_event(ENDED, self._details(session))

        self._started_at = session.started_at
        self._active = session.active
        super()._handle_coordinator_update()
