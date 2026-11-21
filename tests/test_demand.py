"""DemandMeter: peak kW in the demand windows, and the charge (peak x rate x days)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import minimal_plan

from engine import DemandMeter, Resolver, load_plan, validate_plan

SYD = ZoneInfo("Australia/Sydney")
RATE = 0.2  # $/kW/day


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SYD)


def plan_with(*charges, seasons=None, **overrides):
    plan = minimal_plan(demand=list(charges), **overrides)
    if seasons:
        plan["seasons"] = seasons
    assert validate_plan(plan).ok, validate_plan(plan).errors
    return Resolver(load_plan(plan))


def charge(over="month", rate=RATE, unit="per_kw_per_day", windows=None, **measure):
    return {"id": "demand", "name": "Demand", "rate": rate, "unit": unit,
            "windows": windows or [{"days": "weekdays", "start": "17:00", "end": "20:00"}],
            "measure": {"interval_minutes": 30, "method": "max", "over": over, **measure}}


def feed(meter, *readings):
    for moment, value in readings:
        meter.add_reading(value, at(moment))


@pytest.fixture
def meter():
    return DemandMeter(plan_with(charge()), "demand")


def test_half_hour_in_the_window_becomes_kw(meter):
    # Wednesday 4 Nov 2026: 1.5 kWh in 17:00-17:30 is 3 kW
    feed(meter, ("2026-11-04 17:00", 100.0), ("2026-11-04 17:30", 101.5))
    assert meter.demand_kw(date(2026, 11, 4)) == pytest.approx(3.0)
    assert meter.peak(date(2026, 11, 4)).at == at("2026-11-04 17:00")


def test_outside_the_window_doesnt_count(meter):
    feed(meter, ("2026-11-04 12:00", 100.0), ("2026-11-04 12:30", 105.0),  # midday
         ("2026-11-07 17:00", 105.0), ("2026-11-07 17:30", 110.0))  # Saturday
    assert meter.demand_kw(date(2026, 11, 4)) == 0


def test_a_reading_spanning_intervals_is_shared_by_time(meter):
    # 3 kWh from 16:45 to 17:45: a quarter in 16:30 (outside), half in 17:00, a quarter in 17:30
    feed(meter, ("2026-11-04 16:45", 100.0), ("2026-11-04 17:45", 103.0), ("2026-11-04 18:00", 103.0))
    assert meter.demand_kw(date(2026, 11, 4)) == pytest.approx(3.0)  # 1.5 kWh in 17:00-17:30


def test_an_interval_is_settled_only_when_a_reading_covers_it(meter):
    feed(meter, ("2026-11-04 17:00", 100.0), ("2026-11-04 17:20", 101.0))
    assert meter.demand_kw(date(2026, 11, 4)) == 0  # 17:00-17:30 still open
    feed(meter, ("2026-11-04 17:40", 102.0))
    assert meter.demand_kw(date(2026, 11, 4)) == pytest.approx(3.0)  # 1.5 kWh by 17:30


def test_charge_is_peak_times_rate_times_days_and_a_new_peak_reprices_earlier_days(meter):
    feed(meter, ("2026-11-02 17:00", 100.0), ("2026-11-02 17:30", 101.0))  # 2 kW on the 2nd
    assert meter.total(at("2026-11-02 18:00")) == pytest.approx(2 * RATE * 2)  # 1st and 2nd
    feed(meter, ("2026-11-10 17:00", 101.0), ("2026-11-10 17:30", 103.0))  # 4 kW on the 10th
    assert meter.total(at("2026-11-10 18:00")) == pytest.approx(4 * RATE * 10)  # all ten days at 4 kW
    assert meter.total(at("2026-11-11 09:00")) == pytest.approx(4 * RATE * 11)


def test_month_end_locks_the_charge_and_starts_again(meter):
    feed(meter, ("2026-11-02 17:00", 100.0), ("2026-11-02 17:30", 101.0))  # 2 kW
    feed(meter, ("2026-12-01 00:10", 101.0))
    assert meter.locked == pytest.approx(2 * RATE * 30) and meter.locked_periods == 1
    assert meter.demand_kw(date(2026, 12, 1)) == 0
    assert meter.total(at("2026-12-01 12:00")) == pytest.approx(2 * RATE * 30)


def test_total_never_drops_and_counts_months_passed_without_readings(meter):
    feed(meter, ("2026-11-02 17:00", 100.0), ("2026-11-02 17:30", 101.0))
    totals = [meter.total(at(t)) for t in ("2026-11-15 00:00", "2026-11-30 23:00", "2026-12-05 00:00")]
    assert totals == sorted(totals) and totals[-1] == pytest.approx(2 * RATE * 30)


def test_minimum_demand_is_charged_without_readings():
    meter = DemandMeter(plan_with(charge(minimum_kw=1.5)), "demand")
    feed(meter, ("2026-11-03 09:00", 100.0))
    assert meter.total(at("2026-11-03 09:00")) == pytest.approx(1.5 * RATE * 3)


def test_billing_cycle():
    meter = DemandMeter(plan_with(charge(over="bill")), "demand", billing_day=15)
    feed(meter, ("2026-11-16 17:00", 100.0), ("2026-11-16 17:30", 101.0))  # 2 kW in the cycle from the 15th
    assert meter.total(at("2026-11-16 18:00")) == pytest.approx(2 * RATE * 2)
    feed(meter, ("2026-12-15 00:30", 101.0))
    assert meter.locked == pytest.approx(2 * RATE * 30)  # 15 Nov - 14 Dec


def test_rolling_twelve_months_keeps_the_peak_into_later_months():
    meter = DemandMeter(plan_with(charge(over="rolling_12_months")), "demand")
    feed(meter, ("2026-11-02 17:00", 100.0), ("2026-11-02 17:30", 102.0))  # 4 kW in November
    feed(meter, ("2026-12-02 17:00", 102.0), ("2026-12-02 17:30", 102.5))  # 1 kW in December
    assert meter.demand_kw(date(2026, 12, 2)) == pytest.approx(4.0)
    feed(meter, ("2027-11-02 17:00", 102.5), ("2027-11-02 17:30", 103.0))  # a year on: November 2026 has gone
    assert meter.demand_kw(date(2027, 11, 2)) == pytest.approx(1.0)


def test_monthly_rate_spreads_over_the_month():
    meter = DemandMeter(plan_with(charge(rate=9.0, unit="per_kw_per_month")), "demand")
    feed(meter, ("2026-11-02 17:00", 100.0), ("2026-11-02 17:30", 101.0))  # 2 kW
    assert meter.total(at("2026-11-15 12:00")) == pytest.approx(2 * 9.0 * 15 / 30)
    feed(meter, ("2026-12-01 01:00", 101.0))
    assert meter.locked == pytest.approx(2 * 9.0)


def test_seasonal_charge_counts_only_its_months():
    meter = DemandMeter(plan_with(
        charge(windows=[{"days": "weekdays", "start": "17:00", "end": "20:00", "seasons": ["summer"]}], minimum_kw=1.0),
        seasons={"summer": [11, 12, 1, 2, 3], "winter": [4, 5, 6, 7, 8, 9, 10]}), "demand")
    feed(meter, ("2026-10-28 17:00", 100.0), ("2026-10-28 17:30", 105.0))  # October: no demand charge
    assert meter.demand_kw(date(2026, 10, 28)) == 0
    assert meter.total(at("2026-10-31 12:00")) == 0  # the minimum doesn't apply out of season
    assert meter.total(at("2026-11-02 12:00")) == pytest.approx(1.0 * RATE * 2)


def test_mean_of_top_n():
    meter = DemandMeter(plan_with(charge(method="mean_of_top_n", n=2)), "demand")
    feed(meter, ("2026-11-02 17:00", 100.0), ("2026-11-02 17:30", 102.0),  # 4 kW
         ("2026-11-03 17:00", 102.0), ("2026-11-03 17:30", 103.0),  # 2 kW
         ("2026-11-04 17:00", 103.0), ("2026-11-04 17:30", 103.5))  # 1 kW
    assert meter.demand_kw(date(2026, 11, 4)) == pytest.approx(3.0)


def test_intervals_follow_the_local_clock_through_daylight_saving():
    # Adelaide is UTC+10:30 in summer: intervals still start at :00 and :30 local
    meter = DemandMeter(plan_with(charge(), timezone="Australia/Adelaide"), "demand")
    start = meter.interval_start(datetime(2026, 11, 4, 6, 40, tzinfo=ZoneInfo("UTC")))  # 17:10 local
    assert start.astimezone(ZoneInfo("Australia/Adelaide")).strftime("%H:%M") == "17:00"


def test_restore_carries_on(meter):
    feed(meter, ("2026-11-02 17:00", 100.0), ("2026-11-02 17:20", 101.0))
    again = DemandMeter(meter.resolver, "demand")
    again.restore(meter.to_dict())
    feed(again, ("2026-11-02 17:40", 101.5))  # the 17:00 interval closes with 1.25 kWh
    assert again.demand_kw(date(2026, 11, 2)) == pytest.approx(2.5)


def test_meter_reset_and_jitter(meter):
    feed(meter, ("2026-11-02 17:00", 100.0), ("2026-11-02 17:10", 99.99),  # jitter: ignored
         ("2026-11-02 17:20", 0.5), ("2026-11-02 17:30", 1.0))  # the source reset to 0
    assert meter.demand_kw(date(2026, 11, 2)) == pytest.approx(2.0)  # 0.5 + 0.5 kWh in the half hour


def test_unknown_charge():
    with pytest.raises(ValueError):
        DemandMeter(plan_with(charge()), "nope")
