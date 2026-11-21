"""Per plan entry: prices now and ahead, and the grid energy counted toward the plan's caps.

The grid energy sensors are set once, on Active tariff; every plan entry counts the same readings in its own
ledgers, so each plan's capped rates are right whether or not it's the plan in use.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    ENERGY_UNIT_FACTORS,
    FORECAST_DAYS,
    SAVE_DELAY,
    STORAGE_VERSION,
    STREAM_EXPORT,
    STREAM_IMPORT,
    signal_update,
)
from .engine import Ledger, Period, Plan, RateResult, Resolver, build_forecast, build_intervals
from .plan_source import prewarm_holidays

_LOGGER = logging.getLogger(__name__)

# Re-check at least this often, so prices and buckets never drift far
# even if a scheduled change is missed (e.g. the clock jumps).
MAX_TICK = timedelta(minutes=30)


class TariffRuntime:
    """Everything one config entry needs at run time."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        resolver: Resolver,
        sources: dict[str, str] | None = None,
        billing_day: int = 1,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.resolver = resolver
        self.signal = signal_update(entry.entry_id)
        # Grid energy sensors by stream, from Active tariff (none: capped rates last their whole window)
        self.sources: dict[str, str] = {s: e for s, e in (sources or {}).items() if e}
        self.ledgers: dict[str, Ledger] = {
            stream: Ledger(resolver, stream, billing_day) for stream in self.sources
        }
        self.source_ok: dict[str, bool] = dict.fromkeys(self.sources, False)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._unsubs: list[Callable[[], None]] = []
        self._unsub_tick: Callable[[], None] | None = None
        self._forecasts: dict[str, list[dict[str, Any]]] = {}
        self._intervals: dict[str, list[dict[str, Any]]] = {}
        self._year = dt_util.now().year

    # ----- plan helpers -------------------------------------------------

    @property
    def plan(self) -> Plan:
        """The plan version in force today."""
        return self.resolver.plan_for(self.resolver.local(dt_util.utcnow()).date())

    @property
    def currency(self) -> str:
        return self.plan.currency

    def rate(self, stream: str, now: datetime | None = None) -> RateResult:
        now = now or dt_util.utcnow()
        ledger = self.ledgers.get(stream)
        used = ledger.used_for_tiers(now) if ledger else None
        return self.resolver.rate_at(stream, now, used)

    def next_change(self, stream: str) -> datetime | None:
        return self.resolver.next_change(stream, dt_util.utcnow())

    def forecast(self, stream: str) -> list[dict[str, Any]]:
        """`{time, value}` points (Nimbus, HAEO's own format)."""
        if stream not in self._forecasts:
            self._refresh_forecast(stream)
        return self._forecasts[stream]

    def intervals(self, stream: str) -> list[dict[str, Any]]:
        """Amber Electric's `forecasts` layout in fixed 30-minute slots (HAEO, EMHASS templates)."""
        if stream not in self._intervals:
            self._refresh_forecast(stream)
        return self._intervals[stream]

    def _refresh_forecast(self, stream: str) -> None:
        ledger = self.ledgers.get(stream)
        now = dt_util.utcnow()
        used = ledger.used_for_tiers(now) if ledger else None
        self._forecasts[stream] = build_forecast(
            self.resolver, stream, now, horizon_days=FORECAST_DAYS, used_kwh=used
        )
        self._intervals[stream] = build_intervals(
            self.resolver, stream, now, horizon_days=FORECAST_DAYS, used_kwh=used
        )

    def period_window(self, stream: str) -> tuple[datetime | None, datetime | None]:
        """Start and end of the period in force now (the last change before now, the next one after)."""
        now = dt_util.utcnow()
        current = self.resolver.period_at(stream, now).id
        start = None
        # Candidate moments (window edges, midnights) back to two days ago; the latest where the period changed
        for moment in sorted(self.resolver.boundaries(stream, now - timedelta(days=2), now), reverse=True):
            if moment <= now and self.resolver.period_at(stream, moment - timedelta(minutes=1)).id != current:
                start = moment
                break
        return start, self.resolver.next_change(stream, now)

    def capped_periods(self, stream: str) -> list[Period]:
        """Periods with a cap (first N kWh at one rate), across plan versions, first seen wins."""
        seen: dict[str, Period] = {}
        for plan in self.resolver.versions:
            for period in plan.stream(stream).periods:
                if period.tiered and any(t.up_to_kwh is not None for t in period.tiers):
                    seen.setdefault(period.id, period)
        return list(seen.values())

    def used(self, stream: str, period_id: str) -> float | None:
        """kWh counted toward `period_id`'s cap in its current reset (None without an energy sensor)."""
        ledger = self.ledgers.get(stream)
        if ledger is None:
            return None
        return ledger.used_for_tiers(dt_util.utcnow()).get(period_id, 0.0)

    # ----- life cycle ---------------------------------------------------

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        for stream, ledger in self.ledgers.items():
            if saved := stored.get("ledgers", {}).get(stream):
                try:
                    ledger.restore(saved)
                except (KeyError, TypeError, ValueError) as err:
                    _LOGGER.warning("Could not restore %s meters: %s", stream, err)
            ledger.roll(dt_util.utcnow())

        if self.sources:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, list(self.sources.values()), self._async_source_changed
                )
            )
        # Count what happened while Home Assistant was stopped.
        for stream in self.sources:
            self._take_reading(stream, self.hass.states.get(self.sources[stream]), None)
        self._schedule_tick()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {"ledgers": {s: ledger.to_dict() for s, ledger in self.ledgers.items()}}

    @callback
    def async_changed(self, *, forecasts: bool = False) -> None:
        """Save soon and tell the entities."""
        if forecasts:
            self._forecasts.clear()
            self._intervals.clear()
        self._store.async_delay_save(self._data, SAVE_DELAY)
        async_dispatcher_send(self.hass, self.signal)

    # ----- readings -----------------------------------------------------

    def _reading(self, state: State | None) -> float | None:
        """A source state as kWh, or None if it can't be used."""
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT, "kWh")
        factor = ENERGY_UNIT_FACTORS.get(unit)
        if factor is None:
            _LOGGER.warning(
                "%s has unit %r; expected Wh, kWh or MWh", state.entity_id, unit
            )
            return None
        return value * factor

    def _take_reading(
        self, stream: str, state: State | None, moment: datetime | None
    ) -> bool:
        value = self._reading(state)
        self.source_ok[stream] = value is not None
        if value is None or state is None:
            return False
        moment = moment or state.last_updated
        before = self.ledgers[stream].used_for_tiers(moment)
        changed = self.ledgers[stream].add_reading(value, moment)
        # A cap being reached changes the price and the forecast.
        tiers_moved = changed and self._tier_crossed(stream, before, moment)
        if changed:
            self.async_changed(forecasts=tiers_moved)
        return changed

    def _tier_crossed(self, stream: str, before: dict[str, float], moment: datetime) -> bool:
        after = self.ledgers[stream].used_for_tiers(moment)
        old = self.resolver.rate_at(stream, moment, before)
        new = self.resolver.rate_at(stream, moment, after)
        return old.tier_index != new.tier_index or old.rate != new.rate

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        for stream, source in self.sources.items():
            if source == entity_id:
                was_ok = self.source_ok[stream]
                changed = self._take_reading(stream, event.data["new_state"], None)
                if not changed and was_ok != self.source_ok[stream]:
                    async_dispatcher_send(self.hass, self.signal)

    # ----- scheduled changes --------------------------------------------

    def _next_tick(self, now: datetime) -> datetime:
        candidates = [now + MAX_TICK]
        for stream in (STREAM_IMPORT, STREAM_EXPORT):
            if (change := self.resolver.next_change(stream, now)) is not None:
                candidates.append(change)
        local = self.resolver.local(now)
        candidates.append(self.resolver.at(local.date() + timedelta(days=1), 0))
        return min(c.astimezone(dt_util.UTC) for c in candidates)

    @callback
    def _schedule_tick(self) -> None:
        now = dt_util.utcnow()
        when = self._next_tick(now)
        self._unsub_tick = async_track_point_in_utc_time(self.hass, self._async_tick, when)

    async def _async_tick(self, now: datetime) -> None:
        """A period, day or cycle may have changed: refresh prices and buckets.

        The source isn't re-read here. Re-reading an unchanged value at the
        boundary would move the ledger's baseline forward and push the next
        increase entirely into the new period, instead of sharing it across
        the boundary by time.
        """
        self._unsub_tick = None
        for ledger in self.ledgers.values():
            ledger.roll(now)
        if (year := dt_util.now().year) != self._year:
            self._year = year
            await self.hass.async_add_executor_job(prewarm_holidays, self.resolver, year)
        self.async_changed(forecasts=True)
        self._schedule_tick()
