"""Active tariff: choose the plan in use."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntry, ConfigEntryChange
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TariffConfigEntry
from .active import ActiveRuntime, plan_entries
from .const import ACTIVE_TITLE, ACTIVE_UNIQUE_ID, DOMAIN, signal_active

PARALLEL_UPDATES = 0
NO_PLAN = "No active plan"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TariffConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ActivePlanSelect(entry.runtime_data)])


class ActivePlanSelect(SelectEntity):
    """The plan in use, chosen from the imported plans (by their names in Home Assistant)."""

    _attr_has_entity_name = False  # named on its own, like the grid sensors
    _attr_should_poll = False
    _attr_name = "Grid - Active Tariff Plan"
    _attr_icon = "mdi:list-status"

    def __init__(self, runtime: ActiveRuntime) -> None:
        self.runtime = runtime
        self._attr_unique_id = f"{ACTIVE_UNIQUE_ID}_active_plan"
        self.entity_id = "select.eta_grid_active_tariff_plan"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ACTIVE_UNIQUE_ID)},
            name=ACTIVE_TITLE,
            manufacturer="Energy Tariff Assistant",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._by_name: dict[str, str] = {}
        self.hass = runtime.hass
        self._update()  # options are read before the entity is added

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, signal_active(), self._async_refresh))
        # Plans added, renamed or removed change the options
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._async_entries_changed)
        )
        self._update()

    @callback
    def _async_entries_changed(self, _change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if entry.domain == DOMAIN:
            self._async_refresh()

    @callback
    def _async_refresh(self) -> None:
        self._update()
        self.async_write_ha_state()

    def _update(self) -> None:
        self._by_name = {}
        for entry in plan_entries(self.hass):
            name, n = entry.title, 2
            while name in self._by_name:  # two plans with the same name: number the later ones
                name, n = f"{entry.title} ({n})", n + 1
            self._by_name[name] = entry.entry_id
        active = self.runtime.active_entry_id
        current = next((name for name, entry_id in self._by_name.items() if entry_id == active), None)
        self._attr_options = [*self._by_name] if current else [NO_PLAN, *self._by_name]
        self._attr_current_option = current or NO_PLAN

    async def async_select_option(self, option: str) -> None:
        if option == NO_PLAN:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="choose_a_plan")
        self.runtime.async_set_active(self._by_name[option])
