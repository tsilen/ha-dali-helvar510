"""Device layout helpers (0.2.1).

0.1.0 put every entity under ONE device whose identifier was
``(DOMAIN, entry_id)``.  0.2.x keeps that very device as the 510 *hub*
(display name = the name prefix, "DALI" by default, exactly like 0.1.0) and
gives the lights you actually control their own child device
(``via_device`` = hub):

* single short address that is in NO group and NO strip -> ``{entry_id}_sa_{N}``
* assembled strip       -> ``{entry_id}_strip_{key}`` (strip channels live here)
* DALI group with members -> ``{entry_id}_group_{G}``
* broadcast (DALI All)  -> ``{entry_id}_all``

Group member lights (not in a strip) live on the hub device, named
"<hub name> <sa>" (= "DALI 5" as in 0.1.0), hidden by the integration.
async_reconcile_devices() moves registry entries between devices without
losing the user's area or visible name.

Entity unique_ids are NOT touched here (see light.py) - they are exactly the
0.1.0 values so registry names, aliases, entity_ids, hidden/disabled flags and
automations survive.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_NAME_PREFIX,
    CONF_STRIPS,
    DEFAULT_NAME_PREFIX,
    DOMAIN,
    DT_NAMES,
    MANUFACTURER,
    MODEL,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------- unique ids
# (exactly as in 0.1.0 - do not change)
def sa_unique_id(sa: int) -> str:
    return f"{DOMAIN}-sa-{sa}"


# 0.1.0 built the strip unique_id from the *dict order* of strip["channels"].
# That order is r,g,b,w while the strip is still in memory after the options
# flow, but HA writes .storage JSON with sorted keys, so after a restart the
# order became b,g,r,w -> a different unique_id (and a new entity) whenever
# the channels are not in the same numeric order.  0.2.0 stores the unique_id
# in the strip config ("unique_id") and resolves it once from the registry.
STRIP_UID_PREFIX = f"{DOMAIN}-strip-"
_ROLES = ("r", "g", "b", "w")


def role_order_uid(strip: dict) -> str:
    ch = strip["channels"]
    return STRIP_UID_PREFIX + "-".join(str(ch[r]) for r in _ROLES if r in ch)


def sorted_order_uid(strip: dict) -> str:
    ch = strip["channels"]
    return STRIP_UID_PREFIX + "-".join(str(ch[k]) for k in sorted(ch))


def dict_order_uid(strip: dict) -> str:
    return STRIP_UID_PREFIX + "-".join(str(v) for v in strip["channels"].values())


def strip_unique_id(strip: dict) -> str:
    return strip.get("unique_id") or role_order_uid(strip)


def strip_key(strip: dict) -> str:
    return strip_unique_id(strip)[len(STRIP_UID_PREFIX):]


def _is_customised(ent: er.RegistryEntry) -> bool:
    aliases = [a for a in (ent.aliases or ()) if isinstance(a, str)]
    return bool(
        ent.name
        or aliases
        or ent.area_id
        or ent.icon
        or ent.hidden_by == er.RegistryEntryHider.USER
        or ent.disabled_by == er.RegistryEntryDisabler.USER
        or getattr(ent, "labels", None)
    )


def new_strip_uid() -> str:
    import uuid

    return f"{STRIP_UID_PREFIX}{uuid.uuid4().hex[:8]}"


@callback
def resolve_strip_unique_id(
    hass: HomeAssistant,
    entry: ConfigEntry,
    strip: dict,
    used: set[str] | None = None,
) -> str:
    """Pick the unique_id of an already registered entity for this strip.

    Candidates are every form 0.1.0 could have produced. Prefer the one the
    user customised (name/alias/area/...), then the most recently modified.
    Falls back to the canonical r-g-b-w form for brand-new strips.
    """
    ent_reg = er.async_get(hass)
    used = used or set()
    candidates: list[str] = []
    for uid in (strip.get("unique_id"), role_order_uid(strip),
                sorted_order_uid(strip), dict_order_uid(strip)):
        if uid and uid not in candidates and uid not in used:
            candidates.append(uid)
    found: list[er.RegistryEntry] = []
    for uid in candidates:
        eid = ent_reg.async_get_entity_id("light", DOMAIN, uid)
        if eid and (ent := ent_reg.async_get(eid)) is not None:
            if ent.config_entry_id in (None, entry.entry_id):
                found.append(ent)
    if not found:
        return candidates[0] if candidates else new_strip_uid()

    def score(ent: er.RegistryEntry):
        mod = getattr(ent, "modified_at", None) or getattr(ent, "created_at", None)
        return (_is_customised(ent), mod.timestamp() if mod else 0.0)

    best = max(found, key=score)
    if len(found) > 1:
        _LOGGER.info(
            "helvar510: strip %s has several registry entries %s, using %s",
            strip.get("name"), [e.unique_id for e in found], best.unique_id,
        )
    return best.unique_id


def group_unique_id(group: int) -> str:
    return f"{DOMAIN}-group-{group}"


BROADCAST_UNIQUE_ID = f"{DOMAIN}-broadcast"


# ---------------------------------------------------------------- layout
def entry_strips(entry: ConfigEntry) -> list[dict]:
    return list(entry.options.get(CONF_STRIPS, entry.data.get(CONF_STRIPS, [])))


def entry_gears(entry: ConfigEntry) -> list[dict]:
    return list(entry.data.get("gears", []))


def gear_groups(gears: list[dict]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for g in gears:
        for grp in g.get("groups", []) or []:
            out.setdefault(grp, []).append(g["sa"])
    return dict(sorted(out.items()))


def name_prefix(entry: ConfigEntry) -> str:
    return entry.data.get(CONF_NAME_PREFIX) or DEFAULT_NAME_PREFIX


@dataclass(frozen=True)
class ChildDevice:
    key: str  # identifier value (without domain)
    name: str
    model: str


def hub_identifier(entry: ConfigEntry) -> tuple[str, str]:
    return (DOMAIN, entry.entry_id)


def child_identifier(entry: ConfigEntry, key: str) -> tuple[str, str]:
    return (DOMAIN, key)


def sa_key(entry: ConfigEntry, sa: int) -> str:
    return f"{entry.entry_id}_sa_{sa}"


def strip_dev_key(entry: ConfigEntry, strip: dict) -> str:
    return f"{entry.entry_id}_strip_{strip_key(strip)}"


def group_key(entry: ConfigEntry, group: int) -> str:
    return f"{entry.entry_id}_group_{group}"


def all_key(entry: ConfigEntry) -> str:
    return f"{entry.entry_id}_all"


def strip_display_name(strip: dict, index: int) -> str:
    return strip.get("name") or f"Strip {index + 1}"


def expected_children(
    entry: ConfigEntry,
    gears: list[dict] | None = None,
    strips: list[dict] | None = None,
    prefix: str | None = None,
) -> list[ChildDevice]:
    gears = entry_gears(entry) if gears is None else gears
    strips = entry_strips(entry) if strips is None else strips
    prefix = name_prefix(entry) if prefix is None else prefix
    strip_channels: set[int] = set()
    for s in strips:
        strip_channels.update(s["channels"].values())
    members = _group_member_sas(gears)
    out: list[ChildDevice] = []
    for g in gears:
        sa = g["sa"]
        if sa in strip_channels or sa in members:
            continue  # strip channel -> strip device, group member -> hub
        dt = g.get("device_type")
        out.append(
            ChildDevice(sa_key(entry, sa), f"{prefix} {sa}",
                        f"DALI {DT_NAMES.get(dt, dt) if dt is not None else 'gear'}")
        )
    for idx, s in enumerate(strips):
        mode = s.get("mode", "rgbw" if "w" in s["channels"] else "rgb")
        out.append(
            ChildDevice(strip_dev_key(entry, s), f"{prefix} {strip_display_name(s, idx)}",
                        f"DALI {mode.upper()} strip ({len(s['channels'])} channels)")
        )
    for grp in gear_groups(gears):
        out.append(ChildDevice(group_key(entry, grp), f"{prefix} Group {grp}", "DALI group"))
    out.append(ChildDevice(all_key(entry), f"{prefix} All", "DALI broadcast"))
    return out


def expected_identifiers(entry: ConfigEntry) -> set[tuple[str, str]]:
    ids = {hub_identifier(entry)}
    ids.update(child_identifier(entry, c.key) for c in expected_children(entry))
    return ids


# HA 2026.x replaced DeviceInfo/async_get_or_create ``via_device`` (identifier
# tuple, removed in 2027.8) with ``via_device_id`` (device registry id) and
# deprecated ``async_get_device`` in favour of ``async_get_device_by_identifier``.
_HAS_VIA_DEVICE_ID = "via_device_id" in getattr(DeviceInfo, "__annotations__", {})


def get_device(
    dev_reg: dr.DeviceRegistry, identifier: tuple[str, str], entry_id: str
) -> dr.DeviceEntry | None:
    if hasattr(dev_reg, "async_get_device_by_identifier"):
        return dev_reg.async_get_device_by_identifier(identifier, entry_id)
    return dev_reg.async_get_device(identifiers={identifier})


def via_kwargs(entry: ConfigEntry, hub_device_id: str | None) -> dict:
    if _HAS_VIA_DEVICE_ID:
        return {"via_device_id": hub_device_id} if hub_device_id else {}
    return {"via_device": hub_identifier(entry)}


def hub_device_info(entry: ConfigEntry, serial: str | None = None) -> DeviceInfo:
    # Hub name = prefix ("DALI"): group member lights on the hub are then
    # "DALI 5" exactly as in 0.1.0. A name set by the user on the hub wins.
    info = DeviceInfo(
        identifiers={hub_identifier(entry)},
        manufacturer=MANUFACTURER,
        model=MODEL,
        name=name_prefix(entry),
    )
    if serial:
        info["serial_number"] = serial
    return info


def child_device_info(
    entry: ConfigEntry, child: ChildDevice, hub_device_id: str | None
) -> DeviceInfo:
    return DeviceInfo(  # type: ignore[typeddict-item]
        identifiers={child_identifier(entry, child.key)},
        name=child.name,
        model=child.model,
        **via_kwargs(entry, hub_device_id),
    )


# ---------------------------------------------------------------- migration
@callback
def async_migrate_devices(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """0.1.0 -> 0.2.0: create child devices, inherit hub area. Returns prefix.

    Runs once from async_migrate_entry (before the light platform is set up).
    """
    dev_reg = dr.async_get(hass)
    old = get_device(dev_reg, hub_identifier(entry), entry.entry_id)
    prefix = DEFAULT_NAME_PREFIX
    area_id = None
    if old is not None:
        # Entities used to be named "<device name> <entity name>"; if the user
        # renamed the shared device, keep that prefix so visible names stay.
        prefix = old.name_by_user or old.name or DEFAULT_NAME_PREFIX
        area_id = old.area_id
    hub = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={hub_identifier(entry)},
        manufacturer=MANUFACTURER,
        model=MODEL,
        name=prefix,
    )
    # Build children with the persisted prefix
    for child in expected_children(entry, prefix=prefix):
        existed = get_device(dev_reg, child_identifier(entry, child.key), entry.entry_id)
        dev = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={child_identifier(entry, child.key)},
            name=child.name,
            model=child.model,
            **via_kwargs(entry, hub.id),
        )
        if existed is None and area_id and dev.area_id is None:
            # Entities inherited the shared device's area before - keep it.
            dev_reg.async_update_device(dev.id, area_id=area_id)
    _LOGGER.info(
        "helvar510: migrated to per-light devices (hub %s, prefix %r, area %s)",
        hub.id, prefix, area_id,
    )
    return prefix


# ---------------------------------------------------------------- reconcile
def sa_target_key(entry: ConfigEntry, sa: int, gears: list[dict], strips: list[dict]) -> str | None:
    """Child device key for a short-address entity, None = hub."""
    for st in strips:
        if sa in st["channels"].values():
            return strip_dev_key(entry, st)
    if sa in _group_member_sas(gears):
        return None
    return sa_key(entry, sa)


def sa_entity_name(sa: int, gears: list[dict], strips: list[dict]) -> str | None:
    """Entity (has_entity_name) name part of a short-address light."""
    for st in strips:
        for role, v in st["channels"].items():
            if v == sa:
                return f"{role.upper()} ({sa})"
    if sa in _group_member_sas(gears):
        return str(sa)  # on hub "DALI" -> "DALI 5"
    return None  # main entity of its own device "DALI 5"


def _target_map(
    entry: ConfigEntry, gears: list[dict], strips: list[dict]
) -> dict[str, tuple[str | None, str | None, bool]]:
    """unique_id -> (child key | None for hub, entity name part, is_strip_channel)."""
    out: dict[str, tuple[str | None, str | None, bool]] = {}
    channels = _strip_channel_sas(strips)
    for g in gears:
        sa = g["sa"]
        out[sa_unique_id(sa)] = (
            sa_target_key(entry, sa, gears, strips),
            sa_entity_name(sa, gears, strips),
            sa in channels,
        )
    for st in strips:
        out[strip_unique_id(st)] = (strip_dev_key(entry, st), None, False)
    for grp in gear_groups(gears):
        out[group_unique_id(grp)] = (group_key(entry, grp), None, False)
    out[BROADCAST_UNIQUE_ID] = (all_key(entry), None, False)
    return out


def _friendly(hass: HomeAssistant, ent: er.RegistryEntry) -> str | None:
    """Visible name of a registry entry (HA 2026.10 helper, fallback for older)."""
    fn = getattr(er, "async_get_legacy_friendly_name", None)
    if fn is not None:
        return fn(hass, ent) or None
    if ent.name:
        return ent.name
    if not ent.has_entity_name:
        return ent.original_name
    dev = dr.async_get(hass).async_get(ent.device_id) if ent.device_id else None
    return " ".join(x for x in (_dev_display(dev), ent.original_name) if x) or None


def _dev_display(dev: dr.DeviceEntry | None) -> str | None:
    return (dev.name_by_user or dev.name) if dev is not None else None


@callback
def _async_preserve(
    hass: HomeAssistant,
    ent: er.RegistryEntry,
    old_dev: dr.DeviceEntry | None,
    new_dev: dr.DeviceEntry | None,
    new_name_part: str | None,
    *,
    is_channel: bool,
    hub_id: tuple[str, str],
    fresh_devices: set[str],
) -> dict:
    """Registry changes so a device move keeps the entity's area + name."""
    dev_reg = dr.async_get(hass)
    changes: dict = {}
    if old_dev is None or (new_dev is None and hub_id in old_dev.identifiers):
        return changes  # detaching from the hub (which stays): nothing to keep
    # ---- area: the entity inherited the old device's area
    if ent.area_id is None and old_dev.area_id and (
        new_dev is None or new_dev.area_id != old_dev.area_id
    ):
        if new_dev is not None and new_dev.id in fresh_devices and new_dev.area_id is None:
            dev_reg.async_update_device(new_dev.id, area_id=old_dev.area_id)
        else:
            changes["area_id"] = old_dev.area_id
    # ---- visible name
    if ent.name is None:
        old_friendly = _friendly(hass, ent)
        if new_dev is None:
            new_friendly = None
        else:
            new_friendly = " ".join(
                x for x in (_dev_display(new_dev), new_name_part) if x
            )
        old_is_child = hub_id not in old_dev.identifiers
        derived_from_user = bool(old_is_child and old_dev.name_by_user)
        if new_dev is None or is_channel:
            # detached / strip channel: only keep a name the user gave the device
            keep = old_friendly and old_friendly != new_friendly and derived_from_user
        else:
            keep = old_friendly and old_friendly != new_friendly
        if keep:
            changes["name"] = old_friendly
    return changes


