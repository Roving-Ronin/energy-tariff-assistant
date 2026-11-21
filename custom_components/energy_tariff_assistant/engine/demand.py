"""Demand charges: the peak kW in a plan's demand windows, and what it costs.

A DemandMeter takes readings from the cumulative grid import sensor (kWh), like a Ledger, and splits each
increase evenly over time into fixed intervals (30 minutes unless the plan says otherwise), aligned to the
plan-local clock (:00 and :30). When an interval is over and it started inside one of the charge's windows, its
energy becomes kW (kWh x 60 / interval minutes) and the highest such reading in the measurement period is kept:

- ``month``: each calendar month
- ``bill``: each billing cycle (from the billing day)
- ``rolling_12_months``: the current month and the 11 before it (only months the meter has seen)

The charge is the peak (at least ``minimum_kw``) x the rate x the days, the way Energy Made Easy words it: "your
maximum electricity use in a peak 30-minute block is multiplied by the demand charge rate and the number of days
covered by your bill". It's worked out per charge period (the calendar month; the billing cycle for ``bill``),
counting each day of that period so far whose season the charge applies in. A new, higher peak re-prices the days
already counted, so the charge so far only ever goes up within a period. When a period is over its charge is
locked into the total.

A rate per kW per month counts each day as 1/(days in the period) of a month.

Interval readings are settled only when a reading arrives after the interval's end, never at a timer tick:
the energy between two readings is spread over the time between them, so an interval can't be closed before the
reading that covers it.

Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .meter import EPSILON, JITTER_FRACTION, bucket_start
from .model import DemandCharge, Plan
from .resolver import Resolver

ROLLING_MONTHS = 12


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_start(key: str) -> date:
    year, month = key.split("-")
    return date(int(year), int(month), 1)


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


@dataclass
class Reading:
    """One interval's demand: kW and when the interval started (UTC)."""

    kw: float
    at: datetime

    def to_dict(self) -> list[Any]:
        return [self.kw, self.at.isoformat()]

    @classmethod
    def from_list(cls, data: list[Any]) -> Reading:
        return cls(float(data[0]), datetime.fromisoformat(data[1]))


