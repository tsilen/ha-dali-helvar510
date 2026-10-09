"""Read-only scan and group membership against the synthetic simulated bus."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "helvar510"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dali510 import Dali510, suggest_multichannel  # noqa: E402
from sim import SYNTHETIC_GEAR, SimBus  # noqa: E402


def _scan():
    sim = SimBus()
    bus = Dali510("/dev/hidraw0", opener=sim.opener)
    try:
        bus.open()
        return bus.scan(max_sa=15), sim
    finally:
        bus.close()
        sim.close()


def test_scan_inventory():
    gears, sim = _scan()
    assert [g["sa"] for g in gears] == sorted(SYNTHETIC_GEAR)
    for g in gears:
        assert g["device_type"] == SYNTHETIC_GEAR[g["sa"]]["dt"]
        assert g["groups"] == SYNTHETIC_GEAR[g["sa"]]["groups"]
        assert g["random_address"] == SYNTHETIC_GEAR[g["sa"]]["rand"]
    assert suggest_multichannel(gears) == [[0, 1, 2, 3]]
    dt8 = next(g for g in gears if g["sa"] == 11)
    assert dt8["dt8"]["tc_capable"] is True
    assert sim.violations == []


def test_scan_is_query_only():
    _gears, sim = _scan()
    for live in sim.log:
        if live[0] != 3:
            continue
        ctl, addr, data = live[1], live[2], live[3]
        if addr == 0xC1:  # ENABLE DEVICE TYPE 8, immediately followed by a DT8 query
            assert data == 0x08
            continue
        assert ctl & 0x04, f"non-query frame during scan: {live.hex()}"
        assert not ctl & 0x80
    assert sim.config_writes == []


def test_group_membership_roundtrip():
    sim = SimBus()
    bus = Dali510("/dev/hidraw0", opener=sim.opener)
    try:
        bus.open()
        res = bus.set_group_membership(12, 5, True)
        assert res["verified"] and res["groups"] == [5]
        assert sim.config_writes == [(0xD2, 0x19, 0x65)]
        sim.twice_bit_works = False
        res = bus.set_group_membership(12, 5, False)
        assert res["verified"] and res["method"] == "two_frames" and res["groups"] == []
    finally:
        bus.close()
        sim.close()
    assert sim.violations == []
    assert all(addr < 0x80 and 0x60 <= data <= 0x7F for _c, addr, data in sim.config_writes)
