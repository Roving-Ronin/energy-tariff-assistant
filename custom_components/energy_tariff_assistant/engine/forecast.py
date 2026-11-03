"""Price forecasts in the format HAEO and EMHASS template sensors use.

Shape matches the hand-written templates this replaces: 5-minute entries
from now to the next :00/:30, then 30-minute entries for the horizon.
Every real price change is also emitted as its own entry, so a change
that falls inside a slot (e.g. 14:15) is not lost. Values hold until the
next entry (interpolation_mode "previous").
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from .resolver import Resolver

SLOT_MINUTES = 30
STEP_MINUTES = 5


def build_forecast(
    resolver: Resolver,
    stream_key: str,
    now: datetime,
    horizon_days: int = 7,
    used_kwh: Mapping[str, float] | None = None,
) -> list[dict[str, object]]:
    """Forecast entries [{"time": iso, "value": rate}, ...].

    Today's tiered periods use `used_kwh` (counted so far); later days
    assume tiers have reset. Usage cannot be predicted, so a capped rate
    is assumed to last its whole window.
    """
    current = resolver.local(now).replace(second=0, microsecond=0)
    today = current.date()

    # Step in UTC: adding minutes to a zone-aware local time is wall-clock
    # arithmetic and goes wrong across daylight-saving changes.
    start_utc = current.astimezone(UTC)
    to_boundary = SLOT_MINUTES - (current.minute % SLOT_MINUTES)
    times: set[datetime] = {
        start_utc + timedelta(minutes=i * STEP_MINUTES)
        for i in range(-(-to_boundary // STEP_MINUTES))
    }
    block_start = start_utc + timedelta(minutes=to_boundary)
    slots = horizon_days * 24 * 60 // SLOT_MINUTES
    times |= {block_start + timedelta(minutes=i * SLOT_MINUTES) for i in range(slots)}
    end = block_start + timedelta(minutes=(slots - 1) * SLOT_MINUTES)
    times |= {b.astimezone(UTC) for b in resolver.boundaries(stream_key, current, end)}

    entries = []
    for moment in sorted(times):
        moment = resolver.local(moment)
        used = used_kwh if moment.date() == today else None
        rate = resolver.rate_at(stream_key, moment, used).rate
        entries.append({"time": moment.isoformat(), "value": rate})
    return entries
