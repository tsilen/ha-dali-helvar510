"""On/off-only gear (relay / switching module / non-dimmable driver).

Auto-detected from scan data (DT7, PHYSICAL MINIMUM 254, MIN == MAX == 254)
or set per light in the options. Groups / DALI All never dim it: a brightness
change of a group that is already on sends it nothing; the group going from
off to on switches it on (RECALL MAX), off / brightness 0 switches it off.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from custom_components.helvar510.const import CMD_RECALL_MAX, CONF_GEAR_TYPES
from custom_components.helvar510.coordinator import detect_on_off, on_off_sas
from custom_components.helvar510.dali510 import brightness_to_arc
from custom_components.helvar510.devices import (
    BROADCAST_UNIQUE_ID,
    group_unique_id,
    sa_unique_id,
)

from test_all_and_verify import _bri, _off, _on, _setup, _unload, _verify, tsim  # noqa: F401
from topology import STRIPS

RELAY = 38


def test_detect_rules() -> None:
    dimmer = {"sa": 1, "device_type": 4, "phys_min": 170, "min_level": 170, "max_level": 254}
    assert not detect_on_off(dimmer)
    assert detect_on_off({**dimmer, "phys_min": 254})
    assert detect_on_off({"sa": 1, "device_type": 4, "min_level": 254, "max_level": 254})
    assert detect_on_off({"sa": 1, "device_type": 0xFF, "device_types": [6, 7]})
    assert detect_on_off({"sa": 1, "device_type": 7})
    # min raised to 254 but max lower -> not the rule (max 200 = a dimmer)
    assert not detect_on_off({"sa": 1, "min_level": 254, "max_level": 200, "phys_min": 1})
    gears = [{"sa": 0, "phys_min": 254}, {"sa": 1, **dimmer, "sa": 1}, {"sa": 2, "phys_min": 254}]
    strips = [{"channels": {"r": 2, "g": 3, "b": 4}}]
    assert on_off_sas(gears, strips, {}) == {0}            # strip channel never
    assert on_off_sas(gears, strips, {"0": "dimmable", "1": "onoff"}) == {1}


async def test_relay_entity(hass: HomeAssistant, tsim) -> None:
    entry, ents, _strips = await _setup(hass)
    coord = entry.runtime_data
    assert coord.on_off == {RELAY}
    relay = ents[sa_unique_id(RELAY)]
    st = hass.states.get(relay)
    assert st.attributes["supported_color_modes"] == ["onoff"]
    assert st.attributes["on_off_only"] is True and st.attributes["phys_min"] == 254
    dimmer = hass.states.get(ents[sa_unique_id(12)])
    assert dimmer.attributes["supported_color_modes"] == ["brightness"]
    assert dimmer.attributes["on_off_only"] is False and dimmer.attributes["phys_min"] == 170

    n0 = len(tsim.arc_log)
    await _on(hass, relay)
    assert tsim.frames_reaching(RELAY, n0) == [((RELAY << 1) | 1, CMD_RECALL_MAX)]
    assert tsim.levels[RELAY] == 254 and hass.states.get(relay).state == "on"
    assert hass.states.get(relay).attributes.get("brightness") is None
    await _off(hass, relay)
    assert tsim.levels[RELAY] == 0 and hass.states.get(relay).state == "off"
    await _verify(hass, coord, [RELAY])
    assert hass.states.get(relay).state == "off"
    assert tsim.violations == [] and tsim.config_writes == []
    await _unload(hass, entry)


async def test_all_brightness_change_leaves_relay_alone(hass: HomeAssistant, tsim) -> None:
    entry, ents, strips = await _setup(hass)
    coord = entry.runtime_data
    allx = ents[BROADCAST_UNIQUE_ID]
    relay = ents[sa_unique_id(RELAY)]
    assert hass.states.get(allx).attributes["on_off_members"] == [RELAY]

    # off -> on with a brightness: relay switched on too
    await _off(hass, allx)
    n0 = len(tsim.arc_log)
    await _on(hass, allx, brightness=128)
    assert tsim.levels[RELAY] == 254
    assert ((RELAY << 1) | 1, CMD_RECALL_MAX) in tsim.frames_reaching(RELAY, n0)
    assert _bri(hass, allx) == 128            # relay not in the brightness
    await _verify(hass, coord, list(range(39)))
    assert _bri(hass, allx) == 128
    assert coord.stats["rewrites"] == 0 or RELAY not in coord._retries

    # relay off by itself, then brightness changes of the (still on) All:
    # nothing at all is sent to the relay, it stays off
    await _off(hass, relay)
    n1 = len(tsim.arc_log)
    await _on(hass, allx, brightness_step_pct=-20)
    await _on(hass, allx, brightness_step_pct=20)
    await _on(hass, allx, brightness_step_pct=20)
    await _on(hass, allx, brightness=60)
    assert tsim.frames_reaching(RELAY, n1) == []
    assert tsim.levels[RELAY] == 0 and hass.states.get(relay).state == "off"
    assert _bri(hass, allx) == 60
    assert tsim.levels[7] == coord.clamp_levels({7: brightness_to_arc(60)})[7]

    # relay on, others dimmed: the relay stays on and gets nothing
    await _on(hass, relay)
    n2 = len(tsim.arc_log)
    await _on(hass, allx, brightness_step_pct=-10)
    assert tsim.frames_reaching(RELAY, n2) == []
    assert tsim.levels[RELAY] == 254

    # step to 0 = turn off: everything off, relay included
    for _ in range(12):
        if hass.states.get(allx).state == "off":
            break
        await _on(hass, allx, brightness_step_pct=-20)
    assert hass.states.get(allx).state == "off"
    # (13 is a fixture gear that never follows)
    assert tsim.levels[RELAY] == 0 and all(v == 0 for sa, v in tsim.levels.items() if sa != 13)

    # off -> on by brightness_step: relay on again
    await _on(hass, allx, brightness_step_pct=20)
    assert tsim.levels[RELAY] == 254
    await _off(hass, allx)
    assert tsim.levels[RELAY] == 0
    assert tsim.violations == [] and tsim.config_writes == []
    await _unload(hass, entry)


async def test_override_in_options_keeps_entity(hass: HomeAssistant, tsim) -> None:
    """Old entry (no gear types) -> set a grouped dimmer to on/off only and
    the relay to dimmable: entity_id, name, aliases, area stay."""
    entry, ents, _strips = await _setup(hass)
    coord = entry.runtime_data
    reg = er.async_get(hass)
    area = ar.async_get(hass).async_create("Kitchen")
    eid7 = ents[sa_unique_id(7)]
    reg.async_update_entity(eid7, name="Pendant", aliases={"pendant lamp"}, area_id=area.id)
    eid38 = ents[sa_unique_id(RELAY)]
    reg.async_update_entity(eid38, name="Fan relay")
    before = {k: v for k, v in entry.options.items()}
    assert CONF_GEAR_TYPES not in before or not before[CONF_GEAR_TYPES]

    async def set_type(sa: int, mode: str) -> None:
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(
            r["flow_id"],
            {"poll_interval": 30, "hide_strip_channels": True,
             "hide_group_members": True, "action": "gear_type"},
        )
        assert r["step_id"] == "select_gear"
        labels = {o["value"] for o in r["data_schema"].schema["gear"].config["options"]}
        assert str(sa) in labels and not labels & {str(x) for st in STRIPS for x in st}
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"gear": str(sa)})
        assert r["step_id"] == "gear_type"
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"gear_type": mode})
        assert r["step_id"] == "init"
        r = await hass.config_entries.options.async_configure(
            r["flow_id"],
            {"poll_interval": 30, "hide_strip_channels": True,
             "hide_group_members": True, "action": "save"},
        )
        await hass.async_block_till_done()

    await set_type(7, "onoff")
    await set_type(RELAY, "dimmable")
    assert entry.options[CONF_GEAR_TYPES] == {"7": "onoff", "38": "dimmable"}
    assert coord.on_off == {7}
    for k, v in before.items():
        if k != CONF_GEAR_TYPES:
            assert entry.options[k] == v, k
    e7 = reg.async_get(eid7)
    assert (e7.unique_id, e7.name, e7.aliases, e7.area_id) == (
        sa_unique_id(7), "Pendant", {"pendant lamp"}, area.id)
    assert reg.async_get(eid38).name == "Fan relay"
    assert hass.states.get(eid7).attributes["supported_color_modes"] == ["onoff"]
    assert hass.states.get(eid38).attributes["supported_color_modes"] == ["brightness"]
    g0 = ents[group_unique_id(0)]
    assert hass.states.get(g0).attributes["on_off_members"] == [7]

    # group 0 already on: brightness change leaves 7 alone
    await _off(hass, g0)
    await _on(hass, g0, brightness=200)
    assert tsim.levels[7] == 254 and tsim.levels[9] == brightness_to_arc(200)
    await _off(hass, eid7)
    n0 = len(tsim.arc_log)
    await _on(hass, g0, brightness_step_pct=-20)
    assert tsim.frames_reaching(7, n0) == []
    assert tsim.levels[7] == 0 and tsim.levels[9] == brightness_to_arc(_bri(hass, g0))
    # back to auto: removed from the stored overrides
    await set_type(7, "auto")
    assert entry.options[CONF_GEAR_TYPES] == {"38": "dimmable"}
    assert hass.states.get(eid7).attributes["supported_color_modes"] == ["brightness"]
    assert reg.async_get(eid7).area_id == area.id
    assert tsim.violations == [] and tsim.config_writes == []
    await _unload(hass, entry)
