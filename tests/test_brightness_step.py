"""Regression tests for brightness_step / brightness_step_pct (Hue dimmer).

A Hue dimmer automation (mode: queued) sends light.turn_on with
brightness_step_pct +10 / -10, about once a second while a button is held.
Home Assistant computes every step from the entity's *current* brightness
attribute, so the state must be exact right after a command and must not be
overwritten by stale or mid-fade readbacks.
"""
from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.helvar510.const import DOMAIN
from custom_components.helvar510.dali510 import arc_to_percent
from custom_components.helvar510.devices import (
    BROADCAST_UNIQUE_ID,
    group_unique_id,
    sa_unique_id,
)

from test_init import _flow


def _eid(hass: HomeAssistant, entry, unique_id: str) -> str:
    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id("light", DOMAIN, unique_id)
    assert eid, unique_id
    return eid


def _strip_eid(hass: HomeAssistant, entry) -> str:
    ent_reg = er.async_get(hass)
    for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if e.domain == "light" and "strip" in e.unique_id:
            return e.entity_id
    raise AssertionError("no strip entity")


def _bri(hass: HomeAssistant, eid: str) -> int:
    st = hass.states.get(eid)
    assert st is not None and st.state == "on", (eid, st)
    return st.attributes["brightness"]


async def _turn_on(hass: HomeAssistant, eid: str, **data) -> None:
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": eid, **data}, blocking=True
    )
    await hass.async_block_till_done()


def _ha_step(bri: int, pct: int) -> int:
    """Home Assistant 2026.10's own brightness_step_pct arithmetic."""
    return max(0, min(255, round((round(bri / 255 * 100) + pct) / 100 * 255)))


def _steps(start: int, pct: int, n: int) -> list[int]:
    out = [start]
    for _ in range(n):
        out.append(_ha_step(out[-1], pct))
    return out


