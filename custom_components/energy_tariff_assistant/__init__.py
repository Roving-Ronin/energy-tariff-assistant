"""Energy Tariff Assistant: electricity tariff plans with built-in per-rate metering."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_BUCKET,
    ATTR_CONFIG_ENTRY,
    CONF_PLAN,
    DOMAIN,
    EXPOSED_BUCKETS,
    SERVICE_RESET_METERS,
)
from .plan_source import PlanError, check_plan
from .runtime import TariffRuntime

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type TariffConfigEntry = ConfigEntry[TariffRuntime]

RESET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY): cv.string,
        vol.Required(ATTR_BUCKET): vol.In(EXPOSED_BUCKETS),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""

    async def _reset(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY])
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_entry"
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_loaded"
            )
        entry.runtime_data.async_reset(call.data[ATTR_BUCKET])

    hass.services.async_register(DOMAIN, SERVICE_RESET_METERS, _reset, schema=RESET_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TariffConfigEntry) -> bool:
    """Load the plan, start metering and add the entities."""
    try:
        # Validation and the resolver read time zone and holiday data from
        # disk, so they run off the event loop.
        resolver = await hass.async_add_executor_job(check_plan, entry.options[CONF_PLAN])
    except PlanError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="invalid_plan",
            translation_placeholders={"detail": err.detail},
        ) from err

    runtime = TariffRuntime(hass, entry, resolver)
    await runtime.async_start()
    entry.runtime_data = runtime

    @callback
    def _on_stop(_event: Event) -> None:
        hass.async_create_task(runtime.async_stop())

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TariffConfigEntry) -> bool:
    """Stop metering, save the ledgers and remove the entities."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
