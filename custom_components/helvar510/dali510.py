"""Pure-Python driver for the Helvar DIGIDIM 510 USB-DALI interface.

No Home Assistant imports and no native libraries: talks to Linux /dev/hidraw*
with os.open/os.read/os.write and fcntl ioctls, so it runs on HAOS (Alpine/musl,
aarch64) and can be reused by the stand-alone CLI tool.

Protocol (determined by observing the device's USB communication):
  OUT report (36 B, unnumbered, sent by hidraw as SET_REPORT Output on EP0):
      [len][ctl][dali_addr][dali_data] + zero padding     len=3 for a DALI frame
      interface commands: [02 82 04] (info), [01 8C] (end of transaction)
  IN report (36 B, interrupt EP 0x81):
      [len][status][value] + stale bytes; status 64=sent, 6B=no reply,
      6C=multiple/garbled reply, 6D xx=backward frame xx
  ctl: 0x50 base, |0x02 first frame of a transaction (frame right after 8C),
       |0x04 wait for a backward frame, |0x80 send twice (only used for the
       explicit ADD/REMOVE GROUP service).

SAFETY: this module refuses to put any configuration / commissioning frame on
the bus (INITIALISE, RANDOMISE, PROGRAM SHORT ADDRESS, DTR writes, STORE ...,
SET SCENE, RESET, application extended commands, ENABLE DEVICE TYPE outside
the built-in DT8 read-only query, ...). See is_frame_safe(). The only
exception is ADD/REMOVE GROUP to a single short address, reachable only via
Dali510.set_group_membership() (see is_group_membership_frame()).
"""
from __future__ import annotations

import errno
import fcntl
import glob
import logging
import math
import os
import re
import select
import stat
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable

_LOGGER = logging.getLogger(__name__)

VID = 0x16EB
PID = 0x0510
REPORT_LEN = 36

CTL_BASE = 0x50
CTL_FIRST = 0x02
CTL_REPLY = 0x04

ST_OK = 0x64
ST_NO_REPLY = 0x6B
ST_MULTI = 0x6C
ST_REPLY = 0x6D
DALI_STATUSES = (ST_OK, ST_NO_REPLY, ST_MULTI, ST_REPLY)

IF_INFO = bytes([0x02, 0x82, 0x04])
IF_SYNC = bytes([0x01, 0x8C])

# Timeouts (observed: no-reply frame 24-40 ms, query 38-48 ms, 8C 16 ms)
TIMEOUT_FRAME = 0.25
TIMEOUT_QUERY = 0.30
TIMEOUT_IF = 0.30

# HIDIOCGRAWINFO = _IOR('H', 0x03, struct hidraw_devinfo{u32 bustype; s16 vendor; s16 product})
HIDIOCGRAWINFO = 0x80084803


class Dali510Error(Exception):
    """Generic 510 error (I/O, device gone, ...)."""


class Dali510Timeout(Dali510Error):
    """No IN report arrived in time."""


class UnsafeFrameError(Dali510Error):
    """Frame refused because it could change gear configuration."""


# --------------------------------------------------------------------------
# DALI helpers
# --------------------------------------------------------------------------

def _check_range(name: str, value: int, hi: int) -> int:
    # bool is an int subclass; refuse it so True/False never become addresses
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= hi:
        raise ValueError(f"{name} must be an integer 0-{hi}, got {value!r}")
    return value


def short_cmd_addr(sa: int) -> int:
    """Address byte for a command/query to short address sa (0-63)."""
    return (_check_range("short address", sa, 63) << 1) | 1


def short_dapc_addr(sa: int) -> int:
    """Address byte for DAPC (direct arc power) to short address sa (0-63)."""
    return _check_range("short address", sa, 63) << 1


def group_cmd_addr(g: int) -> int:
    """Address byte for a command to DALI group g (0-15)."""
    return 0x80 | (_check_range("group", g, 15) << 1) | 1


def group_dapc_addr(g: int) -> int:
    """Address byte for DAPC to DALI group g (0-15)."""
    return 0x80 | (_check_range("group", g, 15) << 1)