class DemandMeter:
    """One demand charge of a plan (by id, across the plan's versions)."""

    def __init__(self, resolver: Resolver, charge_id: str, billing_day: int = 1) -> None:
        if not 1 <= billing_day <= 28:
            raise ValueError("billing_day must be 1-28")
        self.resolver = resolver
        self.charge_id = charge_id
        self.billing_day = billing_day
        latest = self.charge_for(resolver.versions[-1])
        if latest is None:
            raise ValueError(f"no demand charge {charge_id!r} in the plan")
        self.over = latest.measure.over
        self.interval = timedelta(minutes=latest.measure.interval_minutes)
        self.top_n = latest.measure.n if latest.measure.method == "mean_of_top_n" and latest.measure.n else 1
        self.last_reading: float | None = None
        self.last_time: datetime | None = None
        self._open_start: datetime | None = None  # the interval energy is being added to
        self._open_kwh = 0.0
        self.peaks: dict[str, list[Reading]] = {}  # measurement key -> highest readings, highest first
        self.period_start: date | None = None  # the charge period not yet locked
        self.locked = 0.0  # charges of the periods already over
        self.locked_periods = 0

    # ----- the charge ---------------------------------------------------

    def charge_for(self, plan: Plan) -> DemandCharge | None:
        return next((c for c in plan.demand if c.id == self.charge_id), None)

    def applies_on(self, day: date) -> DemandCharge | None:
        """The charge in force on `day`, if it applies in that day's season."""
        plan = self.resolver.plan_for(day)
        charge = self.charge_for(plan)
        if charge is None:
            return None
        season = plan.season_for_month(day.month)
        if not any(w.seasons is None or season in w.seasons for w in charge.windows):
            return None
        return charge

    def _in_window(self, start: datetime) -> bool:
        local = self.resolver.local(start)
        plan = self.resolver.plan_for(local.date())
        charge = self.charge_for(plan)
        if charge is None:
            return False
        weekday = self.resolver.day_type(plan, local.date())
        season = plan.season_for_month(local.month)
        minute = local.hour * 60 + local.minute
        return any(w.covers(weekday, minute, season) for w in charge.windows)

    # ----- periods ------------------------------------------------------

    def _measure_key(self, day: date) -> str:
        if self.over == "bill":
            return bucket_start("cycle", day, self.billing_day).isoformat()
        return _month_key(day)

    def period_bounds(self, day: date) -> tuple[date, date]:
        """The charge period containing `day`: [start, end)."""
        if self.over == "bill":
            start = bucket_start("cycle", day, self.billing_day)
            return start, _next_month(start).replace(day=self.billing_day)
        start = day.replace(day=1)
        return start, _next_month(start)

    def _readings_for(self, day: date) -> list[Reading]:
        """The readings the charge for `day`'s period is worked out from, highest first."""
        if self.over == "rolling_12_months":
            month = day.replace(day=1)
            keys = []
            for _ in range(ROLLING_MONTHS):
                keys.append(_month_key(month))
                month = (month - timedelta(days=1)).replace(day=1)
            found = [r for k in keys for r in self.peaks.get(k, ())]
            return sorted(found, key=lambda r: -r.kw)[: self.top_n]
        return self.peaks.get(self._measure_key(day), [])

    def demand_kw(self, day: date) -> float:
        """The measured demand for `day`'s period: the peak (or mean of the top n), 0 without readings."""
        readings = self._readings_for(day)
        return sum(r.kw for r in readings) / len(readings) if readings else 0.0

    def peak(self, day: date) -> Reading | None:
        readings = self._readings_for(day)
        return readings[0] if readings else None

    def _billable_kw(self, day: date, charge: DemandCharge) -> float:
        return max(self.demand_kw(day), charge.measure.minimum_kw or 0.0)

    def period_charge(self, day: date, upto: date | None = None) -> tuple[float, int]:
        """($, days counted) for `day`'s charge period, from its start to `upto` (inclusive; default: its end)."""
        start, end = self.period_bounds(day)
        last = min(upto, end - timedelta(days=1)) if upto else end - timedelta(days=1)
        length = (end - start).days
        total, days = 0.0, 0
        current = start
        while current <= last:
            charge = self.applies_on(current)
            if charge is not None:
                share = 1.0 / length if charge.unit == "per_kw_per_month" else 1.0
                total += self._billable_kw(day, charge) * charge.rate * share
                days += 1
            current += timedelta(days=1)
        return total, days

    def total(self, moment: datetime) -> float:
        """Every period locked so far, plus the open one up to `moment` (and any wholly passed since)."""
        today = self.resolver.local(moment).date()
        if self.period_start is None:
            return self.locked
        total = self.locked
        day = self.period_start
        while day <= today:
            total += self.period_charge(day, today)[0]
            day = self.period_bounds(day)[1]
        return total

    def _lock_through(self, moment: datetime) -> None:
        """Lock every charge period that ended at or before `moment` (whose intervals are all settled)."""
        day = self.resolver.local(moment).date()
        if self.period_start is None:
            self.period_start = self.period_bounds(day)[0]
            return
        while True:
            start, end = self.period_bounds(self.period_start)
            if self.resolver.at(end, 0) > moment:
                return
            self.locked += self.period_charge(start)[0]
            self.locked_periods += 1
            self.period_start = end
            self._prune(end)

    def _prune(self, day: date) -> None:
        """Forget readings no later charge can use."""
        if self.over == "rolling_12_months":
            oldest = day.replace(day=1)
            for _ in range(ROLLING_MONTHS - 1):
                oldest = (oldest - timedelta(days=1)).replace(day=1)
            keep = lambda key: _month_start(key) >= oldest  # noqa: E731
        else:
            current = self._measure_key(day)
            keep = lambda key: key >= current  # noqa: E731
        self.peaks = {k: v for k, v in self.peaks.items() if keep(k)}

    # ----- intervals ----------------------------------------------------

    def interval_start(self, moment: datetime) -> datetime:
        """Start of the interval containing `moment`, aligned to the plan-local clock (UTC)."""
        offset = self.resolver.local(moment).utcoffset() or timedelta(0)
        seconds = self.interval.total_seconds()
        shifted = moment.timestamp() + offset.total_seconds()
        return datetime.fromtimestamp(shifted - shifted % seconds - offset.total_seconds(), UTC)

    def _close(self) -> bool:
        """Settle the open interval. Returns True if it set a new peak."""
        start, kwh = self._open_start, self._open_kwh
        self._open_start, self._open_kwh = None, 0.0
        if start is None or not self._in_window(start):
            return False
        kw = kwh * 60 / (self.interval.total_seconds() / 60)
        key = self._measure_key(self.resolver.local(start).date())
        readings = self.peaks.setdefault(key, [])
        if len(readings) >= self.top_n and kw <= readings[-1].kw + EPSILON:
            return False
        readings.append(Reading(round(kw, 6), start))
        readings.sort(key=lambda r: -r.kw)
        del readings[self.top_n:]
        return True

    def _add(self, kwh: float, moment: datetime) -> bool:
        start = self.interval_start(moment)
        new_peak = False
        if self._open_start is not None and start != self._open_start:
            new_peak = self._close()
        if self._open_start is None:
            self._open_start = start
        self._open_kwh += kwh
        return new_peak

    def add_reading(self, reading: float, moment: datetime) -> bool:
        """Account for a new cumulative reading. Returns True if a new peak was set.

        The first reading only sets the baseline. A drop of more than JITTER_FRACTION is the source meter
        resetting to zero, so the new reading itself is the energy used since.
        """
        moment = moment.astimezone(UTC)
        if self.last_reading is None or self.last_time is None:
            self.last_reading, self.last_time = reading, moment
            self._lock_through(moment)
            return False
        if moment < self.last_time:
            return False
        delta = reading - self.last_reading
        if delta < 0:
            if -delta <= abs(self.last_reading) * JITTER_FRACTION:
                return False
            delta = max(reading, 0.0)
        start = self.last_time
        self.last_reading, self.last_time = reading, moment
        new_peak = False
        span = (moment - start).total_seconds()
        if delta > EPSILON and span > 0:
            cursor = start
            while cursor < moment:
                end = min(self.interval_start(cursor) + self.interval, moment)
                new_peak |= self._add(delta * (end - cursor).total_seconds() / span, cursor)
                cursor = end
        elif delta > EPSILON:
            new_peak |= self._add(delta, moment - timedelta(microseconds=1))
        # The reading covers everything up to `moment`: an interval that ended by then is complete
        if self._open_start is not None and self._open_start + self.interval <= moment:
            new_peak |= self._close()
        self._lock_through(moment)
        return new_peak

    # ----- persistence --------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "charge": self.charge_id,
            "last_reading": self.last_reading,
            "last_time": self.last_time.isoformat() if self.last_time else None,
            "open_start": self._open_start.isoformat() if self._open_start else None,
            "open_kwh": self._open_kwh,
            "peaks": {k: [r.to_dict() for r in v] for k, v in self.peaks.items()},
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "locked": self.locked,
            "locked_periods": self.locked_periods,
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.last_reading = data.get("last_reading")
        self.last_time = datetime.fromisoformat(data["last_time"]) if data.get("last_time") else None
        self._open_start = datetime.fromisoformat(data["open_start"]) if data.get("open_start") else None
        self._open_kwh = float(data.get("open_kwh", 0.0))
        self.peaks = {k: [Reading.from_list(r) for r in v] for k, v in (data.get("peaks") or {}).items()}
        self.period_start = date.fromisoformat(data["period_start"]) if data.get("period_start") else None
        self.locked = float(data.get("locked", 0.0))
        self.locked_periods = int(data.get("locked_periods", 0))
