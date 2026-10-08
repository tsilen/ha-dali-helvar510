"""Constants for the Helvar DIGIDIM 510 integration."""
from __future__ import annotations

DOMAIN = "helvar510"

VID = 0x16EB
PID = 0x0510
REPORT_LEN = 36

IF_GET_INFO = bytes([0x02, 0x82, 0x04])
IF_SYNC = bytes([0x01, 0x8C])

CTL_BASE = 0x50
CTL_FIRST = 0x02
CTL_REPLY = 0x04
CTL_TWICE = 0x80

ST_OK = 0x64
ST_NO_REPLY = 0x6B
ST_MULTI = 0x6C
ST_REPLY = 0x6D

CMD_OFF = 0x00
CMD_RECALL_MAX = 0x05
CMD_RECALL_MIN = 0x06
CMD_QUERY_STATUS = 0x90
CMD_QUERY_PRESENT = 0x91
CMD_QUERY_DEVICE_TYPE = 0x99
CMD_QUERY_NEXT_DEVICE_TYPE = 0xA7
CMD_QUERY_ACTUAL = 0xA0
CMD_QUERY_MAX = 0xA1
CMD_QUERY_MIN = 0xA2
CMD_QUERY_GROUPS_0_7 = 0xC0
CMD_QUERY_GROUPS_8_15 = 0xC1

DT_NAMES = {
    0: "DT0 fluorescent",
    1: "DT1 emergency",
    2: "DT2 HID",
    3: "DT3 LV halogen",
    4: "DT4 incandescent",
    5: "DT5 0-10V",
    6: "DT6 LED",
    7: "DT7 switching",
    8: "DT8 colour",
    9: "DT9 sequencer",
    0xFF: "none",
}

CONF_PATH = "path"
CONF_POLL_INTERVAL = "poll_interval"
CONF_RGBW_GROUPS = "rgbw_groups"  # legacy key kept for options
CONF_STRIPS = "strips"
CONF_HIDE_STRIP_CHANNELS = "hide_strip_channels"
CONF_HIDE_GROUP_MEMBERS = "hide_group_members"
DEFAULT_POLL_INTERVAL = 30
DEFAULT_HIDE_STRIP_CHANNELS = True
DEFAULT_HIDE_GROUP_MEMBERS = True

# One strip: {"name": "...", "channels": {"r": sa, "g": sa, "b": sa, "w": sa|null}}
CHANNEL_ROLES = ("r", "g", "b", "w")

ATTR_ADDR = "addr"
ATTR_DATA = "data"
ATTR_EXPECT_REPLY = "expect_reply"
SERVICE_SEND_RAW = "send_raw"

MANUFACTURER = "Helvar"
MODEL = "DIGIDIM 510"

# --- 0.2.0: per-light devices -------------------------------------------
# Config entry minor version 2 adds CONF_NAME_PREFIX (persisted once during
# migration so visible names never change afterwards).
CONF_NAME_PREFIX = "name_prefix"
DEFAULT_NAME_PREFIX = "DALI"  # 0.1.0 shared device was called "DALI"
HUB_NAME = "Helvar 510"  # 0.2.0 hub name; 0.2.1 names the hub with the prefix ("DALI")
ENTRY_VERSION = 1
ENTRY_MINOR_VERSION = 2

SERVICE_ADD_TO_GROUP = "add_to_group"
SERVICE_REMOVE_FROM_GROUP = "remove_from_group"
ATTR_SHORT_ADDRESS = "short_address"
ATTR_GROUP = "group"
ATTR_METHOD = "method"
