"""Diagnostics support for UGREEN Connect."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import UgreenConfigEntry
from .coordinator import device_key
from .protocol import FRAME_QUERY

# The README asks owners of other chargers to attach this file to a public
# issue, so the bar it has to clear is not "no credentials" but "nothing that
# belongs to the household". The MAC, the cloud id, the unit's own code and the
# name of the Wi-Fi network it joined all identify the charger on somebody's
# desk while saying nothing about the model, so they go.
#
# `productSerialNo` deliberately stays. Despite the name it is the model code --
# 030002 is the 300W, 030007 the 160W -- and it is the one identifier a report
# about an unsupported charger is useless without.
TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    "accessToken",
    "refreshToken",
    "token",
    "sid",
    "deviceMac",
    "deviceUniqueCode",
    "iotId",
    "ssid",
}

# Redacting by key name reaches values, and the raw frames are not values --
# they are bytes. The reply to this one carries the household's Wi-Fi network
# name as plain ASCII inside them, where nothing above can see it. Every other
# frame is worth reading byte by byte; this one holds nothing a charger could
# be debugged with.
QUERY_GET_WIFI_SSID = 8
SSID_FRAME = f"{FRAME_QUERY:02X}/{QUERY_GET_WIFI_SSID}"

# Redaction only ever looks at values, and these sections are keyed by the
# device code -- so the code redacted everywhere else would still be sitting
# here in plain sight, as the key.
KEYED_BY_DEVICE = ("detail", "power", "power_errors")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UgreenConfigEntry
) -> dict[str, Any]:
    """Return the raw cloud payload with anything identifying removed."""
    coordinator = entry.runtime_data
    raw = coordinator.data or {}
    data = async_redact_data(raw, TO_REDACT)

    # A stand-in rather than a blanking, so that a charger can still be followed
    # from its entry in `devices` through to its readings.
    names = {
        key: f"device_{index}"
        for index, device in enumerate(raw.get("devices") or [])
        if (key := device_key(device))
    }
    for section in KEYED_BY_DEVICE:
        if isinstance(entries := data.get(section), dict):
            data[section] = {
                names.get(code, code): value for code, value in entries.items()
            }

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "data": data,
        # The raw frames behind the readings above, keyed by the question that
        # was asked. On a charger this integration has never seen, the decoded
        # values are only as good as offsets established on a different one --
        # these bytes are what someone else can check them against, and what
        # turns "my ports are called P1" into a model in the table.
        "frames": {
            name: value
            for name, value in coordinator.rtcx.last_frames.items()
            if name != SSID_FRAME
        },
    }
