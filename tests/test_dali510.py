"""Driver tests: protocol helpers, safety filter, exact frames, robustness.

No Home Assistant needed. A scripted fake device checks byte-exact OUT
reports; the synthetic SimBus (tests/sim.py) is used for behaviour tests.
"""
from __future__ import annotations

import os
import select
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "helvar510"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dali510 import (  # noqa: E402
    Dali510,
    Dali510Error,
    ST_OK,
    UnsafeFrameError,
    arcs_to_rgbw,
    brightness_to_arc,
    decode_groups,
    group_cmd_addr,
    is_frame_safe,
    is_group_membership_frame,
    parse_dali_reply,
    parse_in_report,
    rgb_brightness_to_arcs,
    short_cmd_addr,
    short_dapc_addr,
    suggest_multichannel,
    validate_hidraw_path,
)
from sim import SimBus  # noqa: E402

DEV = "/dev/hidraw0"  # only used as a name; the opener injects the simulator


# --------------------------------------------------------------------------
# Scripted fake device (byte-exact)
# --------------------------------------------------------------------------
class ScriptedDevice:
    """Answers each OUT report with the next scripted IN report."""

    def __init__(self, script: list[tuple[bytes, bytes]]) -> None:
        self.script = list(script)
        self.idx = 0
        self.errors: list[str] = []
        self.a, self.b = socket.socketpair()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self) -> None:
        buf = b""
        while self.idx < len(self.script):
            r, _, _ = select.select([self.b], [], [], 0.5)
            if not r:
                continue
            try:
                chunk = self.b.recv(256)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while len(buf) >= 37 and self.idx < len(self.script):
                report, buf = buf[1:37], buf[37:]
                live = bytes(report[: 1 + report[0]])
                want, answer = self.script[self.idx]
                if live != want:
                    self.errors.append(f"#{self.idx}: got {live.hex()} want {want.hex()}")
                self.b.sendall(answer + bytes(36 - len(answer)))
                self.idx += 1

    def opener(self, _path: str) -> int:
        return os.dup(self.a.fileno())

    def close(self) -> None:
        self.a.close()
        self.b.close()


INFO = (bytes([0x02, 0x82, 0x04]), bytes([0x03, 0x82, 0x09, 0x00]))
SYNC = (bytes([0x01, 0x8C]), bytes([0x01, 0x8C]))


def run_script(script, fn):
    dev = ScriptedDevice(script)
    bus = Dali510(DEV, opener=dev.opener)
    try:
        bus.open()
        result = fn(bus)
    finally:
        bus.close()
        dev.close()
    return result, dev


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def test_parse_reports():
    assert parse_in_report(bytes([0x02, 0x6D, 0xFE]) + bytes(33)) == (2, bytes([0x6D, 0xFE]))
    assert parse_in_report(bytes(36)) is None
    r = parse_dali_reply(bytes([0x6D, 0x2A]))
    assert (r.status, r.value) == (0x6D, 0x2A)
    r = parse_dali_reply(bytes([0x6B]))
    assert (r.status, r.value) == (0x6B, None)
    assert parse_dali_reply(bytes([0x12])) is None


def test_brightness_and_colour():
    for b in (1, 32, 128, 200, 255):
        assert 1 <= brightness_to_arc(b) <= 254
    arcs = rgb_brightness_to_arcs((255, 0, 0), 255)
    assert arcs[0] > 200 and arcs[1] == 0
    colour, _bri = arcs_to_rgbw(arcs)
    assert colour[0] == 255 and colour[1] == 0


def test_suggest_multichannel():
    gears = [{"sa": i, "device_type": 6, "random_address": 0x4A1000 + i} for i in range(4)]
    gears.append({"sa": 4, "device_type": 6, "random_address": 0x000001})
    assert suggest_multichannel(gears) == [[0, 1, 2, 3]]


def test_decode_groups():
    assert decode_groups(0x08, 0x00) == [3]
    assert decode_groups(0x01, 0x80) == [0, 15]
    assert decode_groups(None, 0) is None


