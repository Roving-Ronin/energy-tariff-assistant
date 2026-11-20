from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import minimal_plan

from engine import Ledger, Resolver, bucket_start, load_plan

SYD = ZoneInfo("Australia/Sydney")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SYD)


def feed(ledger: Ledger, *readings: tuple[str, float]) -> None:
    for moment, value in readings:
        ledger.add_reading(value, at(moment))


@pytest.fixture
def import_ledger(solarmax):
    return Ledger(solarmax, "import", billing_day=15)


# ----- bucket starts --------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "day", "billing_day", "expected"),
    [
        ("day", date(2026, 11, 20), 1, date(2026, 11, 20)),
        ("month", date(2026, 11, 20), 1, date(2026, 11, 1)),
        ("cycle", date(2026, 11, 20), 15, date(2026, 11, 15)),
        ("cycle", date(2026, 11, 14), 15, date(2026, 10, 15)),
        ("cycle", date(2026, 1, 3), 28, date(2025, 12, 28)),
        ("cycle", date(2026, 3, 1), 1, date(2026, 3, 1)),
    ],
)
def test_bucket_start(kind, day, billing_day, expected):
    assert bucket_start(kind, day, billing_day) == expected


def test_billing_day_range(solarmax):
    with pytest.raises(ValueError):
        Ledger(solarmax, "import", billing_day=29)


# ----- basic accounting -----------------------------------------------------


def test_first_reading_is_baseline_only(import_ledger):
    feed(import_ledger, ("2026-11-04 03:00", 1000.0))
    assert import_ledger.buckets["day"].total_kwh == 0


def test_energy_within_one_period(import_ledger):
    feed(import_ledger, ("2026-11-04 03:00", 1000.0), ("2026-11-04 04:00", 1002.0))
    day = import_ledger.buckets["day"]
    assert day.kwh == {"offpeak": pytest.approx(2.0)}
    assert day.cost["offpeak"] == pytest.approx(2.0 * 0.165)


def test_energy_split_across_period_boundary(import_ledger):
    # 05:30 -> 06:30 crosses offpeak -> shoulder at 06:00, half each.
    feed(import_ledger, ("2026-11-04 05:30", 0.0), ("2026-11-04 06:30", 4.0))
    day = import_ledger.buckets["day"]
    assert day.kwh["offpeak"] == pytest.approx(2.0)
    assert day.kwh["shoulder"] == pytest.approx(2.0)
    assert day.total_cost == pytest.approx(2.0 * 0.165 + 2.0 * 0.27478)


def test_free_window_cap_splits_exactly(import_ledger):
    # 60 kWh in the free window: first 50 at 0, last 10 at shoulder rate,
    # all counted against the free period.
    feed(import_ledger, ("2026-11-04 11:00", 0.0), ("2026-11-04 12:00", 60.0))
    day = import_ledger.buckets["day"]
    assert day.kwh == {"free": pytest.approx(60.0)}
    assert day.cost["free"] == pytest.approx(10 * 0.27478)


def test_free_cap_resets_daily(import_ledger):
    feed(
        import_ledger,
        ("2026-11-04 11:00", 0.0),
        ("2026-11-04 12:00", 50.0),
        ("2026-11-05 11:00", 50.0),
        ("2026-11-05 12:00", 60.0),
    )
    assert import_ledger.buckets["day"].cost["free"] == pytest.approx(0.0)
    assert import_ledger.buckets["cycle"].kwh["free"] == pytest.approx(60.0)


def test_export_super_fit_cap(solarmax):
    ledger = Ledger(solarmax, "export")
    feed(ledger, ("2026-11-04 18:00", 0.0), ("2026-11-04 19:00", 40.0))
    day = ledger.buckets["day"]
    assert day.kwh["super"] == pytest.approx(40.0)
    assert day.cost["super"] == pytest.approx(30 * 0.15 + 10 * 0.05)


# ----- buckets rolling ------------------------------------------------------


