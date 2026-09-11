"""The two charging entities, driven without a Home Assistant install.

Both of the things this file holds were reverted green by the rest of the
suite: `_added_at` could go back to a `_primed` flag, and the binary sensor's
`available` could be deleted, and nothing noticed. They are entity behaviour, so
nothing in `tests/` reached them.

What is stubbed is only the base classes underneath -- the entity classes
themselves are the real ones, compiled from their source, and it is their real
`_handle_coordinator_update` that runs. What this cannot see is the plumbing
those bases provide on a running Home Assistant; `_trigger_event` not writing
state is exactly the kind of thing that lives there, and it took a merged tree
to find. Treat a green here as "the decision is right", not as "the entity
works".
"""

import sys
import time
import types
from pathlib import Path

import pytest
from conftest import session as session_module

_COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "ugreen_connect"
)


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Base:
    """Stands in for CoordinatorEntity, EventEntity and BinarySensorEntity.

    Records what the real code asks of it rather than doing any of it.
    """

    _attr_has_entity_name = True

    def __class_getitem__(cls, _item):
        # `CoordinatorEntity[UgreenCoordinator]` in the real base's bases.
        return cls

    def __init__(self, coordinator=None, *args, **kwargs):
        self.coordinator = coordinator
        self.events: list[tuple[str, dict]] = []
        self.writes = 0

    @property
    def available(self) -> bool:
        # What CoordinatorEntity does: a failed poll takes its entities with it.
        return bool(getattr(self.coordinator, "last_update_success", True))

    def _trigger_event(self, event_type, event_attributes=None) -> None:
        # The real base refuses a type it was not declared with, and an entity
        # that raised one would fail on a running Home Assistant rather than
        # here. Worth keeping, since a stub that is more permissive than the
        # thing it stands in for is a test that passes for the wrong reason.
        if event_type not in self._attr_event_types:
            raise ValueError(f"Invalid event type {event_type}")
        self.events.append((event_type, event_attributes))

    def async_write_ha_state(self) -> None:
        self.writes += 1

    def _handle_coordinator_update(self) -> None:
        self.writes += 1


def _load(name: str, file_name: str):
    """Compile one module of the integration against stubbed surroundings."""
    for stub, attrs in {
        "homeassistant": {},
        "homeassistant.core": {"HomeAssistant": object, "callback": lambda f: f},
        "homeassistant.exceptions": {
            "HomeAssistantError": type("HomeAssistantError", (Exception,), {}),
            "ServiceValidationError": type("ServiceValidationError", (Exception,), {}),
        },
        "homeassistant.helpers": {},
        "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
        "homeassistant.helpers.update_coordinator": {"CoordinatorEntity": _Base},
        "homeassistant.helpers.device_registry": {
            "CONNECTION_NETWORK_MAC": "mac", "DeviceInfo": dict,
        },
        "homeassistant.components": {},
        "homeassistant.components.event": {"EventEntity": _Base},
        "homeassistant.components.binary_sensor": {
            "BinarySensorEntity": _Base,
            "BinarySensorDeviceClass": types.SimpleNamespace(BATTERY_CHARGING="battery_charging"),
        },
    }.items():
        _module(stub, **attrs)

    package = "custom_components.ugreen_connect"
    _module("custom_components")
    _module(package, UgreenConfigEntry=object)
    # `api.py` is faked down to the one name entity.py imports: compiling it
    # would pull in aiohttp, and this suite is meant to run with nothing
    # installed but pytest.
    _module(f"{package}.api", UgreenError=type("UgreenError", (Exception,), {}))
    # The rest are the real ones, compiled rather than faked. They import
    # nothing outside the standard library, which is why they can be.
    for sibling, source in (("const", "const.py"),
                            ("protocol", "protocol.py"), ("session", "session.py")):
        module = _module(f"{package}.{sibling}")
        exec(compile((_COMPONENT / source).read_text(encoding="utf-8"), source, "exec"),
             module.__dict__)
    _module(
        f"{package}.coordinator",
        UgreenCoordinator=object,
        device_key=lambda device: device.get("deviceUniqueCode"),
    )
    entity_module = _module(f"{package}.entity")
    exec(compile((_COMPONENT / "entity.py").read_text(encoding="utf-8"), "entity.py", "exec"),
         entity_module.__dict__)

    module = _module(f"{package}.{name}")
    exec(compile((_COMPONENT / file_name).read_text(encoding="utf-8"), file_name, "exec"),
         module.__dict__)
    return module