BROADCAST_DAPC = 0xFE
BROADCAST_CMD = 0xFF


def arc_to_percent(level: int) -> float:
    """IEC 62386-102 logarithmic dimming curve: arc level -> % light output."""
    if level <= 0:
        return 0.0
    level = min(level, 254)
    return 10 ** ((level - 1) / (253 / 3) - 1)


def percent_to_arc(pct: float) -> int:
    if pct <= 0:
        return 0
    pct = min(pct, 100.0)
    lvl = 1 + (253 / 3) * (math.log10(pct) + 1)
    return max(1, min(254, int(round(lvl))))


def brightness_to_arc(brightness: int) -> int:
    """HA brightness 0-255 -> DALI arc level 0 (off) / 1-254."""
    if brightness <= 0:
        return 0
    return percent_to_arc(brightness * 100.0 / 255.0)


def arc_to_brightness(level: int | None) -> int | None:
    if level is None:
        return None
    if level <= 0:
        return 0
    if level == 0xFF:  # MASK = unknown / fading
        return None
    return max(1, min(255, int(round(arc_to_percent(level) * 255.0 / 100.0))))


def is_frame_safe(addr: int, data: int) -> bool:
    """True if the 16-bit forward frame cannot change persistent gear config.

    Allowed:
      * DAPC (even address byte: short, group or broadcast), any level
        (0xFF = MASK = stop fading, harmless),
      * arc power commands 0x00-0x1F (OFF, UP, DOWN, RECALL MAX/MIN, GO TO
        SCENE ...) to any address,
      * standard queries 0x90-0xDF to any address,
      * TERMINATE (special command 0xA1).
    Refused:
      * every other special command 0xA0-0xFB (INITIALISE, RANDOMISE,
        COMPARE/WITHDRAW, SEARCHADDR, PROGRAM/VERIFY SHORT ADDRESS, DTR0/1/2,
        ENABLE DEVICE TYPE, WRITE MEMORY LOCATION, reserved codes),
      * configuration commands 0x20-0x8F (RESET, STORE ..., SET ..., scenes,
        ADD/REMOVE GROUP, short address, ENABLE WRITE MEMORY ...) to any
        address - short, group or broadcast,
      * application extended commands 0xE0-0xFF (device-type specific, some
        of them store settings).
    Values outside 0-255 are refused.
    """
    if isinstance(addr, bool) or isinstance(data, bool):
        return False
    if not (isinstance(addr, int) and isinstance(data, int)):
        return False
    if not (0 <= addr <= 0xFF and 0 <= data <= 0xFF):
        return False
    if 0xA0 <= addr <= 0xFB:
        return addr == 0xA1  # special commands: only TERMINATE
    if addr & 1 == 0:
        return True  # DAPC (short 0x00-0x7E, group 0x80-0x9E, broadcast 0xFC/0xFE)
    # Addressed command: short / group / broadcast / broadcast-unaddressed
    if 0x20 <= data <= 0x8F:
        return False
    if data >= 0xE0:
        return False
    return True


CMD_ADD_TO_GROUP = 0x60
CMD_REMOVE_FROM_GROUP = 0x70
CMD_QUERY_GROUPS_0_7 = 0xC0
CMD_QUERY_GROUPS_8_15 = 0xC1
CTL_TWICE = 0x80


def is_group_membership_frame(addr: int, data: int) -> bool:
    """ADD TO GROUP / REMOVE FROM GROUP (0x60-0x7F) to ONE short address.

    The only configuration command the integration may ever send, and only via
    Dali510.set_group_membership (never via send/send_raw). Broadcast and group
    addressing are refused so one call can never touch more than one gear.
    """
    if isinstance(addr, bool) or isinstance(data, bool):
        return False
    if not (isinstance(addr, int) and isinstance(data, int)):
        return False
    return 0 <= addr < 0x80 and addr & 1 == 1 and 0x60 <= data <= 0x7F


def decode_groups(lo: int | None, hi: int | None) -> list[int] | None:
    if lo is None or hi is None:
        return None
    mask = ((hi & 0xFF) << 8) | (lo & 0xFF)
    return [g for g in range(16) if mask >> g & 1]