def test_day_rolls_at_midnight_and_splits(import_ledger):
    # 23:00 -> 01:00 is all shoulder then offpeak; one hour each side.
    feed(import_ledger, ("2026-11-04 23:00", 0.0), ("2026-11-05 01:00", 2.0))
    assert import_ledger.previous["day"].kwh == {"shoulder": pytest.approx(1.0)}
    assert import_ledger.buckets["day"].kwh == {"offpeak": pytest.approx(1.0)}
    assert import_ledger.buckets["cycle"].total_kwh == pytest.approx(2.0)


def test_cycle_rolls_on_billing_day(import_ledger):
    feed(
        import_ledger,
        ("2026-11-14 03:00", 0.0),
        ("2026-11-14 04:00", 1.0),
        ("2026-11-15 03:00", 1.0),
        ("2026-11-15 04:00", 3.0),
    )
    assert import_ledger.previous["cycle"].total_kwh == pytest.approx(1.0)
    assert import_ledger.buckets["cycle"].start == date(2026, 11, 15)
    assert import_ledger.buckets["cycle"].total_kwh == pytest.approx(2.0)


# ----- source quirks --------------------------------------------------------


def test_small_drop_is_jitter(import_ledger):
    feed(
        import_ledger,
        ("2026-11-04 03:00", 1000.0),
        ("2026-11-04 03:10", 999.5),
        ("2026-11-04 03:20", 1001.0),
    )
    assert import_ledger.buckets["day"].total_kwh == pytest.approx(1.0)


def test_large_drop_is_meter_reset(import_ledger):
    feed(
        import_ledger,
        ("2026-11-04 03:00", 1000.0),
        ("2026-11-04 04:00", 2.0),
    )
    assert import_ledger.buckets["day"].total_kwh == pytest.approx(2.0)


def test_out_of_order_reading_ignored(import_ledger):
    feed(import_ledger, ("2026-11-04 04:00", 10.0), ("2026-11-04 03:00", 5.0))
    assert import_ledger.last_reading == 10.0


# ----- adjustments and persistence -----------------------------------------


def test_calibrate_keeps_average_rate(import_ledger):
    feed(import_ledger, ("2026-11-04 03:00", 0.0), ("2026-11-04 04:00", 2.0))
    import_ledger.calibrate("cycle", "offpeak", 4.0)
    cycle = import_ledger.buckets["cycle"]
    assert cycle.kwh["offpeak"] == 4.0
    assert cycle.cost["offpeak"] == pytest.approx(4.0 * 0.165)


def test_round_trip(import_ledger, solarmax):
    feed(
        import_ledger,
        ("2026-11-04 23:00", 0.0),
        ("2026-11-05 01:00", 2.0),
    )
    saved = import_ledger.to_dict()
    restored = Ledger(solarmax, "import", billing_day=15)
    restored.restore(saved)
    assert restored.to_dict() == saved
    feed(restored, ("2026-11-05 02:00", 3.0))
    assert restored.buckets["day"].kwh["offpeak"] == pytest.approx(2.0)


def test_flat_plan():
    resolver = Resolver(load_plan(minimal_plan()))
    ledger = Ledger(resolver, "import")
    feed(ledger, ("2026-11-04 10:00", 0.0), ("2026-11-04 20:00", 10.0))
    assert ledger.buckets["day"].kwh == {"anytime": pytest.approx(10.0)}
    assert ledger.buckets["day"].total_cost == pytest.approx(3.0)


def test_late_reading_after_rollover_goes_to_previous_day(import_ledger):
    # The integration rolls buckets at midnight before the next reading
    # arrives; the pre-midnight share must still land in yesterday.
    feed(import_ledger, ("2026-11-04 23:00", 0.0))
    import_ledger.roll(at("2026-11-05 00:00"))
    feed(import_ledger, ("2026-11-05 01:00", 2.0))
    assert import_ledger.previous["day"].kwh == {"shoulder": pytest.approx(1.0)}
    assert import_ledger.buckets["day"].kwh == {"offpeak": pytest.approx(1.0)}
