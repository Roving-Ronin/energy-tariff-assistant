"""Active tariff: the plan in use, and the stable entities that follow it.

`sensor.eta_grid_import_price` and `_export_price` mirror the active plan entry's price sensors (a "CNAME"), so
the Energy dashboard, HAEO, EMHASS and Nimbus never change when the plan does. The running totals (import cost,
export compensation, supply charge) are kept here, so they carry on across plan switches:

- cost and compensation: each kWh the active plan's ledger counts adds what it cost under that plan (its own
  rate at that moment: caps, boundary splits). A switch starts from the new plan's ledger as it stands.
- supply charge: each day is added at 00:00 at the rate of the plan in use then; days missed while Home
  Assistant was off are added at start; counting starts the day Active tariff was set up.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ACTIVE_PLANS_DIR,
    CONF_ACTIVE_ENTRY,
    CONF_BILLING_DAY,
    CONF_EXPORT_SENSOR,
    CONF_IMPORT_SENSOR,
    CONF_KIND,
    CONF_PLAN,
    CONF_SLUG,
    DOMAIN,
    KIND_PLAN,
    SAVE_DELAY,
    STREAM_EXPORT,
    STREAM_IMPORT,
    signal_active,
    signal_update,
)

if TYPE_CHECKING:
    from .runtime import TariffRuntime

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
ISSUE_NO_ACTIVE = "no_active_plan"


def plan_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every plan entry, by title."""
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(CONF_KIND, KIND_PLAN) == KIND_PLAN
    ]
    return sorted(entries, key=lambda e: e.title.lower())


