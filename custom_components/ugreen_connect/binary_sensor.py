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
from .const import X783_PORTS
from .coordinator import UgreenCoordinator, device_key
from .entity import UgreenPortEntity
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
            if key is None or not (coordinator.data.get("power") or {}).get(key):
                continue
            for port in X783_PORTS:
                if (key, port) in known:
                    continue
                known.add((key, port))
                new.append(UgreenChargingSensor(coordinator, key, port))
        if new:
            async_add_entities(new)

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class UgreenChargingSensor(UgreenPortEntity, BinarySensorEntity):
    """On while charge is actually flowing into whatever is on the port."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_translation_key = "charging"

    def __init__(self, coordinator: UgreenCoordinator, key: str, port: str) -> None:
        super().__init__(coordinator, key)
        self._port = port
        self._attr_unique_id = f"{key}_{port}_charging"

    @property
    def _session(self) -> Session | None:
        return self.coordinator.sessions.session(self._key, self._port)

    @property
    def is_on(self) -> bool:
        session = self._session
        return bool(session and session.active)
