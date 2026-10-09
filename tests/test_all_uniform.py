"""DALI All / group brightness is one light: every member gets the same
brightness (strips with their colour), also while a dimmer button is held
with realistic bus timing and automation mode 'single'."""
from __future__ import annotations

import asyncio
import time

from homeassistant.core import HomeAssistant

from custom_components.helvar510.dali510 import Dali510Error, brightness_to_arc
from custom_components.helvar510.devices import BROADCAST_UNIQUE_ID, sa_unique_id

from test_all_and_verify import _bri, _on, _setup, _unload, _verify, tsim  # noqa: F401
from topology import STRIPS

LIGHTS = [4, 7, 11, 24, 31]   # grouped (G2, G0, G2, G1, G1) normal lights
STRIP_ON = [0, 1]


def _strip_top(sim, i):
    return max(sim.levels[sa] for sa in STRIPS[i])


async def _scene(hass, ents, strips):
    """Normal lights at full, two strips on dim with non-full colours."""
    await _on(hass, strips[0], rgbw_color=(255, 0, 0, 128), brightness=100)
    await _on(hass, strips[1], rgbw_color=(0, 0, 0, 255), brightness=90)
    for sa in LIGHTS:
        await _on(hass, ents[sa_unique_id(sa)], brightness=255)


async def test_all_dim_up_brings_strips_to_full(hass: HomeAssistant, tsim) -> None:
    """0.3.2: with other lights at full, All +20 % stayed at 255 and strips
    were scaled by 255/255 - they never got brighter."""
    entry, ents, strips = await _setup(hass)
    coord = entry.runtime_data
    allx = ents[BROADCAST_UNIQUE_ID]
    await _scene(hass, ents, strips)
    await _verify(hass, coord, list(range(39)))
    await _on(hass, allx, brightness_step_pct=20)
    await _verify(hass, coord, list(range(39)))
    assert _bri(hass, allx) == 255
    for i in STRIP_ON:
        assert _strip_top(tsim, i) == 254, i
        assert _bri(hass, strips[i]) == 255
    # colours kept
    assert tuple(hass.states.get(strips[0]).attributes["rgbw_color"]) == (255, 0, 0, 128)
    assert tsim.violations == [] and tsim.config_writes == []
    await _unload(hass, entry)


async def test_all_brightness_is_uniform(hass: HomeAssistant, tsim) -> None:
    entry, ents, strips = await _setup(hass)
    coord = entry.runtime_data
    allx = ents[BROADCAST_UNIQUE_ID]
    await _scene(hass, ents, strips)
    for bri in (128, 51, 204):
        await _on(hass, allx, brightness=bri)
        await _verify(hass, coord, list(range(39)))
        arc = brightness_to_arc(bri)
        assert _bri(hass, allx) == bri
        for sa in LIGHTS:
            assert tsim.levels[sa] == arc, (bri, sa)
            # a member itself reports its arc level (+-1 rounding)
            assert abs(_bri(hass, ents[sa_unique_id(sa)]) - bri) <= 1
        for i in range(4):
            assert _strip_top(tsim, i) == arc, (bri, i)
            assert abs(_bri(hass, strips[i]) - bri) <= 1
    await _unload(hass, entry)


async def _hold(hass, eid, pct, seconds, interval=0.8):
    """Hue 'continuously pressed' every 0.8 s, automation mode: single
    (a trigger while the previous run is still running is dropped)."""
    task = None
    ran = dropped = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        if task is not None and not task.done():
            dropped += 1
        else:
            ran += 1
            task = hass.async_create_task(hass.services.async_call(
                "light", "turn_on",
                {"entity_id": eid, "brightness_step_pct": pct}, blocking=True,
            ))
        await asyncio.sleep(interval)
    if task is not None:
        await task
    await hass.async_block_till_done()
    return ran, dropped


async def test_all_hold_moves_lights_and_strips_together(hass: HomeAssistant, tsim) -> None:
    """Real-bus-like: 30 ms per frame, 45 ms per query, gear that do not act
    on group frames, one gear stuck at full. Holding dim down must dim the
    normal lights *and* the strips; holding dim up brings everything (strips
    included) to full."""
    tsim.frame_delay_s = 0.03
    tsim.query_delay_s = 0.045
    tsim.ignore_group_frames = True
    entry, ents, strips = await _setup(hass)
    allx = ents[BROADCAST_UNIQUE_ID]
    await _scene(hass, ents, strips)
    await asyncio.sleep(3)  # verification of the scene

    ran, _dropped = await _hold(hass, allx, -20, 3.3)
    assert ran >= 2
    bri = _bri(hass, allx)
    assert 0 < bri <= 153, bri
    arc = brightness_to_arc(bri)
    # straight after the hold (before any readback): everything followed
    for sa in LIGHTS:
        assert tsim.levels[sa] == arc, (sa, tsim.levels[sa], arc)
    for i in range(4):
        assert _strip_top(tsim, i) == arc, i

    await _hold(hass, allx, 20, 6.5)
    assert _bri(hass, allx) == 255
    for sa in LIGHTS:
        assert tsim.levels[sa] == 254
    for i in range(4):
        assert _strip_top(tsim, i) == 254, i
    assert tsim.violations == [] and tsim.config_writes == []
    await _unload(hass, entry)