# --------------------------------------------------------------------------
# hidraw discovery
# --------------------------------------------------------------------------

@dataclass
class HidrawInfo:
    path: str
    vid: int | None = None
    pid: int | None = None
    name: str | None = None
    serial: str | None = None
    error: str | None = None

    @property
    def is_510(self) -> bool:
        return self.vid == VID and self.pid == PID


def _sysfs_info(node: str) -> HidrawInfo:
    info = HidrawInfo(path=f"/dev/{node}")
    uevent = f"/sys/class/hidraw/{node}/device/uevent"
    try:
        with open(uevent, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, _, val = line.strip().partition("=")
                if key == "HID_ID":
                    # e.g. 0003:000016EB:00000510
                    parts = val.split(":")
                    if len(parts) == 3:
                        info.vid = int(parts[1], 16) & 0xFFFF
                        info.pid = int(parts[2], 16) & 0xFFFF
                elif key == "HID_NAME":
                    info.name = val
                elif key == "HID_UNIQ":
                    info.serial = val or None
    except OSError as err:
        info.error = str(err)
    return info


def _ioctl_info(path: str) -> tuple[int, int] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(8)
        fcntl.ioctl(fd, HIDIOCGRAWINFO, buf, True)
        _bus, vendor, product = struct.unpack("<Ihh", bytes(buf))
        return vendor & 0xFFFF, product & 0xFFFF
    except OSError:
        return None
    finally:
        os.close(fd)


def list_hidraw() -> list[HidrawInfo]:
    """List /dev/hidraw* with VID/PID (sysfs first, ioctl fallback)."""
    result: list[HidrawInfo] = []
    paths = sorted(glob.glob("/dev/hidraw*"), key=lambda p: (len(p), p))
    nodes = {os.path.basename(p) for p in paths}
    try:
        nodes |= set(os.listdir("/sys/class/hidraw"))
    except OSError:
        pass
    for node in sorted(nodes, key=lambda n: (len(n), n)):
        info = _sysfs_info(node)
        if info.vid is None and os.path.exists(info.path):
            ids = _ioctl_info(info.path)
            if ids:
                info.vid, info.pid = ids
        if not os.path.exists(info.path):
            info.error = (info.error or "") + " (device node missing in this container)"
        result.append(info)
    return result


_HIDRAW_RE = re.compile(r"^/dev/hidraw[0-9]{1,3}$")


def validate_hidraw_path(path: str) -> str:
    """Only /dev/hidrawN is accepted (also after resolving symlinks).

    Prevents a tampered config entry from making the integration open and
    write to an arbitrary file or device.
    """
    if not isinstance(path, str) or not _HIDRAW_RE.match(path):
        raise Dali510Error(f"refusing device path {path!r}: only /dev/hidrawN is allowed")
    real = os.path.realpath(path)
    if not _HIDRAW_RE.match(real):
        raise Dali510Error(f"refusing device path {path!r}: resolves to {real!r}")
    return path


def find_510() -> list[HidrawInfo]:
    return [i for i in list_hidraw() if i.is_510 and os.path.exists(i.path)]


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

@dataclass
class Reply:
    status: int
    value: int | None = None
    raw: bytes = b""

    @property
    def ok(self) -> bool:
        return self.status in (ST_OK, ST_REPLY)

    @property
    def name(self) -> str:
        return {
            ST_OK: "OK",
            ST_NO_REPLY: "NO_REPLY",
            ST_MULTI: "MULTI_REPLY",
            ST_REPLY: "REPLY",
        }.get(self.status, f"0x{self.status:02x}")

    def as_dict(self) -> dict:
        return {"status": self.name, "status_byte": self.status,
                "value": self.value, "raw": self.raw.hex(" ")}


def build_report(payload: bytes) -> bytes:
    if len(payload) > REPORT_LEN:
        raise ValueError("payload too long")
    return payload + bytes(REPORT_LEN - len(payload))


def parse_in_report(data: bytes) -> tuple[int, bytes] | None:
    """Return (len, live bytes) or None for empty reports."""
    if len(data) == REPORT_LEN + 1 and data[0] == 0:
        data = data[1:]  # tolerate a report-id prefix
    if not data:
        return None
    n = data[0]
    if n == 0 or n >= len(data):
        return None
    return n, bytes(data[1:1 + n])


def parse_dali_reply(live: bytes) -> Reply | None:
    if not live:
        return None
    st = live[0]
    if st not in DALI_STATUSES:
        return None
    val = live[1] if (st == ST_REPLY and len(live) >= 2) else None
    return Reply(st, val, live)


class Dali510:
    """Synchronous, thread-safe driver. Run it in an executor thread."""

    def __init__(self, path: str | None = None,
                 opener: Callable[[str], int] | None = None) -> None:
        self.requested_path = path or "auto"
        self.path: str | None = None
        self.fd: int | None = None
        self.info_reply: bytes | None = None
        self.serial: str | None = None
        self._lock = threading.RLock()
        self._opener = opener
        self._in_tx = False
        self._first = True
        self.frames: deque[str] = deque(maxlen=200)

    # ---------------- open / close ----------------
    def resolve_path(self) -> str:
        if self.requested_path not in (None, "", "auto"):
            return validate_hidraw_path(self.requested_path)
        devs = find_510()
        if not devs:
            raise Dali510Error("Helvar 510 (16eb:0510) not found under /dev/hidraw*")
        self.serial = devs[0].serial
        return devs[0].path

    def open(self, init: bool = True) -> None:
        with self._lock:
            self.close()
            path = self.resolve_path()
            try:
                if self._opener:
                    # Test hook (simulated bus): no device checks.
                    self.fd = self._opener(path)
                else:
                    validate_hidraw_path(path)
                    self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
                    self._verify_device(path)
            except OSError as err:
                self.close()
                raise Dali510Error(f"cannot open {path}: {err}") from err
            self.path = path
            self._first = True
            _LOGGER.debug("opened %s", path)
            if init:
                self.init_interface()

    def _verify_device(self, path: str) -> None:
        """Opened fd must be a character device reporting 16eb:0510."""
        assert self.fd is not None
        if not stat.S_ISCHR(os.fstat(self.fd).st_mode):
            self.close()
            raise Dali510Error(f"{path} is not a character device")
        try:
            buf = bytearray(8)
            fcntl.ioctl(self.fd, HIDIOCGRAWINFO, buf, True)
            _bus, vendor, product = struct.unpack("<Ihh", bytes(buf))
        except OSError as err:
            self.close()
            raise Dali510Error(f"{path}: HIDIOCGRAWINFO failed: {err}") from err
        if (vendor & 0xFFFF, product & 0xFFFF) != (VID, PID):
            self.close()
            raise Dali510Error(
                f"{path} is {vendor & 0xFFFF:04x}:{product & 0xFFFF:04x}, not {VID:04x}:{PID:04x}"
            )

    def close(self) -> None:
        with self._lock:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
            self.fd = None

    @property
    def is_open(self) -> bool:
        return self.fd is not None

    # ---------------- low level ----------------
    def _log(self, direction: str, data: bytes) -> None:
        txt = f"{time.strftime('%H:%M:%S')} {direction} {data.hex(' ')}"
        self.frames.append(txt)
        _LOGGER.debug("%s %s", direction, data.hex(" "))

    def _drain(self) -> None:
        assert self.fd is not None
        for _ in range(64):  # bounded: a flooding device cannot hang us here
            try:
                r, _, _ = select.select([self.fd], [], [], 0)
                if not r:
                    return
                data = os.read(self.fd, 64)
                if not data:
                    return
                parsed = parse_in_report(data)
                if parsed:
                    self._log("RX(stale)", parsed[1])
            except BlockingIOError:
                return
            except OSError as err:
                self._io_error(err)

    def _io_error(self, err: OSError) -> None:
        self.close()
        raise Dali510Error(f"I/O error: {err}") from err

    def _write(self, payload: bytes) -> None:
        if self.fd is None:
            raise Dali510Error("device not open")
        self._log("TX", payload)
        try:
            os.write(self.fd, b"\x00" + build_report(payload))
        except OSError as err:
            self._io_error(err)

    def _read_until(self, accept: Callable[[bytes], bool], timeout: float) -> bytes:
        assert self.fd is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Dali510Timeout("no reply from 510")
            try:
                r, _, _ = select.select([self.fd], [], [], remaining)
            except OSError as err:
                self._io_error(err)
            if not r:
                continue
            try:
                data = os.read(self.fd, 64)
            except BlockingIOError:
                continue
            except OSError as err:
                self._io_error(err)
            if not data:
                # readable but EOF: the device went away
                self._io_error(OSError(errno.ENODEV, "device disconnected"))
            parsed = parse_in_report(data)
            if not parsed:
                continue
            live = parsed[1]
            self._log("RX", live)
            if accept(live):
                return live

    # ---------------- interface commands ----------------
    def init_interface(self) -> bytes | None:
        """Interface info request sent after open: 02 82 04 -> 03 82 09 00."""
        with self._lock:
            self._drain()
            self._write(IF_INFO)
            try:
                live = self._read_until(lambda b: b[:1] == b"\x82", TIMEOUT_IF)
                self.info_reply = live
            except Dali510Timeout:
                _LOGGER.warning("510 did not answer 02 82 04 (continuing)")
                self.info_reply = None
            self._first = True
            return self.info_reply

    def sync(self) -> None:
        """End of transaction (01 8C -> 01 8C)."""
        with self._lock:
            self._write(IF_SYNC)
            try:
                self._read_until(lambda b: b[:1] == b"\x8c", TIMEOUT_IF)
            except Dali510Timeout:
                _LOGGER.debug("no 8C echo")
            self._first = True

    # ---------------- DALI frames ----------------
    def _frame(self, addr: int, data: int, expect_reply: bool,
               *, _internal_ok: bool = False, _membership: bool = False,
               twice: bool = False) -> Reply:
        if _membership:
            # Narrow exception: only ADD/REMOVE GROUP to a single short address
            if not is_group_membership_frame(addr, data):
                raise UnsafeFrameError(
                    f"refusing DALI frame {addr:02x} {data:02x} on group-membership path")
        elif not _internal_ok and not is_frame_safe(addr, data):
            raise UnsafeFrameError(f"refusing unsafe DALI frame {addr:02x} {data:02x}")
        if twice and not _membership:
            raise UnsafeFrameError("send-twice is only used for group membership")
        ctl = CTL_BASE | (CTL_REPLY if expect_reply else 0)
        if twice:
            ctl |= CTL_TWICE
        if self._first:
            ctl |= CTL_FIRST
            self._first = False
        self._drain()
        self._write(bytes([3, ctl, addr & 0xFF, data & 0xFF]))
        live = self._read_until(
            lambda b: b[:1] and b[0] in DALI_STATUSES,
            TIMEOUT_QUERY if expect_reply else TIMEOUT_FRAME,
        )
        reply = parse_dali_reply(live)
        assert reply is not None
        return reply

    def transaction(self) -> "_Tx":
        return _Tx(self)

    def send(self, addr: int, data: int, expect_reply: bool = False) -> Reply:
        """One frame as its own transaction (frame, then 01 8C)."""
        with self.transaction() as tx:
            return tx.frame(addr, data, expect_reply)

    def query(self, addr: int, cmd: int) -> Reply:
        return self.send(addr, cmd, True)

    # ---------------- convenience ----------------
    def query_short(self, sa: int, cmd: int) -> int | None:
        r = self.query(short_cmd_addr(sa), cmd)
        return r.value if r.status == ST_REPLY else None

    def dapc(self, addr_byte: int, level: int) -> Reply:
        return self.send(addr_byte & 0xFE, max(0, min(254, level)))

    def command(self, addr_byte: int, cmd: int) -> Reply:
        return self.send(addr_byte | 1, cmd)

    def dapc_many(self, items: Iterable[tuple[int, int]], retries: int = 1) -> list[Reply]:
        """Several DAPC frames in one transaction (used for RGBW).

        A frame the 510 does not acknowledge in time no longer aborts the
        rest (that left strips with some channels changed and others not):
        every frame is sent, unacknowledged ones are sent again (`retries`
        times). Raises Dali510Timeout only if no frame was acknowledged.
        """
        items = list(items)
        out: list[Reply] = [Reply(0) for _ in items]
        todo = list(range(len(items)))
        with self.transaction() as tx:
            for _attempt in range(1 + max(0, retries)):
                failed = []
                for i in todo:
                    addr_byte, level = items[i]
                    try:
                        if level <= 0:
                            r = tx.frame(addr_byte | 1, 0x00, False)  # OFF
                        else:
                            r = tx.frame(addr_byte & 0xFE, min(254, level), False)
                    except Dali510Timeout:
                        _LOGGER.debug("no ack for frame %02x %02x", addr_byte, level)
                        failed.append(i)
                        continue
                    out[i] = r
                if not failed:
                    break
                todo = failed
        if items and all(r.status == 0 for r in out):
            raise Dali510Timeout("510 acknowledged none of the frames")
        return out

    # ---------------- group membership (explicit, narrow) ----------------
    def query_groups(self, sa: int) -> list[int] | None:
        """QUERY GROUPS 0-7 (0xC0) + 8-15 (0xC1). None if the gear is silent."""
        a = short_cmd_addr(sa)
        with self.transaction() as tx:
            lo = tx.query_value(a, CMD_QUERY_GROUPS_0_7)
            hi = tx.query_value(a, CMD_QUERY_GROUPS_8_15)
        return decode_groups(lo, hi)

    def set_group_membership(self, sa: int, group: int, add: bool,
                             method: str = "auto") -> dict:
        """ADD TO GROUP (0x60+g) / REMOVE FROM GROUP (0x70+g) to short address sa.

        DALI configuration commands must arrive twice within 100 ms. Default
        uses the 510's own "send twice" ctl bit (0x80); if QUERY GROUPS shows no change, it retries
        once with two consecutive frames in one transaction (~25 ms apart).
        method: "auto" | "twice_bit" | "two_frames".
        """
        if not (isinstance(sa, int) and 0 <= sa <= 63):
            raise ValueError("short address must be 0-63")
        if not (isinstance(group, int) and 0 <= group <= 15):
            raise ValueError("group must be 0-15")
        if method not in ("auto", "twice_bit", "two_frames"):
            raise ValueError("method must be auto, twice_bit or two_frames")
        addr = short_cmd_addr(sa)
        data = (CMD_ADD_TO_GROUP if add else CMD_REMOVE_FROM_GROUP) + group
        before = self.query_groups(sa)
        if before is None:
            raise Dali510Error(f"short address {sa} does not answer QUERY GROUPS")

        def done(groups: list[int] | None) -> bool:
            return groups is not None and ((group in groups) == add)

        result = {
            "short_address": sa,
            "group": group,
            "action": "add" if add else "remove",
            "frame": f"{addr:02x} {data:02x}",
            "groups_before": before,
            "groups": before,
            "verified": done(before),
            "method": "already" if done(before) else None,
        }
        if done(before):
            return result
        methods = ["twice_bit", "two_frames"] if method == "auto" else [method]
        for m in methods:
            with self.transaction() as tx:
                if m == "twice_bit":
                    tx.frame(addr, data, False, _membership=True, twice=True)
                else:
                    tx.frame(addr, data, False, _membership=True)
                    tx.frame(addr, data, False, _membership=True)
            after = self.query_groups(sa)
            result.update(groups=after, method=m, verified=done(after))
            if result["verified"]:
                break
        return result

    # ---------------- read-only scan ----------------
    def scan(self, max_sa: int = 63, progress: Callable[[int], None] | None = None
             ) -> list[dict]:
        """Read-only bus scan. Only QUERY frames (+ENABLE DT 8 before a DT8 query)."""
        gears: list[dict] = []
        for sa in range(0, max_sa + 1):
            if progress:
                progress(sa)
            a = short_cmd_addr(sa)
            with self.transaction() as tx:
                r = tx.frame(a, 0x91, True)
            if r.status not in (ST_REPLY, ST_MULTI):
                continue
            gear = GearInfo(sa=sa, collision=(r.status == ST_MULTI))
            with self.transaction() as tx:
                dt = tx.query_value(a, 0x99)
                gear.device_type = dt
                types: list[int] = []
                if dt is not None and dt != 0xFF:
                    types = [dt]
                elif dt == 0xFF:
                    # DALI-2: multiple device types -> QUERY NEXT DEVICE TYPE (0xA7)
                    for _ in range(8):
                        nxt = tx.query_value(a, 0xA7)
                        if nxt is None or nxt in (0xFE, 0xFF):
                            break
                        types.append(nxt)
                gear.device_types = types
            with self.transaction() as tx:
                gear.version = tx.query_value(a, 0x97)
                g_lo = tx.query_value(a, 0xC0)
                g_hi = tx.query_value(a, 0xC1)
                if g_lo is not None and g_hi is not None:
                    mask = g_lo | (g_hi << 8)
                    gear.groups = [g for g in range(16) if mask >> g & 1]
                gear.actual = tx.query_value(a, 0xA0)
                gear.max_level = tx.query_value(a, 0xA1)
                gear.min_level = tx.query_value(a, 0xA2)
                gear.phys_min = tx.query_value(a, 0x9A)
            with self.transaction() as tx:
                rh = tx.query_value(a, 0xC2)
                rm = tx.query_value(a, 0xC3)
                rl = tx.query_value(a, 0xC4)
                if None not in (rh, rm, rl):
                    gear.random_address = (rh << 16) | (rm << 8) | rl
            if 8 in types:
                gear.dt8 = self.query_dt8(sa)
            gears.append(asdict(gear))
        return gears

    def query_dt8(self, sa: int) -> dict:
        """DT8 read-only: ENABLE DEVICE TYPE 8 + QUERY COLOUR TYPE FEATURES/STATUS.

        ENABLE DEVICE TYPE only qualifies the *next* frame; nothing is stored.
        """
        a = short_cmd_addr(sa)
        res: dict = {}
        for cmd, key in ((0xF9, "colour_type_features"), (0xF8, "colour_status"),
                         (0xF7, "gear_features_status")):
            with self.transaction() as tx:
                tx.frame(0xC1, 0x08, False, _internal_ok=True)
                r = tx.frame(a, cmd, True, _internal_ok=True)
                res[key] = r.value if r.status == ST_REPLY else None
        f = res.get("colour_type_features")
        if f is not None:
            res["xy_capable"] = bool(f & 0x01)
            res["tc_capable"] = bool(f & 0x02)
            res["primary_n"] = (f >> 2) & 0x07
            res["rgbwaf_channels"] = (f >> 5) & 0x07
        return res


class _Tx:
    """Transaction: first frame carries ctl|0x02, closes with 01 8C."""

    def __init__(self, bus: Dali510) -> None:
        self.bus = bus

    def __enter__(self) -> "_Tx":
        self.bus._lock.acquire()
        if self.bus.fd is None:
            self.bus._lock.release()
            raise Dali510Error("device not open")
        self.bus._first = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            # Always try to close the transaction unless the device is gone.
            if self.bus.fd is not None:
                try:
                    self.bus.sync()
                except Dali510Error:
                    pass
        finally:
            self.bus._lock.release()

    def frame(self, addr: int, data: int, expect_reply: bool = False,
              *, _internal_ok: bool = False, _membership: bool = False,
              twice: bool = False) -> Reply:
        return self.bus._frame(addr, data, expect_reply, _internal_ok=_internal_ok,
                               _membership=_membership, twice=twice)

    def query_value(self, addr: int, cmd: int) -> int | None:
        r = self.frame(addr, cmd, True)
        return r.value if r.status == ST_REPLY else None


@dataclass
class GearInfo:
    sa: int
    device_type: int | None = None
    device_types: list[int] = field(default_factory=list)
    version: int | None = None
    groups: list[int] = field(default_factory=list)
    actual: int | None = None
    min_level: int | None = None
    max_level: int | None = None
    phys_min: int | None = None
    random_address: int | None = None
    collision: bool = False
    dt8: dict | None = None


def suggest_multichannel(gears: list[dict]) -> list[list[int]]:
    """Consecutive short addresses with the same type whose random addresses are
    consecutive (same H/M byte) -> likely one multi-channel (RGB/RGBW) driver."""
    by_sa = sorted(gears, key=lambda g: g["sa"])
    out: list[list[int]] = []
    cur: list[dict] = []
    for g in by_sa:
        if (cur and g["sa"] == cur[-1]["sa"] + 1
                and g.get("device_type") == cur[-1].get("device_type")
                and g.get("random_address") is not None
                and cur[-1].get("random_address") is not None
                and g["random_address"] == cur[-1]["random_address"] + 1):
            cur.append(g)
        else:
            if len(cur) >= 3:
                out.append([c["sa"] for c in cur])
            cur = [g]
    if len(cur) >= 3:
        out.append([c["sa"] for c in cur])
    return out


# --------------------------------------------------------------------------
# Colour / brightness mapping (HA linear <-> DALI log curve)
# --------------------------------------------------------------------------

def channel_linear_to_arc(linear_0_255: float, brightness_0_255: int = 255) -> int:
    """Map one HA channel (0-255) * master brightness -> DALI arc level."""
    if linear_0_255 <= 0 or brightness_0_255 <= 0:
        return 0
    scaled = (linear_0_255 / 255.0) * (brightness_0_255 / 255.0) * 100.0
    return percent_to_arc(scaled)


def arc_to_channel_linear(level: int | None) -> int:
    """Inverse: DALI arc -> approximate HA channel 0-255 (at full brightness)."""
    if level is None or level <= 0 or level == 0xFF:
        return 0
    return max(0, min(255, int(round(arc_to_percent(level) * 255.0 / 100.0))))


def rgb_brightness_to_arcs(
    rgb: tuple[int, int, int],
    brightness: int,
    *,
    white: int | None = None,
) -> list[int]:
    """Return [R,G,B] or [R,G,B,W] arc levels."""
    arcs = [channel_linear_to_arc(c, brightness) for c in rgb]
    if white is not None:
        arcs.append(channel_linear_to_arc(white, brightness))
    return arcs


def arcs_to_rgbw(levels: list[int | None]) -> tuple[tuple[int, ...], int]:
    """Reconstruct (rgb or rgbw tuple, brightness) from per-channel arc levels.

    Brightness = max channel linear; colour components are normalised to that.
    """
    linears = [arc_to_channel_linear(lv) for lv in levels]
    bri = max(linears) if linears else 0
    if bri <= 0:
        zeros = tuple(0 for _ in levels)
        return zeros, 0
    colour = tuple(int(round(v * 255.0 / bri)) for v in linears)
    return colour, bri


def arcs_to_colour(levels: list[int | None]) -> tuple[tuple[int, ...], int]:
    """Like arcs_to_rgbw but normalises in float space (accurate when dim).

    Returns (colour 0-255 per channel normalised to the brightest channel,
    HA brightness 1-255) or (zeros, 0) when everything is off.
    """
    pcts = [
        arc_to_percent(lv) if (lv is not None and 0 < lv != 0xFF) else 0.0
        for lv in levels
    ]
    top = max(pcts) if pcts else 0.0
    if top <= 0:
        return tuple(0 for _ in levels), 0
    colour = tuple(int(round(p * 255.0 / top)) for p in pcts)
    bri = max(1, min(255, int(round(top * 255.0 / 100.0))))
    return colour, bri


def default_strip_colour(mode: str) -> tuple[int, ...]:
    """Colour used when a strip's last colour is unknown.

    RGBW -> only the W channel; RGB -> white (R=G=B).
    """
    return (0, 0, 0, 255) if mode == "rgbw" else (255, 255, 255)
