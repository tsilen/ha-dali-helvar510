"""Coordinator: owns the 510, serialises bus I/O and polls actual levels."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import functools
import logging
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .dali510 import (
    Dali510,
    Dali510Error,
    Reply,
)

_LOGGER = logging.getLogger(__name__)

# Pause between per-gear polls so user commands can slip in between
POLL_STAGGER_S = 0.05
# Upper bound for one blocking bus call seen from the event loop. Every frame
# has its own short timeout in the driver; this only guards against a stuck
# kernel write so HA never waits forever. A full scan gets longer.
BUS_CALL_TIMEOUT_S = 30.0
SCAN_TIMEOUT_S = 600.0
FAIL_LIMIT = 3


class Helvar510Coordinator(DataUpdateCoordinator[dict[int, int | None]]):
    """data = {short_address: arc level (0 = off) | None (unknown)}."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        path: str,
        poll_interval: int,
        rgbw_groups: list[dict],
    ) -> None:
        try:
            super().__init__(
                hass,
                _LOGGER,
                name=DOMAIN,
                update_interval=timedelta(seconds=max(5, int(poll_interval))),
                config_entry=entry,
            )
        except TypeError:
            super().__init__(
                hass,
                _LOGGER,
                name=DOMAIN,
                update_interval=timedelta(seconds=max(5, int(poll_interval))),
            )
        self.entry = entry
        self.config_path = entry.data.get("path") or "auto"
        self.bus = Dali510(path)
        # Set by the light platform (dynamic layout changes without reload)
        self.async_add_entities: Callable | None = None
        self.live_entities: dict[str, Any] = {}
        self.apply_layout: Callable | None = None
        self.layout_lock = asyncio.Lock()
        self.gears: list[dict] = list(entry.data.get("gears", []))
        self.gear_by_sa: dict[int, dict] = {g["sa"]: g for g in self.gears}
        self.rgbw_groups = rgbw_groups  # legacy
        from .const import CONF_STRIPS
        self.strips: list[dict] = list(
            entry.options.get(CONF_STRIPS, entry.data.get(CONF_STRIPS, rgbw_groups or []))
        )
        # Last known colour per assembled strip (key = strip entity unique_id).
        # Filled/restored by the strip entity, used by group/All lights to
        # re-light an OFF strip with its last colour.
        from .devices import name_prefix, strip_unique_id
        self.name_prefix: str = name_prefix(entry)
        self.hub_serial: str | None = None
        self.hub_device_id: str | None = None
        self.strip_mem: dict[str, dict[str, Any]] = {
            strip_unique_id(s): {"colour": None, "brightness": None}
            for s in self.strips
        }
        self.fail_count: dict[int, int] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="helvar510")
        self._lock = asyncio.Lock()
        self.data = {g["sa"]: g.get("actual") for g in self.gears}
        self._refresh_handles: list[asyncio.TimerHandle] = []

    # ------------------------------------------------------------ helpers
    @property
    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for g in self.gears:
            for grp in g.get("groups", []):
                out.setdefault(grp, []).append(g["sa"])
        return dict(sorted(out.items()))

    def strip_mode(self, strip: dict) -> str:
        return strip.get("mode", "rgbw" if "w" in strip["channels"] else "rgb")

    def strip_roles(self, strip: dict) -> tuple[str, ...]:
        if self.strip_mode(strip) == "rgbw" and "w" in strip["channels"]:
            return ("r", "g", "b", "w")
        return ("r", "g", "b")

    def strip_on_arcs(self, strip: dict, brightness: int) -> dict[int, int]:
        """Arc level per channel to light an OFF strip with its last colour.

        Unknown colour: RGBW -> only W, RGB -> white.
        """
        from .dali510 import default_strip_colour, rgb_brightness_to_arcs
        from .devices import strip_unique_id

        roles = self.strip_roles(strip)
        mem = self.strip_mem.get(strip_unique_id(strip)) or {}
        colour = mem.get("colour")
        if not colour or len(colour) != len(roles) or not any(colour):
            colour = default_strip_colour("rgbw" if len(roles) == 4 else "rgb")
        white = colour[3] if len(roles) == 4 else None
        arcs = rgb_brightness_to_arcs(tuple(colour[:3]), brightness, white=white)
        return {strip["channels"][role]: arcs[i] for i, role in enumerate(roles)}

    @callback
    def note_commanded(self, levels: dict[int, int]) -> None:
        """Remember the arc levels we just sent to strips (avoids re-learning
        our own rounding as a 'new' colour)."""
        from .devices import strip_unique_id

        for strip in self.strips:
            roles = self.strip_roles(strip)
            sas = [strip["channels"][r] for r in roles]
            if all(sa in levels for sa in sas):
                mem = self.strip_mem.setdefault(
                    strip_unique_id(strip), {"colour": None, "brightness": None}
                )
                mem["arcs"] = [levels[sa] for sa in sas]

    def plan_levels(
        self, members: list[int], target_bri: int, cur_bri: int
    ) -> dict[int, int]:
        """Per-address arc levels for a group/All 'on' that keeps strip colours.

        * strip that is ON  -> its channels scaled by target/cur (colour kept)
        * strip that is OFF -> lit with its last colour at target brightness
        * other gear        -> absolute level for target brightness
        """
        from .dali510 import arc_to_percent, brightness_to_arc, percent_to_arc

        data = self.data or {}
        member_set = set(members)
        plan: dict[int, int] = {}
        for strip in self.strips:
            chans = [sa for sa in strip["channels"].values() if sa in member_set]
            if not chans:
                continue
            strip_on = any((data.get(sa) or 0) > 0 for sa in strip["channels"].values())
            if strip_on:
                for sa in chans:
                    lv = data.get(sa) or 0
                    if 0 < lv != 0xFF and cur_bri > 0:
                        plan[sa] = percent_to_arc(
                            arc_to_percent(lv) * (target_bri / cur_bri)
                        )
                    else:
                        plan[sa] = 0
            else:
                arcs = self.strip_on_arcs(strip, target_bri)
                for sa in chans:
                    plan[sa] = arcs.get(sa, 0)
        arc = brightness_to_arc(target_bri)
        for sa in members:
            if sa not in plan:
                plan[sa] = arc
        return plan

    async def _run(
        self, func: Callable, *args: Any, _timeout: float = BUS_CALL_TIMEOUT_S, **kwargs: Any
    ) -> Any:
        """Run a blocking bus call in the dedicated thread, serialised.

        Never blocks the event loop; gives up waiting after _timeout seconds
        (the worker thread finishes on its own, later calls queue behind it).
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            fut = loop.run_in_executor(
                self._executor, functools.partial(func, *args, **kwargs)
            )
            try:
                return await asyncio.wait_for(asyncio.shield(fut), _timeout)
            except TimeoutError as err:
                raise Dali510Error(
                    f"510 did not finish {getattr(func, '__name__', 'call')} within {_timeout:.0f} s"
                ) from err

    async def _ensure_open(self) -> None:
        if not self.bus.is_open:
            await self._run(self.bus.open)

    async def async_open(self) -> None:
        await self._run(self.bus.open)
        _LOGGER.info(
            "Helvar 510 opened at %s (info reply %s)",
            self.bus.path,
            self.bus.info_reply.hex(" ") if self.bus.info_reply else None,
        )

    async def async_shutdown(self) -> None:
        for h in self._refresh_handles:
            h.cancel()
        self._refresh_handles.clear()
        try:
            await super().async_shutdown()
        except AttributeError:
            pass
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self.bus.close)
        except RuntimeError:
            self.bus.close()
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------------ polling
    async def _async_update_data(self) -> dict[int, int | None]:
        try:
            await self._ensure_open()
        except Dali510Error as err:
            raise UpdateFailed(f"510 not available: {err}") from err

        data = dict(self.data or {})
        errors = 0
        for gear in self.gears:
            sa = gear["sa"]
            try:
                level = await self._run(self.bus.query_short, sa, 0xA0)
            except Dali510Error as err:
                errors += 1
                _LOGGER.debug("poll sa %s failed: %s", sa, err)
                if not self.bus.is_open:
                    raise UpdateFailed(f"510 I/O error: {err}") from err
                continue
            if level is None:
                self.fail_count[sa] = self.fail_count.get(sa, 0) + 1
                if self.fail_count[sa] >= FAIL_LIMIT:
                    data[sa] = None
            else:
                self.fail_count[sa] = 0
                if level != 0xFF:  # MASK = fading/unknown -> keep previous
                    data[sa] = level
            await asyncio.sleep(POLL_STAGGER_S)
        if self.gears and errors == len(self.gears):
            raise UpdateFailed("no gear answered")
        return data

    def is_available(self, sa: int) -> bool:
        return self.fail_count.get(sa, 0) < FAIL_LIMIT

    async def async_refresh_addresses(self, sas: list[int]) -> None:
        data = dict(self.data or {})
        for sa in sas:
            try:
                level = await self._run(self.bus.query_short, sa, 0xA0)
            except Dali510Error as err:
                _LOGGER.debug("refresh sa %s failed: %s", sa, err)
                continue
            if level is not None and level != 0xFF:
                data[sa] = level
                self.fail_count[sa] = 0
            await asyncio.sleep(POLL_STAGGER_S)
        self.async_set_updated_data(data)

    @callback
    def schedule_refresh(self, sas: list[int], delay: float = 1.5) -> None:
        def _go() -> None:
            self.hass.async_create_task(self.async_refresh_addresses(list(sas)))

        self._refresh_handles = [h for h in self._refresh_handles if not h.cancelled()]
        self._refresh_handles.append(self.hass.loop.call_later(delay, _go))

    @callback
    def set_optimistic(self, sas: list[int], level: int | None) -> None:
        data = dict(self.data or {})
        for sa in sas:
            gear = self.gear_by_sa.get(sa, {})
            lvl = level
            if lvl is not None and lvl > 0:
                lo = gear.get("min_level") or 1
                hi = gear.get("max_level") or 254
                lvl = max(lo, min(hi, lvl))
            data[sa] = lvl
        self.async_set_updated_data(data)

    # ------------------------------------------------------------ commands
    async def async_dapc(self, addr_byte: int, level: int) -> Reply:
        await self._ensure_open()
        return await self._run(self.bus.dapc, addr_byte, level)

    async def async_command(self, addr_byte: int, cmd: int) -> Reply:
        await self._ensure_open()
        return await self._run(self.bus.command, addr_byte, cmd)

    async def async_dapc_many(self, items: list[tuple[int, int]]) -> list[Reply]:
        await self._ensure_open()
        return await self._run(self.bus.dapc_many, items)

    async def async_send_raw(self, addr: int, data: int, expect_reply: bool) -> Reply:
        await self._ensure_open()
        return await self._run(self.bus.send, addr, data, expect_reply)

    async def async_apply_entry(self) -> None:
        """Pick up changed gear data / strips / poll interval from the entry
        and re-layout entities + devices in place (no reload, no rescan)."""
        from .const import CONF_POLL_INTERVAL, CONF_STRIPS, DEFAULT_POLL_INTERVAL
        from .devices import strip_unique_id

        entry = self.entry
        self.gears = list(entry.data.get("gears", []))
        self.gear_by_sa = {g["sa"]: g for g in self.gears}
        self.strips = list(entry.options.get(CONF_STRIPS, entry.data.get(CONF_STRIPS, [])))
        for st in self.strips:
            self.strip_mem.setdefault(strip_unique_id(st), {"colour": None, "brightness": None})
        poll = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        self.update_interval = timedelta(seconds=max(5, int(poll)))
        data = dict(self.data or {})
        for g in self.gears:
            data.setdefault(g["sa"], g.get("actual"))
        self.data = data
        if self.apply_layout is not None:
            await self.apply_layout()

    async def async_set_group_membership(
        self, sa: int, group: int, add: bool, method: str = "auto"
    ) -> dict:
        """Explicit ADD/REMOVE GROUP for one short address + verify.

        Updates gear data in the config entry and re-lays out devices and
        entities in place - no rescan, no reload.
        """
        if sa not in self.gear_by_sa:
            raise Dali510Error(
                f"short address {sa} is not in the scanned gear list"
            )
        await self._ensure_open()
        result = await self._run(self.bus.set_group_membership, sa, group, add, method)
        groups = result.get("groups")
        if groups is not None and groups != list(self.gear_by_sa[sa].get("groups") or []):
            gears = []
            for g in self.entry.data.get("gears", []):
                g = dict(g)
                if g["sa"] == sa:
                    g["groups"] = list(groups)
                gears.append(g)
            result["entry_updated"] = True
            from .devices import async_apply_sa_visibility, visibility_state

            async_apply_sa_visibility(
                self.hass,
                self.entry,
                old=visibility_state(self.entry),
                new=visibility_state(self.entry, gears=gears),
            )
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, "gears": gears}
            )
            # Apply now so the service returns with devices/entities in place
            # (the update listener runs too; the layout lock makes it a no-op).
            await self.async_apply_entry()
        else:
            result["entry_updated"] = False
        return result

    async def async_scan(self) -> list[dict]:
        await self._ensure_open()
        return await self._run(self.bus.scan, _timeout=SCAN_TIMEOUT_S)