class _Coordinator:
    """Only what the two entities read off it."""

    def __init__(self, sessions, reading):
        self.sessions = sessions
        self.data = {
            "devices": [{"deviceUniqueCode": "DEV", "deviceName": "charger"}],
            "power": {"DEV": reading},
            "detail": {},
        }
        self.last_update_success = True

    def model_for(self, key):
        return "X783"


@pytest.fixture
def entities():
    return _load("event", "event.py"), _load("binary_sensor", "binary_sensor.py")


def _reading(power: float) -> dict:
    """A port drawing `power`, or an empty one when that is zero.

    Zero watts at nine volts with no protocol is a shape the charger does not
    produce, and `_plugged()` would read it as a bare cable -- routing to the
    quiet branch rather than the empty one, so a test meaning "nothing is
    plugged in" would exercise the wrong half and still pass.
    """
    return {
        "ports": {"C1": {"voltage": 9.0 if power else 0.0,
                         "current": 2.2 if power else 0.0,
                         "power": power, "protocol": "PD" if power else "none"}},
        "total": power,
    }


def test_a_bout_starting_on_the_first_poll_the_entity_sees_is_announced(entities):
    """Entities are built inside a coordinator listener, so the poll that adds
    one is never delivered to it -- the first update it receives is the next.
    A flag that skipped "the first update" therefore swallowed a real start,
    and the bout later ended with nothing to pair the end with.
    """
    event_module, _ = entities
    tracker = session_module.SessionTracker()
    coordinator = _Coordinator(tracker, _reading(0.0))
    entity = event_module.UgreenChargingEvent(coordinator, "DEV", "C1")

    now = entity._added_at + 5.0
    tracker.update(now, "DEV", _reading(20.0)["ports"])
    entity._handle_coordinator_update()

    assert [kind for kind, _ in entity.events] == ["started"]


def test_the_charging_sensor_goes_with_the_reading(entities):
    """A charging state held over an outage is asserted rather than stale, and
    the longer it holds the more confidently it is wrong. The port's power
    sensors go unavailable when a reading does not arrive; this has to too.
    """
    _, binary_module = entities
    tracker = session_module.SessionTracker()
    coordinator = _Coordinator(tracker, _reading(20.0))
    sensor = binary_module.UgreenChargingSensor(coordinator, "DEV", "C1")

    tracker.update(1000.0, "DEV", _reading(20.0)["ports"])
    assert sensor.available is True

    coordinator.data["power"]["DEV"] = None
    assert sensor.available is False


def test_the_charging_sensor_follows_the_flow_and_not_the_bout(entities):
    """The whole argument of this branch, asserted on the thing that publishes it.

    `is_on` reading `session.active` is not a hypothetical mutation: it is what
    the code said before, and it kept battery_charging on for two hours after a
    phone finished. The bout stays open across all of this, which is what makes
    the two answers distinguishable here.
    """
    _, binary_module = entities
    tracker = session_module.SessionTracker()
    coordinator = _Coordinator(tracker, _reading(20.0))
    sensor = binary_module.UgreenChargingSensor(coordinator, "DEV", "C1")

    now = 1000.0
    for _poll in range(4):
        tracker.update(now, "DEV", _reading(20.0)["ports"])
        now += 5.0
    assert sensor.is_on is True

    # Live but no longer drawing: still on, because one quiet poll is not a stop.
    quiet = {"C1": {"voltage": 9.0, "current": 0.0, "power": 0.0, "protocol": "PD"}}
    tracker.update(now, "DEV", quiet)
    assert sensor.is_on is True

    last_draw = tracker.session("DEV", "C1").last_draw
    while now - last_draw < session_module.DRAW_SETTLE:
        now += 5.0
        tracker.update(now, "DEV", quiet)
    assert sensor.is_on is False
    assert tracker.session("DEV", "C1").active is True, (
        "the bout is still open, which is what the two answers disagree about"
    )


