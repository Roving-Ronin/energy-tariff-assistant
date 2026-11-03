from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import minimal_plan

from engine import Resolver, load_plan, validate_plan

SYD = ZoneInfo("Australia/Sydney")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SYD)


# ----- SolarMax: your plan ------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "used", "period", "rate"),
    [
        ("2026-11-03 22:20", {}, "shoulder", 0.27478),  # the moment OEC called "off-peak"
        ("2026-11-04 03:00", {}, "offpeak", 0.165),
        ("2026-11-04 05:59", {}, "offpeak", 0.165),
        ("2026-11-04 06:00", {}, "shoulder", 0.27478),
        ("2026-11-04 11:00", {}, "free", 0.0),
        ("2026-11-04 12:30", {"free": 49.9}, "free", 0.0),
        ("2026-11-04 12:30", {"free": 50}, "free", 0.27478),  # cap used: shoulder rate
        ("2026-11-04 14:00", {}, "shoulder", 0.27478),
        ("2026-11-04 15:00", {}, "peak", 0.45331),
        ("2026-11-04 20:59", {}, "peak", 0.45331),
        ("2026-11-04 21:00", {}, "shoulder", 0.27478),
        ("2026-11-07 15:30", {}, "peak", 0.45331),  # Saturday: no weekday rules on this plan
    ],
)
def test_solarmax_import(solarmax, moment, used, period, rate):
    result = solarmax.rate_at("import", at(moment), used)
    assert result.period.id == period
    assert result.rate == pytest.approx(rate)


def test_solarmax_free_cap_reports_tier_and_rate_period(solarmax):
    result = solarmax.rate_at("import", at("2026-11-04 12:00"), {"free": 60})
    assert result.period.id == "free"
    assert result.tier_index == 1
    assert result.rate_period.id == "shoulder"


@pytest.mark.parametrize(
    ("moment", "used", "rate"),
    [
        ("2026-11-04 17:59", {}, 0.05),
        ("2026-11-04 18:00", {}, 0.15),
        ("2026-11-04 19:30", {"super": 29.9}, 0.15),
        ("2026-11-04 19:30", {"super": 30}, 0.05),
        ("2026-11-04 21:00", {}, 0.05),
    ],
)
def test_solarmax_export(solarmax, moment, used, rate):
    assert solarmax.rate_at("export", at(moment), used).rate == pytest.approx(rate)


def test_aware_utc_input_is_converted(solarmax):
    # 2026-11-03 11:20 UTC = 22:20 AEDT
    moment = datetime(2026, 11, 3, 11, 20, tzinfo=UTC)
    assert solarmax.period_at("import", moment).id == "shoulder"


@pytest.mark.parametrize(
    ("stream", "moment", "expected"),
    [
        ("import", "2026-11-03 22:20", "2026-11-04 00:00"),
        ("import", "2026-11-04 05:00", "2026-11-04 06:00"),
        ("import", "2026-11-04 11:30", "2026-11-04 14:00"),
        ("export", "2026-11-04 21:00", "2026-11-05 18:00"),
    ],
)
def test_next_change(solarmax, stream, moment, expected):
    assert solarmax.next_change(stream, at(moment)) == at(expected)


def test_next_change_across_daylight_saving_start(solarmax):
    # Sydney clocks go 02:00 -> 03:00 on 2026-10-04; off-peak still ends at 06:00 local.
    change = solarmax.next_change("import", at("2026-10-04 01:00"))
    assert change == at("2026-10-04 06:00")
    assert change.utcoffset().total_seconds() == 11 * 3600


# ----- weekday/weekend and public holidays ----------------------------------


def red_energy_plan():
    """The weekday TOU plan from red_energy_tariffs.yaml (template rates)."""
    return minimal_plan(
        retailer="Red Energy",
        plan="Time of Use",
        holidays={"treat_as": "sunday"},
        **{
            "import": {
                "periods": [
                    {
                        "id": "peak",
                        "name": "Peak",
                        "rate": 0.41745,
                        "windows": [
                            {"days": "weekdays", "start": "07:00", "end": "09:00"},
                            {"days": "weekdays", "start": "17:00", "end": "20:00"},
                        ],
                    },
                    {
                        "id": "shoulder",
                        "name": "Shoulder",
                        "rate": 0.33495,
                        "windows": [
                            {"days": "weekdays", "start": "09:00", "end": "17:00"},
                            {"days": "weekdays", "start": "20:00", "end": "22:00"},
                        ],
                    },
                    {"id": "offpeak", "name": "Off-peak", "rate": 0.26928, "default": True},
                ]
            }
        },
    )