def test_address_helpers_reject_out_of_range():
    assert short_cmd_addr(63) == 0x7F
    assert short_dapc_addr(0) == 0x00
    assert group_cmd_addr(15) == 0x9F
    for bad in (64, -1, True, 1.0, "1"):
        with pytest.raises(ValueError):
            short_cmd_addr(bad)
        with pytest.raises(ValueError):
            short_dapc_addr(bad)
    for bad in (16, -1, False):
        with pytest.raises(ValueError):
            group_cmd_addr(bad)


# --------------------------------------------------------------------------
# Safety filter
# --------------------------------------------------------------------------
BLOCKED = {
    "INITIALISE": (0xA5, 0x00),
    "RANDOMISE": (0xA7, 0x00),
    "COMPARE": (0xA9, 0x00),
    "WITHDRAW": (0xAB, 0x00),
    "SEARCHADDRH": (0xB1, 0x12),
    "PROGRAM SHORT ADDRESS": (0xB7, 0x03),
    "VERIFY SHORT ADDRESS": (0xB9, 0x03),
    "DTR0": (0xA3, 0x10),
    "DTR1": (0xC3, 0x10),
    "DTR2": (0xC5, 0x10),
    "ENABLE DEVICE TYPE": (0xC1, 0x08),
    "WRITE MEMORY LOCATION": (0xC7, 0x00),
    "WRITE MEMORY LOCATION NO REPLY": (0xC9, 0x00),
    "RESET short": (0x01, 0x20),
    "RESET broadcast": (0xFF, 0x20),
    "STORE ACTUAL LEVEL IN DTR0": (0x01, 0x21),
    "SET MAX LEVEL": (0x01, 0x2A),
    "SET FADE TIME broadcast": (0xFF, 0x2E),
    "SET SCENE short": (0x01, 0x40),
    "SET SCENE group": (0x81, 0x45),
    "REMOVE FROM SCENE": (0xFF, 0x50),
    "ADD TO GROUP short": (0x0B, 0x63),
    "ADD TO GROUP broadcast": (0xFF, 0x60),
    "ADD TO GROUP group-addressed": (0x81, 0x60),
    "REMOVE FROM GROUP": (0x0B, 0x73),
    "STORE DTR AS SHORT ADDRESS": (0x01, 0x80),
    "ENABLE WRITE MEMORY": (0xFF, 0x81),
    "application extended (DT6 reference)": (0xFF, 0xE0),
    "application extended (DT8 store)": (0x01, 0xEF),
    "special reserved even": (0xA4, 0x00),
}

ALLOWED = {
    "DAPC short": (0x00, 0xFE),
    "DAPC group": (0x80, 0x80),
    "DAPC broadcast": (0xFE, 0x00),
    "OFF broadcast": (0xFF, 0x00),
    "RECALL MAX": (0x01, 0x05),
    "GO TO SCENE 3": (0x01, 0x13),
    "QUERY ACTUAL LEVEL": (0x01, 0xA0),
    "QUERY GROUPS": (0x0B, 0xC0),
    "TERMINATE": (0xA1, 0x00),
}


@pytest.mark.parametrize("name", sorted(BLOCKED))
def test_blocked_frames(name):
    assert not is_frame_safe(*BLOCKED[name]), name


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_allowed_frames(name):
    assert is_frame_safe(*ALLOWED[name]), name


def test_filter_exhaustive_invariants():
    for addr in range(256):
        for data in range(256):
            if not is_frame_safe(addr, data):
                continue
            if 0xA0 <= addr <= 0xFB:
                assert addr == 0xA1, f"{addr:02x} {data:02x}"
            elif addr & 1:
                assert data <= 0x1F or 0x90 <= data <= 0xDF, f"{addr:02x} {data:02x}"


@pytest.mark.parametrize("addr,data", [(-1, 0), (256, 0), (0, 256), (True, 0),
                                       (0, False), (1.0, 0), ("1", 0), (None, 0)])
