"""Binary sensor platform: whether a port is actually charging.

Wattage alone does not answer that question on this charger. A full device
still reports the 0.1 A measurement quantum, and a bare cable produces a stray
current the charger itself calls 0.0 W -- so a template threshold either
invents charge overnight or misses a trickle. The session tracker already has
to settle this to do its own job; this publishes its answer rather than
leaving everyone to rebuild the thresholds themselves.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import UgreenConfigEntry
from .coordinator import UgreenCoordinator, device_key
from .entity import UgreenDeviceEntity
from .session import Session


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UgreenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add_new_devices() -> None:
        new: list[BinarySensorEntity] = []
        for device in coordinator.data.get("devices", []):
            key = device_key(device)
            reading = (coordinator.data.get("power") or {}).get(key)
            if key is None or not reading:
                continue
            for port in reading["ports"] or {}:
                if (key, port) in known:
                    continue
                known.add((key, port))
                new.append(UgreenChargingSensor(coordinator, key, port))
        if new:
            async_add_entities(new)

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class UgreenChargingSensor(UgreenDeviceEntity, BinarySensorEntity):
    """On while charge is actually flowing into whatever is on the port."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: UgreenCoordinator, key: str, port: str) -> None:
        super().__init__(coordinator, key)
        self._port = port
        self._attr_translation_key = "charging"
        self._attr_translation_placeholders = {"port": port}
        self._attr_unique_id = f"{key}_{port}_charging"

    @property
    def _session(self) -> Session | None:
        return self.coordinator.sessions.session(self._key, self._port)

    @property
    def available(self) -> bool:
        # The power sensors of the same port go unavailable when a reading does
        # not arrive, and this has to go with them: a charging state held over
        # from before an outage is asserted rather than merely stale, and the
        # longer it holds the more confidently it is wrong.
        return super().available and self._reading is not None

    @property
    def is_on(self) -> bool:
        session = self._session
        return bool(session and session.delivering)
