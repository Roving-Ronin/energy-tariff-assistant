"""Price forecasts in the layouts optimisers read (HAEO, Nimbus, EMHASS templates).

`build_forecast`: `{time, value}` points, as Amber Express publishes them. The first is now, then every :00 and
:30, plus any price change that falls between them (so a 14:15 change isn't lost). Each value holds until the
next point (the price sensor says `interpolation_mode: previous`). Nimbus reads this.

`build_intervals`: Amber Electric's layout, `{start_time, end_time, per_kwh}` in fixed 30-minute slots from the
current slot. HAEO reads this ahead of `forecast`; EMHASS templates written for Amber use it unchanged
(`map(attribute='per_kwh')`, `[:48]` is 24 hours).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from .resolver import Resolver

SLOT_MINUTES = 30
SLOT = timedelta(minutes=SLOT_MINUTES)


def _slot_start(resolver: Resolver, now: datetime) -> datetime:
    """The :00 or :30 at or before `now`, in UTC."""
    current = resolver.local(now).replace(second=0, microsecond=0)
    return (current - timedelta(minutes=current.minute % SLOT_MINUTES)).astimezone(UTC)


def _rate(resolver: Resolver, stream_key: str, moment: datetime, today, used_kwh) -> float:
    local = resolver.local(moment)
    used = used_kwh if local.date() == today else None
    return resolver.rate_at(stream_key, local, used).rate


def build_forecast(
    resolver: Resolver,
    stream_key: str,
    now: datetime,
    horizon_days: int = 2,
    used_kwh: Mapping[str, float] | None = None,
) -> list[dict[str, object]]:
    """Forecast points [{"time": iso, "value": rate}, ...]: now, every :00/:30, and any change between.

    Today's tiered periods use `used_kwh` (counted so far); later days assume tiers have reset. Usage can't be
    predicted, so a capped rate is assumed to last its whole window.
    """
    current = resolver.local(now).replace(second=0, microsecond=0)
    today = current.date()
    # Step in UTC: adding minutes to a zone-aware local time is wall-clock arithmetic and goes wrong across
    # daylight-saving changes.
    first_slot = _slot_start(resolver, now)
    slots = horizon_days * 24 * 60 // SLOT_MINUTES
    end = first_slot + SLOT * slots
    times: set[datetime] = {current.astimezone(UTC)}
    times |= {first_slot + SLOT * i for i in range(1, slots + 1)}
    times |= {b.astimezone(UTC) for b in resolver.boundaries(stream_key, current, end)}
    return [
        {"time": resolver.local(moment).isoformat(), "value": _rate(resolver, stream_key, moment, today, used_kwh)}
        for moment in sorted(t for t in times if t >= current.astimezone(UTC))
    ]


def build_intervals(
    resolver: Resolver,
    stream_key: str,
    now: datetime,
    horizon_days: int = 2,
    used_kwh: Mapping[str, float] | None = None,
) -> list[dict[str, object]]:
    """Amber Electric's layout: [{"start_time", "end_time", "per_kwh"}, ...], fixed 30-minute slots.

    The current slot is priced as of now (so a cap used up earlier in it counts); later slots at their start.
    A change inside a slot (rare: Australian tariffs change on the hour or half hour) takes the slot's
    starting price here; `build_forecast` keeps the exact time.
    """
    current = resolver.local(now)
    today = current.date()
    first_slot = _slot_start(resolver, now)
    out = []
    for i in range(horizon_days * 24 * 60 // SLOT_MINUTES):
        start = first_slot + SLOT * i
        priced_at = current.astimezone(UTC) if i == 0 else start
        out.append({
            "start_time": resolver.local(start).isoformat(),
            "end_time": resolver.local(start + SLOT).isoformat(),
            "per_kwh": _rate(resolver, stream_key, priced_at, today, used_kwh),
        })
    return out
