"""Config and options flows: pick a plan, then the energy sensors to meter."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.loader import async_get_integration

from .const import (
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
    library_label,
    parse_plan_text,
    read_plan_file,
)
from .share import build_share_link, plan_title

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
    """Energy sensors and billing day, with current values as suggestions."""

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


class PlanSteps:
    """Library / paste / file steps shared by the config and options flows."""

    _index: list[dict[str, Any]] | None = None

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
        self, step_id: str, schema: vol.Schema, err: PlanError | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors={"base": err.key} if err else None,
            description_placeholders={"detail": err.detail if err else ""},
        )

    def _go_to_source(self, source: str):
        return {
            SOURCE_LIBRARY: self.async_step_library,
            SOURCE_PASTE: self.async_step_paste,
            SOURCE_FILE: self.async_step_file,
        }[source]()

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
        options = [
            selector.SelectOptionDict(value=entry["file"], label=library_label(entry))
            for entry in sorted(self._index, key=library_label)
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_LIBRARY_FILE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        if user_input is None:
            return self._plan_form("library", schema)
        file = user_input[CONF_LIBRARY_FILE]
        try:
            plan = await async_fetch_library_plan(self.hass, file)
        except PlanError as err:
            return self._plan_form("library", schema, err)
        return await self._async_check(
            plan, {CONF_SOURCE: SOURCE_LIBRARY, CONF_LIBRARY_FILE: file}, "library", schema
        )

    async def async_step_paste(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        schema = vol.Schema(
            {
                vol.Required(CONF_PLAN_JSON): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                )
            }
        )
        if user_input is None:
            return self._plan_form("paste", schema)
        try:
            plan = parse_plan_text(user_input[CONF_PLAN_JSON])
        except PlanError as err:
            return self._plan_form("paste", schema, err)
        return await self._async_check(plan, {CONF_SOURCE: SOURCE_PASTE}, "paste", schema)

    async def async_step_file(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        schema = vol.Schema({vol.Required(CONF_PLAN_PATH): selector.TextSelector()})
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


class EnergyTariffConfigFlow(PlanSteps, ConfigFlow, domain=DOMAIN):
    """Set up a tariff plan for one metering point."""

    VERSION = 1

    def __init__(self) -> None:
        self._name: str = ""
        self._plan: dict[str, Any] = {}
        self._source: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._name = user_input[CONF_NAME].strip()
            return await self._go_to_source(user_input[CONF_SOURCE])
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default="Electricity"): selector.TextSelector(),
                    vol.Required(CONF_SOURCE, default=SOURCE_LIBRARY): SOURCE_SELECTOR,
                }
            ),
        )

    async def _async_plan_ready(
        self, plan: dict[str, Any], source: dict[str, Any]
    ) -> ConfigFlowResult:
        self._plan = plan
        self._source = source
        return await self.async_step_meters()

    async def async_step_meters(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data={},
                options={
                    CONF_PLAN: self._plan,
                    CONF_PLAN_SOURCE: self._source,
                    **clean_meters(user_input),
                },
            )
        return self.async_show_form(
            step_id="meters",
            data_schema=meters_schema({}),
            description_placeholders={
                "plan": f"{self._plan.get('retailer')} {self._plan.get('plan')}"
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EnergyTariffOptionsFlow:
        return EnergyTariffOptionsFlow()


class EnergyTariffOptionsFlow(PlanSteps, OptionsFlowWithReload):
    """Change the meters or replace the plan."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["meters", "plan", "share"])

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

    async def async_step_meters(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={**self.config_entry.options, **clean_meters(user_input)}
            )
        return self.async_show_form(
            step_id="meters", data_schema=meters_schema(dict(self.config_entry.options))
        )

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
