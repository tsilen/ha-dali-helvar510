"""Coordinator: owns the 510, serialises bus I/O and polls actual levels."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import functools
import logging
import time
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
# Delay before a commanded address is read back. Every new command to the
# same address postpones it (a held dimmer button sends a step about every
# second), but never beyond REFRESH_MAX_DEFER_S after the first request.
REFRESH_DELAY_S = 1.5
REFRESH_MAX_DEFER_S = 6.0
# A commanded level stays authoritative while the gear reports a running
# fade (QUERY STATUS bit 4), at most this long (DALI fade time max 90.5 s).
PENDING_MAX_S = 95.0
FADE_RECHECK_S = 2.0
STATUS_FADE_RUNNING = 0x10
# Post-write verification: a level that settled (no fade running) at
# something other than commanded is written again at most this many times.
VERIFY_RETRIES = 2
VERIFY_DELAY_S = 1.0
# Readbacks / polls wait while commands are flowing (held dimmer button):
# the bus (1200 baud, ~25-40 ms per frame) is better spent on the writes.
QUIET_S = 1.0
QUIET_MAX_WAIT_S = 10.0
# Per-address writes are sent in transactions of this many frames so that
# other bus users can get in between.
WRITE_CHUNK = 8
CMD_QUERY_STATUS = 0x90
CMD_QUERY_ACTUAL_LEVEL = 0xA0


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
        self._refresh_sas: set[int] = set()
        self._refresh_first: float | None = None
        # Commanded level per short address that the bus has not confirmed
        # yet: sa -> (level, time.monotonic() of the command)
        self._pending: dict[int, tuple[int, float]] = {}
        # Bumped on every command per short address; a readback whose query
        # started before a newer command is stale and dropped.
        self._cmd_seq: dict[int, int] = {}
        # sa -> (command seq, level): the readback that settled command `seq`
        # showed a level other than commanded (gear clamped it, a frame was
        # lost, the gear does not follow). That level is the gear's answer
        # to our command, not a change made by someone else.
        self._settled: dict[int, tuple[int, int]] = {}
        # Bumped on every write to an address (also verification rewrites);
        # a readback whose query started before a write is stale.
        self._gen: dict[int, int] = {}
        self._retries: dict[int, int] = {}
        self._last_write = 0.0
        # Serialised per-address write queue: sa -> (level, is_rewrite);
        # latest wins while not yet sent.
        self._wq: dict[int, tuple[int, bool]] = {}
        self._wq_waiters: list[asyncio.Future] = []
        self._wq_gens: dict[int, int] = {}  # rewrite -> gen it was decided at
        self._writer: asyncio.Task | None = None
        # frame counters (diagnostics / tests)
        self.stats: dict[str, int] = {"dapc_frames": 0, "group_frames": 0,
                                      "queries": 0, "rewrites": 0, "skipped": 0}

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

    def plan_levels(self, members: list[int], target_bri: int) -> dict[int, int]:
        """Per-address arc levels for a group / DALI All brightness.

        A group or DALI All is one light: setting its brightness sets every
        member to that brightness.
        * strip  -> its colour (the current one, kept normalised in the
                    strip memory) at target brightness, so its brightest
                    channel is at the target - at 255 the strip is at full
        * other gear -> the arc level for the target brightness
        (0.3.2 and older scaled strips relative to the brightest member, so a
        strip never got brighter while other lights were at full, and one
        member stuck at full kept dimming only the strips.)
        """
        from .dali510 import brightness_to_arc

        member_set = set(members)
        plan: dict[int, int] = {}
        for strip in self.strips:
            chans = [sa for sa in strip["channels"].values() if sa in member_set]
            if not chans:
                continue
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
            if getattr(self, "_closed", False):
                raise Dali510Error("integration is shutting down")
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
        self._refresh_handles = []
        self._refresh_sas.clear()
        if self._writer is not None and not self._writer.done():
            self._writer.cancel()
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

        updates: dict[int, int | None] = {}
        fading: list[int] = []
        errors = 0
        rewrites: dict[int, int] = {}
        read_gen: dict[int, int] = {}
        for gear in self.gears:
            sa = gear["sa"]
            await self._wait_quiet()
            try:
                result, level = await self._read_actual(sa)
                read_gen[sa] = self._gen.get(sa, 0)
            except Dali510Error as err:
                errors += 1
                _LOGGER.debug("poll sa %s failed: %s", sa, err)
                if not self.bus.is_open:
                    raise UpdateFailed(f"510 I/O error: {err}") from err
                continue
            if result == "silent":
                self.fail_count[sa] = self.fail_count.get(sa, 0) + 1
                if self.fail_count[sa] >= FAIL_LIMIT:
                    updates[sa] = None
            else:
                self.fail_count[sa] = 0
                if result == "ok":
                    updates[sa] = level
                elif result == "fading":
                    fading.append(sa)
                elif result == "rewrite":
                    rewrites[sa] = level
            await asyncio.sleep(POLL_STAGGER_S)
        if self.gears and errors == len(self.gears):
            raise UpdateFailed("no gear answered")
        if fading:
            self.schedule_refresh(fading, FADE_RECHECK_S)
        if rewrites:
            self._start_rewrite(rewrites)
        # Merge into the *current* data: commands sent while this poll was
        # running must not be overwritten by the snapshot taken at its start,
        # nor by a level read before a newer write (a poll takes many seconds).
        return {**(self.data or {}), **self._fresh(updates, read_gen)}

    async def _read_actual(self, sa: int) -> tuple[str, int | None]:
        """QUERY ACTUAL LEVEL of one address, guarded against stale values.

        Returns (result, level):
          "ok"     -> level is the real level, use it,
          "silent" -> no answer,
          "keep"   -> MASK answer (unknown), keep the previous value,
          "fading" -> differs from what we commanded and the gear says a
                      fade is running: keep the commanded target, re-check,
          "stale"  -> a newer write was sent while querying: drop it,
          "rewrite"-> settled (no fade) at something other than commanded:
                      level = the commanded level to write again.
        Only queries (0xA0 / 0x90) are sent.
        """
        gen = self._gen.get(sa, 0)
        seq = self._cmd_seq.get(sa, 0)
        self.stats["queries"] += 1
        level = await self._run(self.bus.query_short, sa, CMD_QUERY_ACTUAL_LEVEL)
        if level is None:
            return "silent", None
        if level == 0xFF:  # MASK = unknown -> keep previous
            return "keep", None
        pending = self._pending.get(sa)
        if pending is not None and level != pending[0]:
            if time.monotonic() - pending[1] < PENDING_MAX_S:
                self.stats["queries"] += 1
                status = await self._run(self.bus.query_short, sa, CMD_QUERY_STATUS)
                if self._gen.get(sa, 0) != gen:
                    return "stale", None
                if status is None or status & STATUS_FADE_RUNNING:
                    return "fading", None
            if self._gen.get(sa, 0) != gen:
                return "stale", None
            tries = self._retries.get(sa, 0)
            if tries < VERIFY_RETRIES:
                # lost / ignored frame: write the commanded level again
                self._retries[sa] = tries + 1
                return "rewrite", pending[0]
            _LOGGER.warning(
                "DALI short address %s is at level %s, commanded %s "
                "(still different after %s rewrites)",
                sa, level, pending[0], tries,
            )
        if self._gen.get(sa, 0) != gen:
            return "stale", None
        if pending is not None and self._pending.get(sa) is pending:
            # confirmed, or the gear's own answer to the command
            self._pending.pop(sa, None)
            self._retries.pop(sa, None)
            if level != pending[0]:
                self._settled[sa] = (seq, level)
        return "ok", level

    async def _wait_quiet(self) -> None:
        """Hold readbacks back while writes are flowing (bounded)."""
        waited = 0.0
        while waited < QUIET_MAX_WAIT_S and self._busy():
            await asyncio.sleep(0.2)
            waited += 0.2

    def _busy(self) -> bool:
        return bool(self._wq) or (time.monotonic() - self._last_write) < QUIET_S

    @callback
    def _start_rewrite(self, levels: dict[int, int]) -> None:
        # only while nothing newer was written to the address since the
        # readback decided (a rewrite must never send an old target)
        gens = {sa: self._gen.get(sa, 0) for sa in levels}
        self.stats["rewrites"] += len(levels)
        _LOGGER.debug("rewriting %s (level not taken)", levels)

        async def _go() -> None:
            todo = {
                sa: lvl for sa, lvl in levels.items()
                if self._gen.get(sa, 0) == gens[sa]
                and (self._pending.get(sa) or (None,))[0] == lvl
            }
            if not todo:
                return
            try:
                await self.async_write_levels(todo, rewrite=True, gens=gens)
            except Dali510Error as err:
                _LOGGER.debug("rewrite failed: %s", err)
            self.schedule_refresh(list(todo), VERIFY_DELAY_S)

        self.hass.async_create_task(_go())

    def command_seq(self, sa: int) -> int:
        return self._cmd_seq.get(sa, 0)

    def settled_level(self, sa: int, seq: int) -> int | None:
        """Level the bus settled at for command `seq` if it differed."""
        got = self._settled.get(sa)
        if got is not None and got[0] == seq:
            return got[1]
        return None

    def is_available(self, sa: int) -> bool:
        return self.fail_count.get(sa, 0) < FAIL_LIMIT

    async def async_refresh_addresses(self, sas: list[int], *, defer: bool = True) -> None:
        updates: dict[int, int | None] = {}
        fading: list[int] = []
        rewrites: dict[int, int] = {}
        read_gen: dict[int, int] = {}
        sas = list(sas)
        for i, sa in enumerate(sas):
            if defer and self._busy():
                # commands are flowing: read the rest back once they stop
                self._refresh_first = None
                self.schedule_refresh(sas[i:], QUIET_S)
                break
            try:
                result, level = await self._read_actual(sa)
                read_gen[sa] = self._gen.get(sa, 0)
            except Dali510Error as err:
                _LOGGER.debug("refresh sa %s failed: %s", sa, err)
                continue
            if result == "ok":
                updates[sa] = level
                self.fail_count[sa] = 0
            elif result == "fading":
                fading.append(sa)
            elif result == "rewrite":
                rewrites[sa] = level
            await asyncio.sleep(POLL_STAGGER_S)
        if fading:
            self.schedule_refresh(fading, FADE_RECHECK_S)
        if rewrites:
            self._start_rewrite(rewrites)
        # merge into the current data (never write back an old snapshot):
        # a level read before a write that went out since (the readback was
        # cut short by the next brightness step) is dropped
        updates = self._fresh(updates, read_gen)
        if updates:
            self.async_set_updated_data({**(self.data or {}), **updates})

    def _fresh(
        self, updates: dict[int, int | None], read_gen: dict[int, int]
    ) -> dict[int, int | None]:
        out = {
            sa: lvl for sa, lvl in updates.items()
            if read_gen.get(sa, self._gen.get(sa, 0)) == self._gen.get(sa, 0)
        }
        if len(out) != len(updates):
            self.stats["stale_dropped"] = self.stats.get("stale_dropped", 0) + (
                len(updates) - len(out)
            )
            _LOGGER.debug(
                "readback dropped (written since): %s",
                sorted(set(updates) - set(out)),
            )
        return out

    @callback
    def schedule_refresh(self, sas: list[int], delay: float = REFRESH_DELAY_S) -> None:
        """Read the given addresses back after `delay` seconds.

        Requests are coalesced: a new request postpones the pending one (so a
        held dimmer button does not cause a readback between every step),
        but at most REFRESH_MAX_DEFER_S after the first request.
        """
        now = time.monotonic()
        if self._refresh_first is None:
            self._refresh_first = now
        self._refresh_sas.update(sas)
        delay = max(0.0, min(delay, self._refresh_first + REFRESH_MAX_DEFER_S - now))
        for h in self._refresh_handles:
            h.cancel()

        def _go() -> None:
            todo = sorted(self._refresh_sas)
            self._refresh_sas.clear()
            self._refresh_first = None
            self._refresh_handles = []
            if todo:
                self.hass.async_create_task(self.async_refresh_addresses(todo))

        self._refresh_handles = [self.hass.loop.call_later(delay, _go)]

    def clamp_levels(self, levels: dict[int, int | None]) -> dict[int, int | None]:
        """Levels as the gear will really take them (min/max level clamp)."""
        out: dict[int, int | None] = {}
        for sa, lvl in levels.items():
            if lvl is not None and lvl > 0:
                gear = self.gear_by_sa.get(sa, {})
                lo = gear.get("min_level") or 1
                hi = gear.get("max_level") or 254
                lvl = max(lo, min(hi, min(254, lvl)))
            elif lvl is not None:
                lvl = 0
            out[sa] = lvl
        return out

    @callback
    def commit_levels(
        self, levels: dict[int, int | None], *, rewrite: bool = False, notify: bool = True
    ) -> dict[int, int | None]:
        """Record levels just written to the bus and show them immediately.

        The commanded level stays authoritative until the bus confirms it (or
        the gear finished fading to something else), so a following
        brightness_step is computed from the target and not from a stale or
        mid-fade readback.
        """
        clamped = self.clamp_levels(levels)
        now = time.monotonic()
        self._last_write = now
        for sa, lvl in clamped.items():
            self._gen[sa] = self._gen.get(sa, 0) + 1
            if not rewrite:
                # a new command (a rewrite keeps the command it repeats)
                self._cmd_seq[sa] = self._cmd_seq.get(sa, 0) + 1
                self._settled.pop(sa, None)
                self._retries.pop(sa, None)
            if lvl is None:
                self._pending.pop(sa, None)
            else:
                self._pending[sa] = (lvl, now)
        if not rewrite:
            self.note_commanded({sa: v for sa, v in clamped.items() if v is not None})
        if notify:
            self.async_set_updated_data({**(self.data or {}), **clamped})
        else:
            self.data = {**(self.data or {}), **clamped}
        return clamped

    @callback
    def set_optimistic(self, sas: list[int], level: int | None) -> None:
        self.commit_levels({sa: level for sa in sas})

    # ------------------------------------------------------------ commands
    async def async_dapc(self, addr_byte: int, level: int) -> Reply:
        """Group / broadcast / short DAPC, after queued per-address writes."""
        await self._ensure_open()
        await self._async_flush_writes()
        self.stats["group_frames"] += 1
        self._last_write = time.monotonic()
        return await self._run(self.bus.dapc, addr_byte, level)

    async def async_command(self, addr_byte: int, cmd: int) -> Reply:
        await self._ensure_open()
        await self._async_flush_writes()
        self.stats["group_frames"] += 1
        self._last_write = time.monotonic()
        return await self._run(self.bus.command, addr_byte, cmd)

    async def async_dapc_many(self, items: list[tuple[int, int]]) -> list[Reply]:
        await self._ensure_open()
        await self._async_flush_writes()
        self.stats["dapc_frames"] += len(items)
        self._last_write = time.monotonic()
        return await self._run(self.bus.dapc_many, items)

    # ------------------------------------------------------------ write queue
    async def async_write_levels(
        self, levels: dict[int, int], *, rewrite: bool = False,
        gens: dict[int, int] | None = None,
    ) -> None:
        """Write per-address arc levels (0 = OFF) through the serialised queue.

        Levels for an address that is still queued are replaced (latest
        wins), so concurrent / rapid commands do not pile up on the slow bus.
        Returns once the levels (or newer ones) were sent and committed.
        """
        if not levels:
            return
        await self._ensure_open()
        for sa, lvl in levels.items():
            prev = self._wq.get(sa)
            # a rewrite never replaces a newer real command
            if rewrite and prev is not None and not prev[1]:
                continue
            self._wq[sa] = (int(lvl), rewrite)
            if rewrite and gens is not None:
                self._wq_gens[sa] = gens.get(sa, 0)
            else:
                self._wq_gens.pop(sa, None)
        fut = asyncio.get_running_loop().create_future()
        self._wq_waiters.append(fut)
        if self._writer is None or self._writer.done():
            self._writer = self.hass.async_create_background_task(
                self._async_writer(), "helvar510 writer"
            )
        await fut

    async def _async_flush_writes(self) -> None:
        writer = self._writer
        if writer is not None and not writer.done():
            await asyncio.shield(writer)

    async def _async_writer(self) -> None:
        while self._wq:
            batch, self._wq = self._wq, {}
            gens, self._wq_gens = self._wq_gens, {}
            waiters, self._wq_waiters = self._wq_waiters, []
            data = self.data or {}
            send: dict[int, tuple[int, bool]] = {}
            for sa, (lvl, rewrite) in batch.items():
                clamped = self.clamp_levels({sa: lvl})[sa]
                if rewrite and (
                    (sa in gens and self._gen.get(sa, 0) != gens[sa])
                    or (self._pending.get(sa) or (None,))[0] != clamped
                ):
                    continue  # superseded by a newer write
                if (not rewrite and sa not in self._pending
                        and data.get(sa) is not None and data.get(sa) == clamped):
                    # bus already confirmed at this level: no frame needed
                    self.stats["skipped"] += 1
                    continue
                send[sa] = (lvl, rewrite)
            err: BaseException | None = None
            items = sorted(send.items())
            try:
                for i in range(0, len(items), WRITE_CHUNK):
                    chunk = items[i : i + WRITE_CHUNK]
                    self.stats["dapc_frames"] += len(chunk)
                    self._last_write = time.monotonic()
                    await self._run(
                        self.bus.dapc_many,
                        [(sa << 1, lvl) for sa, (lvl, _r) in chunk],
                    )
                    self.commit_levels(
                        {sa: lvl for sa, (lvl, r) in chunk if not r}, notify=False
                    )
                    self.commit_levels(
                        {sa: lvl for sa, (lvl, r) in chunk if r}, rewrite=True, notify=False
                    )
            except (Dali510Error, asyncio.CancelledError) as e:  # noqa: PERF203
                err = e
            for fut in waiters:
                if fut.done():
                    continue
                if err is None:
                    fut.set_result(None)
                elif isinstance(err, asyncio.CancelledError):
                    fut.cancel()
                else:
                    fut.set_exception(err)
            # after the waiting entities ran (they remember what they set)
            asyncio.get_running_loop().call_soon(self.async_update_listeners)
            if isinstance(err, asyncio.CancelledError):
                raise err

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