async def test_stale_rewrite_is_not_sent(hass: HomeAssistant, tsim) -> None:
    """A rewrite decided for an older command must not overwrite a newer one."""
    entry, ents, _strips = await _setup(hass)
    coord = entry.runtime_data
    eid = ents[sa_unique_id(11)]
    await _on(hass, eid, brightness=180)
    old = coord._pending[11][0]
    stale_gen = coord._gen[11]
    await _on(hass, eid, brightness=50)
    new = brightness_to_arc(50)
    assert tsim.levels[11] == new
    a0 = tsim.arc_frames
    # decided before the newer command (old gen) ...
    await coord.async_write_levels({11: old}, rewrite=True, gens={11: stale_gen})
    # ... or with an old target
    coord._start_rewrite({11: old})
    await hass.async_block_till_done()
    assert tsim.arc_frames == a0
    assert tsim.levels[11] == new and coord.data[11] == new
    await _unload(hass, entry)


async def test_unload_with_readback_in_flight(hass: HomeAssistant, tsim) -> None:
    tsim.query_delay_s = 0.02
    entry, _ents, _strips = await _setup(hass)
    coord = entry.runtime_data
    task = hass.async_create_task(coord.async_refresh_addresses(list(range(39)), defer=False))
    await asyncio.sleep(0.1)
    await _unload(hass, entry)
    await task  # finishes quietly, no "cannot schedule new futures"
    try:
        await coord.async_write_levels({11: 100})
    except Dali510Error:
        pass


async def test_real_log_sequence_hold_starts_during_readback(
    hass: HomeAssistant, tsim
) -> None:
    """Replay of the real-bus log (0.3.2): All off, All on at 255, the hold
    starts while the readback of that is still running, one DT4 gear does
    not follow DAPC (stays at full). 0.3.2 sent every step to 204 (lights
    stuck at 246, only strips dimmed). Every step must go lower."""
    tsim.frame_delay_s = 0.025
    tsim.query_delay_s = 0.045
    tsim.gear[38]["deaf"] = True        # stays at whatever it was
    tsim.levels[38] = 254
    entry, ents, strips = await _setup(hass)
    coord = entry.runtime_data
    allx = ents[BROADCAST_UNIQUE_ID]
    await _on(hass, strips[0], rgbw_color=(255, 250, 255, 252), brightness=255)
    await hass.services.async_call("light", "turn_off", {"entity_id": allx}, blocking=True)
    await asyncio.sleep(3.4)            # readback of the OFF running ...
    await _on(hass, allx, brightness=255)   # ... cut short by All on
    await asyncio.sleep(6.3)            # readback of All on still running
    seen = []
    for _ in range(4):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": allx, "brightness_step_pct": -20},
            blocking=True,
        )
        seen.append((_bri(hass, allx), tsim.levels[7], tsim.levels[12],
                     _strip_top(tsim, 0)))
        await asyncio.sleep(0.06)
    assert [s[0] for s in seen] == [204, 153, 102, 51], seen
    for bri, g0, ungrouped, strip in seen:
        arc = brightness_to_arc(bri)
        assert g0 == arc and ungrouped == arc and strip == arc, seen
    # the stuck gear did not pin the group; it is reported where it is
    await _verify(hass, coord, list(range(39)))
    assert tsim.levels[38] == 254
    assert _bri(hass, allx) == 51
    # and back up to full: every member (strips too) at the top
    for _ in range(4):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": allx, "brightness_step_pct": 20},
            blocking=True,
        )
    await _verify(hass, coord, list(range(39)))
    assert _bri(hass, allx) == 255
    assert tsim.levels[7] == 254 and _strip_top(tsim, 0) == 254
    await _unload(hass, entry)


async def test_cut_short_readback_does_not_revert_a_step(
    hass: HomeAssistant, tsim
) -> None:
    """Levels read before a write that went out since are dropped at merge
    (0.3.2 merged them after the step: 01:21:50.144 in the real log)."""
    tsim.query_delay_s = 0.045
    entry, ents, strips = await _setup(hass)
    coord = entry.runtime_data
    allx = ents[BROADCAST_UNIQUE_ID]
    await _on(hass, allx, brightness=255)
    await _verify(hass, coord, list(range(39)))
    task = hass.async_create_task(coord.async_refresh_addresses(list(range(39))))
    await asyncio.sleep(1.0)            # part of the gear read at 254
    await _on(hass, allx, brightness=128)
    await task
    arc = brightness_to_arc(128)
    assert all(coord.data[sa] == arc for sa in range(39) if sa not in {
        s for st in STRIPS for s in st}), coord.data
    assert coord.stats.get("stale_dropped", 0) > 0
    assert _bri(hass, allx) == 128
    await _unload(hass, entry)
