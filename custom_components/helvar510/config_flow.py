"""Config and options flow for Helvar DIGIDIM 510."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_HIDE_GROUP_MEMBERS,
    CONF_HIDE_STRIP_CHANNELS,
    CONF_NAME_PREFIX,
    CONF_PATH,
    CONF_POLL_INTERVAL,
    CONF_STRIPS,
    DEFAULT_HIDE_GROUP_MEMBERS,
    DEFAULT_HIDE_STRIP_CHANNELS,
    DEFAULT_NAME_PREFIX,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    DT_NAMES,
    ENTRY_MINOR_VERSION,
    ENTRY_VERSION,
)
from .devices import (
    async_apply_sa_visibility,
    visibility_state,
    resolve_strip_unique_id,
    role_order_uid,
    strip_unique_id,
)
from .dali510 import (
    Dali510,
    Dali510Error,
    find_510,
    list_hidraw,
    suggest_multichannel,
)

STEP_USER = "user"
STEP_SCAN = "scan"
STEP_STRIPS = "strips"
STEP_CONFIRM = "confirm"


def _device_choices() -> dict[str, str]:
    found = find_510()
    if found:
        return {
            d.path: f"{d.path} — {d.name or '510 Digidim Interface'}"
            + (f" (S/N {d.serial})" if d.serial else "")
            for d in found
        }
    # Still offer "auto" + any hidraw for troubleshooting
    choices = {"auto": "Automatic (16eb:0510)"}
    for d in list_hidraw():
        label = f"{d.path} vid={d.vid:04x} pid={d.pid:04x}" if d.vid is not None else d.path
        if d.name:
            label += f" — {d.name}"
        choices[d.path] = label
    return choices


def _gear_label(g: dict) -> str:
    dt = g.get("device_type")
    dt_name = DT_NAMES.get(dt, f"DT{dt}") if dt is not None else "?"
    groups = g.get("groups") or []
    gtxt = f", G{','.join(map(str, groups))}" if groups else ""
    return f"SA {g['sa']} — {dt_name}{gtxt}"


def _strip_schema(gears: list[dict], suggestion: list[int] | None = None) -> vol.Schema:
    options = [
        {"value": str(g["sa"]), "label": _gear_label(g)} for g in gears
    ]
    sug = suggestion or []
    first = str(gears[0]["sa"]) if gears else "0"
    defaults = {
        "name": (f"Strip {'-'.join(map(str, sug))}" if sug else "RGB strip"),
        "mode": "rgbw" if len(sug) >= 4 else "rgb",
        "r": str(sug[0]) if len(sug) > 0 else first,
        "g": str(sug[1]) if len(sug) > 1 else first,
        "b": str(sug[2]) if len(sug) > 2 else first,
        "w": str(sug[3]) if len(sug) > 3 else "",
    }
    return vol.Schema(
        {
            vol.Required("name", default=defaults["name"]): str,
            vol.Required("mode", default=defaults["mode"]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["rgb", "rgbw"],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="strip_mode",
                )
            ),
            vol.Required("r", default=defaults["r"]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode="dropdown")
            ),
            vol.Required("g", default=defaults["g"]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode="dropdown")
            ),
            vol.Required("b", default=defaults["b"]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode="dropdown")
            ),
            vol.Optional("w", default=defaults["w"]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[{"value": "", "label": "-"}] + options,
                    mode="dropdown",
                )
            ),
            vol.Required("add_another", default=False): bool,
        }
    )


def _normalize_strip(user: dict) -> dict:
    mode = user["mode"]
    channels = {
        "r": int(user["r"]),
        "g": int(user["g"]),
        "b": int(user["b"]),
    }
    w = user.get("w")
    if mode == "rgbw":
        if not w:
            raise vol.Invalid("RGBW mode needs a white channel")
        channels["w"] = int(w)
    vals = list(channels.values())
    if len(set(vals)) != len(vals):
        raise vol.Invalid("the same short address is selected for several channels")
    strip = {"name": user["name"].strip() or "RGB strip", "mode": mode, "channels": channels}
    strip["unique_id"] = role_order_uid(strip)
    return strip


def _strip_label(s: dict) -> str:
    ch = s["channels"]
    roles = "/".join(f"{r.upper()}={ch[r]}" for r in ("r", "g", "b", "w") if r in ch)
    return f"{s.get('name')} ({s.get('mode', 'rgb')}: {roles})"


class Helvar510ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = ENTRY_VERSION
    MINOR_VERSION = ENTRY_MINOR_VERSION

    def __init__(self) -> None:
        self._path: str = "auto"
        self._poll: int = DEFAULT_POLL_INTERVAL
        self._gears: list[dict] = []
        self._suggestions: list[list[int]] = []
        self._strips: list[dict] = []
        self._sug_idx = 0
        self._hide_channels = DEFAULT_HIDE_STRIP_CHANNELS
        self._hide_groups = DEFAULT_HIDE_GROUP_MEMBERS

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        choices = await self.hass.async_add_executor_job(_device_choices)
        if user_input is not None:
            self._path = user_input[CONF_PATH]
            self._poll = int(user_input[CONF_POLL_INTERVAL])
            self._hide_channels = bool(user_input.get(CONF_HIDE_STRIP_CHANNELS, True))
            self._hide_groups = bool(
                user_input.get(CONF_HIDE_GROUP_MEMBERS, DEFAULT_HIDE_GROUP_MEMBERS)
            )
            try:
                gears = await self.hass.async_add_executor_job(self._do_scan, self._path)
            except Dali510Error as err:
                errors["base"] = "cannot_connect"
                self.context["last_error"] = str(err)
            else:
                if not gears:
                    errors["base"] = "no_gears"
                else:
                    self._gears = gears
                    self._suggestions = suggest_multichannel(gears)
                    return await self.async_step_strips()

        schema = vol.Schema(
            {
                vol.Required(CONF_PATH, default=next(iter(choices))): vol.In(choices),
                vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(5, 300)
                ),
                vol.Required(
                    CONF_HIDE_STRIP_CHANNELS, default=DEFAULT_HIDE_STRIP_CHANNELS
                ): bool,
                vol.Required(
                    CONF_HIDE_GROUP_MEMBERS, default=DEFAULT_HIDE_GROUP_MEMBERS
                ): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    def _do_scan(self, path: str) -> list[dict]:
        bus = Dali510(path)
        try:
            bus.open()
            return bus.scan()
        finally:
            bus.close()

    async def async_step_strips(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        # Skip offered when no suggestions and user wants to finish early
        if user_input is not None:
            if user_input.get("skip"):
                return await self.async_step_confirm()
            try:
                strip = _normalize_strip(user_input)
            except vol.Invalid as err:
                errors["base"] = "invalid_strip"
                self.context["last_error"] = str(err)
            else:
                used = {c for s in self._strips for c in s["channels"].values()}
                if used.intersection(strip["channels"].values()):
                    errors["base"] = "strip_overlap"
                else:
                    self._strips.append(strip)
                    if user_input.get("add_another"):
                        self._sug_idx += 1
                        return await self.async_step_strips()
                    return await self.async_step_confirm()

        suggestion = (
            self._suggestions[self._sug_idx]
            if self._sug_idx < len(self._suggestions)
            else None
        )
        # Filter suggestions already used
        used = {c for s in self._strips for c in s["channels"].values()}
        while suggestion and any(sa in used for sa in suggestion):
            self._sug_idx += 1
            suggestion = (
                self._suggestions[self._sug_idx]
                if self._sug_idx < len(self._suggestions)
                else None
            )

        placeholders = {
            "count": str(len(self._gears)),
            "suggestions": str(len(self._suggestions)),
            "suggestion": ", ".join(map(str, suggestion)) if suggestion else "-",
        }

        schema = _strip_schema(self._gears, suggestion)
        # Add skip toggle
        schema = vol.Schema({**schema.schema, vol.Optional("skip", default=False): bool})
        return self.async_show_form(
            step_id="strips",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            await self.async_set_unique_id(f"{DOMAIN}-{self._path}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="DALI USB (Helvar 510)",
                data={
                    CONF_PATH: self._path,
                    "gears": self._gears,
                    CONF_STRIPS: self._strips,
                    CONF_NAME_PREFIX: DEFAULT_NAME_PREFIX,
                },
                options={
                    CONF_POLL_INTERVAL: self._poll,
                    CONF_STRIPS: self._strips,
                    CONF_HIDE_STRIP_CHANNELS: self._hide_channels,
                    CONF_HIDE_GROUP_MEMBERS: self._hide_groups,
                },
            )

        types: dict[str, int] = {}
        for g in self._gears:
            name = DT_NAMES.get(g.get("device_type"), str(g.get("device_type")))
            types[name] = types.get(name, 0) + 1
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "count": str(len(self._gears)),
                "types": ", ".join(f"{n}: {c}" for n, c in types.items()) or "-",
                "strips": str(len(self._strips)),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        return Helvar510OptionsFlow()


class Helvar510OptionsFlow(config_entries.OptionsFlow):
    def __init__(self) -> None:
        self._strips: list[dict] = []
        self._gears: list[dict] = []
        self._suggestions: list[list[int]] = []
        self._sug_idx = 0
        self._poll = DEFAULT_POLL_INTERVAL
        self._hide = DEFAULT_HIDE_STRIP_CHANNELS
        self._hide_groups = DEFAULT_HIDE_GROUP_MEMBERS
        self._pending_action = ""
        self._edit_idx = 0

    def _ensure_state(self) -> None:
        if self._gears:
            return
        entry = self.config_entry
        self._strips = list(
            entry.options.get(CONF_STRIPS, entry.data.get(CONF_STRIPS, []))
        )
        self._gears = list(entry.data.get("gears", []))
        self._suggestions = suggest_multichannel(self._gears)
        self._sug_idx = 0
        self._poll = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        self._hide = entry.options.get(
            CONF_HIDE_STRIP_CHANNELS, DEFAULT_HIDE_STRIP_CHANNELS
        )
        self._hide_groups = entry.options.get(
            CONF_HIDE_GROUP_MEMBERS, DEFAULT_HIDE_GROUP_MEMBERS
        )

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        self._ensure_state()
        if user_input is not None:
            self._poll = int(user_input[CONF_POLL_INTERVAL])
            self._hide = bool(user_input[CONF_HIDE_STRIP_CHANNELS])
            self._hide_groups = bool(
                user_input.get(CONF_HIDE_GROUP_MEMBERS, self._hide_groups)
            )
            action = user_input.get("action", "save")
            if action == "add_strip":
                return await self.async_step_add_strip()
            if action in ("edit_strip", "delete_strip"):
                if not self._strips:
                    return await self.async_step_init()
                self._pending_action = action
                return await self.async_step_select_strip()
            if action == "clear_strips":
                self._strips = []
            if action == "rescan":
                return await self.async_step_rescan()
            return self._save()

        strip_txt = ", ".join(_strip_label(s) for s in self._strips) or "-"
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLL_INTERVAL, default=self._poll): vol.All(
                        vol.Coerce(int), vol.Range(5, 300)
                    ),
                    vol.Required(CONF_HIDE_STRIP_CHANNELS, default=self._hide): bool,
                    vol.Required(CONF_HIDE_GROUP_MEMBERS, default=self._hide_groups): bool,
                    vol.Required("action", default="save"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                "save", "add_strip", "edit_strip",
                                "delete_strip", "clear_strips", "rescan",
                            ],
                            mode="dropdown",
                            translation_key="options_action",
                        )
                    ),
                }
            ),
            description_placeholders={"strips": strip_txt},
        )

    async def async_step_add_strip(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("skip"):
                return await self.async_step_init()
            try:
                strip = _normalize_strip(user_input)
            except vol.Invalid as err:
                errors["base"] = "invalid_strip"
                self.context["last_error"] = str(err)
            else:
                used = {c for s in self._strips for c in s["channels"].values()}
                if used.intersection(strip["channels"].values()):
                    errors["base"] = "strip_overlap"
                else:
                    strip["unique_id"] = resolve_strip_unique_id(
                        self.hass, self.config_entry, strip,
                        used={strip_unique_id(x) for x in self._strips},
                    )
                    self._strips.append(strip)
                    if user_input.get("add_another"):
                        self._sug_idx += 1
                        return await self.async_step_add_strip()
                    return await self.async_step_init()

        suggestion = (
            self._suggestions[self._sug_idx]
            if self._sug_idx < len(self._suggestions)
            else None
        )
        schema = _strip_schema(self._gears, suggestion)
        schema = vol.Schema({**schema.schema, vol.Optional("skip", default=False): bool})
        return self.async_show_form(
            step_id="add_strip", data_schema=schema, errors=errors
        )

    def _save(self) -> FlowResult:
        entry = self.config_entry
        async_apply_sa_visibility(
            self.hass,
            entry,
            old=visibility_state(entry),
            new=visibility_state(
                entry,
                strips=self._strips,
                hide_strips=self._hide,
                hide_groups=self._hide_groups,
            ),
        )
        return self.async_create_entry(
            title="",
            data={
                CONF_POLL_INTERVAL: self._poll,
                CONF_STRIPS: self._strips,
                CONF_HIDE_STRIP_CHANNELS: self._hide,
                CONF_HIDE_GROUP_MEMBERS: self._hide_groups,
            },
        )

    async def async_step_select_strip(self, user_input: dict | None = None) -> FlowResult:
        self._ensure_state()
        if user_input is not None:
            self._edit_idx = int(user_input["strip"])
            if self._pending_action == "delete_strip":
                del self._strips[self._edit_idx]
                return self._save()
            return await self.async_step_edit_strip()
        return self.async_show_form(
            step_id="select_strip",
            data_schema=vol.Schema(
                {
                    vol.Required("strip", default="0"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": str(i), "label": _strip_label(s)}
                                for i, s in enumerate(self._strips)
                            ],
                            mode="dropdown",
                        )
                    )
                }
            ),
        )

    async def async_step_edit_strip(self, user_input: dict | None = None) -> FlowResult:
        """Edit name / mode / channel roles. unique_id (and device) stay."""
        errors: dict[str, str] = {}
        current = self._strips[self._edit_idx]
        if user_input is not None:
            try:
                strip = _normalize_strip({**user_input, "add_another": False})
            except vol.Invalid as err:
                errors["base"] = "invalid_strip"
                self.context["last_error"] = str(err)
            else:
                others = [s for i, s in enumerate(self._strips) if i != self._edit_idx]
                used = {c for s in others for c in s["channels"].values()}
                if used.intersection(strip["channels"].values()):
                    errors["base"] = "strip_overlap"
                else:
                    strip["unique_id"] = strip_unique_id(current)  # stable id
                    self._strips[self._edit_idx] = strip
                    return self._save()
        ch = current["channels"]
        schema = _strip_schema(self._gears, None)
        defaults = {
            "name": current.get("name") or "",
            "mode": current.get("mode", "rgbw" if "w" in ch else "rgb"),
            "r": str(ch.get("r", "")),
            "g": str(ch.get("g", "")),
            "b": str(ch.get("b", "")),
            "w": str(ch["w"]) if "w" in ch else "",
        }
        if user_input is not None:
            defaults.update({k: user_input.get(k, v) for k, v in defaults.items()})
        fields = {}
        for key, validator in schema.schema.items():
            name = str(key)
            if name in ("add_another",):
                continue
            cls = vol.Required if isinstance(key, vol.Required) else vol.Optional
            fields[cls(name, default=defaults.get(name, ""))] = validator
        return self.async_show_form(
            step_id="edit_strip",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"strip": _strip_label(current)},
        )

    async def async_step_rescan(self, user_input: dict | None = None) -> FlowResult:
        # Use the running coordinator's bus so only one program talks to the 510
        try:
            coord = self.config_entry.runtime_data
            gears = await coord.async_scan()
        except (AttributeError, Dali510Error):
            return self.async_abort(reason="cannot_connect")
        # Update entry data with new gear list (options flow can't always mutate data;
        # store gears snapshot in options too and let coordinator merge on reload)
        self._gears = gears
        self._suggestions = suggest_multichannel(gears)
        # Persist gears via hass.config_entries.async_update_entry
        new_data = {**self.config_entry.data, "gears": gears}
        async_apply_sa_visibility(
            self.hass,
            self.config_entry,
            old=visibility_state(self.config_entry),
            new=visibility_state(self.config_entry, gears=gears),
        )
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
        return await self.async_step_init()


def _rescan(path: str) -> list[dict]:
    bus = Dali510(path)
    try:
        bus.open()
        return bus.scan()
    finally:
        bus.close()