async def _unload(hass: HomeAssistant, entry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_set_brightness_reports_exactly_what_was_set(hass: HomeAssistant, sim) -> None:
    """set -> report is idempotent for single gear, groups, strips and All."""
    entry = await _flow(hass)
    targets = [
        (_eid(hass, entry, sa_unique_id(12)), [12]),
        (_eid(hass, entry, group_unique_id(1)), [7, 8]),
        (_strip_eid(hass, entry), [0, 1, 2, 3]),
    ]
    for eid, sas in targets:
        for b in (1, 2, 3, 10, 26, 51, 100, 128, 179, 200, 229, 240, 250, 254, 255):
            await _turn_on(hass, eid, brightness=b)
            assert _bri(hass, eid) == b, (eid, b)
            # a readback of the same bus levels must not change the reported value
            await entry.runtime_data.async_refresh_addresses(sas, defer=False)
            await hass.async_block_till_done()
            assert _bri(hass, eid) == b, (eid, b, "after readback")
    assert sim.violations == []
    await _unload(hass, entry)


async def test_step_up_and_down_single_gear(hass: HomeAssistant, sim) -> None:
    entry = await _flow(hass)
    eid = _eid(hass, entry, sa_unique_id(12))
    await _turn_on(hass, eid, brightness=26)
    seen = [_bri(hass, eid)]
    for _ in range(9):
        await _turn_on(hass, eid, brightness_step_pct=10)
        seen.append(_bri(hass, eid))
    assert seen == _steps(26, 10, 9)
    assert seen[-1] == 255
    for _ in range(4):
        await _turn_on(hass, eid, brightness_step_pct=-10)
        seen.append(_bri(hass, eid))
    assert seen[-5:] == _steps(255, -10, 4)
    await _unload(hass, entry)


async def test_strip_step_up_with_dim_colour(hass: HomeAssistant, sim) -> None:
    """A colour whose brightest component is < 255 (e.g. W only at 128) must
    not shrink the brightness on every step: dimming up has to go up."""
    entry = await _flow(hass)
    eid = _strip_eid(hass, entry)
    await _turn_on(hass, eid, rgbw_color=(0, 0, 0, 128), brightness=255)
    start = _bri(hass, eid)
    # what HA shows is what is lit: W channel at ~50 %
    assert abs(start - 128) <= 1
    seen = [start]
    for _ in range(6):
        await _turn_on(hass, eid, brightness_step_pct=10)
        seen.append(_bri(hass, eid))
    assert seen == _steps(start, 10, 6), seen
    assert seen[-1] == 255, seen
    for _ in range(3):
        await _turn_on(hass, eid, brightness_step_pct=-10)
        seen.append(_bri(hass, eid))
    assert seen[-4:] == _steps(255, -10, 3), seen
    # colour stays: only W lit, reported normalised
    st = hass.states.get(eid)
    assert tuple(st.attributes["rgbw_color"]) == (0, 0, 0, 255)
    assert [sim.levels[sa] for sa in (0, 1, 2)] == [0, 0, 0]
    await _unload(hass, entry)


async def test_strip_step_up_with_mixed_colour(hass: HomeAssistant, sim) -> None:
    entry = await _flow(hass)
    eid = _strip_eid(hass, entry)
    await _turn_on(hass, eid, rgbw_color=(200, 100, 50, 0), brightness=128)
    seen = [_bri(hass, eid)]
    for _ in range(8):
        await _turn_on(hass, eid, brightness_step_pct=10)
        seen.append(_bri(hass, eid))
    assert seen == _steps(seen[0], 10, 8), seen
    assert seen[-1] == 255
    # hue kept: R:G:B ratio on the bus still 2:1:0.5
    p = [arc_to_percent(sim.levels[sa]) for sa in (0, 1, 2)]
    assert abs(p[1] / p[0] - 0.5) < 0.05 and abs(p[2] / p[0] - 0.25) < 0.05
    await _unload(hass, entry)


async def test_step_during_fade_is_not_pulled_back(hass: HomeAssistant, sim) -> None:
    """Gear with a DALI fade time: a readback while fading must not replace
    the commanded level with the intermediate one."""
    entry = await _flow(hass)
    coord = entry.runtime_data
    eid = _eid(hass, entry, sa_unique_id(12))
    grp = _eid(hass, entry, group_unique_id(1))
    await _turn_on(hass, eid, brightness=26)
    await _turn_on(hass, grp, brightness=26)
    for sa in (7, 8, 12):
        sim.fade_s[sa] = 30.0
    seen, gseen = [_bri(hass, eid)], [_bri(hass, grp)]
    for _ in range(4):
        await _turn_on(hass, eid, brightness_step_pct=10)
        await _turn_on(hass, grp, brightness_step_pct=10)
        # readback / poll while the gear is still fading
        await coord.async_refresh_addresses([7, 8, 12], defer=False)
        await coord.async_refresh()
        await hass.async_block_till_done()
        seen.append(_bri(hass, eid))
        gseen.append(_bri(hass, grp))
    assert seen == _steps(26, 10, 4), seen
    assert gseen == _steps(26, 10, 4), gseen
    # once the fade has finished the bus confirms the target
    sim.fade_s.clear()
    for sa in (7, 8, 12):
        sim._fades.pop(sa, None)
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert _bri(hass, eid) == seen[-1] and _bri(hass, grp) == gseen[-1]
    assert sim.violations == [] and sim.config_writes == []
    await _unload(hass, entry)


async def test_external_change_is_still_learnt(hass: HomeAssistant, sim) -> None:
    """A change made by someone else (wall panel) shows up on the next poll."""
    entry = await _flow(hass)
    coord = entry.runtime_data
    eid = _eid(hass, entry, sa_unique_id(12))
    await _turn_on(hass, eid, brightness=128)
    # our command is verified first ...
    await coord.async_refresh_addresses([12], defer=False)
    assert 12 not in coord._pending
    # ... then a wall panel changes the level
    sim.levels[12] = 254
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert _bri(hass, eid) == 255
    await _unload(hass, entry)


async def test_readback_snapshot_does_not_revert_other_lights(hass: HomeAssistant, sim) -> None:
    """A refresh/poll that started before a command must not write its old
    snapshot back over the optimistic state of that command."""
    entry = await _flow(hass)
    coord = entry.runtime_data
    a = _eid(hass, entry, sa_unique_id(10))
    b = _eid(hass, entry, sa_unique_id(12))
    await _turn_on(hass, a, brightness=128)
    await _turn_on(hass, b, brightness=51)

    # refresh of another address in flight while b is stepped up
    task = hass.async_create_task(coord.async_refresh_addresses([10, 11], defer=False))
    await asyncio.sleep(0)
    await _turn_on(hass, b, brightness_step_pct=10)
    await task
    await hass.async_block_till_done()
    assert _bri(hass, b) == _ha_step(51, 10)

    # full poll in flight while b is stepped up again
    task = hass.async_create_task(coord.async_refresh())
    await asyncio.sleep(0)
    await _turn_on(hass, b, brightness_step_pct=10)
    await task
    await hass.async_block_till_done()
    assert _bri(hass, b) == _steps(51, 10, 2)[-1]
    assert sim.levels[12] == coord.data[12]
    await _unload(hass, entry)


async def test_broadcast_with_strip_steps_up(hass: HomeAssistant, sim) -> None:
    entry = await _flow(hass)
    strip = _strip_eid(hass, entry)
    allx = _eid(hass, entry, BROADCAST_UNIQUE_ID)
    await _turn_on(hass, strip, rgbw_color=(0, 0, 0, 255), brightness=51)
    await _turn_on(hass, allx, brightness=51)
    seen = [_bri(hass, allx)]
    for _ in range(4):
        await _turn_on(hass, allx, brightness_step_pct=10)
        seen.append(_bri(hass, allx))
    assert seen == _steps(51, 10, 4), seen
    assert sim.violations == [] and sim.config_writes == []
    await _unload(hass, entry)


async def test_restored_unnormalised_colour_steps_continuously(hass: HomeAssistant, sim) -> None:
    """A colour remembered by 0.3.0 (e.g. W at 128) is normalised: the next
    step continues from the brightness that was shown, it does not jump."""
    entry = await _flow(hass)
    coord = entry.runtime_data
    eid = _strip_eid(hass, entry)
    await _turn_on(hass, eid, rgbw_color=(0, 0, 0, 255), brightness=100)
    uid = next(iter(coord.strip_mem))
    coord.strip_mem[uid]["colour"] = [0, 0, 0, 128]
    before = _bri(hass, eid)
    await _turn_on(hass, eid, brightness_step_pct=10)
    assert _bri(hass, eid) == _ha_step(before, 10)
    await _unload(hass, entry)


async def test_readbacks_are_coalesced(hass: HomeAssistant, sim) -> None:
    """Steps arriving faster than the readback delay cause one readback."""
    entry = await _flow(hass)
    coord = entry.runtime_data
    eid = _eid(hass, entry, sa_unique_id(12))
    for _ in range(5):
        await _turn_on(hass, eid, brightness_step_pct=10)
    assert len([h for h in coord._refresh_handles if not h.cancelled()]) == 1
    assert coord._refresh_sas == {12}
    await _unload(hass, entry)
    assert coord._refresh_handles == []
