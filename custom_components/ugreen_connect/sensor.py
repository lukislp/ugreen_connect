"""Sensor platform for UGREEN Connect.

Two kinds of entity are published: the account's device inventory with its
online state, and -- for chargers that answer the RTCX gateway's binary
``PT_data`` protocol -- live voltage, current and power for every port.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import UgreenConfigEntry
from .const import (
    CONF_EFFICIENCY,
    CONF_NOMINAL_VOLTAGE,
    DEFAULT_EFFICIENCY,
    DEFAULT_NOMINAL_VOLTAGE,
    CUSTOM_PORTS,
    CUSTOM_SHARED_GROUP,
    CUSTOM_SHARED_MEMBERS,
    DOMAIN,
    HANDSHAKE_PROTOCOL,
    X783_PORTS,
)
from .coordinator import UgreenCoordinator, device_key
from .entity import ONLINE, UgreenDeviceEntity, UgreenPortEntity
from .session import Session, charge_mah

# The report always carries all eight slots.
MEASUREMENTS: dict[str, tuple[SensorDeviceClass, str, int]] = {
    "power": (SensorDeviceClass.POWER, UnitOfPower.WATT, 1),
    "voltage": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, 1),
    "current": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE, 1),
}

# Every port the report carries gets entities, DC included: which sockets a
# given model actually has is not something this can know, and a port nobody
# uses simply reads zero.
ALWAYS_PORTS: tuple[str, ...] = X783_PORTS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UgreenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up connectivity and, where available, live power sensors."""
    coordinator = entry.runtime_data
    known: set[str] = set()
    known_ports: set[tuple[str, str]] = set()

    # A port only reveals itself by drawing power, but once it has, its entities
    # should stay put -- otherwise unplugging a cable makes them vanish on the
    # next restart, taking their history with them. The registry remembers.
    registry = er.async_get(hass)
    seen_before = {
        (key, port)
        for key in {device_key(d) for d in coordinator.data.get("devices", [])}
        if key
        for port in X783_PORTS
        if registry.async_get_entity_id("sensor", DOMAIN, f"{key}_{port}_power")
    }

    @callback
    def _add_new_devices() -> None:
        new: list[SensorEntity] = []
        for device in coordinator.data.get("devices", []):
            key = device_key(device)
            if key is None:
                continue
            if key not in known:
                known.add(key)
                new.append(UgreenStatusSensor(coordinator, key))
                new.append(UgreenLastPollSensor(coordinator, key))

            reading = (coordinator.data.get("power") or {}).get(key)
            if not reading:
                continue
            if (key, "total") not in known_ports:
                known_ports.add((key, "total"))
                new.append(UgreenTotalPowerSensor(coordinator, key))
            # Only once the charger has actually reported a custom mode: a
            # device that has never had one configured would otherwise carry
            # six entities that can never say anything.
            if reading.get("custom") and (key, "custom") not in known_ports:
                known_ports.add((key, "custom"))
                for port in CUSTOM_PORTS:
                    if port != CUSTOM_SHARED_GROUP:
                        new.append(UgreenCustomLimitSensor(coordinator, key, port))
                        continue
                    # One setting, two sockets: both say so, and each names
                    # the other.
                    new.extend(
                        UgreenCustomLimitSensor(coordinator, key, socket, group=port,
                                                shared_with=other)
                        for socket, other in CUSTOM_SHARED_MEMBERS.items()
                    )
            # Every real port of the device gets its entities up front, so the
            # dashboard shows the full layout from the start rather than waiting
            # for a port to happen to be drawing power during a poll. DC is the
            # exception: it only matters when something is actually plugged in.
            for port in X783_PORTS:
                values = reading["ports"].get(port) or {}
                live = any(v for k, v in values.items() if k in MEASUREMENTS)
                always = port in ALWAYS_PORTS
                if (
                    (not live and not always and (key, port) not in seen_before)
                    or (key, port) in known_ports
                ):
                    continue
                known_ports.add((key, port))
                new.extend(
                    UgreenPortSensor(coordinator, key, port, kind)
                    for kind in MEASUREMENTS
                )
                new.append(UgreenPortProtocolSensor(coordinator, key, port))
                new.append(UgreenPortEnergySensor(coordinator, key, port))
                new.append(UgreenSessionEnergySensor(coordinator, key, port))
                new.append(UgreenSessionChargeSensor(coordinator, key, port))
        if new:
            async_add_entities(new)

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class UgreenStatusSensor(UgreenDeviceEntity, SensorEntity):
    """Cloud connectivity state of one bound UGREEN device."""

    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["online", "offline"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: UgreenCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_unique_id = f"{key}_status"

    @property
    def native_value(self) -> str:
        extra = self._device.get("extra") or {}
        return "online" if extra.get("onlineStatus") == ONLINE else "offline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = self._device
        extra = device.get("extra") or {}
        return {
            "device_type": device.get("deviceType"),
            "product_serial_no": device.get("productSerialNo"),
            "product_key": self._product.get("productKey"),
            "iot_id": extra.get("iotId"),
            "network_connected": extra.get("networkStatus") == ONLINE,
            "mac": device.get("deviceMac"),
        }


class UgreenLastPollSensor(UgreenDeviceEntity, SensorEntity):
    """When a poll last came back whole.

    The cloud this integration talks to drops out for minutes at a time, and
    every other entity simply keeps its last value while that happens. This is
    the one that says how old that value is.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_poll"

    def __init__(self, coordinator: UgreenCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_unique_id = f"{key}_last_poll"

    @property
    def available(self) -> bool:
        # Deliberately not tied to the last poll succeeding. Saying when the
        # last good reading arrived is exactly what is wanted while polls are
        # failing, which is when every other entity has gone unavailable.
        return bool(self._device)

    @property
    def native_value(self):
        stamp = self.coordinator.last_success
        return dt_util.utc_from_timestamp(stamp) if stamp else None


class UgreenPortEnergySensor(UgreenPortEntity, SensorEntity, RestoreEntity):
    """Everything this port has ever delivered, for the Energy dashboard.

    The charger keeps no energy total of its own -- it reports watts and
    nothing else -- so this is integrated from those, by the same code and the
    same thresholds the sessions use. The tracker only counts from the moment
    it was made, so what came before a restart is read back from the sensor's
    own last state and added underneath.

    Kilowatt-hours here rather than the watt-hours a session is measured in:
    a bout is worth tens of watt-hours and reads badly as 0.02 kWh, while a
    total climbs past a kilowatt-hour and reads badly the other way round.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _attr_translation_key = "energy_total"

    def __init__(self, coordinator: UgreenCoordinator, key: str, port: str) -> None:
        super().__init__(coordinator, key)
        self._port = port
        self._attr_translation_placeholders = {"port": port}
        self._attr_unique_id = f"{key}_{port}_energy_total"
        self._before_restart = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is None:
            return
        try:
            self._before_restart = float(last.state)
        except (TypeError, ValueError):
            # `unknown` or `unavailable` from a start that never got a reading.
            # Starting from zero would make the dashboard read this as a meter
            # replacement rather than as a gap, which is the honest outcome
            # anyway when nothing better is known.
            self._before_restart = 0.0

    @property
    def native_value(self) -> float:
        return round(
            self._before_restart
            + self.coordinator.sessions.delivered(self._key, self._port) / 1000,
            6,
        )


class UgreenPortSensor(UgreenPortEntity, SensorEntity):
    """Voltage, current or power of a single charging port."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: UgreenCoordinator, key: str, port: str, kind: str
    ) -> None:
        super().__init__(coordinator, key)
        self._port = port
        self._kind = kind
        device_class, unit, digits = MEASUREMENTS[kind]
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_suggested_display_precision = digits
        # Named through a placeholder so a translation only has to give the
        # word, not one entry per port.
        self._attr_translation_key = f"port_{kind}"
        self._attr_translation_placeholders = {"port": port}
        self._attr_unique_id = f"{key}_{port}_{kind}"

    @property
    def available(self) -> bool:
        return super().available and self._reading is not None

    @property
    def native_value(self) -> float | None:
        reading = self._reading
        if not reading:
            return None
        return (reading["ports"].get(self._port) or {}).get(self._kind)


class UgreenPortProtocolSensor(UgreenPortEntity, SensorEntity):
    """Fast-charge protocol a port negotiated with whatever is plugged into it."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = sorted(set(HANDSHAKE_PROTOCOL.values()) | {"unknown"})
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: UgreenCoordinator, key: str, port: str) -> None:
        super().__init__(coordinator, key)
        self._port = port
        self._attr_translation_key = "port_protocol"
        self._attr_translation_placeholders = {"port": port}
        self._attr_unique_id = f"{key}_{port}_protocol"

    @property
    def available(self) -> bool:
        return super().available and self._reading is not None

    @property
    def native_value(self) -> str | None:
        reading = self._reading
        if not reading:
            return None
        return (reading["ports"].get(self._port) or {}).get("protocol")


