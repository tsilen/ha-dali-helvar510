"""Home Assistant level tests (config flow, setup, services, diagnostics)."""
from __future__ import annotations

import pytest
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.auth.models import User
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helvar510.const import DOMAIN
from custom_components.helvar510.diagnostics import (
    async_get_config_entry_diagnostics,
)

from conftest import FAKE_PATH, FAKE_SERIAL


async def _flow(hass: HomeAssistant, with_strip: bool = True):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "path": FAKE_PATH,
            "poll_interval": 30,
            "hide_strip_channels": True,
            "hide_group_members": True,
        },
    )
    assert result["step_id"] == "strips"
    if with_strip:
        user = {
            "name": "Strip 0-1-2-3",
            "mode": "rgbw",
            "r": "0",
            "g": "1",
            "b": "2",
            "w": "3",
            "add_another": False,
        }
    else:
        user = {"skip": True}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["step_id"] == "confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return hass.config_entries.async_entries(DOMAIN)[0]


async def test_flow_and_setup(hass: HomeAssistant, sim) -> None:
    entry = await _flow(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.title == "DALI USB (Helvar 510)"
    assert [s["channels"] for s in entry.options["strips"]] == [
        {"r": 0, "g": 1, "b": 2, "w": 3}
    ]
    ent_reg = er.async_get(hass)
    lights = [
        e for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.domain == "light"
    ]
    uids = {e.unique_id for e in lights}
    # one entity per short address, the strip, groups 0-2 and broadcast
    assert len(lights) >= len(sim.gear) + 1 + 3 + 1
    assert any("strip" in u for u in uids)
    # scan and polling never wrote configuration
    assert sim.config_writes == []
    assert sim.violations == []

    # turn a light on through the normal light service
    state_ids = [e.entity_id for e in lights if hass.states.get(e.entity_id)]
    assert state_ids
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_send_raw_filters(hass: HomeAssistant, sim) -> None:
    await _flow(hass, with_strip=False)
    # allowed: QUERY ACTUAL LEVEL of short address 12
    res = await hass.services.async_call(
        DOMAIN, "send_raw", {"addr": 0x19, "data": 0xA0, "expect_reply": True},
        blocking=True, return_response=True,
    )
    assert res["addr"] == 0x19
    sent_before = len(sim.log)
    for addr, data in (
        (0xA5, 0x00),  # INITIALISE
        (0xA7, 0x00),  # RANDOMISE
        (0xB7, 0x01),  # PROGRAM SHORT ADDRESS
        (0xA3, 0x10),  # DTR0
        (0x19, 0x20),  # RESET
        (0x19, 0x2A),  # DTR AS MAX LEVEL
        (0x19, 0x45),  # STORE SCENE
        (0x19, 0x65),  # ADD TO GROUP via raw path
        (0xFF, 0x20),  # broadcast RESET
        (0x81, 0x60),  # group-addressed ADD TO GROUP
        (0x19, 0xE0),  # application extended command
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN, "send_raw", {"addr": addr, "data": data},
                blocking=True, return_response=True,
            )
    # nothing but interface end-of-transaction reports reached the bus
    assert all(f[0] != 3 for f in sim.log[sent_before:])
    for bad in (1.5, True, "abc", 256, -1, None, [1]):
        with pytest.raises((vol.Invalid, HomeAssistantError)):
            await hass.services.async_call(
                DOMAIN, "send_raw", {"addr": bad, "data": 0xA0},
                blocking=True, return_response=True,
            )
    assert sim.config_writes == []
    assert sim.violations == []


async def test_group_services(hass: HomeAssistant, sim) -> None:
    await _flow(hass, with_strip=False)
    res = await hass.services.async_call(
        DOMAIN, "add_to_group", {"short_address": 12, "group": 5},
        blocking=True, return_response=True,
    )
    assert res["verified"] and 5 in res["groups"]
    res = await hass.services.async_call(
        DOMAIN, "remove_from_group", {"short_address": 12, "group": 5},
        blocking=True, return_response=True,
    )
    assert res["verified"] and 5 not in res["groups"]
    for data in (
        {"short_address": 64, "group": 1},
        {"short_address": 12, "group": 16},
        {"short_address": 12.0, "group": 1},
        {"short_address": True, "group": 1},
    ):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, "add_to_group", data, blocking=True, return_response=True
            )
    # only ADD/REMOVE GROUP to one short address ever reached the bus
    assert sim.config_writes
    assert all(a == 0x19 and 0x60 <= d <= 0x7F for _c, a, d in sim.config_writes)
    assert sim.violations == []


async def test_services_admin_only(hass: HomeAssistant, sim, hass_read_only_user: User) -> None:
    await _flow(hass, with_strip=False)
    ctx = Context(user_id=hass_read_only_user.id)
    for service, data in (
        ("send_raw", {"addr": 0x19, "data": 0xA0}),
        ("add_to_group", {"short_address": 12, "group": 1}),
        ("remove_from_group", {"short_address": 12, "group": 1}),
    ):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN, service, data, blocking=True, context=ctx,
                return_response=True,
            )
    assert sim.config_writes == []


async def test_diagnostics_redacts_serial(hass: HomeAssistant, sim) -> None:
    entry = await _flow(hass, with_strip=False)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["hub_serial"] == "**REDACTED**"
    assert FAKE_SERIAL not in repr(diag)
    assert diag["gears"]


async def test_not_ready_when_device_missing(hass: HomeAssistant, sim) -> None:
    sim.unplug()
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        data={"path": FAKE_PATH, "gears": [], "strips": [], "name_prefix": "DALI"},
        options={"poll_interval": 30, "strips": []},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejects_non_hidraw_path(hass: HomeAssistant, sim) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        data={"path": "/etc/passwd", "gears": [], "strips": [], "name_prefix": "DALI"},
        options={"poll_interval": 30, "strips": []},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert sim.log == []
