"""Per-period energy and cost accounting for one stream.

A Ledger takes readings from a cumulative energy sensor (kWh) and splits
each increase across the plan's periods, pricing it as it goes. It keeps
three buckets that reset on their own clocks, in plan-local time:

- ``day``: local midnight
- ``month``: the 1st of the month (used for ``tier_reset: month``)
- ``cycle``: the billing day (used for ``tier_reset: bill`` and the bill)

When a bucket resets, its totals are kept as ``previous[kind]``.

How an increase is split: the energy between two readings is assumed to
have been used evenly over the time between them. If a period boundary or
a midnight falls inside that time, the energy is shared out in proportion
to the time either side, and each share is priced at the rate in force for
its part. Capped (tiered) rates are split exactly at the cap.

Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .resolver import Resolver

BUCKETS = ("day", "month", "cycle")
TIER_RESET_BUCKET = {"day": "day", "month": "month", "bill": "cycle"}
# A drop smaller than this fraction of the last reading is sensor jitter,
# not a meter reset, and is ignored.
JITTER_FRACTION = 0.01
EPSILON = 1e-9


def bucket_start(kind: str, day: date, billing_day: int) -> date:
    """First day of the bucket of `kind` that contains `day`."""
    if kind == "day":
        return day
    if kind == "month":
        return day.replace(day=1)
    if kind == "cycle":
        if day.day >= billing_day:
            return day.replace(day=billing_day)
        previous_month_end = day.replace(day=1) - timedelta(days=1)
        return previous_month_end.replace(day=billing_day)
    raise ValueError(f"unknown bucket kind: {kind!r}")


@dataclass
class Bucket:
    """kWh and cost per period id since `start` (a plan-local date)."""

    start: date
    kwh: dict[str, float] = field(default_factory=dict)
    cost: dict[str, float] = field(default_factory=dict)

    @property
    def total_kwh(self) -> float:
        return sum(self.kwh.values())

    @property
    def total_cost(self) -> float:
        return sum(self.cost.values())

    def add(self, period_id: str, kwh: float, cost: float) -> None:
        self.kwh[period_id] = self.kwh.get(period_id, 0.0) + kwh
        self.cost[period_id] = self.cost.get(period_id, 0.0) + cost

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "kwh": dict(self.kwh), "cost": dict(self.cost)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Bucket:
        return cls(
            start=date.fromisoformat(data["start"]),
            kwh={k: float(v) for k, v in data.get("kwh", {}).items()},
            cost={k: float(v) for k, v in data.get("cost", {}).items()},
        )


class Ledger:
    """Energy and cost accounting for one stream of a plan."""

    def __init__(self, resolver: Resolver, stream_key: str, billing_day: int = 1) -> None:
        if not 1 <= billing_day <= 28:
            raise ValueError("billing_day must be 1-28")
        self.resolver = resolver
        self.stream_key = stream_key
        self.billing_day = billing_day
        self.buckets: dict[str, Bucket] = {}
        self.previous: dict[str, Bucket | None] = dict.fromkeys(BUCKETS)
        self.last_reading: float | None = None
        self.last_time: datetime | None = None

    # ----- buckets ------------------------------------------------------

    def roll(self, moment: datetime) -> bool:
        """Start new buckets for any that `moment` has moved past.

        Returns True if any bucket was reset.
        """
        day = self.resolver.local(moment).date()
        rolled = False
        for kind in BUCKETS:
            start = bucket_start(kind, day, self.billing_day)
            current = self.buckets.get(kind)
            if current is None:
                self.buckets[kind] = Bucket(start)
            elif start > current.start:
                self.previous[kind] = current
                self.buckets[kind] = Bucket(start)
                rolled = True
        return rolled

    def _bucket_for(self, kind: str, moment: datetime | None) -> Bucket | None:
        """The bucket energy used at `moment` belongs in.

        Usually the current one. If the buckets have already rolled past
        `moment` (a reading arrives just after midnight for energy used just
        before it), it is the previous bucket when that one covers `moment`.
        """
        current = self.buckets.get(kind)
        if current is None or moment is None:
            return current
        start = bucket_start(kind, self.resolver.local(moment).date(), self.billing_day)
        if start >= current.start:
            return current
        previous = self.previous.get(kind)
        if previous is not None and previous.start == start:
            return previous
        return current

    def bucket_start_time(self, kind: str) -> datetime:
        """Plan-local midnight at which the current `kind` bucket began."""
        return self.resolver.at(self.buckets[kind].start, 0)

    def used_for_tiers(self, moment: datetime | None = None) -> dict[str, float]:
        """kWh counted so far, per period, since each period's tier reset."""
        used: dict[str, float] = {}
        if moment is None:
            plan = self.resolver.versions[-1]
        else:
            plan = self.resolver.plan_for(self.resolver.local(moment).date())
        for period in plan.stream(self.stream_key).periods:
            kind = TIER_RESET_BUCKET.get(period.tier_reset, "day")
            bucket = self._bucket_for(kind, moment)
            used[period.id] = bucket.kwh.get(period.id, 0.0) if bucket else 0.0
        return used

    # ----- readings -----------------------------------------------------

    def add_reading(self, reading: float, moment: datetime) -> bool:
        """Account for a new cumulative reading. Returns True if anything changed.

        The first reading only sets the baseline. A drop of more than
        JITTER_FRACTION is taken as the source meter resetting to zero, so
        the new reading itself is the energy used since.
        """
        moment = moment.astimezone(UTC)
        if self.last_reading is None or self.last_time is None:
            self.last_reading = reading
            self.last_time = moment
            return self.roll(moment)

        if moment < self.last_time:
            # Out of order; keep the newer baseline.
            return False

        delta = reading - self.last_reading
        if delta < 0:
            if -delta <= abs(self.last_reading) * JITTER_FRACTION:
                return False
            delta = max(reading, 0.0)

        start = self.last_time
        self.last_reading = reading
        self.last_time = moment
        if delta <= EPSILON:
            return self.roll(moment)
        self._spread(delta, start, moment)
        return True

    def _spread(self, kwh: float, start: datetime, end: datetime) -> None:
        """Share `kwh` over [start, end] across period boundaries and price it."""
        points = [start]
        points += [
            b.astimezone(UTC)
            for b in self.resolver.boundaries(self.stream_key, start, end)
            if b.astimezone(UTC) < end
        ]
        points.append(end)
        span = (end - start).total_seconds()
        if span <= 0:
            self._charge(kwh, end)
            return
        for seg_start, seg_end in itertools.pairwise(points):
            share = kwh * (seg_end - seg_start).total_seconds() / span
            if share > EPSILON:
                self._charge(share, seg_start)

    def _charge(self, kwh: float, moment: datetime) -> None:
        """Add `kwh` used at `moment`, splitting at any cap it crosses."""
        self.roll(moment)
        remaining = kwh
        for _ in range(100):
            if remaining <= EPSILON:
                return
            used = self.used_for_tiers(moment)
            result = self.resolver.rate_at(self.stream_key, moment, used)
            period = result.period
            take = remaining
            if period.tiered and result.tier_index is not None:
                cap = period.tiers[result.tier_index].up_to_kwh
                if cap is not None:
                    room = cap - used.get(period.id, 0.0)
                    if room > EPSILON:
                        take = min(remaining, room)
            cost = take * result.rate
            for kind in self.buckets:
                self._bucket_for(kind, moment).add(period.id, take, cost)
            remaining -= take
        raise RuntimeError("tier split did not converge")

    # ----- adjustments --------------------------------------------------

    def calibrate(
        self, kind: str, period_id: str, kwh: float, cost: float | None = None
    ) -> None:
        """Set one period's kWh (and optionally cost) in the current `kind` bucket.

        Without `cost`, the cost is scaled to keep the same average rate,
        or left at 0 if there was no energy before.
        """
        bucket = self.buckets[kind]
        old_kwh = bucket.kwh.get(period_id, 0.0)
        if cost is None:
            old_cost = bucket.cost.get(period_id, 0.0)
            cost = old_cost * kwh / old_kwh if old_kwh > EPSILON else 0.0
        bucket.kwh[period_id] = kwh
        bucket.cost[period_id] = cost

    def reset(self, kind: str, moment: datetime) -> None:
        """End the current `kind` bucket now; it becomes `previous[kind]`."""
        day = self.resolver.local(moment).date()
        if kind in self.buckets:
            self.previous[kind] = self.buckets[kind]
        self.buckets[kind] = Bucket(day)

    # ----- persistence --------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream_key,
            "billing_day": self.billing_day,
            "last_reading": self.last_reading,
            "last_time": self.last_time.isoformat() if self.last_time else None,
            "buckets": {k: b.to_dict() for k, b in self.buckets.items()},
            "previous": {k: b.to_dict() for k, b in self.previous.items() if b is not None},
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Load state saved by `to_dict`. Buckets roll on the next reading."""
        self.last_reading = data.get("last_reading")
        last_time = data.get("last_time")
        self.last_time = datetime.fromisoformat(last_time) if last_time else None
        self.buckets = {
            k: Bucket.from_dict(v) for k, v in data.get("buckets", {}).items() if k in BUCKETS
        }
        self.previous = dict.fromkeys(BUCKETS)
        for k, v in data.get("previous", {}).items():
            if k in BUCKETS:
                self.previous[k] = Bucket.from_dict(v)
