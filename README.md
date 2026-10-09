# DALI USB (Helvar 510) – unofficial Home Assistant integration

> This is an independent, unofficial project. It is not affiliated with, endorsed by, or supported by Helvar. "Helvar" and "DIGIDIM" are trademarks of their respective owners and are used only to identify compatible hardware. No Helvar software, firmware or documentation is included. The USB protocol was determined by observing the device's USB communication for the purpose of interoperability. Use at your own risk: commands that modify DALI gear (e.g. group membership) change your installation; the authors accept no liability.

A Home Assistant custom integration (domain `helvar510`) that controls DALI
lighting through a **Helvar DIGIDIM 510 USB-DALI interface** (USB ID
`16eb:0510`) plugged directly into the Home Assistant host. It talks to
`/dev/hidraw*` in pure Python: no hidapi, libusb or other extra libraries.

## Features

- **One light per DALI short address** (`DALI 5`): brightness via DAPC
  (direct arc power), off, on (RECALL MAX).
- **RGB / RGBW strips as one light.** Many multi-colour LED drivers appear on
  the bus as 3–4 separate DT6 short addresses (one per R, G, B, W channel).
  Assemble them into a single `rgb` / `rgbw` light; colour and brightness are
  converted to per-channel DAPC levels on the DALI logarithmic curve and sent in
  one transaction. Likely strips (consecutive short addresses of the same type
  with consecutive random addresses) are suggested automatically – you confirm
  and choose the channel order.
- **DALI groups as lights** (`DALI Group 0` … `DALI Group 15`, DALI numbering
  0–15) for every non-empty group, controlled with a single group command.
  Groups that contain strip channels are dimmed with per-address commands so
  strip colours are kept.
- **`DALI All`** – broadcast light for the whole bus.
- **Last-colour restore:** a strip that is off comes back with its last colour
  when it is switched on through a group or `DALI All`. The colour is
  remembered across restarts and also learned when it was changed elsewhere
  (e.g. another controller or a wall panel).
- **Devices only where useful:** non-empty groups, assembled strips, `DALI All`
  and lights that are in no group and no strip each get their own device, so
  you can assign areas per light. Lights that are members of a group stay on
  the 510 hub device.
- **Hiding:** strip channel lights and group member lights are enabled but
  hidden by default (they still work in automations); both are options.
- **Edit strips** later (name, RGB/RGBW, channel roles) without losing the
  entity, its name, aliases, area or device.
- **Group membership services** `add_to_group` / `remove_from_group`
  (admin only, one short address at a time, verified with QUERY GROUPS).
  Layout changes are applied immediately without a rescan or reload.
- **`send_raw`** service for advanced use, with a safety filter.
- **Polling** of QUERY ACTUAL LEVEL (default every 30 s) plus a quick
  re-check after each command. The state follows the commanded level
  immediately; a readback taken while the gear is still fading (DALI fade
  time) does not pull it back, so `brightness_step` / dimmer remotes step
  smoothly.
- **Verified writes:** after every per-address write (single lights, strip
  on/off/colour/brightness, group and `DALI All` commands) the levels are
  read back once the gear has settled; an address that did not take its
  level (lost frame) is written again, at most twice, then a warning is
  logged. Writes go through one serialised queue (latest level per address
  wins) and readbacks wait while commands are flowing.
- **Diagnostics** download with the scan result and the last bus frames
  (USB serial number redacted).

## Safety

- The bus scan uses **queries only** (QUERY CONTROL GEAR PRESENT, DEVICE TYPE,
  VERSION, GROUPS, levels, RANDOM ADDRESS H/M/L, DT8 colour feature queries).
  Nothing is written during setup or rescans.
- Normal operation only sends DAPC, OFF and RECALL MAX.
- The driver **refuses** to send commands that change gear configuration, on
  every code path including `send_raw`: INITIALISE, RANDOMISE, PROGRAM SHORT
  ADDRESS and all other special commands (except TERMINATE), DTR writes,
  STORE SCENE, all SET… commands, RESET, ADD/REMOVE GROUP and
  application-extended commands (DT8 colour writes etc.).
- **The only exception** is group membership (ADD TO GROUP / REMOVE FROM
  GROUP), which is only possible through the explicit `add_to_group` /
  `remove_from_group` services and only to **one short address** – never
  broadcast or group addressed.
- `send_raw`, `add_to_group` and `remove_from_group` are **admin-only**
  services and validate their input strictly (integers in range; floats,
  booleans and other types are refused).
- The configured device path must be `/dev/hidrawN`; the opened file must be a
  character device that reports USB ID `16eb:0510`.

## Requirements

