#!/usr/bin/env python3
"""Command-line smoke test for a Helvar DIGIDIM 510 USB-DALI interface.

Works on any Linux host that exposes the interface as /dev/hidraw*. On Home
Assistant OS run it where /dev/hidraw* is visible, e.g. inside the Home
Assistant Core container:
  docker exec -it homeassistant python3 /config/tools/dali510_test.py list

Stop/disable the helvar510 integration while running this script, so two
programs do not talk to the 510 at the same time.

Usage (from /config or anywhere the files are readable):
  python3 dali510_test.py list
  python3 dali510_test.py query 0          # QUERY ACTUAL LEVEL short 0
  python3 dali510_test.py off              # broadcast OFF  (asks confirm)
  python3 dali510_test.py on               # broadcast RECALL MAX (asks confirm)
  python3 dali510_test.py dapc 0 200       # DAPC short 0 level 200
  python3 dali510_test.py scan             # read-only scan
  python3 dali510_test.py groups 5         # QUERY GROUPS of short 5 (read-only)

Copy the driver next to this script, or set PYTHONPATH to
custom_components/helvar510.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATES = [
    os.path.join(HERE, "..", "custom_components", "helvar510"),
    "/config/custom_components/helvar510",
    HERE,
]
for c in CANDIDATES:
    if os.path.isfile(os.path.join(c, "dali510.py")):
        sys.path.insert(0, c)
        break
else:
    sys.stderr.write("Cannot find dali510.py - put this script next to the integration\n")
    sys.exit(2)

from dali510 import (  # noqa: E402
    BROADCAST_CMD,
    Dali510,
    find_510,
    list_hidraw,
    short_dapc_addr,
)


def cmd_list(_: argparse.Namespace) -> int:
    print("hidraw devices:")
    for d in list_hidraw():
        mark = " << 510" if d.is_510 else ""
        print(
            f"  {d.path}  vid={d.vid and f'{d.vid:04x}'} pid={d.pid and f'{d.pid:04x}'} "
            f"name={d.name!r} serial={d.serial!r}{mark}"
            + (f"  ERR={d.error}" if d.error else "")
        )
    found = find_510()
    if not found:
        print("No 16eb:0510 found. Is the 510 plugged in?")
        return 1
    return 0


def _open(path: str | None) -> Dali510:
    bus = Dali510(path or "auto")
    bus.open()
    print(f"opened {bus.path}; info={bus.info_reply and bus.info_reply.hex(' ')}")
    return bus


def cmd_query(ns: argparse.Namespace) -> int:
    bus = _open(ns.path)
    try:
        val = bus.query_short(ns.sa, 0xA0)
        print(f"short {ns.sa} QUERY ACTUAL LEVEL -> {val}")
        return 0 if val is not None else 2
    finally:
        bus.close()


def cmd_dapc(ns: argparse.Namespace) -> int:
    bus = _open(ns.path)
    try:
        r = bus.dapc(short_dapc_addr(ns.sa), ns.level)
        print(r.as_dict())
        return 0
    finally:
        bus.close()


def cmd_off(ns: argparse.Namespace) -> int:
    if not ns.yes and input("Broadcast OFF to ALL gear? [y/N] ").lower() != "y":
        return 1
    bus = _open(ns.path)
    try:
        print(bus.command(BROADCAST_CMD, 0x00).as_dict())
        return 0
    finally:
        bus.close()


def cmd_on(ns: argparse.Namespace) -> int:
    if not ns.yes and input("Broadcast RECALL MAX to ALL gear? [y/N] ").lower() != "y":
        return 1
    bus = _open(ns.path)
    try:
        print(bus.command(BROADCAST_CMD, 0x05).as_dict())
        return 0
    finally:
        bus.close()


def cmd_groups(ns: argparse.Namespace) -> int:
    bus = _open(ns.path)
    try:
        groups = bus.query_groups(ns.sa)
        if groups is None:
            print(f"short {ns.sa} QUERY GROUPS -> no answer")
            return 2
        print(f"short {ns.sa} QUERY GROUPS -> {groups}  (DALI groups 0-15; tools numbering groups 1-16 show these +1)")
        return 0
    finally:
        bus.close()


def cmd_scan(ns: argparse.Namespace) -> int:
    bus = _open(ns.path)
    try:
        gears = bus.scan()
        print(f"found {len(gears)} gear")
        for g in gears:
            ra = g.get("random_address")
            ra_s = f"{ra:06X}" if ra is not None else None
            print(
                f"  SA {g['sa']:2d} type={g.get('device_type')} "
                f"actual={g.get('actual')} groups={g.get('groups')} rand={ra_s}"
            )
        from dali510 import suggest_multichannel

        print("suggested multi-channel strips:", suggest_multichannel(gears))
        return 0
    finally:
        bus.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Helvar 510 USB-DALI smoke test")
    p.add_argument("--path", help="hidraw path (default: auto-detect 16eb:0510)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List hidraw devices")
    q = sub.add_parser("query", help="QUERY ACTUAL LEVEL")
    q.add_argument("sa", type=int)
    d = sub.add_parser("dapc", help="DAPC to short address")
    d.add_argument("sa", type=int)
    d.add_argument("level", type=int)
    o = sub.add_parser("off", help="Broadcast OFF")
    o.add_argument("-y", "--yes", action="store_true")
    n = sub.add_parser("on", help="Broadcast RECALL MAX")
    n.add_argument("-y", "--yes", action="store_true")
    sub.add_parser("scan", help="Read-only bus scan")
    g = sub.add_parser("groups", help="QUERY GROUPS 0-7/8-15 of a short address (read-only)")
    g.add_argument("sa", type=int)

    ns = p.parse_args()
    return {
        "list": cmd_list,
        "query": cmd_query,
        "dapc": cmd_dapc,
        "off": cmd_off,
        "on": cmd_on,
        "scan": cmd_scan,
        "groups": cmd_groups,
    }[ns.cmd](ns)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