def test_filter_rejects_bad_types(addr, data):
    assert not is_frame_safe(addr, data)
    assert not is_group_membership_frame(addr, data)


def test_group_membership_filter():
    assert is_group_membership_frame(0x0B, 0x63)       # short 5 ADD group 3
    assert is_group_membership_frame(0x0B, 0x73)       # short 5 REMOVE group 3
    for addr, data in ((0x0A, 0x63), (0xFF, 0x60), (0x81, 0x60), (0x0B, 0x80),
                       (0x0B, 0x20), (0x0B, 0x5F), (0xA5, 0x00)):
        assert not is_group_membership_frame(addr, data), f"{addr:02x} {data:02x}"


def test_send_refuses_every_blocked_frame_without_touching_the_bus():
    def body(bus):
        for name, (addr, data) in BLOCKED.items():
            with pytest.raises(UnsafeFrameError, match="refusing"):
                bus.send(addr, data, False)
            with pytest.raises(UnsafeFrameError):
                bus.send(addr, data, True)
        # membership path refuses everything except 0x60-0x7F to one short address
        for addr, data in ((0xFF, 0x63), (0x81, 0x63), (0x0B, 0x20), (0xA5, 0x00), (0xA3, 0x10)):
            with pytest.raises(UnsafeFrameError):
                bus._frame(addr, data, False, _membership=True, twice=True)
        # the send-twice bit is not available outside the membership path
        with pytest.raises(UnsafeFrameError):
            bus._frame(0x01, 0x05, False, twice=True)
        return True

    ok, dev = run_script([INFO], body)
    assert ok
    assert dev.idx == 1  # nothing but the info request reached the device


# --------------------------------------------------------------------------
# Device path validation
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/dev/hidraw0", "/dev/hidraw12"])
def test_valid_hidraw_paths(path):
    assert validate_hidraw_path(path) == path


@pytest.mark.parametrize("path", ["/etc/passwd", "/dev/sda", "/dev/hidraw", "/dev/hidraw0/../../etc/passwd",
                                  "/dev/hidraw0 ", "dev/hidraw0", "/config/x", "", "/dev/hidrawX", None])
def test_invalid_hidraw_paths(path):
    with pytest.raises(Dali510Error):
        validate_hidraw_path(path)


def test_symlink_to_other_file_refused(tmp_path, monkeypatch):
    target = tmp_path / "secret"
    target.write_text("x")
    monkeypatch.setattr(os.path, "realpath", lambda p: str(target))
    with pytest.raises(Dali510Error, match="resolves"):
        validate_hidraw_path("/dev/hidraw0")


def test_open_never_touches_non_hidraw_path(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "open", lambda *a, **k: calls.append(a) or 99)
    for path in ("/etc/passwd", "/config/configuration.yaml", "/dev/sda"):
        with pytest.raises(Dali510Error):
            Dali510(path).open()
    assert calls == []


# --------------------------------------------------------------------------
# Exact frames
# --------------------------------------------------------------------------
def test_query_frame_layout():
    script = [INFO, (bytes([0x03, 0x56, 0x01, 0xA0]), bytes([0x02, 0x6D, 0xFE])), SYNC]
    val, dev = run_script(script, lambda b: b.query_short(0, 0xA0))
    assert val == 0xFE
    assert dev.errors == []


def test_dapc_and_broadcast_off_frames():
    script = [
        INFO,
        (bytes([0x03, 0x52, 0x00, 0xFC]), bytes([0x01, 0x64])), SYNC,
        (bytes([0x03, 0x52, 0xFF, 0x00]), bytes([0x01, 0x64])), SYNC,
    ]

    def body(bus):
        return bus.dapc(0x00, 0xFC).status, bus.command(0xFF, 0x00).status

    res, dev = run_script(script, body)
    assert res == (ST_OK, ST_OK)
    assert dev.errors == []


def _groups_q(addr, lo, hi):
    return [(bytes([0x03, 0x56, addr, 0xC0]), bytes([0x02, 0x6D, lo])),
            (bytes([0x03, 0x54, addr, 0xC1]), bytes([0x02, 0x6D, hi])), SYNC]


