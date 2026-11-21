"""Config and options flows: import a plan (library, build, paste, file); Active tariff's settings."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .active import plan_entries
from .const import (
    ACTIVE_TITLE,
    ACTIVE_UNIQUE_ID,
    CONF_ACTIVE_ENTRY,
    CONF_KIND,
    CONF_SLUG,
    KIND_ACTIVE,
    KIND_PLAN,
    CONF_BILLING_DAY,
    CONF_EXPORT_SENSOR,
    CONF_IMPORT_SENSOR,
    CONF_LIBRARY_FILE,
    CONF_PLAN,
    CONF_PLAN_JSON,
    CONF_PLAN_PATH,
    CONF_PLAN_SOURCE,
    CONF_SOURCE,
    DOMAIN,
    BUILT_PLANS_DIR,
    PLAN_BROWSER_URL,
    SOURCE_BUILD,
    SOURCE_FILE,
    SOURCE_LIBRARY,
    SOURCE_PASTE,
    SOURCES,
)
from .plan_source import (
    PlanError,
    async_fetch_index,
    async_fetch_library_plan,
    check_plan,
    parse_plan_text,
    read_plan_file,
    write_plan_file,
)
from . import wizard
from .share import build_share_link, plan_title
from .wizard import (
    AFTER_CAP_RATE,
    COUNTRIES,
    CUSTOMER_TYPES,
    DEFAULT_CUSTOMER_TYPE,
    DAY_CHOICES,
    DEFAULT_COUNTRY,
    DEFAULT_REGION,
    DNSPS,
    REGIONS,
    TIER_RESETS,
    Draft,
    DraftPeriod,
)

SOURCE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=SOURCES, translation_key=CONF_SOURCE, mode=selector.SelectSelectorMode.LIST
    )
)
ENERGY_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class=SensorDeviceClass.ENERGY)
)
BILLING_DAY = selector.NumberSelector(
    selector.NumberSelectorConfig(min=1, max=28, step=1, mode=selector.NumberSelectorMode.BOX)
)


def meters_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Active tariff's grid energy sensors and billing day, with current values as suggestions."""

    def suggested(key: str) -> dict[str, Any]:
        value = defaults.get(key)
        return {"suggested_value": value} if value else {}

    return vol.Schema(
        {
            vol.Optional(CONF_IMPORT_SENSOR, description=suggested(CONF_IMPORT_SENSOR)): ENERGY_SENSOR,
            vol.Optional(CONF_EXPORT_SENSOR, description=suggested(CONF_EXPORT_SENSOR)): ENERGY_SENSOR,
            vol.Required(CONF_BILLING_DAY, default=defaults.get(CONF_BILLING_DAY, 1)): BILLING_DAY,
        }
    )


def clean_meters(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_IMPORT_SENSOR: user_input.get(CONF_IMPORT_SENSOR) or None,
        CONF_EXPORT_SENSOR: user_input.get(CONF_EXPORT_SENSOR) or None,
        CONF_BILLING_DAY: int(user_input[CONF_BILLING_DAY]),
    }


NONE_DNSP = "__none__"
TOU = "tou"
FLAT = "flat"
STREAMS = ("import", "export")
DEFAULT_NAMES = {"import": ("Anytime", "Shoulder"), "export": ("Feed-in", "Standard FiT")}

RATE = selector.NumberSelector(
    selector.NumberSelectorConfig(min=-5, max=10, step="any", mode=selector.NumberSelectorMode.BOX, unit_of_measurement="$/kWh")
)
KWH = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=100000, step="any", mode=selector.NumberSelectorMode.BOX, unit_of_measurement="kWh")
)
TEXT = selector.TextSelector()
TIME = selector.TimeSelector()


