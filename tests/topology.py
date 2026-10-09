"""Synthetic copy of a real installation's topology (no real identifiers).

39 gear: DT6 at 0-11, 14-18, 23-37; DT4 at 12, 13, 19-22, 38.
RGBW strips (channel order R, B, W, G by ascending address):
[0-3], [15-18], [25-28], [33-36].
Groups: G0=[7,9,14,23,29,37] G1=[5,6,10,24,31] G2=[4,11] G3=[8,30,32] G4=[19,21].
"""
from __future__ import annotations

DT4 = {12, 13, 19, 20, 21, 22, 38}
STRIPS = [[0, 1, 2, 3], [15, 16, 17, 18], [25, 26, 27, 28], [33, 34, 35, 36]]
GROUPS = {
    0: [7, 9, 14, 23, 29, 37],
    1: [5, 6, 10, 24, 31],
    2: [4, 11],
    3: [8, 30, 32],
    4: [19, 21],
}
# some gear with a raised MIN LEVEL (phase dimmers, some LED drivers)
MIN_LEVEL = {12: 120, 13: 120, 19: 110, 20: 110, 21: 110, 22: 110, 38: 130,
             5: 85, 6: 85, 29: 100}


def topology_gear() -> dict[int, dict]:
    gear: dict[int, dict] = {}
    in_strip = {sa: i for i, st in enumerate(STRIPS) for sa in st}
    for sa in range(39):
        groups = [g for g, m in GROUPS.items() if sa in m]
        if sa in in_strip:
            rand = 0x300000 + in_strip[sa] * 0x100 + STRIPS[in_strip[sa]].index(sa)
        else:
            rand = 0x100000 + sa * 0x1111
        g = {"dt": 4 if sa in DT4 else 6, "rand": rand, "groups": groups}
        if sa in MIN_LEVEL:
            g["min"] = MIN_LEVEL[sa]
        gear[sa] = g
    return gear


def strip_inputs() -> list[dict]:
    out = []
    for i, st in enumerate(STRIPS):
        r, b, w, g = st
        out.append({
            "name": f"Strip {i}", "mode": "rgbw",
            "r": str(r), "g": str(g), "b": str(b), "w": str(w),
        })
    return out
