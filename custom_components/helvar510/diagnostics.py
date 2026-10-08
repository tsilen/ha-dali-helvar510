"""Diagnostics for Helvar DIGIDIM 510."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .dali510 import suggest_multichannel


# The 510's USB serial number identifies one physical unit -> redacted.
TO_REDACT = {"serial", "serial_number", "hub_serial"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coord = entry.runtime_data
    return async_redact_data({
        "path": coord.bus.path,
        "is_open": coord.bus.is_open,
        "info_reply": coord.bus.info_reply.hex(" ") if coord.bus.info_reply else None,
        "gears": coord.gears,
        "groups": coord.groups,
        "strips": coord.strips,
        "suggested_multichannel": suggest_multichannel(coord.gears),
        "levels": coord.data,
        "fail_count": coord.fail_count,
        "last_frames": list(coord.bus.frames)[-100:],
        "options": dict(entry.options),
        "entry_version": f"{entry.version}.{entry.minor_version}",
        "name_prefix": coord.name_prefix,
        "strip_memory": coord.strip_mem,
        "hub_serial": coord.hub_serial,
    }, TO_REDACT)