def test_a_bout_older_than_the_entity_is_not_announced(entities):
    """The guard's own job, and the direction the permissive test cannot see.

    Deleting the clause leaves this file green otherwise: the next reader trims
    a three-part condition whose only cover exercises the yes direction, and the
    boot notification comes back -- restart with a laptop on the port, the
    sensor platform restores an hours-old `started_at`, and every charging port
    announces a start that happened last night.
    """
    event_module, _ = entities
    tracker = session_module.SessionTracker()
    coordinator = _Coordinator(tracker, _reading(20.0))
    entity = event_module.UgreenChargingEvent(coordinator, "DEV", "C1")

    # Seeded from the wall clock rather than from the entity's own field: taking
    # `_added_at - 3600` would be satisfied by any clock at all, and the obvious
    # reading of a duration comparison is to reach for `time.monotonic()`. On a
    # host up three days that is about 259,200 while every `started_at` is near
    # 1.76e9, so the guard would be dead in production and green here.
    started_at = time.time() - 3600.0
    tracker.restore(
        "DEV", "C1",
        {"energy_wh": 40.0, "active": True, "protocol": "PD",
         "started_at": started_at, "last_draw": time.time() - 10.0},
    )
    tracker.update(time.time() + 5.0, "DEV", _reading(20.0)["ports"])
    entity._handle_coordinator_update()

    assert entity.events == []


def test_a_bout_that_ends_is_announced_once_with_its_own_figures(entities):
    """The other half of the pair, which nothing covered.

    Round two's regression lived on this path: ENDED was raised and then
    overwritten before anything could hear it. The figures have to be the
    bout's own, taken before the tracker moves on.
    """
    event_module, _ = entities
    tracker = session_module.SessionTracker()
    coordinator = _Coordinator(tracker, _reading(20.0))
    entity = event_module.UgreenChargingEvent(coordinator, "DEV", "C1")

    now = entity._added_at + 5.0
    for _poll in range(4):
        tracker.update(now, "DEV", _reading(20.0)["ports"])
        entity._handle_coordinator_update()
        now += 5.0

    # An empty port, held past UNPLUG_DEBOUNCE, which is what ends the bout --
    # `_reading(0.0)` is an empty port rather than a live idle one, so this
    # goes through `_empty` and not `_quiet`.
    while tracker.session("DEV", "C1").active:
        tracker.update(now, "DEV", _reading(0.0)["ports"])
        entity._handle_coordinator_update()
        now += 600.0

    kinds = [kind for kind, _ in entity.events]
    assert kinds == ["started", "ended"], kinds
    ended = entity.events[-1][1]
    assert ended["energy_wh"] > 0


def test_a_swapped_device_ends_its_own_bout_before_the_next_one_starts(entities):
    """Both halves in one poll, and the first described as it was.

    A different device answering inside the unplug debounce ends one bout and
    starts another before anything is published. The tracker has replaced the
    Session by the time the entity looks, so the figures for the bout that
    ended have to have been kept -- otherwise the notification for the laptop
    that just came off reports the phone's first few joules.
    """
    event_module, _ = entities
    tracker = session_module.SessionTracker()
    coordinator = _Coordinator(tracker, _reading(20.0))
    entity = event_module.UgreenChargingEvent(coordinator, "DEV", "C1")

    now = entity._added_at + 5.0
    for _poll in range(6):
        tracker.update(now, "DEV", _reading(20.0)["ports"])
        entity._handle_coordinator_update()
        now += 5.0
    delivered = tracker.session("DEV", "C1").energy_wh
    assert delivered > 0

    # The port empties briefly, then a different protocol answers on it.
    tracker.update(now, "DEV", _reading(0.0)["ports"])
    entity._handle_coordinator_update()
    now += 5.0
    swapped = _reading(20.0)
    swapped["ports"]["C1"]["protocol"] = "QC"
    tracker.update(now, "DEV", swapped["ports"])
    entity._handle_coordinator_update()

    kinds = [kind for kind, _ in entity.events]
    assert kinds == ["started", "ended", "started"], kinds
    ended = entity.events[1][1]
    assert ended["energy_wh"] == pytest.approx(round(delivered, 3)), (
        "the bout that ended has to be reported with its own figures"
    )

