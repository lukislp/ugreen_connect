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

import time
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import UgreenConfigEntry
from .coordinator import UgreenCoordinator, device_key
from .entity import UgreenDeviceEntity
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
            reading = (coordinator.data.get("power") or {}).get(key)
            if key is None or not reading:
                continue
            for port in reading["ports"] or {}:
                if (key, port) in known:
                    continue
                known.add((key, port))
                new.append(UgreenChargingEvent(coordinator, key, port))
        if new:
            async_add_entities(new)

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class UgreenChargingEvent(UgreenDeviceEntity, EventEntity):
    """Fires when a bout of charging starts on this port, and when it ends."""

    _attr_event_types = [STARTED, ENDED]
    _attr_icon = "mdi:battery-charging"

    def __init__(self, coordinator: UgreenCoordinator, key: str, port: str) -> None:
        super().__init__(coordinator, key)
        self._port = port
        self._attr_translation_key = "charging_event"
        self._attr_translation_placeholders = {"port": port}
        self._attr_unique_id = f"{key}_{port}_charging_event"
        # What the last poll saw, so a change can be recognised as one.
        self._started_at: float | None = None
        self._active = False
        # The last figures seen, so an ended bout is reported as it was rather
        # than as whatever replaced it.
        self._last: dict[str, Any] | None = None
        # A bout that began before this entity existed is old news. Compared
        # against rather than skipping the first update, which swallowed a real
        # start: entities are built inside a coordinator listener, so the poll
        # that adds one is never delivered to it and "the first update" is
        # already the next one -- a bout starting there lost its STARTED and
        # ended later with nothing to pair the end with. It also does not care
        # whether the session has been restored yet, and the restore happens on
        # a platform forwarded after this one.
        #
        # Two cases still produce a lone ENDED, for different reasons: a bout
        # that survives a restart, whose `started_at` comes out of storage and
        # so predates this entity by design; and one starting on the very poll
        # that adds the entity, which loses only because the coordinator stamps
        # its readings before the listener that builds entities runs. Both are
        # arguably right -- a charge still running when Home Assistant came
        # back, and finishing afterwards, is worth being told about even though
        # nothing announced its start. A bout that ended during the downtime
        # announces nothing at all: it is finished on the first poll, while
        # `_active` is still False.
        self._added_at = time.time()

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

        started = (
            session.started_at is not None
            and session.started_at != self._started_at
            and session.started_at >= self._added_at
        )
        # The bout, not the flow. A port with a full phone on it stops and
        # resumes every few minutes all night, and every one of those is a real
        # change in whether charge is moving -- which is what the battery
        # sensor is for. "Finished" is a different claim, and firing it thirty
        # times before breakfast would make it worthless.
        #
        # Not an elif: a device swapped inside the unplug debounce ends one bout
        # and starts another in the same poll, and the automation waiting for
        # the first to finish is exactly the one that would never hear about it.
        # Ended first, because that is the order it happened in -- and with the
        # figures from the bout that ended, which the tracker has already
        # replaced by the time this runs.
        if self._active and (started or not session.active):
            self._trigger_event(ENDED, self._last or self._details(session))
            # Written here rather than left to the update at the end. The base
            # class records an event and returns; the state is what carries it
            # to anything listening, and one write at the end would let the
            # second of a pair overwrite the first -- which is exactly the
            # swap case the two calls exist for.
            self.async_write_ha_state()
        if started:
            self._trigger_event(STARTED, self._details(session))
            self.async_write_ha_state()

        self._started_at = session.started_at
        self._active = session.active
        self._last = self._details(session)
        super()._handle_coordinator_update()
