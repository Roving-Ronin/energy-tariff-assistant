from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import minimal_plan

from engine import Resolver, build_forecast, build_intervals, load_plan

SYD = ZoneInfo("Australia/Sydney")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SYD)


def as_map(entries):
    return {datetime.fromisoformat(e["time"]): e["value"] for e in entries}


def test_shape_now_then_every_half_hour(solarmax):
    now = at("2026-11-03 22:23")
    entries = build_forecast(solarmax, "import", now)
    times = [datetime.fromisoformat(e["time"]) for e in entries]

    assert times[0] == now
    assert entries[0]["value"] == pytest.approx(0.27478)
    assert times == sorted(times) and len(set(times)) == len(times)
    # Now, then every :00 and :30 for 48 hours: no 5-minute steps, so [:48] after now is 24 hours.
    assert times[:3] == [at("2026-11-03 22:23"), at("2026-11-03 22:30"), at("2026-11-03 23:00")]
    assert times[-1] == at("2026-11-03 22:00") + timedelta(hours=48)
    assert all(t.minute in (0, 30) for t in times[1:])
    assert entries[0]["time"].endswith("+11:00")


def test_intervals_are_amber_layout_in_fixed_slots(solarmax):
    slots = build_intervals(solarmax, "import", at("2026-11-04 14:40"))
    assert len(slots) == 96  # 48 hours
    assert slots[0] == {"start_time": at("2026-11-04 14:30").isoformat(),
                        "end_time": at("2026-11-04 15:00").isoformat(), "per_kwh": pytest.approx(0.27478)}
    assert slots[1]["start_time"] == at("2026-11-04 15:00").isoformat()
    assert slots[1]["per_kwh"] == pytest.approx(0.45331)  # Peak
    for a, b in zip(slots, slots[1:], strict=False):
        assert a["end_time"] == b["start_time"]


def test_intervals_price_the_current_slot_with_todays_usage(solarmax):
    slots = build_intervals(solarmax, "import", at("2026-11-04 11:10"), used_kwh={"free": 60})
    assert slots[0]["per_kwh"] == pytest.approx(0.27478)  # cap used: Shoulder
    tomorrow = next(s for s in slots if s["start_time"] == at("2026-11-05 11:00").isoformat())
    assert tomorrow["per_kwh"] == pytest.approx(0.0)  # reset tomorrow


def test_values_follow_the_plan(solarmax):
    values = as_map(build_forecast(solarmax, "import", at("2026-11-04 00:10")))
    assert values[at("2026-11-04 03:00")] == pytest.approx(0.165)
    assert values[at("2026-11-04 12:00")] == pytest.approx(0.0)
    assert values[at("2026-11-04 16:00")] == pytest.approx(0.45331)
    assert values[at("2026-11-04 22:00")] == pytest.approx(0.27478)


def test_used_kwh_applies_today_only(solarmax):
    values = as_map(build_forecast(solarmax, "import", at("2026-11-04 10:00"), used_kwh={"free": 60}))
    assert values[at("2026-11-04 11:00")] == pytest.approx(0.27478)  # today: cap already used
    assert values[at("2026-11-05 11:00")] == pytest.approx(0.0)  # tomorrow: reset


def test_mid_slot_change_gets_its_own_entry():
    plan = minimal_plan(
        **{
            "import": {
                "periods": [
                    {
                        "id": "short",
                        "name": "Short",
                        "rate": 0.10,
                        "windows": [{"days": "all", "start": "14:15", "end": "14:45"}],
                    },
                    {"id": "anytime", "name": "Anytime", "rate": 0.30, "default": True},
                ]
            }
        }
    )
    values = as_map(build_forecast(Resolver(load_plan(plan)), "import", at("2026-11-04 12:00"), horizon_days=1))
    assert values[at("2026-11-04 14:00")] == pytest.approx(0.30)
    assert values[at("2026-11-04 14:15")] == pytest.approx(0.10)
    assert values[at("2026-11-04 14:30")] == pytest.approx(0.10)
    assert values[at("2026-11-04 14:45")] == pytest.approx(0.30)


def test_daylight_saving_day_has_no_duplicate_or_missing_slots(solarmax):
    entries = build_forecast(solarmax, "import", at("2026-10-03 12:00"), horizon_days=2)
    times = [datetime.fromisoformat(e["time"]) for e in entries]
    assert len(set(times)) == len(times)
    gaps = {b - a for a, b in zip(times, times[1:], strict=False)}
    assert max(gaps) <= timedelta(minutes=30)
    slots = build_intervals(solarmax, "import", at("2026-10-03 12:00"))
    starts = [s["start_time"] for s in slots]
    assert len(set(starts)) == len(starts) == 96  # 48 real hours across the clock change
