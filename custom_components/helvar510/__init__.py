"""Helvar DIGIDIM 510 (USB-DALI) custom integration.

Read-only bus scan + arc-power control only. See README.md.
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall

try:
    from homeassistant.core import ServiceResponse, SupportsResponse
except ImportError:  # older HA
    ServiceResponse = dict  # type: ignore
    SupportsResponse = None  # type: ignore
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_ADDR,
    ATTR_DATA,
    ATTR_EXPECT_REPLY,
    CONF_NAME_PREFIX,
    CONF_PATH,
    CONF_POLL_INTERVAL,
    CONF_RGBW_GROUPS,
    CONF_STRIPS,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    ENTRY_MINOR_VERSION,
    ENTRY_VERSION,
    SERVICE_SEND_RAW,
    SERVICE_ADD_TO_GROUP,
    SERVICE_REMOVE_FROM_GROUP,
    ATTR_SHORT_ADDRESS,
    ATTR_GROUP,
    ATTR_METHOD,
)
from .coordinator import Helvar510Coordinator
from .dali510 import Dali510Error, UnsafeFrameError, list_hidraw
from .devices import (
    async_reconcile_devices,
    async_migrate_devices,
    expected_identifiers,
    resolve_strip_unique_id,
    async_apply_sa_visibility,
    visibility_state,
    hub_device_info,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

def strict_int(lo: int, hi: int):
    """Validator: int (or decimal / 0x-hex string) within lo..hi.

    Unlike vol.Coerce(int) it refuses floats (39.9 -> 39), booleans and other
    types, so a malformed call can never be silently turned into a frame.
    """

    def _validate(value):
        if isinstance(value, bool):
            raise vol.Invalid("expected an integer, got a boolean")
        if isinstance(value, int):
            out = value
        elif isinstance(value, str):
            txt = value.strip().lower()
            try:
                out = int(txt, 16) if txt.startswith("0x") else int(txt, 10)
            except ValueError as err:
                raise vol.Invalid(f"not an integer: {value!r}") from err
        else:
            raise vol.Invalid(f"expected an integer, got {type(value).__name__}")
        if not lo <= out <= hi:
            raise vol.Invalid(f"must be between {lo} and {hi}")
        return out

    return _validate


SEND_RAW_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ADDR): strict_int(0, 255),
        vol.Required(ATTR_DATA): strict_int(0, 255),
        vol.Optional(ATTR_EXPECT_REPLY, default=False): cv.boolean,
    }
)

GROUP_MEMBERSHIP_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SHORT_ADDRESS): strict_int(0, 63),
        vol.Required(ATTR_GROUP): strict_int(0, 15),
        vol.Optional(ATTR_METHOD, default="auto"): vol.In(["auto", "twice_bit", "two_frames"]),
    }
)

type Helvar510ConfigEntry = ConfigEntry[Helvar510Coordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain-level send_raw service."""

    async def handle_send_raw(call: ServiceCall):
        loaded = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise HomeAssistantError("Helvar 510 integration is not loaded")
        coord: Helvar510Coordinator = loaded[0].runtime_data
        addr = call.data[ATTR_ADDR]
        data = call.data[ATTR_DATA]
        expect = call.data[ATTR_EXPECT_REPLY]
        try:
            reply = await coord.async_send_raw(addr, data, expect)
        except UnsafeFrameError as err:
            raise HomeAssistantError(f"Blocked (could change gear config): {err}") from err
        except Dali510Error as err:
            raise HomeAssistantError(str(err)) from err
        result = {"addr": addr, "data": data, **reply.as_dict()}
        _LOGGER.info("send_raw %02x %02x -> %s", addr, data, result)
        return result

    async def _membership(call: ServiceCall, add: bool):
        loaded = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise HomeAssistantError("Helvar 510 integration is not loaded")
        entry = loaded[0]
        coord: Helvar510Coordinator = entry.runtime_data
        sa = call.data[ATTR_SHORT_ADDRESS]
        group = call.data[ATTR_GROUP]
        try:
            result = await coord.async_set_group_membership(
                sa, group, add, call.data[ATTR_METHOD]
            )
        except UnsafeFrameError as err:
            raise HomeAssistantError(f"Blocked: {err}") from err
        except (Dali510Error, ValueError) as err:
            raise HomeAssistantError(str(err)) from err
        _LOGGER.warning("helvar510 group membership change: %s", result)
        if not result.get("verified"):
            raise HomeAssistantError(
                f"DALI {'ADD TO' if add else 'REMOVE FROM'} GROUP sent to short "
                f"address {sa}, but QUERY GROUPS still reports {result.get('groups')}"
            )
        return result

    async def handle_add(call: ServiceCall):
        return await _membership(call, True)

    async def handle_remove(call: ServiceCall):
        return await _membership(call, False)

    kwargs = {}
    if SupportsResponse is not None:
        kwargs["supports_response"] = SupportsResponse.OPTIONAL
    # Admin-only: these services talk to the bus directly (raw frames, group
    # membership writes); non-admin users must not be able to call them.
    for name, handler, schema in (
        (SERVICE_SEND_RAW, handle_send_raw, SEND_RAW_SCHEMA),
        (SERVICE_ADD_TO_GROUP, handle_add, GROUP_MEMBERSHIP_SCHEMA),
        (SERVICE_REMOVE_FROM_GROUP, handle_remove, GROUP_MEMBERSHIP_SCHEMA),
    ):
        async_register_admin_service(hass, DOMAIN, name, handler, schema=schema, **kwargs)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: Helvar510ConfigEntry) -> bool:
    path = entry.data.get(CONF_PATH) or "auto"
    poll = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    rgbw = entry.options.get(CONF_RGBW_GROUPS, [])

    coordinator = Helvar510Coordinator(hass, entry, path, poll, rgbw)
    try:
        await coordinator.async_open()
    except Dali510Error as err:
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(str(err)) from err

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    # 510 hub device (same identifier as the 0.1.0 shared "DALI" device, so a
    # user-set name/area on it is kept). Each light gets a child device.
    serial = await hass.async_add_executor_job(_serial_for, coordinator.bus.path)
    coordinator.hub_serial = serial
    hub = hub_device_info(entry, serial)
    hub_dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **hub
    )
    coordinator.hub_device_id = hub_dev.id

    # Move registry entries to their device for the current layout BEFORE the
    # platform adds entities (keeps user areas / names, removes orphans).
    async_reconcile_devices(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _serial_for(path: str | None) -> str | None:
    try:
        for info in list_hidraw():
            if info.path == path and info.serial:
                return info.serial
    except OSError:
        pass
    return None


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.1 (0.1.0, one shared device) -> 1.2 (hub + device per light)."""
    _LOGGER.debug(
        "Migrating helvar510 entry from %s.%s", entry.version, entry.minor_version
    )
    if entry.version > ENTRY_VERSION:
        return False  # downgrade from an unknown future version
    if entry.version == 1 and entry.minor_version < 2:
        # Pin each strip's unique_id to the registry entity 0.1.0 created
        new_options = dict(entry.options)
        new_data = dict(entry.data)
        for container in (new_options, new_data):
            if container.get(CONF_STRIPS):
                strips = []
                for strip in container[CONF_STRIPS]:
                    strip = dict(strip)
                    strip["unique_id"] = resolve_strip_unique_id(hass, entry, strip)
                    strips.append(strip)
                container[CONF_STRIPS] = strips
        hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)
        # 0.2.0 visibility: strip channels enabled+hidden (0.1.0: disabled),
        # group-member lights hidden by default. USER flags untouched.
        async_apply_sa_visibility(
            hass,
            entry,
            old=visibility_state(entry, hide_groups=False),
            new=visibility_state(entry),
            enable_integration_disabled=True,
        )
        prefix = async_migrate_devices(hass, entry)
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_NAME_PREFIX: prefix},
            minor_version=ENTRY_MINOR_VERSION,
            version=ENTRY_VERSION,
        )
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting only devices that no longer belong to the layout."""
    return not (device_entry.identifiers & expected_identifiers(entry))


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options / gear data changed: apply in place, reload only if needed."""
    coord: Helvar510Coordinator | None = getattr(entry, "runtime_data", None)
    if (
        coord is None
        or (entry.data.get(CONF_PATH) or "auto") != coord.config_path
        or coord.name_prefix != (entry.data.get(CONF_NAME_PREFIX) or coord.name_prefix)
    ):
        await hass.config_entries.async_reload(entry.entry_id)
        return
    try:
        await coord.async_apply_entry()
    except Exception:  # noqa: BLE001 - never leave a half-applied layout
        _LOGGER.exception("helvar510: in-place update failed, reloading")
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: Helvar510ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok
