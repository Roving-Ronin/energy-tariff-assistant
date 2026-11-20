"""Diagnostics: the plan and the meter state, for bug reports."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import TariffConfigEntry
from .const import CONF_PLAN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TariffConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    options = {k: v for k, v in entry.options.items() if k != CONF_PLAN}
    return {
        "options": options,
        "plan": entry.options.get(CONF_PLAN),
        "sources": runtime.sources,
        "source_ok": runtime.source_ok,
        "ledgers": {s: ledger.to_dict() for s, ledger in runtime.ledgers.items()},
    }
