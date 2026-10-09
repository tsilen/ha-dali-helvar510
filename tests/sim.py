"""Simulated 510 USB-DALI interface + DALI bus for tests.

The bus content is synthetic (made-up short addresses, random addresses and
group memberships). The simulator speaks the 510 report format over a
socketpair, so the real driver code (dali510.Dali510) runs unmodified with an
injected opener.
"""
from __future__ import annotations

import select
import socket
import threading

# Synthetic installation:
#   0-3  DT6, consecutive random addresses -> one RGBW multi-channel driver
#   4-6  DT6 in group 0
#   7-8  DT6 in group 1
#   9    DT6 in groups 0 and 2
#   10   DT4, no group
#   11   DT8, no group
#   12   DT6, no group
SYNTHETIC_GEAR: dict[int, dict] = {
    0: {"dt": 6, "rand": 0x4A1000, "groups": []},
    1: {"dt": 6, "rand": 0x4A1001, "groups": []},
    2: {"dt": 6, "rand": 0x4A1002, "groups": []},
    3: {"dt": 6, "rand": 0x4A1003, "groups": []},
    4: {"dt": 6, "rand": 0x10203F, "groups": [0]},
    5: {"dt": 6, "rand": 0x7700A1, "groups": [0]},
    6: {"dt": 6, "rand": 0x0B0C0D, "groups": [0]},
    7: {"dt": 6, "rand": 0x5F0011, "groups": [1]},
    8: {"dt": 6, "rand": 0x220099, "groups": [1]},
    9: {"dt": 6, "rand": 0x31AA05, "groups": [0, 2]},
    10: {"dt": 4, "rand": 0x6E0102, "groups": []},
    11: {"dt": 8, "rand": 0x0F0F0F, "groups": []},
    12: {"dt": 6, "rand": 0x123456, "groups": []},
}

STATUS_OK = bytes([1, 0x64])
NO_REPLY = bytes([1, 0x6B])


def reply(value: int) -> bytes:
    return bytes([2, 0x6D, value & 0xFF])


class SimBus:
    """Answers 510 OUT reports like the device + a DALI bus would."""

    def __init__(self, gear: dict[int, dict] | None = None) -> None:
        self.gear = {sa: dict(v) for sa, v in (gear or SYNTHETIC_GEAR).items()}
        self.a, self.b = socket.socketpair()
        self.log: list[bytes] = []            # every live OUT payload
        self.violations: list[str] = []       # frames a correct driver never sends
        self.levels = {sa: 0 for sa in self.gear}
        self.groups = {sa: sum(1 << g for g in v["groups"]) for sa, v in self.gear.items()}
        self.twice_bit_works = True
        self.silent = False                   # stop answering (timeout tests)
        self.config_writes: list[tuple[int, int, int]] = []
        self._last_cfg: bytes | None = None
        self._dt_enabled: int | None = None
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    # ------------------------------------------------------------ DALI model
    def _targets(self, addr: int) -> list[int]:
        if addr < 0x80:
            sa = addr >> 1
            return [sa] if sa in self.gear else []
        if addr in (0xFE, 0xFF):
            return list(self.gear)
        if 0x80 <= addr < 0xA0:
            g = (addr >> 1) & 0x0F
            return [sa for sa in self.gear if self.groups[sa] >> g & 1]
        return []

    def answer(self, live: bytes) -> bytes:
        if live[:3] == bytes([0x02, 0x82, 0x04]):
            return bytes([3, 0x82, 0x09, 0x00])
        if live[:2] == bytes([0x01, 0x8C]):
            return bytes([1, 0x8C])
        if live[0] != 3:
            self.violations.append(f"unknown report {live.hex()}")
            return NO_REPLY
        ctl, addr, data = live[1], live[2], live[3]
        dt_enabled, self._dt_enabled = self._dt_enabled, None
        if 0xA0 <= addr <= 0xFB:  # special commands
            if addr == 0xC1:      # ENABLE DEVICE TYPE (only before a DT8 query)
                self._dt_enabled = data
                return STATUS_OK
            if addr != 0xA1:
                self.violations.append(f"special command {live.hex()}")
            return STATUS_OK
        is_cmd = addr & 1
        if is_cmd and 0x20 <= data <= 0x8F:
            self.config_writes.append((ctl, addr, data))
            if addr >= 0x80 or not 0x60 <= data <= 0x7F:
                self.violations.append(f"config command {live.hex()}")
            twice = (ctl & 0x80 and self.twice_bit_works) or self._last_cfg == bytes([addr, data])
            self._last_cfg = None if twice else bytes([addr, data])
            sa = addr >> 1
            if twice and 0x60 <= data <= 0x7F and sa in self.groups:
                g = data & 0x0F
                if data < 0x70:
                    self.groups[sa] |= 1 << g
                else:
                    self.groups[sa] &= ~(1 << g)
            return STATUS_OK
        self._last_cfg = None
        if is_cmd and data >= 0xE0 and dt_enabled is None:
            self.violations.append(f"application extended command without ENABLE DT {live.hex()}")
        if not ctl & 0x04:  # no reply expected: DAPC / arc command
            targets = self._targets(addr)
            if not is_cmd:
                for sa in targets:
                    self.levels[sa] = 0 if data == 0 else data
            elif data == 0x00:
                for sa in targets:
                    self.levels[sa] = 0
            elif data == 0x05:
                for sa in targets:
                    self.levels[sa] = 254
            return STATUS_OK
        sa = addr >> 1
        if addr >= 0x80 or sa not in self.gear:
            return NO_REPLY
        g = self.gear[sa]
        if dt_enabled == 8 and g["dt"] == 8 and data in (0xF7, 0xF8, 0xF9):
            return reply({0xF9: 0x02, 0xF8: 0x00, 0xF7: 0x00}[data])
        table = {
            0x90: 0x04,               # QUERY STATUS
            0x91: 0xFF,               # QUERY CONTROL GEAR PRESENT
            0x97: 0x08,               # QUERY VERSION NUMBER
            0x99: g["dt"],            # QUERY DEVICE TYPE
            0x9A: 1,                  # QUERY PHYSICAL MINIMUM
            0xA0: self.levels[sa],    # QUERY ACTUAL LEVEL
            0xA1: 254,                # QUERY MAX LEVEL
            0xA2: 1,                  # QUERY MIN LEVEL
            0xC0: self.groups[sa] & 0xFF,
            0xC1: self.groups[sa] >> 8,
            0xC2: g["rand"] >> 16,
            0xC3: (g["rand"] >> 8) & 0xFF,
            0xC4: g["rand"] & 0xFF,
        }
        if data in table:
            return reply(table[data])
        return NO_REPLY

    # ------------------------------------------------------------ transport
    def _serve(self) -> None:
        buf = b""
        while True:
            try:
                r, _, _ = select.select([self.b], [], [], 1)
                if not r:
                    continue
                chunk = self.b.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while len(buf) >= 37:  # report id 0 + 36 byte report
                rep, buf = buf[1:37], buf[37:]
                live = rep[: 1 + rep[0]]
                self.log.append(live)
                if self.silent:
                    continue
                ans = self.answer(live)
                try:
                    self.b.sendall(ans + bytes(36 - len(ans)))
                except OSError:
                    return

    def opener(self, _path: str) -> int:
        import os

        return os.dup(self.a.fileno())

    def unplug(self) -> None:
        """Simulate pulling the USB plug: the device side goes away."""
        try:
            self.b.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.b.close()

    def close(self) -> None:
        for s in (self.a, self.b):
            try:
                s.close()
            except OSError:
                pass
