from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import minimal_plan

from engine import Resolver, build_forecast, load_plan

SYD = ZoneInfo("Australia/Sydney")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SYD)


def as_map(entries):
    return {datetime.fromisoformat(e["time"]): e["value"] for e in entries}


def test_shape_matches_template_sensors(solarmax):
    now = at("2026-11-03 22:23")
    entries = build_forecast(solarmax, "import", now)
    times = [datetime.fromisoformat(e["time"]) for e in entries]

    assert times[0] == now
    assert entries[0]["value"] == pytest.approx(0.27478)
    assert times == sorted(times) and len(set(times)) == len(times)
    # 5-minute steps up to 22:30, then 30-minute slots for 7 days.
    assert times[:3] == [at("2026-11-03 22:23"), at("2026-11-03 22:28"), at("2026-11-03 22:30")]
    assert times[-1] == at("2026-11-03 22:30") + timedelta(minutes=30 * (7 * 48 - 1))
    assert entries[0]["time"].endswith("+11:00")


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
