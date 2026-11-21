"""Diagnostics: the plan and the counting state (plan entries), or the plan in use and totals (Active tariff)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import TariffConfigEntry
from .active import ActiveRuntime
from .const import CONF_PLAN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TariffConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    if isinstance(runtime, ActiveRuntime):
        return {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "active_plan": runtime.active_entry.title if runtime.active_entry else None,
            "totals": runtime.totals,
            "days_counted": runtime.days_counted,
        }
    options = {k: v for k, v in entry.options.items() if k != CONF_PLAN}
    return {
        "data": dict(entry.data),
        "options": options,
        "plan": entry.options.get(CONF_PLAN),
        "sources": runtime.sources,
        "source_ok": runtime.source_ok,
        "ledgers": {s: ledger.to_dict() for s, ledger in runtime.ledgers.items()},
    }
