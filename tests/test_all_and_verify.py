"""DALI All / strip regression tests on a copy of a real 39-gear topology.

* DALI All brightness_step must keep stepping although single gear do not
  end up at the commanded level (clamped higher than reported, lost frame,
  gear that does not follow).
* Every per-address write (strips on/off/colour, single lights, plans of
  groups / DALI All) is verified after it settled and mismatching addresses
  are written again (bounded), so a lost channel frame no longer leaves a
  strip with a wrong colour.
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import patch

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.helvar510 import dali510
from custom_components.helvar510.const import DOMAIN
from custom_components.helvar510.dali510 import arc_to_percent
from custom_components.helvar510.devices import (
    BROADCAST_UNIQUE_ID,
    group_unique_id,
    sa_unique_id,
)

from conftest import FAKE_PATH
from sim import SimBus
from test_brightness_step import _ha_step, _steps
from topology import STRIPS, strip_inputs, topology_gear

ALL_SAS = list(range(39))


@pytest.fixture
def tsim():
    gear = topology_gear()
    gear[13]["deaf"] = True        # does not follow (stays where it is)
    gear[20]["hidden_min"] = 160   # clamps higher than QUERY MIN LEVEL says
    bus = SimBus(gear)
    bus.levels[13] = 254
    orig = dali510.Dali510.__init__

    def patched(self, path=None, opener=None):
        orig(self, path, opener=bus.opener)

    with (
        patch.object(dali510.Dali510, "__init__", patched),
        patch(
            "custom_components.helvar510.config_flow._device_choices",
            return_value={FAKE_PATH: "simulated 510"},
        ),
        patch("custom_components.helvar510._serial_for", return_value="SIM0000000002"),
    ):
        yield bus
    bus.close()
    bus._t.join(timeout=5)


async def _setup(hass: HomeAssistant):
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"],
        {"path": FAKE_PATH, "poll_interval": 30,
         "hide_strip_channels": True, "hide_group_members": True},
    )
    ins = strip_inputs()
    for i, st in enumerate(ins):
        assert r["step_id"] == "strips"
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {**st, "add_another": i < len(ins) - 1}
        )
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    reg = er.async_get(hass)
    ents = {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(reg, entry.entry_id)
    }
    strips = sorted(eid for uid, eid in ents.items() if "strip" in uid)
    assert len(strips) == 4
    return entry, ents, strips


async def _on(hass, eid, **data):
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": eid, **data}, blocking=True
    )
    await hass.async_block_till_done()


async def _off(hass, eid):
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": eid}, blocking=True
    )
    await hass.async_block_till_done()


def _bri(hass, eid):
    st = hass.states.get(eid)
    return st.attributes.get("brightness") if st.state == "on" else 0


async def _verify(hass, coord, sas, rounds=4):
    """Run the post-write verification (readback + rewrite) to the end."""
    for _ in range(rounds):
        await coord.async_refresh_addresses(sas, defer=False)
        await hass.async_block_till_done()


async def _unload(hass, entry):
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _ratio(sim, sas):
    p = [arc_to_percent(sim.levels[sa]) for sa in sas]
    top = max(p)
    return [round(x / top, 2) if top else 0 for x in p]


async def test_all_steps_with_gear_that_does_not_follow(hass: HomeAssistant, tsim) -> None:
    """0.3.1: one gear not at the commanded level pinned DALI All at the
    brightness of the brightest member (255): it dimmed once, then never."""
    entry, ents, _strips = await _setup(hass)
    coord = entry.runtime_data
    allx = ents[BROADCAST_UNIQUE_ID]
    await _on(hass, allx, brightness=200)
    await _verify(hass, coord, ALL_SAS)
    seen = [_bri(hass, allx)]
    for _ in range(7):
        await _on(hass, allx, brightness_step_pct=-10)
        await _verify(hass, coord, ALL_SAS, rounds=1)
        seen.append(_bri(hass, allx))
    assert seen == _steps(200, -10, 7), seen
    seen = [seen[-1]]
    for _ in range(7):
        await _on(hass, allx, brightness_step_pct=10)
        await _verify(hass, coord, ALL_SAS, rounds=1)
        seen.append(_bri(hass, allx))
    assert seen == _steps(seen[0], 10, 7), seen
    # the clamped gear is where the gear put it, not rewritten forever
    assert tsim.levels[20] >= 160 and tsim.levels[13] == 254
    assert tsim.violations == [] and tsim.config_writes == []
    await _unload(hass, entry)


async def test_all_held_button_real_timing(hass: HomeAssistant, tsim) -> None:
    """Hold 'dim down' ~10 s (step every 0.8 s), then 'dim up': automatic
    readbacks, fades and verification run in between, like on the bus."""
    entry, ents, strips = await _setup(hass)
    allx = ents[BROADCAST_UNIQUE_ID]
    await _on(hass, strips[0], rgbw_color=(255, 0, 0, 128), brightness=200)
    await _on(hass, strips[1], rgbw_color=(0, 0, 0, 255), brightness=150)
    await _on(hass, allx, brightness=230)
    for sa in (5, 12, 7, 26, 33):
        tsim.fade_s[sa] = 0.7
    frames = []
    for d, n in ((-10, 7), (10, 5)):
        seen = [_bri(hass, allx)]
        for _ in range(n):
            a0, q0 = tsim.arc_frames, tsim.queries
            t0 = time.monotonic()
            await _on(hass, allx, brightness_step_pct=d)
            frames.append((tsim.arc_frames - a0, tsim.queries - q0))
            await asyncio.sleep(max(0.0, 0.8 - (time.monotonic() - t0)))
            seen.append(_bri(hass, allx))
        assert seen == _steps(seen[0], d, n), (d, seen)
    # DALI All with 4 strips: 39 gear, groups G0-G4 (18 gear, no strip
    # channels) take one group DAPC each -> 5 + 21 = 26 frames per step at
    # most (fewer when strip channels at 0 stay at 0); no readback queries
    # while the button is held.
    assert all(a <= 26 for a, _q in frames), frames
    assert all(q == 0 for _a, q in frames[1:]), frames
    assert tsim.violations == [] and tsim.config_writes == []
    await _unload(hass, entry)


async def test_four_strips_concurrently(hass: HomeAssistant, tsim) -> None:
    """One service call for all 4 strips (HA runs them in parallel)."""
    entry, ents, strips = await _setup(hass)
    coord = entry.runtime_data
    colours = [(255, 0, 0, 128), (0, 0, 0, 255), (255, 128, 0, 0), (0, 64, 255, 32)]
    for eid, col in zip(strips, colours):
        await _on(hass, eid, rgbw_color=col, brightness=200)
    await _verify(hass, coord, ALL_SAS)
    ratios = [_ratio(tsim, st) for st in STRIPS]
    a0 = tsim.arc_frames
    for _ in range(3):
        await hass.services.async_call(
            "light", "turn_on",
            {"entity_id": strips, "brightness_step_pct": -20}, blocking=True,
        )
        await hass.async_block_till_done()
    assert [_bri(hass, e) for e in strips] == [_steps(200, -20, 3)[-1]] * 4
    per_step = (tsim.arc_frames - a0) / 3
    # 16 channels, those at 0 that stay at 0 are not sent again
    assert per_step <= 16
    await _verify(hass, coord, ALL_SAS)
    for st, before in zip(STRIPS, ratios):
        after = _ratio(tsim, st)
        assert all(abs(x - y) < 0.08 for x, y in zip(before, after)), (st, before, after)
    await _unload(hass, entry)


@pytest.mark.parametrize("fault", ["dropped", "unacknowledged"])
async def test_strip_on_off_colour_with_lost_channel_frame(
    hass: HomeAssistant, tsim, fault: str
) -> None:
    entry, _ents, strips = await _setup(hass)
    coord = entry.runtime_data
    eid = strips[1]  # channels R=15, B=16, W=17, G=18
    r, b, w, g = STRIPS[1]

    def lose(sa):
        if fault == "dropped":
            tsim.drop[sa] = 1          # gear never sees it
        else:
            tsim.swallow_frames = 1    # 510 never acknowledges the next frame

    # turn on with a colour: the B frame is lost
    lose(b) if fault == "dropped" else None
    if fault == "unacknowledged":
        tsim.swallow_frames = 1        # first frame of the transaction
    await _on(hass, eid, rgbw_color=(255, 0, 128, 0), brightness=255)
    await _verify(hass, coord, STRIPS[1])
    assert tsim.levels[r] == 254 and tsim.levels[w] == 0 and tsim.levels[g] == 0
    assert tsim.levels[b] > 200
    assert tuple(hass.states.get(eid).attributes["rgbw_color"]) == (255, 0, 128, 0)

    # colour change: the R frame is lost
    lose(r)
    await _on(hass, eid, rgbw_color=(0, 255, 0, 0))
    await _verify(hass, coord, STRIPS[1])
    assert [tsim.levels[sa] for sa in (r, b, w)] == [0, 0, 0]
    assert tsim.levels[g] == 254
    assert tuple(hass.states.get(eid).attributes["rgbw_color"]) == (0, 255, 0, 0)

    # turn off: the G frame is lost
    lose(g)
    await _off(hass, eid)
    await _verify(hass, coord, STRIPS[1])
    assert all(tsim.levels[sa] == 0 for sa in STRIPS[1])
    assert hass.states.get(eid).state == "off"

    # plain on again restores the colour
    await _on(hass, eid)
    await _verify(hass, coord, STRIPS[1])
    assert tsim.levels[g] == 254 and tsim.levels[r] == 0
    assert tsim.violations == [] and tsim.config_writes == []
    await _unload(hass, entry)


async def test_group_and_all_commands_verified(hass: HomeAssistant, tsim) -> None:
    entry, ents, strips = await _setup(hass)
    coord = entry.runtime_data
    grp = ents[group_unique_id(1)]  # 5, 6, 10, 24, 31 (group DAPC)
    tsim.drop[24] = 1
    await _on(hass, grp, brightness=128)
    await _verify(hass, coord, [5, 6, 10, 24, 31])
    assert tsim.levels[24] == tsim.levels[10] > 0
    # DALI All with a lost strip channel frame keeps the strip colour
    await _on(hass, strips[2], rgbw_color=(255, 0, 0, 255), brightness=200)
    await _verify(hass, coord, STRIPS[2])
    before = _ratio(tsim, STRIPS[2])
    tsim.drop[STRIPS[2][2]] = 1  # W channel
    await _on(hass, ents[BROADCAST_UNIQUE_ID], brightness=100)
    await _verify(hass, coord, ALL_SAS)
    after = _ratio(tsim, STRIPS[2])
    assert all(abs(x - y) < 0.08 for x, y in zip(before, after)), (before, after)
    assert tsim.violations == [] and tsim.config_writes == []
    await _unload(hass, entry)


async def test_single_light_lost_frame_rewritten(hass: HomeAssistant, tsim) -> None:
    entry, ents, _strips = await _setup(hass)
    coord = entry.runtime_data
    eid = ents[sa_unique_id(11)]
    tsim.drop[11] = 1
    await _on(hass, eid, brightness=180)
    await _verify(hass, coord, [11])
    assert tsim.levels[11] > 0
    tsim.drop[11] = 1
    await _off(hass, eid)
    await _verify(hass, coord, [11])
    assert tsim.levels[11] == 0
    await _unload(hass, entry)


async def test_rewrites_are_bounded_and_logged(
    hass: HomeAssistant, tsim, caplog: pytest.LogCaptureFixture
) -> None:
    entry, _ents, strips = await _setup(hass)
    coord = entry.runtime_data
    eid = strips[3]  # R=33 B=34 W=35 G=36
    await _on(hass, eid, rgbw_color=(0, 0, 0, 255), brightness=200)
    await _verify(hass, coord, STRIPS[3])
    tsim.drop[33] = 99            # R channel never takes a frame
    a0 = tsim.arc_frames
    with caplog.at_level(logging.WARNING):
        await _on(hass, eid, rgbw_color=(255, 0, 0, 255), brightness=200)
        await _verify(hass, coord, STRIPS[3], rounds=6)
    # only the R frame (B, G stay 0, W stays: not sent) + 2 rewrites of R
    assert tsim.arc_frames - a0 == 1 + 2
    assert any("short address 33" in r.message for r in caplog.records
               if r.levelno == logging.WARNING)
    # the state shows what is really lit, but the colour the user picked is
    # remembered (not 'learnt' from the broken channel) for the next command
    assert tuple(hass.states.get(eid).attributes["rgbw_color"]) == (0, 0, 0, 255)
    assert any(m.get("colour") == [255, 0, 0, 255] for m in coord.strip_mem.values())
    await _unload(hass, entry)


async def test_step_once_regression_hold_mode_single_drops(hass: HomeAssistant, tsim) -> None:
    """A step on DALI All finishes well within the 0.8 s hold interval on
    the simulated bus (so automation mode 'single' does not drop steps)."""
    entry, ents, _strips = await _setup(hass)
    allx = ents[BROADCAST_UNIQUE_ID]
    await _on(hass, allx, brightness=200)
    t0 = time.monotonic()
    await _on(hass, allx, brightness_step_pct=-10)
    assert time.monotonic() - t0 < 0.8
    assert _bri(hass, allx) == _ha_step(200, -10)
    await _unload(hass, entry)