def _select(options: list[str] | list[selector.SelectOptionDict], key: str | None = None, *, custom: bool = False, multiple: bool = False) -> selector.SelectSelector:
    config = selector.SelectSelectorConfig(
        options=options,
        custom_value=custom,
        multiple=multiple,
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
    if key:
        config["translation_key"] = key
    return selector.SelectSelector(config)


def _opts(values: list[str]) -> list[selector.SelectOptionDict]:
    return [selector.SelectOptionDict(value=v, label=v) for v in values]


class PlanSteps:
    """Plan source steps shared by the config and options flows.

    Library (country → state → DNSP → retailer → plan), build step by step,
    paste, or a file in the config folder.
    """

    _index: list[dict[str, Any]] | None = None
    _lib: dict[str, str]
    _entries: list[dict[str, Any]]
    _draft: Draft
    _modes: dict[str, str]

    async def _async_plan_ready(
        self, plan: dict[str, Any], source: dict[str, Any]
    ) -> ConfigFlowResult:
        raise NotImplementedError

    async def _async_check(
        self, plan: dict[str, Any], source: dict[str, Any], step_id: str, schema: vol.Schema
    ) -> ConfigFlowResult:
        try:
            await self.hass.async_add_executor_job(check_plan, plan)
        except PlanError as err:
            return self._plan_form(step_id, schema, err)
        return await self._async_plan_ready(plan, source)

    def _plan_form(
        self, step_id: str, schema: vol.Schema, err: PlanError | None = None, **placeholders: str
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors={"base": err.key} if err else None,
            description_placeholders={"detail": err.detail if err else "", **placeholders},
        )

    def _go_to_source(self, source: str):
        return {
            SOURCE_LIBRARY: self.async_step_library,
            SOURCE_BUILD: self.async_step_build,
            SOURCE_PASTE: self.async_step_paste,
            SOURCE_FILE: self.async_step_file,
        }[source]()

    # ----- library: country → state → DNSP → retailer → plan -------------------

    async def async_step_library(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._index is None:
            try:
                self._index = await async_fetch_index(self.hass)
            except PlanError as err:
                return self.async_abort(
                    reason="cannot_connect", description_placeholders={"detail": err.detail}
                )
        if not self._index:
            return self.async_abort(reason="library_empty")
        self._lib = {}
        # Selector option keys must be lowercase: "au" in the form, "AU" in plans
        values = [c.lower() for c in wizard.countries(self._index)]
        if user_input is not None:
            self._lib["country"] = user_input["country"].upper()
            # Later steps only offer what matches the customer type
            self._entries = wizard.current(wizard.for_customer(self._index, user_input["customer_type"]),
                                           dt_util.now().date().isoformat())
            return await self.async_step_library_region()
        default = wizard.pick_default(values, DEFAULT_COUNTRY.lower())
        return self.async_show_form(
            step_id="library",
            data_schema=vol.Schema(
                {
                    vol.Required("customer_type", default=DEFAULT_CUSTOMER_TYPE): _select(
                        list(CUSTOMER_TYPES), "customer_type"
                    ),
                    vol.Required("country", default=default): _select(values, "country"),
                }
            ),
            description_placeholders={"browser_url": PLAN_BROWSER_URL},
        )

    async def async_step_library_region(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        country = self._lib["country"]
        if user_input is not None:
            self._lib["region"] = user_input["region"]
            return await self.async_step_library_dnsp()
        values = wizard.regions(self._entries, country)
        if not values:
            return self.async_abort(reason="library_no_plans")
        default = wizard.pick_default(values, DEFAULT_REGION.get(country))
        return self.async_show_form(
            step_id="library_region",
            data_schema=vol.Schema({vol.Required("region", default=default): _select(_opts(values))}),
            description_placeholders={"country": country},
        )

    async def async_step_library_dnsp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            value = user_input["dnsp"]
            self._lib["dnsp"] = "" if value == NONE_DNSP else value
            return await self.async_step_library_retailer()
        values = wizard.dnsps(self._entries, self._lib["country"], self._lib["region"])
        options = [
            selector.SelectOptionDict(value=v or NONE_DNSP, label=v or "-") for v in values
        ]
        return self.async_show_form(
            step_id="library_dnsp",
            data_schema=vol.Schema(
                {vol.Required("dnsp", default=options[0]["value"]): _select(options)}
            ),
            description_placeholders={"region": self._lib["region"]},
        )

    async def async_step_library_retailer(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._lib["retailer"] = user_input["retailer"]
            return await self.async_step_library_plan()
        values = wizard.retailers(
            self._entries, self._lib["country"], self._lib["region"], self._lib["dnsp"]
        )
        return self.async_show_form(
            step_id="library_retailer",
            data_schema=vol.Schema({vol.Required("retailer", default=values[0]): _select(_opts(values))}),
            description_placeholders={"region": self._lib["region"], "dnsp": self._lib["dnsp"] or "-"},
        )

    async def async_step_library_plan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entries = wizard.plans_newest_first(wizard.filter_entries(self._entries, **self._lib))
        options = [
            selector.SelectOptionDict(value=e["file"], label=wizard.plan_label(e)) for e in entries
        ]
        schema = vol.Schema(
            {vol.Required(CONF_LIBRARY_FILE, default=options[0]["value"]): _select(options)}
        )
        placeholders = {"retailer": self._lib["retailer"]}
        if user_input is None:
            return self._plan_form("library_plan", schema, **placeholders)
        file = user_input[CONF_LIBRARY_FILE]
        try:
            plan = await async_fetch_library_plan(self.hass, file)
        except PlanError as err:
            return self._plan_form("library_plan", schema, err, **placeholders)
        try:
            await self.hass.async_add_executor_job(check_plan, plan)
        except PlanError as err:
            return self._plan_form("library_plan", schema, err, **placeholders)
        return await self._async_plan_ready(
            plan, {CONF_SOURCE: SOURCE_LIBRARY, CONF_LIBRARY_FILE: file}
        )

    # ----- paste / file ---------------------------------------------------------

    async def async_step_paste(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        schema = vol.Schema(
            {vol.Required(CONF_PLAN_JSON): selector.TextSelector(selector.TextSelectorConfig(multiline=True))}
        )
        if user_input is None:
            return self._plan_form("paste", schema)
        try:
            plan = parse_plan_text(user_input[CONF_PLAN_JSON])
        except PlanError as err:
            return self._plan_form("paste", schema, err)
        return await self._async_check(plan, {CONF_SOURCE: SOURCE_PASTE}, "paste", schema)

    async def async_step_file(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        schema = vol.Schema({vol.Required(CONF_PLAN_PATH): TEXT})
        if user_input is None:
            return self._plan_form("file", schema)
        path = user_input[CONF_PLAN_PATH]
        try:
            plan = await self.hass.async_add_executor_job(
                read_plan_file, self.hass.config.config_dir, path
            )
        except PlanError as err:
            return self._plan_form("file", schema, err)
        return await self._async_check(
            plan, {CONF_SOURCE: SOURCE_FILE, CONF_PLAN_PATH: path}, "file", schema
        )

    # ----- build step by step ---------------------------------------------------

    async def async_step_build(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Country and state (the next step's DNSP list depends on them)."""
        if user_input is not None:
            self._draft = Draft(country=user_input["country"].upper(), region=user_input["region"])
            return await self.async_step_build_details()
        country = DEFAULT_COUNTRY
        return self.async_show_form(
            step_id="build",
            data_schema=vol.Schema(
                {
                    vol.Required("country", default=country.lower()): _select(
                        [c.lower() for c in COUNTRIES], "country"
                    ),
                    vol.Required("region", default=DEFAULT_REGION[country]): _select(
                        _opts(list(REGIONS[country]))
                    ),
                }
            ),
        )

    async def async_step_build_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        draft = self._draft
        if user_input is not None:
            draft.dnsp = user_input.get("dnsp", "")
            draft.retailer = user_input["retailer"]
            draft.plan = user_input["plan_name"]
            draft.plan_id = user_input.get("plan_id", "")
            draft.effective_from = user_input["effective_from"]
            draft.supply_charge = float(user_input["supply_charge"])
            draft.customer_type = user_input["customer_type"]
            self._modes = {"import": user_input["import_type"], "export": user_input["export_type"]}
            return await self._async_build_stream("import")
        today = dt_util.now().date().isoformat()
        dnsps = list(DNSPS.get((draft.country, draft.region), ()))
        return self.async_show_form(
            step_id="build_details",
            data_schema=vol.Schema(
                {
                    vol.Optional("dnsp", description={"suggested_value": dnsps[0] if dnsps else None}): _select(
                        _opts(dnsps), custom=True
                    ),
                    vol.Required("retailer"): TEXT,
                    vol.Required("plan_name"): TEXT,
                    vol.Optional("plan_id"): TEXT,
                    vol.Required("effective_from", default=today): selector.DateSelector(),
                    vol.Required("supply_charge"): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=20, step="any", mode=selector.NumberSelectorMode.BOX, unit_of_measurement="$/day"
                        )
                    ),
                    vol.Required("customer_type", default=DEFAULT_CUSTOMER_TYPE): _select(
                        list(CUSTOMER_TYPES), "customer_type"
                    ),
                    vol.Required("import_type", default=TOU): _select([FLAT, TOU], "rate_type"),
                    vol.Required("export_type", default=FLAT): _select([FLAT, TOU], "rate_type"),
                }
            ),
            description_placeholders={"region": draft.region},
        )

    async def _async_build_stream(self, stream: str) -> ConfigFlowResult:
        step = "flat" if self._modes[stream] == FLAT else "default"
        return await getattr(self, f"async_step_build_{stream}_{step}")()

    def _cap_fields(self, stream: str, with_periods: bool) -> dict[Any, Any]:
        fields: dict[Any, Any] = {vol.Optional("cap_kwh"): KWH}
        if with_periods:
            choices = [selector.SelectOptionDict(value=AFTER_CAP_RATE, label="$")]
            choices += [
                selector.SelectOptionDict(value=pid, label=name)
                for pid, name in self._draft.streams[stream].choices()
            ]
            fields[vol.Optional("after_cap", default=AFTER_CAP_RATE)] = _select(choices)
        fields[vol.Optional("rate_after_cap")] = RATE
        fields[vol.Optional("cap_reset", default="day")] = _select(list(TIER_RESETS), "cap_reset")
        return fields

    @staticmethod
    def _cap_errors(user_input: dict[str, Any]) -> dict[str, str]:
        if not user_input.get("cap_kwh"):
            return {}
        after = user_input.get("after_cap", AFTER_CAP_RATE)
        if after == AFTER_CAP_RATE and user_input.get("rate_after_cap") is None:
            return {"rate_after_cap": "rate_after_cap_required"}
        return {}

    @staticmethod
    def _window_errors(user_input: dict[str, Any]) -> dict[str, str]:
        if user_input["days"] == "custom" and not user_input.get("custom_days"):
            return {"custom_days": "days_required"}
        if str(user_input["start"])[:5] == str(user_input["end"])[:5]:
            return {"end": "times_equal"}
        return {}

    def _period_from(self, user_input: dict[str, Any], default: bool) -> DraftPeriod:
        cap = user_input.get("cap_kwh") or None
        return DraftPeriod(
            name=user_input["name"].strip(),
            rate=float(user_input["rate"]),
            default=default,
            cap_kwh=float(cap) if cap else None,
            after_cap=user_input.get("after_cap", AFTER_CAP_RATE) if cap else None,
            rate_after_cap=user_input.get("rate_after_cap"),
            cap_reset=user_input.get("cap_reset", "day"),
        )

    async def _build_default(self, stream: str, flat: bool, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        """The default period: 'all the time' (flat) or 'all other times' (time of use)."""
        step_id = f"build_{stream}_{'flat' if flat else 'default'}"
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._cap_errors(user_input)
            if not errors:
                self._draft.streams[stream] = wizard.DraftStream()
                self._draft.streams[stream].add(self._period_from(user_input, default=True))
                if flat:
                    return await self._async_after_stream(stream)
                return await getattr(self, f"async_step_build_{stream}_period")()
        name = DEFAULT_NAMES[stream][0 if flat else 1]
        schema = vol.Schema(
            {
                vol.Required("name", default=name): TEXT,
                vol.Required("rate"): RATE,
                **self._cap_fields(stream, with_periods=False),
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def _build_period(self, stream: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        """A period with its first time window."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._window_errors(user_input) | self._cap_errors(user_input)
            if not errors:
                period = self._period_from(user_input, default=False)
                period.windows.append(
                    wizard.window(user_input["days"], user_input.get("custom_days"), user_input["start"], user_input["end"])
                )
                self._draft.streams[stream].add(period)
                return await getattr(self, f"async_step_build_{stream}_more")()
        schema = vol.Schema(
            {
                vol.Required("name"): TEXT,
                vol.Required("rate"): RATE,
                **self._window_fields(),
                **self._cap_fields(stream, with_periods=True),
            }
        )
        return self.async_show_form(
            step_id=f"build_{stream}_period",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    @staticmethod
    def _window_fields() -> dict[Any, Any]:
        return {
            vol.Required("days", default="all"): _select(list(DAY_CHOICES), "days"),
            vol.Optional("custom_days"): _select(list(wizard.DAY_NAMES), "day", multiple=True),
            vol.Required("start"): TIME,
            vol.Required("end"): TIME,
        }

    async def _build_window(self, stream: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        """Another time window for the period entered last."""
        period = self._draft.streams[stream].periods[-1]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._window_errors(user_input)
            if not errors:
                period.windows.append(
                    wizard.window(user_input["days"], user_input.get("custom_days"), user_input["start"], user_input["end"])
                )
                return await getattr(self, f"async_step_build_{stream}_more")()
        return self.async_show_form(
            step_id=f"build_{stream}_window",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(self._window_fields()), user_input or {}),
            errors=errors,
            description_placeholders={"period": period.name},
        )

    def _build_more(self, stream: str) -> ConfigFlowResult:
        stream_draft = self._draft.streams[stream]
        last = stream_draft.periods[-1]
        return self.async_show_menu(
            step_id=f"build_{stream}_more",
            menu_options=[f"build_{stream}_window", f"build_{stream}_period", f"build_{stream}_done"],
            description_placeholders={
                "period": last.name,
                "periods": ", ".join(p.name for p in stream_draft.periods),
            },
        )

    async def _async_after_stream(self, stream: str) -> ConfigFlowResult:
        if stream == "import":
            return await self._async_build_stream("export")
        return await self.async_step_build_review()

    # Thin per-stream wrappers: each step id needs its own method and strings.
    async def async_step_build_import_flat(self, user_input=None):
        return await self._build_default("import", True, user_input)

    async def async_step_build_import_default(self, user_input=None):
        return await self._build_default("import", False, user_input)

    async def async_step_build_import_period(self, user_input=None):
        return await self._build_period("import", user_input)

    async def async_step_build_import_window(self, user_input=None):
        return await self._build_window("import", user_input)

    async def async_step_build_import_more(self, user_input=None):
        return self._build_more("import")

    async def async_step_build_import_done(self, user_input=None):
        return await self._async_after_stream("import")

    async def async_step_build_export_flat(self, user_input=None):
        return await self._build_default("export", True, user_input)

    async def async_step_build_export_default(self, user_input=None):
        return await self._build_default("export", False, user_input)

    async def async_step_build_export_period(self, user_input=None):
        return await self._build_period("export", user_input)

    async def async_step_build_export_window(self, user_input=None):
        return await self._build_window("export", user_input)

    async def async_step_build_export_more(self, user_input=None):
        return self._build_more("export")

    async def async_step_build_export_done(self, user_input=None):
        return await self._async_after_stream("export")

    async def async_step_build_review(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the plan; save it to a file and use it, or start again."""
        plan = wizard.assemble_plan(self._draft, dt_util.now().date())
        filename = wizard.plan_filename(plan)
        schema = vol.Schema(
            {vol.Required("action", default="save"): _select(["save", "start_over"], "review_action")}
        )
        placeholders = {"summary": wizard.summary(plan), "file": f"{BUILT_PLANS_DIR}/{filename}"}
        if user_input is None:
            return self._plan_form("build_review", schema, **placeholders)
        if user_input["action"] == "start_over":
            return await self.async_step_build()
        try:
            await self.hass.async_add_executor_job(check_plan, plan)
            path = await self.hass.async_add_executor_job(
                write_plan_file, self.hass.config.config_dir, filename, plan
            )
        except PlanError as err:
            return self._plan_form("build_review", schema, err, **placeholders)
        return await self._async_plan_ready(plan, {CONF_SOURCE: SOURCE_BUILD, CONF_PLAN_PATH: path})


class EnergyTariffConfigFlow(PlanSteps, ConfigFlow, domain=DOMAIN):
    """Import a tariff plan. The first one also creates Active tariff (from `async_step_import`)."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._go_to_source(user_input[CONF_SOURCE])
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_SOURCE, default=SOURCE_LIBRARY): SOURCE_SELECTOR}),
        )

    async def _async_plan_ready(
        self, plan: dict[str, Any], source: dict[str, Any]
    ) -> ConfigFlowResult:
        return self.async_create_entry(
            title=wizard.display_name(plan),
            data={CONF_KIND: KIND_PLAN, CONF_SLUG: wizard.entity_slug(plan)},
            options={CONF_PLAN: plan, CONF_PLAN_SOURCE: source},
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Active tariff, created by the first plan entry with that plan in use."""
        await self.async_set_unique_id(ACTIVE_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=ACTIVE_TITLE,
            data={CONF_KIND: KIND_ACTIVE},
            options={
                CONF_ACTIVE_ENTRY: import_data.get(CONF_ACTIVE_ENTRY),
                CONF_IMPORT_SENSOR: import_data.get(CONF_IMPORT_SENSOR) or None,
                CONF_EXPORT_SENSOR: import_data.get(CONF_EXPORT_SENSOR) or None,
                CONF_BILLING_DAY: int(import_data.get(CONF_BILLING_DAY) or 1),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EnergyTariffOptionsFlow | ActiveTariffOptionsFlow:
        if config_entry.data.get(CONF_KIND) == KIND_ACTIVE:
            return ActiveTariffOptionsFlow()
        return EnergyTariffOptionsFlow()


class ActiveTariffOptionsFlow(OptionsFlow):
    """The plan in use, and the grid energy sensors every plan counts toward its caps."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_active()

    async def async_step_active(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        plans = plan_entries(self.hass)
        if user_input is not None:
            return self.async_create_entry(
                data={**options, CONF_ACTIVE_ENTRY: user_input[CONF_ACTIVE_ENTRY], **clean_meters(user_input)}
            )
        choices = [selector.SelectOptionDict(value=e.entry_id, label=e.title) for e in plans]
        current = options.get(CONF_ACTIVE_ENTRY)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ACTIVE_ENTRY, default=current if any(e.entry_id == current for e in plans) else vol.UNDEFINED
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=choices, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
            }
        ).extend(meters_schema(options).schema)
        return self.async_show_form(step_id="active", data_schema=schema)


class EnergyTariffOptionsFlow(PlanSteps, OptionsFlowWithReload):
    """Replace the plan, or share it with the library."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["plan", "share"])

    async def async_step_share(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show a pre-filled link to the plan submission form on GitHub.

        Nothing is sent from Home Assistant: the user opens the link, checks
        the form and submits it from their own GitHub account.
        """
        if user_input is not None:
            return self.async_abort(reason="share_done")
        options = self.config_entry.options
        plan = options[CONF_PLAN]
        integration = await async_get_integration(self.hass, DOMAIN)
        link = build_share_link(plan, options.get(CONF_PLAN_SOURCE), str(integration.version))
        placeholders = {"plan": plan_title(plan), "url": link.url}
        if link.plan_in_link:
            return self.async_show_form(
                step_id="share", data_schema=vol.Schema({}), description_placeholders=placeholders
            )
        # Too long for a link: show the JSON to copy into the form by hand.
        return self.async_show_form(
            step_id="share_paste",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PLAN_JSON, description={"suggested_value": link.plan_json}
                    ): selector.TextSelector(selector.TextSelectorConfig(multiline=True))
                }
            ),
            description_placeholders=placeholders,
        )

    async def async_step_share_paste(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason="share_done")

    async def async_step_plan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._go_to_source(user_input[CONF_SOURCE])
        current = self.config_entry.options.get(CONF_PLAN_SOURCE, {}).get(CONF_SOURCE, SOURCE_LIBRARY)
        return self.async_show_form(
            step_id="plan",
            data_schema=vol.Schema({vol.Required(CONF_SOURCE, default=current): SOURCE_SELECTOR}),
        )

    async def _async_plan_ready(
        self, plan: dict[str, Any], source: dict[str, Any]
    ) -> ConfigFlowResult:
        return self.async_create_entry(
            data={**self.config_entry.options, CONF_PLAN: plan, CONF_PLAN_SOURCE: source}
        )