- Home Assistant OS or Home Assistant Core **2026.2 or newer** on Linux, with
  the 510 visible as `/dev/hidraw*` (HA OS: works out of the box; HA Container:
  pass the device, e.g. `--device /dev/hidraw0`).
- A Helvar DIGIDIM 510 USB-DALI interface.
- A **powered DALI bus** (separate DALI bus power supply – the 510 does not
  power the bus).
- **Only one DALI master** controlling the bus. Disconnect or disable any other
  controller while using this integration, otherwise commands collide.

## Installation

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/tsilen/ha-dali-helvar510`, category **Integration**.
2. Install **DALI USB (Helvar 510) – unofficial** and restart Home Assistant.

### Manual

1. Copy `custom_components/helvar510/` to
   `/config/custom_components/helvar510/` (Samba share, Studio Code Server,
   File editor …).
2. Restart Home Assistant.

When upgrading manually, replace the whole folder (or also delete its
`__pycache__`). Existing installations are upgraded in place: the domain,
entity unique IDs, entity IDs, names, aliases and areas are kept. Do not
remove and re-add the integration.

## Configuration

1. Plug the 510 into the Home Assistant host. Under **Settings → System →
   Hardware → All hardware** you should see a `hidraw` device /
   `16eb:0510`.
2. **Settings → Devices & services → Add integration → DALI USB (Helvar 510)**.
3. Pick the USB device (auto-detected), the poll interval and the hiding
   options. The query-only scan of short addresses 0–63 takes 20–40 s.
4. Assemble RGB/RGBW strips from the suggestions (choose the address for each
   of R, G, B and W – the channel order cannot be detected, try and correct it
   later), or skip.

**Configure** (options) later lets you change the poll interval and hiding
options, add / edit / delete strips, and rescan the bus (queries only).

## Services

All three services are admin-only and return a response
(enable "Return response" in Developer tools → Actions).

### `helvar510.add_to_group` / `helvar510.remove_from_group`

```yaml
action: helvar510.add_to_group
data:
  short_address: 5   # 0-63, "DALI 5"
  group: 3           # DALI group 0-15
```

- Sends ADD TO GROUP (`0x60+group`) / REMOVE FROM GROUP (`0x70+group`) to the
  command address `2×short_address+1`.
- DALI requires configuration commands to be received twice within 100 ms.
  `method: auto` (default) uses the 510's send-twice flag and falls back to two
  consecutive frames if the result could not be verified; force one with
  `method: twice_bit` or `method: two_frames`.
- QUERY GROUPS is read before and after; nothing is sent if the gear is
  already in the requested state, and an error is raised if the result does
  not match. Example response:
  `{groups_before: [], groups: [3], verified: true, method: twice_bit}`.
- Every write is logged at WARNING level.
- Group lights, devices and visibility are updated immediately.
- Numbering: tools that number groups 1–16 call DALI group 3 "Group 4".

### `helvar510.send_raw`

Sends one 16-bit DALI forward frame (`addr`, `data`) and optionally waits for a
backward frame:

```yaml
action: helvar510.send_raw
data:
  addr: 0x0B        # short address 5, command
  data: 0xA0        # QUERY ACTUAL LEVEL
  expect_reply: true
```

More examples: `addr: 255, data: 0` = broadcast OFF;
`addr: 0x81, data: 5` = group 0 RECALL MAX; `addr: 0x0A, data: 128` = DAPC
level 128 to short address 5. Frames that could change configuration are
refused (see [Safety](#safety)).

## Troubleshooting

| Symptom | Check |
|---|---|
| The 510 is not found | Hardware page (`16eb:0510`), another USB port, reboot the host, select the device path manually |
| Wrong hidraw device | Pick the path manually during setup (`tools/dali510_test.py list` shows which one is the 510) |
| Permission denied on `/dev/hidraw*` | HA Container: pass the device to the container |
| Scan finds no gear | Is the DALI bus powered? Is the 510's DALI connector wired to the bus? Is another master active? |
| Status `MULTI_REPLY` | Two gear share a short address, or another master is talking |
| Strip shows wrong colours | Configure → Edit strip and swap the R/G/B/W addresses |
| Entity "no longer provided" | Left over from an earlier layout; delete it in the entity settings |

### Debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.helvar510: debug
```

Every outgoing and incoming report is then logged in hex
(e.g. `TX 03 52 00 fc` / `RX 64`). The last frames are also included in the
integration's **Download diagnostics**.

## Command-line tool

`tools/dali510_test.py` uses the same driver to test the interface outside
Home Assistant (stop the integration first so only one program talks to the
510). On HA OS run it where `/dev/hidraw*` is visible, e.g. inside the Core
container (`docker exec -it homeassistant python3 /config/tools/dali510_test.py list`).