@callback
def _safe_update(ent_reg: er.EntityRegistry, entity_id: str, **changes) -> None:
    """Registry update that never breaks setup: if HA rejects the area/name
    part, apply the device move alone and log it."""
    try:
        ent_reg.async_update_entity(entity_id, **changes)
    except ValueError as err:
        _LOGGER.warning("helvar510: %s: %s (%s) - applying device change only", entity_id, err, changes)
        rest = {k: v for k, v in changes.items() if k in ("device_id", "original_name")}
        if rest:
            ent_reg.async_update_entity(entity_id, **rest)


@callback
def async_reconcile_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    gears: list[dict] | None = None,
    strips: list[dict] | None = None,
    prefix: str | None = None,
) -> None:
    """Bring devices + entity device_ids in line with the current layout.

    * creates missing child devices (via the hub),
    * moves registry entries to their target device (keeping the user's
      area and visible name, see _async_preserve),
    * deletes the light of an emptied group (unless customised -> detached),
    * detaches entities that are no longer provided and removes devices that
      are no longer part of the layout (no orphans).
    Never touches unique_id / entity_id / aliases / hidden / disabled flags.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    gears = entry_gears(entry) if gears is None else gears
    strips = entry_strips(entry) if strips is None else strips
    prefix = name_prefix(entry) if prefix is None else prefix
    hub_id = hub_identifier(entry)
    hub = get_device(dev_reg, hub_id, entry.entry_id)
    if hub is None:
        hub = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={hub_id},
            manufacturer=MANUFACTURER, model=MODEL, name=prefix,
        )
    children = expected_children(entry, gears, strips, prefix)
    fresh: set[str] = set()
    dev_by_key: dict[str, dr.DeviceEntry] = {}
    for child in children:
        ident = child_identifier(entry, child.key)
        dev = get_device(dev_reg, ident, entry.entry_id)
        if dev is None:
            dev = dev_reg.async_get_or_create(
                config_entry_id=entry.entry_id, identifiers={ident},
                name=child.name, model=child.model, **via_kwargs(entry, hub.id),
            )
            fresh.add(dev.id)
        dev_by_key[child.key] = dev

    targets = _target_map(entry, gears, strips)
    our_devices = {
        d.id: d for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    }
    for ent in list(er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
        if ent.domain != "light":
            continue
        tgt = targets.get(ent.unique_id)
        old_dev = our_devices.get(ent.device_id) if ent.device_id else None
        if tgt is None:
            # Not provided any more.
            if ent.unique_id.startswith(f"{DOMAIN}-group-") and not _is_customised(ent):
                _LOGGER.info("helvar510: removing light of empty group %s", ent.entity_id)
                ent_reg.async_remove(ent.entity_id)
                continue
            if ent.device_id is not None:
                changes = _async_preserve(
                    hass, ent, old_dev, None, None, is_channel=False,
                    hub_id=hub_id, fresh_devices=fresh,
                )
                _LOGGER.info("helvar510: detaching stale entity %s", ent.entity_id)
                _safe_update(ent_reg, ent.entity_id, device_id=None, **changes)
            continue
        key, name_part, is_channel = tgt
        new_dev = hub if key is None else dev_by_key.get(key)
        if new_dev is None:
            continue
        if ent.device_id == new_dev.id:
            changes = {}
        else:
            changes = _async_preserve(
                hass, ent, old_dev, new_dev, name_part, is_channel=is_channel,
                hub_id=hub_id, fresh_devices=fresh,
            )
            # Same update as the platform will do; needed so HA's "own area
            # needs own name" check sees the new name part.
            changes["original_name"] = name_part
        # HA 2026.x: an entity with an area of its own must have a name of its
        # own. The main light of a device has none -> keep the user's entity
        # area and pin the visible name instead (it does not change).
        area = changes.get("area_id", ent.area_id)
        name = changes.get("name", ent.name)
        if area and not name and not name_part:
            changes["name"] = (
                _friendly(hass, ent)
                if old_dev is not None and ent.device_id != new_dev.id
                else None
            ) or _dev_display(new_dev)
        if not changes:
            continue
        if ent.device_id == new_dev.id:
            _LOGGER.info("helvar510: %s keeps its own area, pinning name %s", ent.entity_id, changes)
            _safe_update(ent_reg, ent.entity_id, **changes)
            continue
        _LOGGER.info(
            "helvar510: moving %s to device %s %s",
            ent.entity_id, _dev_display(new_dev), changes or "",
        )
        _safe_update(ent_reg, ent.entity_id, device_id=new_dev.id, **changes)

    expected = {hub_id} | {child_identifier(entry, c.key) for c in children}
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if device.identifiers & expected:
            continue
        for ent in er.async_entries_for_device(
            ent_reg, device.id, include_disabled_entities=True
        ):
            if ent.config_entry_id == entry.entry_id:
                changes = _async_preserve(
                    hass, ent, device, None, None, is_channel=False,
                    hub_id=hub_id, fresh_devices=fresh,
                )
                _safe_update(ent_reg, ent.entity_id, device_id=None, **changes)
        _LOGGER.info("helvar510: removing unused device %s (%s)", device.name, device.identifiers)
        # Entities were detached above, so removing the device keeps them.
        dev_reg.async_remove_device(device.id)


# 0.2.0 name kept for callers
async_cleanup_devices = async_reconcile_devices


def provided_unique_ids(entry: ConfigEntry) -> set[str]:
    gears = entry_gears(entry)
    ids = {sa_unique_id(g["sa"]) for g in gears}
    ids.update(strip_unique_id(s) for s in entry_strips(entry))
    ids.update(group_unique_id(g) for g in gear_groups(gears))
    ids.add(BROADCAST_UNIQUE_ID)
    return ids


def _strip_channel_sas(strips: list[dict]) -> set[int]:
    return {sa for s in strips for sa in s["channels"].values()}


def _group_member_sas(gears: list[dict]) -> set[int]:
    return {g["sa"] for g in gears if g.get("groups")}


def hide_flags(entry: ConfigEntry) -> tuple[bool, bool]:
    from .const import (
        CONF_HIDE_GROUP_MEMBERS,
        CONF_HIDE_STRIP_CHANNELS,
        DEFAULT_HIDE_GROUP_MEMBERS,
        DEFAULT_HIDE_STRIP_CHANNELS,
    )

    return (
        entry.options.get(CONF_HIDE_STRIP_CHANNELS, DEFAULT_HIDE_STRIP_CHANNELS),
        entry.options.get(CONF_HIDE_GROUP_MEMBERS, DEFAULT_HIDE_GROUP_MEMBERS),
    )


def sa_should_hide(
    sa: int, strips: list[dict], gears: list[dict], hide_strips: bool, hide_groups: bool
) -> bool:
    if hide_strips and sa in _strip_channel_sas(strips):
        return True
    if hide_groups and sa in _group_member_sas(gears):
        return True
    return False


@callback
def async_apply_sa_visibility(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    old: tuple[list[dict], list[dict], bool, bool],
    new: tuple[list[dict], list[dict], bool, bool],
    enable_integration_disabled: bool = False,
) -> None:
    """Update hidden_by=INTEGRATION when the hide *rule* changes for a light.

    old/new = (strips, gears, hide_strip_channels, hide_group_members).
    Only lights whose "should be hidden" result changes are touched, so a
    user's own un-hide/hide is respected; USER flags are never changed.
    Lights stay ENABLED: with enable_integration_disabled (0.1.0 migration)
    strip channels that 0.1.0 created disabled_by=INTEGRATION are enabled.
    """
    ent_reg = er.async_get(hass)
    sas = {g["sa"] for g in old[1]} | {g["sa"] for g in new[1]}
    for sa in sorted(sas):
        eid = ent_reg.async_get_entity_id("light", DOMAIN, sa_unique_id(sa))
        if not eid or (ent := ent_reg.async_get(eid)) is None:
            continue
        before = sa_should_hide(sa, *old)
        after = sa_should_hide(sa, *new)
        changes: dict = {}
        if enable_integration_disabled and ent.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
            changes["disabled_by"] = None
            if after and ent.hidden_by is None:
                changes["hidden_by"] = er.RegistryEntryHider.INTEGRATION
        if after and not before and ent.hidden_by is None:
            changes["hidden_by"] = er.RegistryEntryHider.INTEGRATION
        elif before and not after and ent.hidden_by == er.RegistryEntryHider.INTEGRATION:
            changes["hidden_by"] = None
            if ent.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
                changes["disabled_by"] = None
        if changes:
            _LOGGER.info("helvar510: %s visibility -> %s", eid, changes)
            ent_reg.async_update_entity(eid, **changes)


def visibility_state(
    entry: ConfigEntry,
    *,
    strips: list[dict] | None = None,
    gears: list[dict] | None = None,
    hide_strips: bool | None = None,
    hide_groups: bool | None = None,
) -> tuple[list[dict], list[dict], bool, bool]:
    hs, hg = hide_flags(entry)
    return (
        entry_strips(entry) if strips is None else strips,
        entry_gears(entry) if gears is None else gears,
        hs if hide_strips is None else hide_strips,
        hg if hide_groups is None else hide_groups,
    )
