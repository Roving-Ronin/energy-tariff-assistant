"""Energy Tariff Assistant: Australian tariff plans as price and forecast sensors.

Two kinds of entry. One per imported plan (its own price sensors, capped-rate usage, library updates), and one
"Active tariff" that points at the plan in use: `sensor.eta_grid_import_price` / `_export_price` follow it, so
dashboards and optimisers never change when the plan does.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigEntryState
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service
from homeassistant.helpers.typing import ConfigType

from . import wizard
from .active import ActiveRuntime, plan_entries, write_plan_files
from .const import (
    ACTIVE_UNIQUE_ID,
    ATTR_CONFIG_ENTRY,
    CONF_ACTIVE_ENTRY,
    CONF_BILLING_DAY,
    CONF_EXPORT_SENSOR,
    CONF_IMPORT_SENSOR,
    CONF_KIND,
    CONF_PLAN,
    CONF_SLUG,
    DOMAIN,
    KIND_ACTIVE,
    KIND_PLAN,
    ATTR_VALUE,
    SERVICE_CALIBRATE,
    SERVICE_SET_ACTIVE_PLAN,
    STREAM_EXPORT,
    STREAM_IMPORT,
)
from .plan_source import PlanError, check_plan
from .runtime import TariffRuntime

_LOGGER = logging.getLogger(__name__)

PLAN_PLATFORMS = [Platform.SENSOR, Platform.UPDATE]
ACTIVE_PLATFORMS = [Platform.SENSOR, Platform.SELECT]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type TariffConfigEntry = ConfigEntry[TariffRuntime | ActiveRuntime]

SET_ACTIVE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional("device_id"): vol.Any(cv.string, [cv.string]),
    }
)


def active_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """The Active tariff entry, if it exists."""
    return next(
        (e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(CONF_KIND) == KIND_ACTIVE), None
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""

    async def _set_active(call: ServiceCall) -> None:
        entry_id = call.data.get(ATTR_CONFIG_ENTRY)
        if not entry_id and (device_ids := call.data.get("device_id")):
            device_id = device_ids[0] if isinstance(device_ids, list) else device_ids
            device = dr.async_get(hass).async_get(device_id)
            entry_id = next(iter(device.config_entries), None) if device else None
        entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
        if entry is None or entry.domain != DOMAIN or entry.data.get(CONF_KIND, KIND_PLAN) != KIND_PLAN:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_plan")
        hub = active_entry(hass)
        if hub is None or hub.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_active_tariff")
        hub.runtime_data.async_set_active(entry.entry_id)

    hass.services.async_register(DOMAIN, SERVICE_SET_ACTIVE_PLAN, _set_active, schema=SET_ACTIVE_SCHEMA)
    async def _calibrate(entity: Any, call: ServiceCall) -> None:
        # Only Active Tariff's totals can be set; the other sensors are worked out from the plan
        if not hasattr(entity, "async_calibrate"):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_a_total")
        entity.async_calibrate(call.data[ATTR_VALUE])

    # Set one of Active Tariff's totals (to match a bill)
    service.async_register_platform_entity_service(
        hass, DOMAIN, SERVICE_CALIBRATE, entity_domain=SENSOR_DOMAIN,
        schema={vol.Required(ATTR_VALUE): vol.Coerce(float)}, func=_calibrate,
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Version 1 entries metered their own energy sensors; version 2 plan entries take them from Active tariff.

    The plan entry gets its kind and entity slug; its energy sensors stay in its options until Active tariff is
    created, which takes them from there (the first entry's, if several had them).
    """
    if entry.version == 1:
        plan = entry.options.get(CONF_PLAN, {})
        data = {**entry.data, CONF_KIND: KIND_PLAN, CONF_SLUG: wizard.entity_slug(plan)}
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TariffConfigEntry) -> bool:
    if entry.data.get(CONF_KIND) == KIND_ACTIVE:
        return await _async_setup_active(hass, entry)
    return await _async_setup_plan(hass, entry)


