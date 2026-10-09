"""Light entities for Helvar DIGIDIM 510.

0.2.1: strips, non-empty groups, DALI All and lights that are in no group and
no strip have their own device (via the 510 hub) so an area can be set per
light; group member lights live on the hub. unique_ids are exactly the 0.1.0 ones (see devices.py).
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CMD_OFF,
    CMD_RECALL_MAX,
    CONF_HIDE_STRIP_CHANNELS,
    DEFAULT_HIDE_STRIP_CHANNELS,
    DT_NAMES,
)
from .coordinator import Helvar510Coordinator
from .dali510 import (
    BROADCAST_CMD,
    BROADCAST_DAPC,
    arc_to_brightness,
    arcs_to_colour,
    brightness_to_arc,
    default_strip_colour,
    group_cmd_addr,
    group_dapc_addr,
    rgb_brightness_to_arcs,
    short_cmd_addr,
)
from .devices import (
    BROADCAST_UNIQUE_ID,
    ChildDevice,
    all_key,
    child_device_info,
    expected_children,
    group_key,
    group_unique_id,
    hub_identifier,
    sa_unique_id,
    strip_dev_key,
    strip_unique_id,
)

# Colour difference (0-255 scale, per component) above which a colour seen on
# the bus is treated as a real change (e.g. another controller or a wall panel) and learnt.
LEARN_TOLERANCE = 12

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: Helvar510Coordinator = entry.runtime_data
    coord.async_add_entities = async_add_entities
    coord.live_entities = {}
    coord.apply_layout = lambda: async_apply_layout(hass, entry)
    entities = _build_entities(entry, coord)
    for ent in entities:
        coord.live_entities[ent.unique_id] = ent
    async_add_entities(entities)


def _build_entities(entry: ConfigEntry, coord: Helvar510Coordinator) -> list[LightEntity]:
    """Entity objects for the current layout (each carries a layout signature)."""
    from .const import CONF_HIDE_GROUP_MEMBERS, DEFAULT_HIDE_GROUP_MEMBERS
    from .devices import sa_entity_name, sa_should_hide, sa_target_key

    strips: list[dict] = list(coord.strips)
    gears: list[dict] = list(coord.gears)
    hide_strips = entry.options.get(CONF_HIDE_STRIP_CHANNELS, DEFAULT_HIDE_STRIP_CHANNELS)
    hide_groups = entry.options.get(CONF_HIDE_GROUP_MEMBERS, DEFAULT_HIDE_GROUP_MEMBERS)
    children: dict[str, ChildDevice] = {
        c.key: c for c in expected_children(entry, gears, strips, coord.name_prefix)
    }

    def dev(key: str | None) -> DeviceInfo:
        if key is None:
            return DeviceInfo(identifiers={hub_identifier(entry)})
        return child_device_info(entry, children[key], coord.hub_device_id)

    strip_of_sa: dict[int, dict] = {}
    for st in strips:
        for sa in st["channels"].values():
            strip_of_sa[sa] = st
    strip_channels = set(strip_of_sa)

    entities: list[LightEntity] = []

    # Per-short-address lights
    for gear in gears:
        sa = gear["sa"]
        key = sa_target_key(entry, sa, gears, strips)
        name = sa_entity_name(sa, gears, strips)
        if sa in strip_channels:
            # Channel of an assembled strip -> lives on the strip's device.
            ent: DaliShortLight = DaliStripChannelLight(
                coord, gear, dev(key), strip_of_sa[sa]
            )
        else:
            # Own device (name None -> "DALI 19") or, as a group member, on the
            # hub (name "5" -> "DALI 5").
            ent = DaliShortLight(coord, gear, dev(key), name)
        if sa_should_hide(sa, strips, gears, hide_strips, hide_groups):
            # Keep ENABLED; only hide from the UI by default.
            ent._attr_entity_registry_visible_default = False
        ent.layout_sig = (
            type(ent).__name__, key, name, tuple(gear.get("groups") or ()),
        )
        entities.append(ent)

    # Assembled RGB/RGBW strips
    for strip in strips:
        ent = DaliStripLight(coord, strip, dev(strip_dev_key(entry, strip)))
        ent.layout_sig = (
            "strip", strip_dev_key(entry, strip), strip.get("name"),
            strip.get("mode"), tuple(sorted(strip["channels"].items())),
        )
        entities.append(ent)

    # DALI groups that have members
    for gnum, members in coord.groups.items():
        ent = DaliGroupLight(coord, gnum, members, strip_channels, dev(group_key(entry, gnum)))
        ent.layout_sig = ("group", tuple(members), tuple(sorted(strip_channels)))
        entities.append(ent)

    # Broadcast (own device "DALI All", so its name and area stay as before)
    ent = DaliBroadcastLight(coord, strip_channels, dev(all_key(entry)))
    ent.layout_sig = ("all", tuple(sorted(strip_channels)))
    entities.append(ent)
    return entities


async def async_apply_layout(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply a changed layout (group membership / strips) without a reload.

    1. entity objects whose layout changed or that are gone are removed from
       HA (registry entries stay),
    2. the registries are reconciled (devices created/moved/removed, area and
       visible name preserved, emptied group light deleted),
    3. new / changed entity objects are added again (same unique_id ->
       same entity_id, name, aliases ...).
    """
    from .devices import async_reconcile_devices

    coord: Helvar510Coordinator = entry.runtime_data
    async with coord.layout_lock:
        wanted = {e.unique_id: e for e in _build_entities(entry, coord)}
        live = coord.live_entities
        to_add: list[LightEntity] = []
        for uid, ent in list(live.items()):
            new = wanted.get(uid)
            if new is not None and getattr(new, "layout_sig", None) == getattr(ent, "layout_sig", None):
                continue
            live.pop(uid, None)
            if ent.hass is not None and ent.platform is not None:
                await ent.async_remove()
        async_reconcile_devices(hass, entry, gears=coord.gears, strips=coord.strips,
                                prefix=coord.name_prefix)
        for uid, new in wanted.items():
            if uid not in live:
                live[uid] = new
                to_add.append(new)
        if to_add:
            coord.async_add_entities(to_add)