class UgreenCustomLimitSensor(UgreenPortEntity, SensorEntity):
    """What one port is allowed in the custom charging mode.

    This is the mode the app calls its own editor, and the charger reports the
    whole of it on every poll whether or not it is the mode in use. So these
    say what custom *would* do, not what is happening now -- which is why they
    are diagnostic rather than sitting beside the live readings.

    C6 and A share a group, exactly as the app's editor does; the protocols the
    group may negotiate ride along as an attribute rather than as six more
    entities.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: UgreenCoordinator,
        key: str,
        port: str,
        *,
        group: str | None = None,
        shared_with: str | None = None,
    ) -> None:
        super().__init__(coordinator, key)
        # Which socket this sits on, and which entry of the mode it reads --
        # the same for every port but the shared pair.
        self._port = port
        self._group_name = group or port
        self._attr_translation_key = (
            "custom_limit_shared" if shared_with else "custom_limit"
        )
        self._attr_translation_placeholders = {"port": port, "other": shared_with or ""}
        self._attr_unique_id = f"{key}_{port}_custom_limit"

    @property
    def _group(self) -> dict[str, Any] | None:
        for group in (self._reading or {}).get("custom") or []:
            if group["port"] == self._group_name:
                return group
        return None

    @property
    def available(self) -> bool:
        return super().available and self._group is not None

    @property
    def native_value(self) -> int | None:
        group = self._group
        return group["limit"] if group else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        group = self._group or {}
        return {
            "protocols": group.get("protocols") or [],
            "group": self._group_name,
        }


class UgreenTotalPowerSensor(UgreenDeviceEntity, SensorEntity):
    """Combined output of every port."""

    _attr_translation_key = "total_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: UgreenCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_unique_id = f"{key}_total_power"

    @property
    def available(self) -> bool:
        return super().available and self._reading is not None

    @property
    def native_value(self) -> float | None:
        reading = self._reading
        return reading["total"] if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reading = self._reading or {}
        return {
            "firmware": reading.get("firmware"),
            "ssid": reading.get("ssid"),
        }


class UgreenSessionSensor(UgreenPortEntity, SensorEntity):
    """Shared base for the two views of one port's charging session.

    A session runs from the moment something is plugged into the port until it is
    taken off again, and the total stays on show afterwards -- so "how much did that
    get?" is still answerable once the device is gone. Plugging the next thing in
    starts a new session from zero.
    """

    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: UgreenCoordinator, key: str, port: str) -> None:
        super().__init__(coordinator, key)
        self._port = port
        self._attr_translation_placeholders = {"port": port}

    @property
    def _session(self) -> Session | None:
        return self.coordinator.sessions.session(self._key, self._port)

    @property
    def available(self) -> bool:
        # Unlike the live measurements, a finished session is still worth showing
        # when the cloud is unreachable -- that is the whole point of keeping it.
        return super().available and self._session is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        session = self._session
        if session is None:
            return {}
        return {
            "charging": session.active,
            "started": _as_local(session.started_at),
            "ended": _as_local(session.ended_at),
            "duration": round(session.duration),
            "peak_power": round(session.peak_w, 1),
            "last_draw": _as_local(session.last_draw),
            "average_power": round(session.average_w, 1),
            "protocol": session.protocol,
        }


def _as_local(stamp: float | None) -> str | None:
    return dt_util.utc_from_timestamp(stamp).isoformat() if stamp else None


class UgreenSessionEnergySensor(UgreenSessionSensor, RestoreEntity):
    """Watt-hours this port has delivered to whatever is currently plugged into it.

    This one owns the restore: the tracker's state is shared by both session
    sensors, so exactly one of them may hand it back after a restart.
    """

    _attr_translation_key = "session_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: UgreenCoordinator, key: str, port: str) -> None:
        super().__init__(coordinator, key, port)
        self._attr_unique_id = f"{key}_{port}_session_energy"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is None:
            return
        try:
            energy = float(last.state)
        except (TypeError, ValueError):
            return
        self.coordinator.sessions.restore(
            self._key,
            self._port,
            {
                "energy_wh": energy,
                "active": bool(last.attributes.get("charging")),
                "protocol": last.attributes.get("protocol") or "none",
                "started_at": _as_timestamp(last.attributes.get("started")),
                "ended_at": _as_timestamp(last.attributes.get("ended")),
                "peak_w": last.attributes.get("peak_power") or 0.0,
                # How long ago charge last flowed is what decides whether a session
                # survives the downtime, so it has to come back with the rest.
                "last_draw": _as_timestamp(last.attributes.get("last_draw")),
            },
        )

    @property
    def native_value(self) -> float | None:
        session = self._session
        return round(session.energy_wh, 3) if session else None


def _as_timestamp(value: Any) -> float | None:
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    return parsed.timestamp() if parsed else None


class UgreenSessionChargeSensor(UgreenSessionSensor):
    """The same session read as charge into a battery rather than energy out of a port.

    An estimate, not a measurement: see ``session.charge_mah`` for what is assumed.
    """

    _attr_translation_key = "session_charge"
    _attr_native_unit_of_measurement = "mAh"
    _attr_icon = "mdi:battery-charging"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: UgreenCoordinator, key: str, port: str) -> None:
        super().__init__(coordinator, key, port)
        self._attr_unique_id = f"{key}_{port}_session_charge"

    @property
    def native_value(self) -> float | None:
        session = self._session
        if session is None:
            return None
        options = self.coordinator.config_entry.options
        return round(
            charge_mah(
                session.energy_wh,
                options.get(CONF_NOMINAL_VOLTAGE, DEFAULT_NOMINAL_VOLTAGE),
                options.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY) / 100,
            )
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        options = self.coordinator.config_entry.options
        return {
            **super().extra_state_attributes,
            "nominal_voltage": options.get(
                CONF_NOMINAL_VOLTAGE, DEFAULT_NOMINAL_VOLTAGE
            ),
            "efficiency": options.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY),
        }