async def _async_setup_plan(hass: HomeAssistant, entry: TariffConfigEntry) -> bool:
    """Load the plan, count the grid energy toward its caps and add its entities."""
    try:
        # Validation and the resolver read time zone and holiday data from disk, so they run off the event loop.
        resolver = await hass.async_add_executor_job(check_plan, entry.options[CONF_PLAN])
    except PlanError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="invalid_plan",
            translation_placeholders={"detail": err.detail},
        ) from err

    hub = active_entry(hass)
    settings: dict[str, Any] = hub.options if hub else entry.options  # before Active tariff exists: v1 sensors
    sources = {STREAM_IMPORT: settings.get(CONF_IMPORT_SENSOR), STREAM_EXPORT: settings.get(CONF_EXPORT_SENSOR)}
    runtime = TariffRuntime(hass, entry, resolver, sources, int(settings.get(CONF_BILLING_DAY, 1) or 1))
    await runtime.async_start()
    entry.runtime_data = runtime

    @callback
    def _on_stop(_event: Event) -> None:
        hass.async_create_task(runtime.async_stop())

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))
    _remove_retired_entities(hass, entry, runtime)
    await hass.config_entries.async_forward_entry_setups(entry, PLAN_PLATFORMS)
    await hass.async_add_executor_job(
        write_plan_files, hass.config.config_dir, {f"{entry.data[CONF_SLUG]}.json": dict(entry.options[CONF_PLAN])}
    )

    if hub is None:
        # The first plan: create Active tariff, with this plan in use (and any v1 energy sensors it had)
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={
                    CONF_ACTIVE_ENTRY: entry.entry_id,
                    CONF_IMPORT_SENSOR: entry.options.get(CONF_IMPORT_SENSOR),
                    CONF_EXPORT_SENSOR: entry.options.get(CONF_EXPORT_SENSOR),
                    CONF_BILLING_DAY: entry.options.get(CONF_BILLING_DAY, 1),
                },
            )
        )
    elif isinstance(active := getattr(hub, "runtime_data", None), ActiveRuntime) and (
        active.active_entry_id == entry.entry_id
    ):
        # The plan in use (re)loaded, perhaps after Active tariff started: follow it now
        active.async_follow()
    return True


def _remove_retired_entities(hass: HomeAssistant, entry: ConfigEntry, runtime: TariffRuntime) -> None:
    """Drop entities this version no longer makes (v1 meters, period and bill sensors; removed capped periods),
    and move the price sensors to the plan's entity IDs."""
    registry = er.async_get(hass)
    slug = entry.data[CONF_SLUG]
    keep = {f"{entry.entry_id}_{s}_price" for s in (STREAM_IMPORT, STREAM_EXPORT)}
    keep |= {f"{entry.entry_id}_plan_update"}
    keep |= {
        f"{entry.entry_id}_{s}_{p.id}_used" for s in (STREAM_IMPORT, STREAM_EXPORT) for p in runtime.capped_periods(s)
    }
    keep |= {f"{entry.entry_id}_demand_{c.id}_peak" for c in runtime.demand_charges()}
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.unique_id not in keep:
            registry.async_remove(item.entity_id)
            continue
        if item.unique_id.endswith("_price"):
            stream = item.unique_id.removeprefix(f"{entry.entry_id}_").removesuffix("_price")
            wanted = f"sensor.{slug}_{stream}_price"
            if item.entity_id != wanted and registry.async_get(wanted) is None:
                registry.async_update_entity(item.entity_id, new_entity_id=wanted)


async def _async_setup_active(hass: HomeAssistant, entry: TariffConfigEntry) -> bool:
    runtime = ActiveRuntime(hass, entry)
    entry.runtime_data = runtime
    await runtime.async_start()

    @callback
    def _on_stop(_event: Event) -> None:
        hass.async_create_task(runtime.async_stop())

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))
    entry.async_on_unload(entry.add_update_listener(_async_active_updated))
    await hass.config_entries.async_forward_entry_setups(entry, ACTIVE_PLATFORMS)
    return True


async def _async_active_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Active tariff's options changed. A new plan in use needs nothing more (the runtime follows it); new
    energy sensors or billing day reload every plan entry, which counts them."""
    runtime: ActiveRuntime = entry.runtime_data
    settings = runtime.current_settings()
    if settings != runtime.settings:
        runtime.settings = settings
        for plan in plan_entries(hass):
            if plan.state is ConfigEntryState.LOADED:
                hass.async_create_task(hass.config_entries.async_reload(plan.entry_id))
    runtime.async_follow()


async def async_unload_entry(hass: HomeAssistant, entry: TariffConfigEntry) -> bool:
    kind = entry.data.get(CONF_KIND, KIND_PLAN)
    platforms = ACTIVE_PLATFORMS if kind == KIND_ACTIVE else PLAN_PLATFORMS
    unloaded = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unloaded:
        await entry.runtime_data.async_stop()
        if kind == KIND_PLAN and (hub := active_entry(hass)) and hub.state is ConfigEntryState.LOADED:
            hub.runtime_data.async_follow()  # its sensors go unavailable until the plan is back
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A plan removed: delete its plan file; if it was in use, Active tariff shows no plan (never picks another).
    Active tariff removed: its totals go with it."""
    if entry.data.get(CONF_KIND) == KIND_ACTIVE:
        return
    if slug := entry.data.get(CONF_SLUG):
        await hass.async_add_executor_job(write_plan_files, hass.config.config_dir, {f"{slug}.json": None})
    hub = active_entry(hass)
    if hub and hub.options.get(CONF_ACTIVE_ENTRY) == entry.entry_id:
        if hub.state is ConfigEntryState.LOADED:
            hub.runtime_data.async_set_active(None)
        else:
            hass.config_entries.async_update_entry(hub, options={**hub.options, CONF_ACTIVE_ENTRY: None})


__all__ = ["ACTIVE_UNIQUE_ID", "TariffConfigEntry", "active_entry"]