def test_add_to_group_uses_send_twice_bit():
    # short 5 -> address byte 0x0B; ADD TO GROUP 3 = 0x63; ctl 0x50|0x02|0x80
    script = [INFO] + _groups_q(0x0B, 0x00, 0x00) + [
        (bytes([0x03, 0xD2, 0x0B, 0x63]), bytes([0x01, 0x64])), SYNC,
    ] + _groups_q(0x0B, 0x08, 0x00)
    res, dev = run_script(script, lambda b: b.set_group_membership(5, 3, True))
    assert dev.errors == [] and dev.idx == len(script)
    assert res["groups_before"] == [] and res["groups"] == [3]
    assert res["verified"] and res["method"] == "twice_bit" and res["frame"] == "0b 63"


def test_auto_falls_back_to_two_frames():
    script = [INFO] + _groups_q(0x0B, 0x00, 0x00) + [
        (bytes([0x03, 0xD2, 0x0B, 0x63]), bytes([0x01, 0x64])), SYNC,
    ] + _groups_q(0x0B, 0x00, 0x00) + [
        (bytes([0x03, 0x52, 0x0B, 0x63]), bytes([0x01, 0x64])),
        (bytes([0x03, 0x50, 0x0B, 0x63]), bytes([0x01, 0x64])), SYNC,
    ] + _groups_q(0x0B, 0x08, 0x00)
    res, dev = run_script(script, lambda b: b.set_group_membership(5, 3, True))
    assert dev.errors == []
    assert res["method"] == "two_frames" and res["verified"]


def test_already_member_sends_no_write():
    script = [INFO] + _groups_q(0x0B, 0x08, 0x00)
    res, dev = run_script(script, lambda b: b.set_group_membership(5, 3, True))
    assert res["method"] == "already" and dev.idx == len(script)


def test_membership_argument_validation():
    def body(bus):
        for args in ((64, 3), (-1, 3), (5, 16), (True, 3), (5, 3.0), ("5", 3)):
            with pytest.raises(ValueError):
                bus.set_group_membership(*args, True)
        with pytest.raises(ValueError):
            bus.set_group_membership(5, 3, True, method="shell")
        return True

    ok, dev = run_script([INFO], body)
    assert ok and dev.idx == 1


# --------------------------------------------------------------------------
# Robustness: timeouts and unplug never hang
# --------------------------------------------------------------------------
def test_silent_device_times_out_quickly():
    sim = SimBus()
    bus = Dali510(DEV, opener=sim.opener)
    try:
        bus.open()
        sim.silent = True
        t0 = time.monotonic()
        with pytest.raises(Dali510Error):
            bus.query_short(0, 0xA0)
        assert time.monotonic() - t0 < 2.0
        # the lock is released again: another call does not dead-lock
        with pytest.raises(Dali510Error):
            bus.query_short(1, 0xA0)
    finally:
        bus.close()
        sim.close()


def test_unplug_closes_and_raises():
    sim = SimBus()
    bus = Dali510(DEV, opener=sim.opener)
    try:
        bus.open()
        assert bus.query_short(0, 0x91) == 0xFF
        sim.unplug()
        t0 = time.monotonic()
        with pytest.raises(Dali510Error):
            for _ in range(3):
                bus.query_short(0, 0xA0)
        assert time.monotonic() - t0 < 3.0
        assert not bus.is_open
    finally:
        bus.close()
        sim.close()


def test_concurrent_callers_are_serialised():
    sim = SimBus()
    bus = Dali510(DEV, opener=sim.opener)
    errors = []
    try:
        bus.open()

        def worker(sa):
            try:
                for _ in range(5):
                    assert bus.query_short(sa, 0x91) == 0xFF
            except Exception as err:  # noqa: BLE001
                errors.append(err)

        threads = [threading.Thread(target=worker, args=(sa,)) for sa in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert not any(t.is_alive() for t in threads)
    finally:
        bus.close()
        sim.close()
    assert errors == []
    assert sim.violations == []