def test_red_energy_plan_is_valid():
    assert validate_plan(red_energy_plan()).ok


@pytest.mark.parametrize(
    ("moment", "period"),
    [
        ("2026-11-02 08:00", "peak"),  # Monday
        ("2026-11-02 12:00", "shoulder"),
        ("2026-11-02 23:00", "offpeak"),
        ("2026-11-07 08:00", "offpeak"),  # Saturday
        ("2026-12-24 08:00", "peak"),  # Thursday
        ("2026-12-25 08:00", "offpeak"),  # Christmas Day, Friday: treated as Sunday
    ],
)
def test_weekdays_and_holidays(moment, period):
    resolver = Resolver(load_plan(red_energy_plan()))
    assert resolver.period_at("import", at(moment)).id == period


def test_holidays_ignored_when_treat_as_none():
    plan = red_energy_plan()
    plan["holidays"] = {"treat_as": "none"}
    resolver = Resolver(load_plan(plan))
    assert resolver.period_at("import", at("2026-12-25 08:00")).id == "peak"


def test_wrapping_window_matches_by_calendar_day():
    plan = minimal_plan(
        **{
            "import": {
                "periods": [
                    {
                        "id": "night",
                        "name": "Night",
                        "rate": 0.10,
                        "windows": [{"days": "weekdays", "start": "22:00", "end": "07:00"}],
                    },
                    {"id": "day", "name": "Day", "rate": 0.30, "default": True},
                ]
            }
        }
    )
    resolver = Resolver(load_plan(plan))
    assert resolver.period_at("import", at("2026-11-02 03:00")).id == "night"  # Monday early
    assert resolver.period_at("import", at("2026-11-06 23:00")).id == "night"  # Friday late
    assert resolver.period_at("import", at("2026-11-07 03:00")).id == "day"  # Saturday early


def test_seasons():
    plan = minimal_plan(
        seasons={"summer": [11, 12, 1, 2, 3]},
        **{
            "import": {
                "periods": [
                    {
                        "id": "summer_peak",
                        "name": "Summer peak",
                        "rate": 0.60,
                        "windows": [{"days": "all", "start": "14:00", "end": "20:00", "seasons": ["summer"]}],
                    },
                    {"id": "anytime", "name": "Anytime", "rate": 0.30, "default": True},
                ]
            }
        },
    )
    assert validate_plan(plan).ok
    resolver = Resolver(load_plan(plan))
    assert resolver.period_at("import", at("2026-12-01 15:00")).id == "summer_peak"
    assert resolver.period_at("import", at("2026-07-01 15:00")).id == "anytime"
    assert resolver.next_change("import", at("2026-10-31 15:00")) == at("2026-11-01 14:00")


# ----- rate versions ------------------------------------------------------


def test_plan_versions_switch_at_midnight():
    old = minimal_plan(effective_to="2026-12-31")
    new = minimal_plan(effective_from="2027-01-01")
    new["import"]["periods"][0]["rate"] = 0.35
    resolver = Resolver([load_plan(new), load_plan(old)])
    assert resolver.rate_at("import", at("2026-12-31 23:59")).rate == pytest.approx(0.30)
    assert resolver.rate_at("import", at("2027-01-01 00:00")).rate == pytest.approx(0.35)
    assert resolver.next_change("import", at("2026-12-31 12:00")) == at("2027-01-01 00:00")


def test_expired_plan_keeps_last_rates():
    old = minimal_plan(effective_to="2026-12-31")
    resolver = Resolver(load_plan(old))
    assert resolver.rate_at("import", at("2027-03-01 12:00")).rate == pytest.approx(0.30)
