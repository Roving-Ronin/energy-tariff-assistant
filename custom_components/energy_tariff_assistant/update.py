"""Plan updates from the library, shown under Settings → Updates. Install replaces the plan in place:
the entry, its name, entity IDs and history stay; only rates, times and caps change."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any
from urllib.parse import quote

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import TariffConfigEntry
from .const import (
    CONF_LIBRARY_FILE,
    CONF_PLAN,
    CONF_PLAN_SOURCE,
    CONF_SLUG,
    CONF_SOURCE,
    DOMAIN,
    PLAN_BROWSER_URL,
    SOURCE_LIBRARY,
)
from .plan_source import PlanError, async_fetch_index, async_fetch_library_plan, check_plan
from .runtime import TariffRuntime
from .updates import candidates, describe_changes, same_pricing, version_label

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0
CHECK_EVERY = timedelta(hours=12)
FIRST_CHECK = 60  # seconds after start: don't hold up Home Assistant starting


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TariffConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([PlanUpdate(entry.runtime_data)])


class PlanUpdate(UpdateEntity):
    """Whether the library has newer prices for this plan."""

    _attr_has_entity_name = True
    _attr_name = "Plan"
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES
    _attr_icon = "mdi:file-document-refresh-outline"

    def __init__(self, runtime: TariffRuntime) -> None:
        self.runtime = runtime
        entry = runtime.entry
        self._attr_unique_id = f"{entry.entry_id}_plan_update"
        self.entity_id = f"update.{entry.data[CONF_SLUG]}_plan"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        plan = entry.options[CONF_PLAN]
        self._attr_title = entry.title
        self._attr_installed_version = version_label(plan)
        self._attr_latest_version = self._attr_installed_version
        self._new: tuple[dict[str, Any], str] | None = None  # (plan, library file) to install
        self._notes: list[str] = []

    @property
    def _source(self) -> dict[str, Any]:
        return self.runtime.entry.options.get(CONF_PLAN_SOURCE) or {}

    async def async_added_to_hass(self) -> None:
        if self._source.get(CONF_SOURCE) != SOURCE_LIBRARY:
            return  # built, pasted or from a file: nothing in the library to follow
        self.async_on_remove(async_call_later(self.hass, FIRST_CHECK, self._async_check))
        self.async_on_remove(async_track_time_interval(self.hass, self._async_check, CHECK_EVERY))

    async def _async_check(self, _now: Any = None) -> None:
        try:
            await self.async_check()
        except PlanError as err:
            _LOGGER.debug("Plan update check for %s failed: %s", self.runtime.entry.title, err)

    async def async_check(self) -> None:
        """Look in the library for this plan's replacement and what it changes."""
        installed: dict[str, Any] = self.runtime.entry.options[CONF_PLAN]
        current_file = self._source.get(CONF_LIBRARY_FILE)
        target: str | None = None
        if current_file:
            try:
                library_copy = await async_fetch_library_plan(self.hass, current_file, keep_feedback=True)
            except PlanError:
                library_copy = {}
            if isinstance(replaced := library_copy.get("replaced_by"), str) and replaced.startswith("plans/"):
                target = replaced
        if target is None:
            index = await async_fetch_index(self.hass)
            newest = candidates(index, installed, dt_util.now().date().isoformat())
            target = newest[0]["file"] if newest else current_file
        if not target:
            return
        latest = await async_fetch_library_plan(self.hass, target)
        if target == current_file and same_pricing(installed, latest) and installed.get("plan_id") == latest.get(
            "plan_id"
        ):
            self._new, self._notes = None, []
            self._attr_latest_version = self._attr_installed_version
        else:
            self._new = (latest, target)
            self._notes = describe_changes(installed, latest) or ["Library copy revised (no price changes)"]
            label = version_label(latest)
            self._attr_latest_version = label if label != self._attr_installed_version else f"{label} · revised"
        self._attr_release_summary = "; ".join(self._notes)[:255] if self._new else None
        self._attr_release_url = f"{PLAN_BROWSER_URL}#/plan/{quote(target, safe='')}"
        self.async_write_ha_state()

    async def async_release_notes(self) -> str | None:
        if not self._notes:
            return None
        return "\n".join(f"- {line}" for line in self._notes)

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        if self._new is None:
            return
        plan, file = self._new
        # The same checks as importing it: a plan the validator refuses is never installed
        await self.hass.async_add_executor_job(check_plan, plan)
        entry = self.runtime.entry
        source = {**self._source, CONF_LIBRARY_FILE: file}
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PLAN: plan, CONF_PLAN_SOURCE: source}
        )
        self._new, self._notes = None, []
        await self.hass.config_entries.async_reload(entry.entry_id)