```bash
python3 tools/dali510_test.py list        # hidraw devices, marks 16eb:0510
python3 tools/dali510_test.py query 0     # QUERY ACTUAL LEVEL of short address 0
python3 tools/dali510_test.py scan        # query-only scan + strip suggestions
python3 tools/dali510_test.py groups 5    # QUERY GROUPS of short address 5
python3 tools/dali510_test.py dapc 0 200  # DAPC level 200 to short address 0
python3 tools/dali510_test.py off         # broadcast OFF (asks for confirmation)
python3 tools/dali510_test.py on          # broadcast RECALL MAX
```

## Protocol notes

The following was determined by observing the device's USB communication; it
is a description in our own words, not vendor documentation.

- The 510 is a USB HID device with 36-byte input and output reports (no
  feature reports). On Linux an output report is written to `/dev/hidrawN`
  with a leading report ID `0`.
- **Output report:** `[len][ctl][addr][data]` followed by zero padding.
  For a DALI forward frame `len = 3`, `addr` and `data` are the two bytes of
  the 16-bit DALI frame.
- **`ctl`** byte: base `0x50`; `|0x02` marks the first frame of a
  transaction, `|0x04` asks the interface to wait for a backward frame,
  `|0x80` asks it to send the frame twice (for DALI configuration commands).
- **Interface commands:** `02 82 04` requests interface information (answer
  `03 82 09 00`); `01 8C` ends a transaction (echoed back).
- **Input report:** `[len][status][value…]`. Status `0x64` = frame sent,
  `0x6B` = no reply, `0x6C` = several gear answered (collision),
  `0x6D` followed by the 8-bit backward frame = reply.
- DALI addressing: short address *n* → `2n` (DAPC) / `2n+1` (command);
  group *g* → `0x80+2g` / `0x81+2g`; broadcast `0xFE` / `0xFF`.

## Development

Requires Python 3.14.2 or newer (same as Home Assistant 2026.10).

```bash
pip install --require-hashes -r requirements_test.txt
pytest
```

`requirements_test.txt` is a fully pinned, hashed lock file. To change the
test dependencies, edit `requirements_test.in` and regenerate the lock file:

```bash
uv pip compile requirements_test.in --universal --python-version 3.14.2 \
  --generate-hashes -o requirements_test.txt
```

The tests run the real driver against a simulated 510 and DALI bus with
synthetic gear (`tests/sim.py`).

## Changelog

### 0.3.2

- Fix `DALI All` dimming only once with `brightness_step` (Hue dimmer held):
  a single gear not ending at the commanded level (clamped higher than its
  reported minimum, a lost frame, gear that does not follow) made `DALI All`
  report the brightest member again, so every step was computed from the
  same value. The level a gear settles at in answer to a command is now
  attributed to that command.
- Fix strips changing colour when a channel frame was lost: a frame the 510
  did not acknowledge aborted the remaining channels, and the half-applied
  colour was then learnt. All channels are now sent (unacknowledged frames
  retried), every write is verified and mismatching channels rewritten (at
  most 2 times, then a warning), and a channel that did not take its level
  is never learnt as a new colour.
- Fewer frames per step: `DALI All` uses one group DAPC for groups without
  strip channels whose members get the same level, and channels that are
  already confirmed at the target level are not sent again (on a 39-gear
  bus with 4 RGBW strips: 39 -> 18 frames per step for `DALI All`, 16 -> 8
  for the 4 strips).
- Readbacks and polls wait while commands are flowing.

### 0.3.1

- Fix `light.turn_on` with `brightness_step` / `brightness_step_pct` (e.g. a
  Hue dimmer automation): dimming up barely raised the brightness and the
  value jumped back and forth.
  - Strips whose colour was not at full value (e.g. RGBW `(0, 0, 0, 128)`)
    applied it twice, so the reported brightness was lower than the one set
    and every step up ended darker. Colours are now kept normalised and the
    reported brightness is what is lit (output unchanged).
  - A poll or readback that started before a command wrote its old snapshot
    back over the new state; readbacks now only update the addresses they
    read and drop values superseded by a newer command.
  - A readback while the gear is fading no longer replaces the commanded
    level with the intermediate one (QUERY STATUS "fade running" is checked;
    queries only).
  - The brightness that was set is reported exactly (HA 0–255 and DALI arc
    levels do not map 1:1).
  - Readbacks after rapid repeated commands are coalesced.

### 0.3.0

- Initial public release.

## License

MIT – see [LICENSE](LICENSE).
