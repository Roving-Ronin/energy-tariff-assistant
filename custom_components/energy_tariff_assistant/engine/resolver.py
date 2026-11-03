"""Resolve a moment in time to a period and a rate.

A Resolver holds one or more versions of the same plan (different
effective dates). All matching is done in the plan's local wall-clock
time, so daylight-saving changes need no special handling: a window
covers the clock times it names, and the day is 23 or 25 hours long.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .model import MINUTES_PER_DAY, Period, Plan

_LOGGER = logging.getLogger(__name__)

TREAT_AS_WEEKDAY = {"saturday": 5, "sunday": 6}
MAX_USE_PERIOD_DEPTH = 10


@dataclass(frozen=True)
class RateResult:
    """The rate at one moment.

    `period` is the period whose window covers the moment. `rate_period`
    is the period whose rate is charged: different only when a tier says
    `use_period` (e.g. the free window is used up, so shoulder applies).
    `tier_index` is the tier in force on `period`, or None if not tiered.
    """

    rate: float
    period: Period
    rate_period: Period
    tier_index: int | None
    plan: Plan


class Resolver:
    """Period and rate lookups for one plan, across its rate versions."""

    def __init__(self, plans: Plan | list[Plan]) -> None:
        versions = [plans] if isinstance(plans, Plan) else list(plans)
        if not versions:
            raise ValueError("at least one plan version is required")
        self.versions = sorted(versions, key=lambda p: p.effective_from)
        self.tz = ZoneInfo(self.versions[0].timezone)
        self._holidays: dict[tuple[str, str | None], object | None] = {}

    # ----- time helpers -------------------------------------------------

    def local(self, moment: datetime) -> datetime:
        """Convert to plan-local time. Naive datetimes are taken as local."""
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self.tz)
        return moment.astimezone(self.tz)

    def at(self, day: date, minute: int) -> datetime:
        """Plan-local datetime for a minute of a calendar day."""
        if minute >= MINUTES_PER_DAY:
            return datetime.combine(day + timedelta(days=1), time(0), tzinfo=self.tz)
        return datetime.combine(day, time(minute // 60, minute % 60), tzinfo=self.tz)

    def plan_for(self, day: date) -> Plan:
        """The plan version in force on `day`.

        Falls back to the latest version that started before `day` (an
        expired plan keeps its last rates), or the earliest if none has
        started yet.
        """
        for plan in reversed(self.versions):
            if plan.covers_date(day):
                return plan
        started = [p for p in self.versions if p.effective_from <= day]
        return started[-1] if started else self.versions[0]

    def is_holiday(self, plan: Plan, day: date) -> bool:
        if plan.holidays_treat_as == "none" or plan.holidays_calendar is None:
            return False
        key = plan.holidays_calendar
        if key not in self._holidays:
            self._holidays[key] = _load_holidays(*key)
        calendar = self._holidays[key]
        return calendar is not None and day in calendar

    def day_type(self, plan: Plan, day: date) -> int:
        """Weekday number (Mon=0) used for window matching, after holidays."""
        if self.is_holiday(plan, day):
            return TREAT_AS_WEEKDAY[plan.holidays_treat_as]
        return day.weekday()

    # ----- lookups ------------------------------------------------------

    def period_at(self, stream_key: str, moment: datetime) -> Period:
        local = self.local(moment)
        plan = self.plan_for(local.date())
        return self._period(plan, stream_key, local)

    def _period(self, plan: Plan, stream_key: str, local: datetime) -> Period:
        stream = plan.stream(stream_key)
        weekday = self.day_type(plan, local.date())
        season = plan.season_for_month(local.month)
        minute = local.hour * 60 + local.minute
        for period in stream.periods:
            if period.default:
                continue
            if any(w.covers(weekday, minute, season) for w in period.windows):
                return period
        return stream.default_period

    def rate_at(
        self,
        stream_key: str,
        moment: datetime,
        used_kwh: Mapping[str, float] | None = None,
    ) -> RateResult:
        """Rate at `moment`.

        `used_kwh` maps period id -> kWh already counted in that period
        since its tier reset. Tiered periods with no entry count as 0.
        """
        local = self.local(moment)
        plan = self.plan_for(local.date())
        period = self._period(plan, stream_key, local)
        stream = plan.stream(stream_key)
        used = used_kwh or {}

        current = period
        tier_index: int | None = None
        for depth in range(MAX_USE_PERIOD_DEPTH):
            if not current.tiered:
                return RateResult(current.rate, period, current, tier_index, plan)
            index = _tier_index(current, used.get(current.id, 0.0))
            if depth == 0:
                tier_index = index
            tier = current.tiers[index]
            if tier.rate is not None:
                return RateResult(tier.rate, period, current, tier_index, plan)
            current = stream.period(tier.use_period)
        raise ValueError(f"use_period chain too deep from {period.id!r}")

    # ----- changes ------------------------------------------------------

    def boundaries(self, stream_key: str, start: datetime, end: datetime) -> Iterator[datetime]:
        """Every moment in (start, end] at which the period *could* change.

        Includes each window start/end and every local midnight (holidays,
        seasons and plan versions change there). Callers compare periods
        either side to find real changes.
        """
        local_start = self.local(start)
        local_end = self.local(end)
        minutes: set[int] = {0}
        for plan in self.versions:
            for period in plan.stream(stream_key).periods:
                for window in period.windows:
                    minutes |= window.boundaries()
        ordered = sorted(m for m in minutes if m < MINUTES_PER_DAY)
        day = local_start.date()
        while day <= local_end.date():
            for minute in ordered:
                moment = self.at(day, minute)
                if local_start < moment <= local_end:
                    yield moment
            day += timedelta(days=1)

    def next_change(
        self, stream_key: str, moment: datetime, horizon_days: int = 8
    ) -> datetime | None:
        """Next moment the active period or plan version changes, or None.

        Tier changes depend on usage, so they are not predicted here.
        """
        local = self.local(moment)
        current = (self.plan_for(local.date()), self._period(self.plan_for(local.date()), stream_key, local).id)
        for candidate in self.boundaries(stream_key, local, local + timedelta(days=horizon_days)):
            plan = self.plan_for(candidate.date())
            state = (plan, self._period(plan, stream_key, candidate).id)
            if state != current:
                return candidate
        return None


def _tier_index(period: Period, used: float) -> int:
    for index, tier in enumerate(period.tiers):
        if tier.up_to_kwh is not None and used < tier.up_to_kwh:
            return index
    return len(period.tiers) - 1


def _load_holidays(country: str, subdivision: str | None):
    try:
        import holidays  # noqa: PLC0415 - optional at import time
    except ImportError:
        _LOGGER.warning("python-holidays is not installed; public holidays are ignored")
        return None
    try:
        return holidays.country_holidays(country, subdiv=subdivision)
    except NotImplementedError:
        _LOGGER.warning("No holiday calendar for %s/%s; public holidays are ignored", country, subdivision)
        return None