def write_plan_files(config_dir: str, files: dict[str, dict[str, Any] | None]) -> None:
    """Write (or, for None, delete) plan JSON files under the active plans folder. Runs in the executor."""
    folder = Path(config_dir) / ACTIVE_PLANS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    for name, plan in files.items():
        path = folder / name
        if plan is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class ActiveRuntime:
    """Active tariff's state: which plan, and the totals."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.active")
        self.totals = {"import_cost": 0.0, "export_compensation": 0.0, "supply_charge": 0.0}
        self.days_counted = 0
        self._supply_last_day: date | None = None
        self._seen: dict[str, dict[str, float]] = {}  # entry id -> stream -> ledger total_cost last counted
        self._unsub_plan: Callable[[], None] | None = None
        self._unsub_midnight: Callable[[], None] | None = None
        self.settings = self.current_settings()

    def current_settings(self) -> tuple[Any, ...]:
        """What the plan entries take from Active tariff: a change reloads them."""
        options = self.entry.options
        return (options.get(CONF_IMPORT_SENSOR), options.get(CONF_EXPORT_SENSOR), options.get(CONF_BILLING_DAY))

    # ----- the plan in use ----------------------------------------------

    @property
    def active_entry_id(self) -> str | None:
        return self.entry.options.get(CONF_ACTIVE_ENTRY)

    @property
    def active_entry(self) -> ConfigEntry | None:
        entry_id = self.active_entry_id
        entry = self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        return entry if entry and entry.domain == DOMAIN else None

    @property
    def plan_runtime(self) -> TariffRuntime | None:
        """The active plan entry's runtime, while it's loaded."""
        entry = self.active_entry
        # Setting up counts too: the plan entry calls `async_follow` from the end of its own setup
        if entry is None or entry.state not in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS):
            return None
        return getattr(entry, "runtime_data", None)

    def source_entity(self, stream: str) -> str | None:
        entry = self.active_entry
        return f"sensor.{entry.data[CONF_SLUG]}_{stream}_price" if entry and CONF_SLUG in entry.data else None

    @callback
    def async_set_active(self, entry_id: str | None) -> None:
        """Point Active tariff at another plan entry (None: no plan in use)."""
        if entry_id == self.active_entry_id:
            return
        if self.active_entry_id:
            self.async_plan_switched_from(self.active_entry_id)
        options = {**self.entry.options, CONF_ACTIVE_ENTRY: entry_id}
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.async_follow()

    # ----- life cycle ---------------------------------------------------

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self.totals.update({k: float(v) for k, v in stored.get("totals", {}).items() if k in self.totals})
        self.days_counted = int(stored.get("days_counted", 0))
        if last := stored.get("supply_last_day"):
            self._supply_last_day = date.fromisoformat(last)
        self._seen = stored.get("seen", {})
        self.async_follow()
        self._schedule_midnight()

    async def async_stop(self) -> None:
        if self._unsub_plan:
            self._unsub_plan()
            self._unsub_plan = None
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {
            "totals": self.totals,
            "days_counted": self.days_counted,
            "supply_last_day": self._supply_last_day.isoformat() if self._supply_last_day else None,
            "seen": self._seen,
        }

    @callback
    def async_follow(self) -> None:
        """Listen to the active plan entry (again), count what it has since, and refresh everything."""
        if self._unsub_plan:
            self._unsub_plan()
            self._unsub_plan = None
        entry_id = self.active_entry_id
        if entry_id:
            self._unsub_plan = async_dispatcher_connect(
                self.hass, signal_update(entry_id), self._async_plan_changed
            )
        self._update_issue()
        self._async_plan_changed()
        self._count_supply()
        self.hass.async_create_task(self._async_write_active_file())

    @callback
    def _update_issue(self) -> None:
        if self.active_entry is None:
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_NO_ACTIVE, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_NO_ACTIVE,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_ACTIVE)

    async def _async_write_active_file(self) -> None:
        entry = self.active_entry
        plan = dict(entry.options[CONF_PLAN]) if entry else None
        await self.hass.async_add_executor_job(write_plan_files, self.hass.config.config_dir, {"active.json": plan})

    # ----- totals -------------------------------------------------------

    @callback
    def _async_plan_changed(self) -> None:
        """The active plan's figures changed: add what its ledgers counted since we last looked."""
        runtime = self.plan_runtime
        if runtime is not None:
            seen = self._seen.setdefault(runtime.entry.entry_id, {})
            for stream, key in ((STREAM_IMPORT, "import_cost"), (STREAM_EXPORT, "export_compensation")):
                ledger = runtime.ledgers.get(stream)
                if ledger is None:
                    continue
                last = seen.get(stream)
                # First look at this plan (or its ledger was cleared): start from where it stands, count nothing
                if last is not None and ledger.total_cost >= last:
                    self.totals[key] += ledger.total_cost - last
                seen[stream] = ledger.total_cost
            self._store.async_delay_save(self._data, SAVE_DELAY)
        async_dispatcher_send(self.hass, signal_active())

    @callback
    def async_plan_switched_from(self, entry_id: str) -> None:
        """Forget where we were in a plan's ledgers, so a later switch back counts nothing from the gap."""
        self._seen.pop(entry_id, None)

    def supply_rate(self) -> float | None:
        runtime = self.plan_runtime
        return runtime.plan.supply_charge if runtime else None

    @callback
    def _count_supply(self) -> None:
        """Add each day's supply charge not yet counted, up to and including today."""
        runtime = self.plan_runtime
        if runtime is None:
            return
        today = runtime.resolver.local(dt_util.utcnow()).date()
        day = self._supply_last_day + timedelta(days=1) if self._supply_last_day else today
        changed = False
        while day <= today:
            self.totals["supply_charge"] += runtime.resolver.plan_for(day).supply_charge
            self.days_counted += 1
            self._supply_last_day = day
            day += timedelta(days=1)
            changed = True
        if changed:
            self._store.async_delay_save(self._data, SAVE_DELAY)
            async_dispatcher_send(self.hass, signal_active())

    @callback
    def _schedule_midnight(self) -> None:
        runtime = self.plan_runtime
        now = dt_util.utcnow()
        if runtime is not None:
            local = runtime.resolver.local(now)
            when = runtime.resolver.at(local.date() + timedelta(days=1), 0)
        else:
            when = dt_util.start_of_local_day() + timedelta(days=1)
        self._unsub_midnight = async_track_point_in_utc_time(
            self.hass, self._async_midnight, dt_util.as_utc(when) + timedelta(seconds=1)
        )

    async def _async_midnight(self, _now: datetime) -> None:
        self._unsub_midnight = None
        self._count_supply()
        self._schedule_midnight()
