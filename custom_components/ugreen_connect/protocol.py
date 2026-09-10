"""The charger's own protocol: frames in, readings out.

Kept clear of Home Assistant and of aiohttp on purpose, exactly as
``session.py`` is. Every byte offset in here was established against a live
charger -- and that is precisely the kind of knowledge a test suite has to be
able to reach without an installation standing in the way.

The frame::

    TYPE(1) CMD(1) LEN(2, big endian) PAYLOAD(LEN) CRC16(2, MODBUS, low byte first)
"""

from __future__ import annotations

import logging
from typing import Any, Final

_LOGGER = logging.getLogger(__name__)

FRAME_QUERY = 0xAA
FRAME_NOTIFY = 0xEE
FRAME_SETTING = 0x11

QUERY_GET_DEVICE_STATE = 1
QUERY_GET_SN = 5
QUERY_GET_POWER_INFO = 6
QUERY_GET_UPGRADE_STATUS = 7
QUERY_GET_WIFI_SSID = 8
QUERY_GET_PRODUCT_VERSION = 10

SETTING_SET_BRIGHTNESS = 1
SETTING_SET_SLEEP_TIME = 2
SETTING_SET_CHARGING_MODE = 4
SETTING_SET_SCREENSAVER = 5

# Which frames a diagnostics download may carry, as an allowlist rather than a
# list of the ones to leave out.
#
# The direction matters more than the contents. That file is written to be
# posted publicly, and a denylist publishes anything added here later by
# default -- while two of the queries above answer with the household's own
# details: the Wi-Fi network name in plain ASCII, and the serial number the
# redaction elsewhere goes to some trouble to remove. Forgetting to add a frame
# here costs a reader some bytes; forgetting to exclude one costs somebody
# their network name.
PUBLISHABLE_FRAMES: Final[frozenset[str]] = frozenset(
    f"{FRAME_QUERY:02X}/{cmd}"
    for cmd in (
        QUERY_GET_DEVICE_STATE,
        QUERY_GET_POWER_INFO,
        QUERY_GET_PRODUCT_VERSION,
    )
)

# One 7-byte record per port, then up to one handshake-protocol byte per port.
PORT_RECORD = 7

# The charging protocol each port negotiated, reported one byte per port at the
# tail of the power frame.
HANDSHAKE_PROTOCOL: Final[dict[int, str]] = {
    0: "none", 1: "QC", 2: "AFC", 3: "FCP", 4: "UFCS", 5: "PD", 6: "PPS", 7: "AVS",
}

# The order the power report puts its ports in, per model. `productNo` is what
# the account API calls the model, and it is fetched already for the device
# page, so knowing which list to use costs nothing extra.
PORTS_BY_MODEL: Final[dict[str, tuple[str, ...]]] = {
    # Read off the app's own port table, and confirmed against a charger.
    "X783": ("C1", "C2", "C3", "C4", "C5", "C6", "A1", "DC"),
    # Nexode Pro 160W, confirmed against one by its owner in issue #2: devices
    # were put on the built-in cable and on C2, and records 0 and 2 -- and only
    # those -- carried voltage.
    "X776": ("C-Cable", "C1", "C2", "A"),
}


def ports_for(model: str | None, body_length: int) -> tuple[str, ...]:
    """What to call each port of a report this long, on this model.

    A model nobody has a table for still gets its readings: seven bytes of
    measurement and up to one protocol byte per port means the report's own
    length says how many there are. Only the names are lost, and numbered ports
    are honest about that -- better than one model's labels on another's
    sockets.

    "Up to" is what makes this awkward, and it is measured rather than assumed.
    The X783 sends 63 bytes for eight ports: 56 of measurement and only seven
    protocol bytes, the last one simply absent. The X776 sends 32 for four --
    28 and four, with nothing left off. So the count is taken as high as the
    protocol block allows and no higher than the measurements can fill, which
    reads both shapes without having to know which it is looking at.

    One length is genuinely undecidable: 56 is eight ports with no protocol
    tail and seven ports with a full one, and nothing in the frame separates
    them. This answers seven, and a model table is the only thing that could
    answer better.

    Resist the obvious repair. Letting the measurement count win on exact
    multiples of seven looks like it settles 56 and breaks 63 instead, which
    is 7 x 9: the X783 would come back with nine ports.
    """
    if known := PORTS_BY_MODEL.get(model or ""):
        return known
    by_protocol = -(-body_length // (PORT_RECORD + 1))   # rounded up
    by_measurement = body_length // PORT_RECORD
    return tuple(f"P{index + 1}" for index in range(min(by_protocol, by_measurement)))


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS, the checksum the charger's frames carry."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_frame(frame_type: int, cmd: int, payload: bytes = b"\x00") -> str:
    """Encode one protocol frame as the uppercase hex string ``PT_data`` wants."""
    body = bytes((frame_type, cmd)) + len(payload).to_bytes(2, "big") + payload
    return (body + crc16_modbus(body).to_bytes(2, "little")).hex().upper()


def frame_body(value: str, frame_type: int, cmd: int) -> bytes | None:
    """Return a frame's payload if it is the reply we asked for and the CRC holds."""
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        _LOGGER.debug("PT_data is not hex: %r", value)
        return None
    if len(raw) < 6:
        return None
    length = int.from_bytes(raw[2:4], "big")
    body = raw[4 : 4 + length]
    if len(body) != length:
        return None
    if crc16_modbus(raw[: 4 + length]) != int.from_bytes(
        raw[4 + length : 6 + length], "little"
    ):
        _LOGGER.debug("PT_data CRC mismatch: %s", value)
        return None
    if raw[0] != frame_type or raw[1] != cmd:
        return None
    return body


def parse_power_frame(
    value: str, model: str | None = None
) -> dict[str, dict[str, Any]] | None:
    """Decode a ``GET_POWER_INFO`` reply into ``{port_name: {volt, amp, watt}}``.

    Returns None for anything else -- the property also holds replies to other
    commands, and the last one simply stays there until the device sends a new.
    """
    body = frame_body(value, FRAME_QUERY, QUERY_GET_POWER_INFO)
    if body is None:
        return None
    named = ports_for(model, len(body))
    measured = PORT_RECORD * len(named)
    if not named or len(body) < measured:
        _LOGGER.debug("power body too short: %d for %d ports", len(body), len(named))
        return None

    def u16(offset: int) -> int:
        return int.from_bytes(body[offset : offset + 2], "big")

    ports: dict[str, dict[str, Any]] = {}
    for index, name in enumerate(named):
        base = PORT_RECORD * index
        # The protocol byte block follows the port records. A port with nothing
        # attached reports 0 ("none") -- and so does a port whose byte was
        # never sent, which is the honest answer for the X783's DC socket.
        proto_at = measured + index
        ports[name] = {
            "voltage": u16(base) / 10,
            "current": u16(base + 2) / 10,
            "power": u16(base + 4) / 10,
            "protocol": HANDSHAKE_PROTOCOL.get(
                body[proto_at] if len(body) > proto_at else 0, "unknown"
            ),
        }
    return ports
