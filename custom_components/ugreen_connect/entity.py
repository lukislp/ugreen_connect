"""Shared entity base: which device an entity belongs to, and its metadata."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    async_get as async_get_device_registry,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import UgreenCoordinator, device_key

# extra.onlineStatus / extra.networkStatus are 1 when up, 0 when down.
ONLINE = 1


class UgreenDeviceEntity(CoordinatorEntity[UgreenCoordinator]):
    """Common plumbing for every platform.

    Deliberately not a ``SensorEntity``: the controls derive from this too, and
    a sensor refuses to be added with a config entity category.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: UgreenCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key

    @property
    def _device(self) -> dict[str, Any]:
        for device in self.coordinator.data.get("devices", []):
            if device_key(device) == self._key:
                return device
        return {}

    @property
    def _product(self) -> dict[str, Any]:
        product = self.coordinator.data.get("detail", {}).get(self._key)
        return product if isinstance(product, dict) else {}

    @property
    def _reading(self) -> dict[str, Any] | None:
        return (self.coordinator.data.get("power") or {}).get(self._key)

    @property
    def _iot_id(self) -> str | None:
        return (self._device.get("extra") or {}).get("iotId")

    @property
    def available(self) -> bool:
        return super().available and bool(self._device)

    @property
    def device_info(self) -> DeviceInfo:
        device = self._device
        info = DeviceInfo(
            identifiers={(DOMAIN, self._key)},
            manufacturer="UGREEN",
            name=device.get("deviceName") or f"UGREEN {self._key}",
            model=self._product.get("name") or device.get("deviceName"),
            model_id=self._product.get("productNo"),
            # deviceUniqueCode, which the charger confirms is its own serial:
            # asking it directly with GET_SN answers with the same string.
            serial_number=self._key,
        )
        if firmware := (self._reading or {}).get("firmware"):
            info["sw_version"] = firmware
        if mac := device.get("deviceMac"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        return info


class UgreenPortEntity(UgreenDeviceEntity):
    """An entity belonging to one port rather than to the charger as a whole.

    The charger is a single box, but a port is what anyone actually reasons
    about -- "what is C3 doing" -- and eight ports with six readings each turn
    one device page into a list of fifty. So each port becomes a device of its
    own, hung under the charger with ``via_device``, and Home Assistant gives
    it a page of its own.

    Every entity here belongs to a socket. The custom mode's shared C6+A
    setting is published once per socket rather than as a device of its own,
    which would be a device that is not a port and holds a single entity.
    """

    _port: str

    @property
    def device_info(self) -> DeviceInfo:
        charger = super().device_info
        info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._key}_{self._port}")},
            manufacturer="UGREEN",
            name=f"{charger.get('name')} {self._port}",
            model="Charging port",
        )
        # The parent is named by its registry id rather than its identifiers:
        # `via_device` was retired from DeviceInfo. The charger's own entity is
        # added in the same batch and ahead of these, so it is already there --
        # and if it somehow is not, the port simply stands on its own until the
        # next start rather than failing to appear at all.
        # Looked up by identifier within this config entry: identifiers are no
        # longer unique across entries, so the plain `async_get_device` cannot
        # say which charger it found.
        parent = async_get_device_registry(self.hass).async_get_device_by_identifier(
            (DOMAIN, self._key), self.coordinator.config_entry.entry_id
        )
        if parent:
            info["via_device_id"] = parent.id
        return info