class _HelvarBase(CoordinatorEntity[Helvar510Coordinator], LightEntity):
    # Main light of its own device: friendly name = device name, which is
    # "<prefix> <0.1.0 entity name>" -> identical to the 0.1.0 friendly name
    # ("DALI 5", "DALI Group 0", "DALI All", "DALI <strip name>").
    # A name set by the user in the entity registry always wins.
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_color_modes: set[ColorMode]
    _attr_color_mode: ColorMode

    def __init__(self, coordinator: Helvar510Coordinator, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_device_info = device_info
        # (arc levels as commanded, command seq per address, HA brightness
        # that was asked for). While the bus still shows the result of that
        # command, report exactly that brightness: HA -> DALI -> HA is not
        # 1:1 (log curve, 254 vs 255 steps), and brightness_step must start
        # from what was set - also when single gear of a group / DALI All
        # ended up elsewhere (clamped, lost frame, gear that does not follow).
        self._commanded: tuple[tuple[int | None, ...], tuple[int, ...], int] | None = None

    @property
    def available(self) -> bool:
        return self.coordinator.bus.is_open or super().available

    def _commit(self, levels: dict[int, int | None], brightness: int | None) -> None:
        """Show levels just written, remembering the requested brightness."""
        coord = self.coordinator
        sas = self._brightness_sas()
        if brightness is not None and brightness > 0:
            data = coord.data or {}
            clamped = coord.clamp_levels(levels)
            # commit_levels bumps the command seq of every address it gets
            self._commanded = (
                tuple(clamped[sa] if sa in clamped else data.get(sa) for sa in sas),
                tuple(coord.command_seq(sa) + (1 if sa in clamped else 0) for sa in sas),
                int(brightness),
            )
        else:
            self._commanded = None
        coord.commit_levels(levels)

    async def _write(self, levels: dict[int, int], brightness: int | None) -> None:
        """Per-address write through the coordinator's serialised queue.

        Every write is verified later (readback after the fade, rewrite of
        levels the gear did not take).
        """
        await self.coordinator.async_write_levels(levels)
        self._remember_command(levels, brightness)

    def _remember_command(self, levels: dict[int, int | None], brightness: int | None) -> None:
        """Remember what was set (after the levels were committed)."""
        coord = self.coordinator
        if brightness is None or brightness <= 0:
            self._commanded = None
            return
        data = coord.data or {}
        sas = self._brightness_sas()
        clamped = coord.clamp_levels(levels)
        self._commanded = (
            tuple(clamped[sa] if sa in clamped else data.get(sa) for sa in sas),
            tuple(coord.command_seq(sa) for sa in sas),
            int(brightness),
        )

    def _commanded_brightness(self) -> int | None:
        """The brightness that was set, unless the bus changed since.

        Per address the current level must be the commanded one, the level
        the gear settled at in answer to that command, or unknown (silent
        gear). Anything else is a change made elsewhere (wall panel, other
        controller, another entity) -> None, report from the bus.
        """
        if self._commanded is None:
            return None
        coord = self.coordinator
        data = coord.data or {}
        levels, seqs, bri = self._commanded
        sas = self._brightness_sas()
        if len(sas) != len(levels):
            return None
        for sa, want, seq in zip(sas, levels, seqs):
            cur = data.get(sa)
            if cur is None or cur == want:
                continue
            if coord.command_seq(sa) == seq and coord.settled_level(sa, seq) == cur:
                continue
            return None
        return bri

    def _brightness_sas(self) -> list[int]:
        raise NotImplementedError


class DaliShortLight(_HelvarBase):
    """One DALI short address as a brightness light."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(
        self,
        coordinator: Helvar510Coordinator,
        gear: dict,
        device_info: DeviceInfo,
        name: str | None = None,
    ) -> None:
        super().__init__(coordinator, device_info)
        self._attr_name = name
        self.sa = gear["sa"]
        self.gear = gear
        self._attr_unique_id = sa_unique_id(self.sa)
        dt = gear.get("device_type")
        self._attr_extra_state_attributes = {
            "short_address": self.sa,
            "device_type": DT_NAMES.get(dt, dt),
            "device_types": gear.get("device_types") or [],
            "groups": gear.get("groups") or [],
            "min_level": gear.get("min_level"),
            "max_level": gear.get("max_level"),
            "random_address": (
                f"{gear['random_address']:06X}"
                if gear.get("random_address") is not None
                else None
            ),
        }

    @property
    def available(self) -> bool:
        return self.coordinator.is_available(self.sa) and super().available

    @property
    def is_on(self) -> bool | None:
        level = (self.coordinator.data or {}).get(self.sa)
        if level is None:
            return None
        return level > 0

    def _brightness_sas(self) -> list[int]:
        return [self.sa]

    @property
    def brightness(self) -> int | None:
        level = (self.coordinator.data or {}).get(self.sa)
        if level is None:
            return None
        if level > 0 and (bri := self._commanded_brightness()) is not None:
            return bri
        return arc_to_brightness(level)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            bri = int(kwargs[ATTR_BRIGHTNESS])
            arc = brightness_to_arc(bri)
            if arc <= 0:
                await self._write({self.sa: 0}, None)
            else:
                await self._write({self.sa: arc}, bri)
        else:
            await self.coordinator.async_command(
                short_cmd_addr(self.sa), CMD_RECALL_MAX
            )
            self._commit({self.sa: self.gear.get("max_level") or 254}, None)
        self.coordinator.schedule_refresh([self.sa])

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write({self.sa: 0}, None)
        self.coordinator.schedule_refresh([self.sa])


class DaliStripChannelLight(DaliShortLight):
    """A single channel of an assembled strip, attached to the strip device.

    Named by its colour role, e.g. "R (0)", so the friendly name becomes
    "<strip device name> R (0)". (0.1.0 called it "DALI 0"; HA 2026.x always
    prefixes the device name for entities on a device, so the old name cannot
    be kept once the channel lives on the strip device. A name set by the
    user in the entity registry still wins.)
    """

    def __init__(
        self,
        coordinator: Helvar510Coordinator,
        gear: dict,
        device_info: DeviceInfo,
        strip: dict,
    ) -> None:
        super().__init__(coordinator, gear, device_info)
        role = next((r for r, v in strip["channels"].items() if v == self.sa), "?")
        self._attr_name = f"{role.upper()} ({self.sa})"
        self._attr_extra_state_attributes = {
            **self._attr_extra_state_attributes,
            "strip_role": role,
        }


@dataclass
class StripExtraData(ExtraStoredData):
    colour: list[int] | None
    brightness: int | None

    def as_dict(self) -> dict[str, Any]:
        return {"colour": self.colour, "brightness": self.brightness}


class DaliStripLight(_HelvarBase, RestoreEntity):
    """RGB / RGBW strip assembled from 3-4 short addresses."""

    def __init__(
        self, coordinator: Helvar510Coordinator, strip: dict, device_info: DeviceInfo
    ) -> None:
        super().__init__(coordinator, device_info)
        self.strip = strip
        self.channels = strip["channels"]  # r,g,b[,w] -> sa
        self.mode = coordinator.strip_mode(strip)
        self._roles = coordinator.strip_roles(strip)
        if len(self._roles) == 3:
            self.mode = "rgb"
        self._sas = list(self.channels.values())
        self._attr_unique_id = strip_unique_id(strip)
        if self.mode == "rgbw":
            self._attr_supported_color_modes = {ColorMode.RGBW}
            self._attr_color_mode = ColorMode.RGBW
        else:
            self._attr_supported_color_modes = {ColorMode.RGB}
            self._attr_color_mode = ColorMode.RGB
        self._mem: dict[str, Any] = coordinator.strip_mem.setdefault(
            self._attr_unique_id, {"colour": None, "brightness": None}
        )
        self._attr_extra_state_attributes = {
            "channels": dict(self.channels),
            "mode": self.mode,
        }

    # ---------------------------------------------------------- memory
    def _valid(self, colour: Any) -> tuple[int, ...] | None:
        """Clean colour, normalised so its brightest component is 255.

        Brightness lives only in `brightness`; a colour like (0, 0, 0, 128)
        would otherwise scale the output a second time and the reported
        brightness (brightest channel on the bus) would not match the one
        that was set - every brightness_step up would end up darker.
        """
        if not colour or len(colour) != len(self._roles):
            return None
        try:
            col = tuple(max(0, min(255, int(c))) for c in colour)
        except (TypeError, ValueError):
            return None
        top = max(col)
        if top <= 0:
            return None
        if top < 255:
            col = tuple(int(round(c * 255.0 / top)) for c in col)
        return col

    def _mem_colour(self) -> tuple[int, ...]:
        return self._valid(self._mem.get("colour")) or default_strip_colour(self.mode)

    def _remember(self, colour: tuple[int, ...] | None, brightness: int | None) -> None:
        col = self._valid(colour)
        if col is not None:
            self._mem["colour"] = list(col)
        if brightness:
            self._mem["brightness"] = int(brightness)

    @property
    def extra_restore_state_data(self) -> StripExtraData:
        return StripExtraData(self._mem.get("colour"), self._mem.get("brightness"))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_extra_data()
        if last is not None:
            d = last.as_dict()
            col = self._valid(d.get("colour"))
            if col is not None and self._valid(self._mem.get("colour")) is None:
                self._mem["colour"] = list(col)
            if d.get("brightness") and not self._mem.get("brightness"):
                self._mem["brightness"] = int(d["brightness"])
        self._learn_from_bus()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._learn_from_bus()
        super()._handle_coordinator_update()

    def _learn_from_bus(self) -> None:
        """Remember the colour actually on the bus when the strip is lit.

        Small rounding differences are ignored so the remembered colour does
        not drift; a real change (another controller, wall panel, scene) is learnt.
        """
        levels = self._levels()
        if any(v is None for v in levels):
            return
        colour, bri = arcs_to_colour(levels)
        if bri <= 0:
            return
        coord = self.coordinator
        if any(
            coord.settled_level(sa, coord.command_seq(sa)) is not None
            for sa in self._brightness_sas()
        ):
            # a channel did not take what we sent (verified, rewrites
            # exhausted): that is not a colour picked elsewhere -> keep ours
            self._mem["brightness"] = bri
            return
        if self._mem.get("arcs") == list(levels):
            # exactly what we commanded ourselves -> colour already known
            self._mem["brightness"] = bri
            return
        known = self._valid(self._mem.get("colour"))
        if known is None or max(abs(a - b) for a, b in zip(colour, known)) > LEARN_TOLERANCE:
            self._remember(colour, None)
        self._mem["brightness"] = bri

    # ---------------------------------------------------------- state
    def _levels(self) -> list[int | None]:
        data = self.coordinator.data or {}
        return [data.get(self.channels[k]) for k in self._roles]

    @property
    def is_on(self) -> bool | None:
        lv = self._levels()
        if all(v is None for v in lv):
            return None
        return any((v or 0) > 0 for v in lv)

    def _brightness_sas(self) -> list[int]:
        return [self.channels[k] for k in self._roles]

    @property
    def brightness(self) -> int | None:
        _colour, bri = arcs_to_colour(self._levels())
        if bri > 0 and (cmd := self._commanded_brightness()) is not None:
            return cmd
        return bri

    def _current_colour(self) -> tuple[int, ...]:
        colour, bri = arcs_to_colour(self._levels())
        if bri <= 0:
            return self._mem_colour()
        known = self._valid(self._mem.get("colour"))
        if known is not None and (
            self._mem.get("arcs") == list(self._levels())
            or max(abs(a - b) for a, b in zip(colour, known)) <= LEARN_TOLERANCE
        ):
            return known  # show the exact colour the user picked
        return colour

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        if self.mode != "rgb":
            return None
        return tuple(self._current_colour()[:3])  # type: ignore[return-value]

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        if self.mode != "rgbw":
            return None
        return tuple(self._current_colour()[:4])  # type: ignore[return-value]

    # ---------------------------------------------------------- commands
    async def async_turn_on(self, **kwargs: Any) -> None:
        bri: float = int(kwargs.get(ATTR_BRIGHTNESS, self._mem.get("brightness") or 255))
        key = ATTR_RGBW_COLOR if self.mode == "rgbw" else ATTR_RGB_COLOR
        n = 4 if self.mode == "rgbw" else 3
        given = kwargs.get(key)
        if given is not None:
            raw = [max(0.0, min(255.0, float(c))) for c in tuple(given)[:n]]
            top = max(raw) if raw else 0.0
            if top <= 0:
                await self.async_turn_off()
                return
            # A colour that is not at full value (e.g. W only at 128) dims
            # the output: keep that output, but report it as brightness with
            # the colour normalised, so state == what is lit.
            colour_f = [c * 255.0 / top for c in raw]
            bri = bri * top / 255.0
        else:
            colour_f = [float(c) for c in self._mem_colour()[:n]]
        white = colour_f[3] if n == 4 else None
        arcs = rgb_brightness_to_arcs(tuple(colour_f[:3]), bri, white=white)
        if not any(arcs):
            await self.async_turn_off()
            return
        reported = max(1, min(255, int(round(bri))))
        self._remember(tuple(int(round(c)) for c in colour_f), reported)
        sent = {self.channels[role]: arcs[i] for i, role in enumerate(self._roles)}
        await self._write(sent, reported)
        self.coordinator.schedule_refresh(self._sas)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write({sa: 0 for sa in self._sas}, None)
        self.coordinator.schedule_refresh(self._sas)


class _MultiLight(_HelvarBase):
    """Shared logic for group and broadcast lights."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    members: list[int]
    strip_channels: set[int]

    def __init__(self, coordinator: Helvar510Coordinator, device_info: DeviceInfo) -> None:
        super().__init__(coordinator, device_info)
        self._last_brightness = 255

    def _member_levels(self) -> list[int | None]:
        data = self.coordinator.data or {}
        return [data.get(sa) for sa in self._members()]

    def _members(self) -> list[int]:
        return self.members

    def _brightness_sas(self) -> list[int]:
        return self._members()

    @property
    def is_on(self) -> bool | None:
        lv = self._member_levels()
        if not lv or all(v is None for v in lv):
            return None
        return any((v or 0) > 0 for v in lv)

    @property
    def brightness(self) -> int | None:
        lv = [v for v in self._member_levels() if v is not None]
        if not lv:
            return None
        lit = [v for v in lv if v > 0]
        if not lit:
            return 0
        # A group / DALI All sets every member to one brightness: report that
        # while most members are still where it put them. Otherwise report
        # the level most members are at - not the brightest member, so one
        # gear that does not follow (stuck at full, raised min level) cannot
        # pin the whole group and make every step start from 255.
        bri = self._commanded_brightness()
        if bri is None:
            bri = self._typical_brightness(lit)
        if bri:
            self._last_brightness = bri
        return bri

    def _typical_brightness(self, lit: list[int]) -> int:
        """Brightness most lit non-strip members are at (strips report
        their colour, not their brightness, per channel)."""
        data = self.coordinator.data or {}
        plain = [
            data.get(sa) for sa in self._members()
            if sa not in self.strip_channels and (data.get(sa) or 0) > 0
        ]
        pool = plain or lit
        counts: dict[int, int] = {}
        for v in pool:
            counts[v] = counts.get(v, 0) + 1
        level = max(counts, key=lambda v: (counts[v], v))
        return arc_to_brightness(level)

    def _commanded_brightness(self) -> int | None:
        """The brightness last set, while the majority of members still are
        at the level it put them at (or settled at in answer to it)."""
        if self._commanded is None:
            return None
        coord = self.coordinator
        data = coord.data or {}
        levels, seqs, bri = self._commanded
        sas = self._brightness_sas()
        if len(sas) != len(levels):
            return None
        same = other = 0
        for sa, want, seq in zip(sas, levels, seqs):
            cur = data.get(sa)
            if cur is None:
                continue
            if cur == want or (
                coord.command_seq(sa) == seq and coord.settled_level(sa, seq) == cur
            ):
                same += 1
            else:
                other += 1
        if same == 0 or other > same:
            return None
        return bri

    def _has_strip_members(self) -> bool:
        return bool(set(self._members()) & self.strip_channels)

    async def _apply_plan(self, target_bri: int) -> None:
        """Every member to `target_bri`, per short address (verified).

        Strips get their colour at that brightness, other gear the matching
        arc level. Per-address frames only: each one is verified after it
        settled (0.3.2 also used group DAPC for DALI All; group frames are
        not verified per gear and, when they did not take, grouped lights
        stayed behind while a dimmer button was held).
        """
        coord = self.coordinator
        plan = coord.plan_levels(self._members(), target_bri)
        _LOGGER.debug(
            "%s: brightness %s -> %s per-address frames", self.entity_id, target_bri, len(plan)
        )
        await coord.async_write_levels(plan)
        self._remember_command(plan, target_bri)
        coord.async_update_listeners()


class DaliGroupLight(_MultiLight):
    """One DALI group as a brightness light."""

    def __init__(
        self,
        coordinator: Helvar510Coordinator,
        group: int,
        members: list[int],
        strip_channels: set[int],
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator, device_info)
        self.dali_group = group
        self.members = list(members)
        self.strip_channels = strip_channels
        self._attr_unique_id = group_unique_id(group)
        self._attr_extra_state_attributes = {
            "dali_group": group,
            "members": self.members,
            "contains_strip_channels": bool(set(self.members) & strip_channels),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            bri = int(kwargs[ATTR_BRIGHTNESS])
            if brightness_to_arc(bri) <= 0:
                await self.async_turn_off()
                return
            self._last_brightness = bri
        else:
            bri = self._last_brightness or 255
        if self._has_strip_members():
            await self._apply_plan(bri)
        elif ATTR_BRIGHTNESS in kwargs:
            arc = brightness_to_arc(bri)
            await self.coordinator.async_dapc(group_dapc_addr(self.dali_group), arc)
            self._commit({sa: arc for sa in self.members}, bri)
        else:
            await self.coordinator.async_command(group_cmd_addr(self.dali_group), CMD_RECALL_MAX)
            self._commit({sa: 254 for sa in self.members}, None)
        self.coordinator.schedule_refresh(self.members)

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Group OFF is colour-safe (all channels to 0)
        await self.coordinator.async_command(group_cmd_addr(self.dali_group), CMD_OFF)
        self._commit({sa: 0 for sa in self.members}, None)
        self.coordinator.schedule_refresh(self.members)


class DaliBroadcastLight(_MultiLight):
    """Broadcast (all gear) brightness light."""

    def __init__(
        self,
        coordinator: Helvar510Coordinator,
        strip_channels: set[int],
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator, device_info)
        self.strip_channels = strip_channels
        self._attr_unique_id = BROADCAST_UNIQUE_ID

    def _members(self) -> list[int]:
        return list(self.coordinator.gear_by_sa)

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Broadcast DAPC would wreck strip colours - with strips, address
        # every gear individually (same rule as groups with strip members).
        if ATTR_BRIGHTNESS in kwargs:
            bri = int(kwargs[ATTR_BRIGHTNESS])
            if brightness_to_arc(bri) <= 0:
                await self.async_turn_off()
                return
            self._last_brightness = bri
        else:
            bri = self._last_brightness or 255
        everyone = self._members()
        if self.strip_channels:
            await self._apply_plan(bri)
        elif ATTR_BRIGHTNESS in kwargs:
            arc = brightness_to_arc(bri)
            await self.coordinator.async_dapc(BROADCAST_DAPC, arc)
            self._commit({sa: arc for sa in everyone}, bri)
        else:
            await self.coordinator.async_command(BROADCAST_CMD, CMD_RECALL_MAX)
            self._commit({sa: 254 for sa in everyone}, None)
        self.coordinator.schedule_refresh(everyone)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_command(BROADCAST_CMD, CMD_OFF)
        self._commit({sa: 0 for sa in self._members()}, None)
        self.coordinator.schedule_refresh(self._members())
