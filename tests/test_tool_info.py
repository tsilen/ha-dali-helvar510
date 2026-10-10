"""tools/dali510_test.py `info`: standard queries only (no DTR, no writes)."""
from __future__ import annotations

import importlib.util
import os

from sim import SimBus
from topology import topology_gear

TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "dali510_test.py")


def _tool():
    spec = importlib.util.spec_from_file_location("dali510_test_tool", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_info_reads_on_off_gear() -> None:
    gear = topology_gear()
    sim = SimBus(gear)
    try:
        tool = _tool()
        bus = tool.Dali510("/dev/hidraw0", opener=sim.opener)
        bus.open()
        try:
            relay = tool.query_info(bus, 38)
            dimmer = tool.query_info(bus, 12)
        finally:
            bus.close()
        assert relay[0x99] == 4 and relay[0x9A] == 254
        assert relay[0xA1] == 254 and relay[0xA2] == 254
        assert dimmer[0x9A] == 170 and dimmer[0xA2] == 170
        assert sim.arc_frames == 0 and sim.config_writes == [] and sim.violations == []
    finally:
        sim.close()
        sim._t.join(timeout=5)
