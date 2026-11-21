"""engine.importers.cdr: CDR (Energy Made Easy) plan detail -> ha_tariff_plan.

The samples follow the Consumer Data Standards' Get Generic Plan Detail v3
shape. They are written by hand from the standard (this repo's CI can't reach
Energy Made Easy in tests); real samples get added from the importer workflow.
"""

from __future__ import annotations

import copy
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from engine import Resolver, load_plan, validate_plan
from engine.importers.cdr import CdrImportError, convert, map_distributor, rate_name, with_gst
from engine.importers import cdr as cdr_module

TODAY = date(2026, 11, 20)
SYD = ZoneInfo("Australia/Sydney")


def ex(inc: float) -> str:
    """A GST-inclusive price as CDR publishes it: ex GST, as a string."""
    return f"{inc / 1.1:.6f}"


def tou(type_, name, rates, windows, period=None):
    item = {"type": type_, "displayName": name, "rates": rates,
            "timeOfUse": [{"days": d, "startTime": s, "endTime": e} for d, s, e in windows]}
    if period:
        item["period"] = period
    return item


ALL = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

# Covau SolarMax Residential TOU (the library plan), as CDR would publish it
SOLARMAX = {
    "data": {
        "planId": "COV1117612MRE3@EME",
        "displayName": "SolarMax Residential TOU",
        "brand": "covau",
        "brandName": "CovaU",
        "type": "MARKET",
        "fuelType": "ELECTRICITY",
        "customerType": "RESIDENTIAL",
        "effectiveFrom": "2026-07-01T00:00:00Z",
        "lastUpdated": "2026-07-01T00:00:00Z",
        "geography": {"distributors": ["Essential Energy"]},
        "electricityContract": {
            "pricingModel": "TIME_OF_USE",
            "timeZone": "LOCAL",
            "isFixed": False,
            "paymentOption": ["DIRECT_DEBIT"],
            "tariffPeriod": [{
                "displayName": "All year",
                "startDate": "01-01",
                "endDate": "12-31",
                "dailySupplyChargeType": "SINGLE",
                "dailySupplyCharge": ex(2.29999),
                "rateBlockUType": "timeOfUseRates",
                "timeOfUseRates": [
                    tou("OFF_PEAK", "Off Peak", [{"unitPrice": ex(0.165)}], [(ALL, "00:00", "05:59")]),
                    tou("SHOULDER", "Shoulder", [{"unitPrice": ex(0.27478)}],
                        [(ALL, "06:00", "10:59"), (ALL, "14:00", "14:59"), (ALL, "21:00", "23:59")]),
                    tou("OFF_PEAK", "Free", [{"unitPrice": "0", "volume": 50}, {"unitPrice": ex(0.27478)}],
                        [(ALL, "11:00", "13:59")], period="P1D"),
                    tou("PEAK", "Peak", [{"unitPrice": ex(0.45331)}], [(ALL, "15:00", "20:59")]),
                ],
            }],
            "solarFeedInTariff": [{
                "displayName": "Super FiT",
                "scheme": "OTHER",
                "payerType": "RETAILER",
                "tariffUType": "timeVaryingTariffs",
                "timeVaryingTariffs": [
                    {"type": "PEAK", "displayName": "Super FiT",
                     # Feed-in is published as paid: households get no GST on it
                     "rates": [{"unitPrice": "0.15", "volume": 30}, {"unitPrice": "0.05"}], "period": "P1D",
                     "timeVariations": [{"days": ALL, "startTime": "18:00", "endTime": "20:59"}]},
                    {"type": "OFF_PEAK", "displayName": "Standard FiT", "rates": [{"unitPrice": "0.05"}],
                     "timeVariations": [{"days": ALL, "startTime": "00:00", "endTime": "17:59"},
                                        {"days": ALL, "startTime": "21:00", "endTime": "23:59"}]},
                ],
            }],
            "discounts": [{"displayName": "Pay on time", "type": "CONDITIONAL", "category": "PAY_ON_TIME",
                           "methodUType": "percentOfUse", "percentOfUse": {"rate": "0.05"}}],
        },
    }
}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SYD)


@pytest.fixture
def solarmax_cdr():
    return copy.deepcopy(SOLARMAX)


def test_gst():
    assert with_gst("0.25") == pytest.approx(0.275)
    with pytest.raises(CdrImportError):
        with_gst("n/a")


def test_distributors():
    assert map_distributor("Essential Energy") == ("Essential Energy", "NSW")
    assert map_distributor("United Energy Distribution") == ("United Energy", "VIC")
    assert map_distributor("Somewhere") is None


def test_solarmax_converts_and_validates(solarmax_cdr):
    result = convert(solarmax_cdr, TODAY)
    assert result.skipped == []
    (plan,) = result.plans
    check = validate_plan(plan)
    assert check.ok, check.errors
    assert plan["region"] == "NSW" and plan["dnsp"] == "Essential Energy"
    assert plan["timezone"] == "Australia/Sydney"  # LOCAL
    assert plan["customer_type"] == "residential"
    assert plan["needs_review"] is True
    assert plan["supply_charge"]["rate"] == pytest.approx(2.29999)
    assert plan["effective_from"] == "2026-07-01"
    assert "Pay on time" in plan["notes"] and "GST" in plan["notes"]
    # Shoulder covers the most time: it's the default
    default = [p for p in plan["import"]["periods"] if p.get("default")]
    assert [p["id"] for p in default] == ["shoulder"]


@pytest.mark.parametrize(
    ("moment", "used", "stream"),
    [
        ("2026-11-04 03:00", {}, "import"),
        ("2026-11-04 06:00", {}, "import"),
        ("2026-11-04 12:00", {}, "import"),
        ("2026-11-04 16:00", {}, "import"),
        ("2026-11-04 21:00", {}, "import"),
        ("2026-11-04 23:59", {}, "import"),
        ("2026-11-04 19:00", {}, "export"),
        ("2026-11-04 10:00", {}, "export"),
    ],
)
def test_solarmax_prices_match_the_library_plan(solarmax, solarmax_cdr, moment, used, stream):
    converted = Resolver(load_plan(convert(solarmax_cdr, TODAY).plans[0]))
    assert converted.rate_at(stream, at(moment)).rate == pytest.approx(
        solarmax.rate_at(stream, at(moment)).rate, abs=1e-5
    )


def test_caps_become_tiers(solarmax_cdr):
    plan = convert(solarmax_cdr, TODAY).plans[0]
    free = next(p for p in plan["import"]["periods"] if p["id"] == "free")
    assert free["tiers"] == [{"up_to_kwh": 50.0, "rate": 0.0}, {"rate": pytest.approx(0.27478)}]
    assert free["tier_reset"] == "day"
    resolver = Resolver(load_plan(plan))
    assert resolver.rate_at("import", at("2026-11-04 12:00"), {"free": 60}).rate == pytest.approx(0.27478)
    sfit = next(p for p in plan["export"]["periods"] if p["id"] == "super_fit")
    assert sfit["tiers"][0] == {"up_to_kwh": 30.0, "rate": pytest.approx(0.15)}


@pytest.fixture
def other_retailer(solarmax_cdr):
    """SolarMax as if from a retailer with no known clock rule."""
    solarmax_cdr["data"]["brand"] = "example"
    solarmax_cdr["data"]["brandName"] = "Example Energy"
    return solarmax_cdr


def test_aest_windows_use_brisbane_time(other_retailer):
    other_retailer["data"]["electricityContract"]["timeZone"] = "AEST"
    result = convert(other_retailer, TODAY)
    plan = result.plans[0]
    assert plan["timezone"] == "Australia/Brisbane"
    resolver = Resolver(load_plan(plan))
    # 16:00 AEDT (summer) is 15:00 AEST: peak starts at 15:00 AEST = 16:00 Sydney summer time
    assert resolver.rate_at("import", at("2026-11-04 15:30")).period.name == "Shoulder"
    assert resolver.rate_at("import", at("2026-11-04 16:30")).period.name == "Peak"


def test_stated_aest_against_a_local_time_network_is_flagged(other_retailer):
    other_retailer["data"]["electricityContract"]["timeZone"] = "AEST"
    result = convert(other_retailer, TODAY)
    plan = result.plans[0]
    assert plan["timezone"] == "Australia/Brisbane"  # the plan's word is kept
    assert "Essential Energy's time-of-use periods follow local time" in plan["notes"]
    assert any("plan's own time zone was used" in note for note in result.notes)


def test_covau_bills_on_local_time_whatever_its_data_says(solarmax_cdr):
    # Covau SolarMax's Energy Made Easy data says AEST; CovaU bills on local time (maintainer, 2026-11-21).
    solarmax_cdr["data"]["electricityContract"]["timeZone"] = "AEST"
    result = convert(solarmax_cdr, TODAY)
    plan = result.plans[0]
    assert plan["timezone"] == "Australia/Sydney"
    assert "CovaU bills on local time" in plan["notes"]
    resolver = Resolver(load_plan(plan))
    assert resolver.rate_at("import", at("2026-11-04 15:30")).period.name == "Peak"


def test_stated_local_on_a_local_network_has_no_note(solarmax_cdr):
    assert solarmax_cdr["data"]["electricityContract"]["timeZone"] == "LOCAL"
    result = convert(solarmax_cdr, TODAY)
    assert result.plans[0]["timezone"] == "Australia/Sydney"
    assert not any("time zone" in note for note in result.notes)


def test_no_time_zone_uses_the_network_clock(other_retailer):
    # The standard says absent = AEST, but NSW networks follow local time and Evoenergy stays on AEST.
    del other_retailer["data"]["electricityContract"]["timeZone"]
    other_retailer["data"]["geography"]["distributors"] = ["Essential Energy", "Evoenergy"]
    result = convert(other_retailer, TODAY)
    by_dnsp = {p["dnsp"]: p for p in result.plans}
    assert by_dnsp["Essential Energy"]["timezone"] == "Australia/Sydney"
    assert by_dnsp["Evoenergy"]["timezone"] == "Australia/Brisbane"
    assert "Essential Energy's clock (local time" in by_dnsp["Essential Energy"]["notes"]
    assert "Evoenergy's clock (AEST all year" in by_dnsp["Evoenergy"]["notes"]
    resolver = Resolver(load_plan(by_dnsp["Essential Energy"]))
    assert resolver.rate_at("import", at("2026-11-04 15:30")).period.name == "Peak"


def test_no_time_zone_without_daylight_saving_has_no_note(solarmax_cdr):
    del solarmax_cdr["data"]["electricityContract"]["timeZone"]
    solarmax_cdr["data"]["geography"]["distributors"] = ["Energex"]
    result = convert(solarmax_cdr, TODAY)
    assert result.plans[0]["timezone"] == "Australia/Brisbane"
    assert not any("time zone" in note for note in result.notes)


def test_no_time_zone_note_only_when_times_matter(solarmax_cdr):
    contract = solarmax_cdr["data"]["electricityContract"]
    del contract["timeZone"]
    for tariff in contract["tariffPeriod"]:
        tariff["rateBlockUType"] = "singleRate"
        tariff["singleRate"] = {"rates": [{"unitPrice": "0.25"}]}
        tariff.pop("timeOfUseRates", None)
    contract["solarFeedInTariff"] = [
        {"scheme": "OTHER", "payerType": "RETAILER", "tariffUType": "singleTariff",
         "singleTariff": {"rates": [{"unitPrice": "0.05"}]}}
    ]
    result = convert(solarmax_cdr, TODAY)
    assert result.plans
    assert not any("time zone" in note for note in result.notes)


def test_one_plan_per_distributor(solarmax_cdr):
    solarmax_cdr["data"]["geography"]["distributors"] = ["Ausgrid", "Endeavour", "Nowhere Networks"]
    result = convert(solarmax_cdr, TODAY)
    assert [p["dnsp"] for p in result.plans] == ["Ausgrid", "Endeavour Energy"]
    assert result.skipped == ["CovaU SolarMax Residential TOU (COV1117612MRE3@EME): unknown distributor 'Nowhere Networks'"]


def test_single_rate_and_flat_feed_in():
    detail = copy.deepcopy(SOLARMAX)
    contract = detail["data"]["electricityContract"]
    contract["pricingModel"] = "SINGLE_RATE"
    contract["tariffPeriod"] = [{
        "displayName": "All year", "startDate": "01-01", "endDate": "12-31",
        "dailySupplyCharge": ex(1.1), "rateBlockUType": "singleRate",
        "singleRate": {"displayName": "Anytime", "rates": [{"unitPrice": ex(0.3)}]},
    }]
    contract["solarFeedInTariff"] = [
        {"displayName": "Premium", "scheme": "PREMIUM", "payerType": "GOVERNMENT", "tariffUType": "singleTariff",
         "singleTariff": {"rates": [{"unitPrice": "0.6"}]}},
        {"displayName": "Retailer FiT", "scheme": "CURRENT", "payerType": "RETAILER", "tariffUType": "singleTariff",
         "singleTariff": {"rates": [{"unitPrice": "0.04"}]}},
    ]
    plan = convert(detail, TODAY).plans[0]
    assert validate_plan(plan).ok
    assert plan["import"]["periods"] == [{"id": "anytime", "name": "Anytime", "rate": pytest.approx(0.3), "default": True}]
    assert plan["export"]["periods"][0]["rate"] == pytest.approx(0.04)  # the premium scheme is left out


def test_residential_feed_in_is_paid_without_gst(solarmax_cdr):
    result = convert(solarmax_cdr, TODAY)
    plan = result.plans[0]
    sfit = next(p for p in plan["export"]["periods"] if p["id"] == "super_fit")
    assert sfit["tiers"] == [{"up_to_kwh": 30.0, "rate": 0.15}, {"rate": 0.05}]
    # Charges still get GST added
    peak = next(p for p in plan["import"]["periods"] if p["id"] == "peak")
    assert peak["rate"] == pytest.approx(0.45331)
    assert not any("Feed-in" in note and "GST" in note for note in result.notes)


def test_business_feed_in_includes_gst(solarmax_cdr):
    # A GST-registered business is paid GST on feed-in and its bill shows it.
    solarmax_cdr["data"]["customerType"] = "BUSINESS"
    result = convert(solarmax_cdr, TODAY)
    plan = result.plans[0]
    sfit = next(p for p in plan["export"]["periods"] if p["id"] == "super_fit")
    assert sfit["tiers"] == [{"up_to_kwh": 30.0, "rate": pytest.approx(0.165)}, {"rate": pytest.approx(0.055)}]
    assert "if you aren't registered for GST" in plan["notes"]


def test_unstated_customer_type_pays_feed_in_without_gst_and_says_so(solarmax_cdr):
    del solarmax_cdr["data"]["customerType"]
    plan = convert(solarmax_cdr, TODAY).plans[0]
    sfit = next(p for p in plan["export"]["periods"] if p["id"] == "super_fit")
    assert sfit["tiers"][0]["rate"] == 0.15
    assert "a GST-registered business is paid 10% more" in plan["notes"]


def test_seasons_from_month_aligned_periods(solarmax_cdr):
    contract = solarmax_cdr["data"]["electricityContract"]
    base = contract["tariffPeriod"][0]
    summer = {**copy.deepcopy(base), "displayName": "Summer", "startDate": "11-01", "endDate": "03-31"}
    winter = {**copy.deepcopy(base), "displayName": "Winter", "startDate": "04-01", "endDate": "10-31"}
    winter["timeOfUseRates"][3]["rates"] = [{"unitPrice": ex(0.40)}]
    contract["tariffPeriod"] = [summer, winter]
    plan = convert(solarmax_cdr, TODAY).plans[0]
    check = validate_plan(plan)
    assert check.ok, check.errors
    assert plan["seasons"] == {"summer": [11, 12, 1, 2, 3], "winter": [4, 5, 6, 7, 8, 9, 10]}
    resolver = Resolver(load_plan(plan))
    assert resolver.rate_at("import", at("2026-12-02 16:00")).rate == pytest.approx(0.45331)
    assert resolver.rate_at("import", at("2026-06-02 16:00")).rate == pytest.approx(0.40)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda c: c.update(pricingModel="FLEXIBLE"), "FLEXIBLE pricing"),
        (lambda c: c["tariffPeriod"][0].update(dailySupplyChargeType="BAND"), "banded daily supply"),
        (lambda c: c["tariffPeriod"][0].update(startDate="01-15"), None),  # single period: dates don't matter
        (lambda c: c["tariffPeriod"].append({**copy.deepcopy(c["tariffPeriod"][0]), "startDate": "07-15"}),
         "doesn't follow month boundaries"),
        (lambda c: c["tariffPeriod"][0]["timeOfUseRates"][0]["rates"].__setitem__(
            slice(None), [{"unitPrice": "0.1", "volume": 100}, {"unitPrice": "0.2"}]), "per year"),
    ],
)
def test_skipped_with_a_reason(solarmax_cdr, change, reason):
    change(solarmax_cdr["data"]["electricityContract"])
    result = convert(solarmax_cdr, TODAY)
    if reason is None:
        assert result.plans and not result.skipped
    else:
        assert result.plans == []
        assert len(result.skipped) == 1 and reason in result.skipped[0]


def test_gas_and_garbage():
    gas = copy.deepcopy(SOLARMAX)
    gas["data"]["fuelType"] = "GAS"
    assert "gas only" in convert(gas, TODAY).skipped[0]
    with pytest.raises(CdrImportError):
        convert({"something": "else"}, TODAY)


def test_tou_controlled_load_noted(solarmax_cdr):
    contract = solarmax_cdr["data"]["electricityContract"]
    contract["tariffPeriod"].append({"displayName": "Demand", "startDate": "01-01", "endDate": "12-31",
                                     "rateBlockUType": "demandCharges", "demandCharges": []})
    contract["controlledLoad"] = [
        {"displayName": "CL1", "rateBlockUType": "singleRate",
         "singleRate": {"displayName": "CL1", "dailySupplyCharge": ex(0.1), "rates": [{"unitPrice": ex(0.2)}]}},
        {"displayName": "CL2", "rateBlockUType": "timeOfUseRates", "timeOfUseRates": []},
    ]
    result = convert(solarmax_cdr, TODAY)
    (plan,) = result.plans
    assert validate_plan(plan).ok, validate_plan(plan).errors
    assert [c["id"] for c in plan["controlled_load"]] == ["cl1"]
    assert plan["controlled_load"][0]["periods"][0]["rate"] == pytest.approx(0.2)
    assert plan["demand"] == [] and "CL2" in plan["notes"]


# ----- feed-in split over several entries (as Energy Made Easy data does) ---------------------------------------


def fit_entry(rate, *, volume=None, period="P1D", times=None, kind="PEAK", name="SolarMax_26: Time of Use Feed In Tariff",
              description="", dates=None):
    """One retailer feed-in entry: time-varying when `times` is given, else a single tariff."""
    rates = [{"unitPrice": rate, "measureUnit": "KWH", **({"volume": volume} if volume else {})}]
    entry = {"scheme": "OTHER", "payerType": "RETAILER", "displayName": name, "description": description}
    if dates:
        entry["startDate"], entry["endDate"] = dates
    if times is None:
        entry.update(tariffUType="singleTariff", singleTariff={"rates": rates, "period": period})
    else:
        entry.update(tariffUType="timeVaryingTariffs", timeVaryingTariffs=[{
            "type": kind, "displayName": name, "rates": rates, "period": period,
            "timeVariations": [{"days": ALL, "startTime": a, "endTime": b} for a, b in times],
        }])
    return entry


def with_feed_in(detail, entries):
    detail["data"]["electricityContract"]["solarFeedInTariff"] = entries
    return detail


def test_covau_feed_in_over_three_entries_matches_the_library(solarmax_cdr):
    # As Covau SolarMax is published: 6-9pm first 30 kWh/day at 15c, 6-9pm after that at 5c, 5c at other times
    dates = ("2026-07-01", "2030-06-30")
    with_feed_in(solarmax_cdr, [
        fit_entry("0.05", times=[("21:00", "17:59")], kind="OFF_PEAK", dates=dates),
        fit_entry("0.05", times=[("18:00", "20:59")], dates=dates),
        fit_entry("0.15", volume=30, times=[("18:00", "20:59")], dates=dates),
    ])
    plan = convert(solarmax_cdr, TODAY).plans[0]
    assert validate_plan(plan).ok
    export = plan["export"]["periods"]
    assert export[0]["name"] == "Peak"
    assert export[0]["tiers"] == [{"up_to_kwh": 30.0, "rate": 0.15}, {"rate": 0.05}]
    assert export[0]["tier_reset"] == "day"
    assert export[0]["windows"] == [{"days": "all", "start": "18:00", "end": "21:00"}]
    assert export[1] == {"id": "off_peak", "name": "Off-peak", "rate": 0.05, "default": True}
    resolver = Resolver(load_plan(plan))
    assert resolver.rate_at("export", at("2026-11-04 19:00")).rate == 0.15
    assert resolver.rate_at("export", at("2026-11-04 19:00"), {"peak": 31}).rate == 0.05
    assert resolver.rate_at("export", at("2026-11-04 12:00")).rate == 0.05


def test_cap_per_year_in_the_data_but_per_day_in_the_text(solarmax_cdr):
    # 1st Energy: "first 5 kWh's/day" at 1c then 0.1c, but both entries give period P1Y
    with_feed_in(solarmax_cdr, [
        fit_entry("0.01", volume=5, period="P1Y", name="Feed-in tariff (GST exempt)",
                  description="Feed-in tariff (GST exempt), first 5 kWh's/day"),
        fit_entry("0.001", period="P1Y", name="Feed-in tariff (GST exempt)",
                  description="Feed-in tariff (GST exempt), thereafter (>5 kWh's/day)"),
    ])
    result = convert(solarmax_cdr, TODAY)
    export = result.plans[0]["export"]["periods"]
    assert export == [{"id": "feed_in", "name": "Feed-in",
                       "tiers": [{"up_to_kwh": 5.0, "rate": 0.01}, {"rate": 0.001}], "tier_reset": "day",
                       "default": True}]
    assert any("its text says per day: per day was used" in note for note in result.notes)


def test_cap_per_year_with_no_per_day_text_is_skipped(solarmax_cdr):
    with_feed_in(solarmax_cdr, [fit_entry("0.01", volume=500, period="P1Y"), fit_entry("0.001", period="P1Y")])
    result = convert(solarmax_cdr, TODAY)
    assert not result.plans
    assert "limit per P1Y, which plans can't express" in result.skipped[0]


def test_cap_with_nothing_after_it_is_skipped(solarmax_cdr):
    with_feed_in(solarmax_cdr, [fit_entry("0.15", volume=30, times=[("18:00", "20:59")]),
                                fit_entry("0.05", times=[("21:00", "17:59")], kind="OFF_PEAK")])
    result = convert(solarmax_cdr, TODAY)
    assert "30 kWh limit and no rate after it" in result.skipped[0]


def test_capped_peak_with_its_balance_named_by_the_capped_entry(solarmax_cdr):
    # Flow Power: "Peak FiT" first 15 kWh/day at 40c, "Peak FiT balance" 10c, 5:30-9:30pm; 0c otherwise
    with_feed_in(solarmax_cdr, [
        fit_entry("0.00", period="P1Y", times=[("21:30", "17:29")], kind="OFF_PEAK", name="Off-Peak FiT"),
        fit_entry("0.10", period="P1Y", times=[("17:30", "21:29")], name="Peak FiT balance"),
        fit_entry("0.40", volume=15, times=[("17:30", "21:29")], name="Peak FiT"),
    ])
    plan = convert(solarmax_cdr, TODAY).plans[0]
    peak = plan["export"]["periods"][0]
    assert (peak["id"], peak["tiers"]) == ("peak_fit", [{"up_to_kwh": 15.0, "rate": 0.4}, {"rate": 0.1}])
    assert plan["export"]["periods"][1]["id"] == "off_peak_fit"


def test_feed_in_seasons_from_entry_dates(solarmax_cdr):
    # Red Energy style: summer (Nov – Mar) and winter (Apr – Oct) entries with their own rates; usage has no seasons
    summer, winter = ("2024-11-01", "2025-03-31"), ("2025-04-01", "2025-10-31")
    with_feed_in(solarmax_cdr, [
        fit_entry("0.154", period="P1Y", times=[("16:00", "19:59")], dates=summer, name="Smart"),
        fit_entry("0.063", period="P1Y", times=[("16:00", "19:59")], dates=winter, name="Smart"),
        fit_entry("0.025", period="P1Y", times=[("20:00", "15:59")], kind="SHOULDER", dates=summer, name="Solar"),
        fit_entry("0.025", period="P1Y", times=[("20:00", "15:59")], kind="SHOULDER", dates=winter, name="Solar"),
    ])
    plan = convert(solarmax_cdr, TODAY).plans[0]
    assert validate_plan(plan).ok, validate_plan(plan).errors
    assert plan["seasons"] == {"nov_mar": [11, 12, 1, 2, 3], "apr_oct": [4, 5, 6, 7, 8, 9, 10]}
    resolver = Resolver(load_plan(plan))
    assert resolver.rate_at("export", at("2026-12-02 17:00")).rate == 0.154
    assert resolver.rate_at("export", at("2026-07-01 17:00")).rate == 0.063
    assert resolver.rate_at("export", at("2026-07-01 12:00")).rate == 0.025
    # The same 2.5c in both seasons is one period, with no seasons on its windows
    solar = [p for p in plan["export"]["periods"] if p.get("rate") == 0.025]
    assert len(solar) == 1 and solar[0]["default"]


def test_feed_in_seasons_unlike_the_usage_seasons_are_skipped(solarmax_cdr):
    contract = solarmax_cdr["data"]["electricityContract"]
    base = contract["tariffPeriod"][0]
    contract["tariffPeriod"] = [
        {**copy.deepcopy(base), "displayName": "Summer", "startDate": "12-01", "endDate": "02-28"},
        {**copy.deepcopy(base), "displayName": "Rest", "startDate": "03-01", "endDate": "11-30"},
    ]
    with_feed_in(solarmax_cdr, [
        fit_entry("0.10", period="P1Y", dates=("2024-11-01", "2025-03-31")),
        fit_entry("0.05", period="P1Y", dates=("2025-04-01", "2025-10-31")),
    ])
    result = convert(solarmax_cdr, TODAY)
    assert "feed-in seasons differ from the usage seasons" in result.skipped[0]


def test_one_date_range_on_every_entry_is_not_a_season(solarmax_cdr):
    # Sumo: a single 0c entry dated April to December: when the rate applies, not a season
    with_feed_in(solarmax_cdr, [fit_entry("0.00", dates=("2026-04-01", "2026-12-31"), name="Sumo Zero Feed-In")])
    result = convert(solarmax_cdr, TODAY)
    plan = result.plans[0]
    assert "seasons" not in plan
    assert plan["export"]["periods"] == [{"id": "feed_in", "name": "Feed-in", "rate": 0.0,
                                          "default": True}]
    assert any("2026-04-01 to 2026-12-31 only; used all year" in note for note in result.notes)


# ----- seasons listed separately, as Energy Made Easy data does (Amber Solar Sharer) -----------------------------


def tou(name, rate, *times, kind="PEAK", volume=None):
    rates = [{"unitPrice": ex(rate), **({"volume": volume} if volume else {})}]
    if volume:
        rates.append({"unitPrice": ex(0.275597)})
    return {"type": kind, "displayName": name, "rates": rates, "period": "P1D",
            "timeOfUse": [{"days": ALL, "startTime": a, "endTime": b} for a, b in times]}


def season(start, end, *rates):
    return {"displayName": "Time of Use Tariff Period", "startDate": start, "endDate": end,
            "rateBlockUType": "timeOfUseRates", "timeOfUseRates": list(rates)}


def test_each_rate_is_one_period_across_seasons(solarmax_cdr):
    peak = lambda: tou("Peak", 0.637177, ("15:00", "20:59"))  # noqa: E731
    free = lambda: tou("Free Period", 0.0, ("11:00", "13:59"), kind="SOLAR_SPONGE", volume=24)  # noqa: E731
    contract = solarmax_cdr["data"]["electricityContract"]
    contract["tariffPeriod"] = [
        season("11-01", "03-31", peak(), free(), tou("Off Peak", 0.275597, ("21:00", "10:59"), ("14:00", "14:59"),
                                                     kind="OFF_PEAK")),
        season("06-01", "08-31", peak(), free(), tou("Off Peak", 0.275597, ("21:00", "10:59"), ("14:00", "14:59"),
                                                     kind="OFF_PEAK")),
        season("04-01", "05-31", free(), tou("Off Peak", 0.275597, ("14:00", "10:59"), kind="OFF_PEAK")),
        season("09-01", "10-31", free(), tou("Off Peak", 0.275597, ("14:00", "10:59"), kind="OFF_PEAK")),
    ]
    for tariff in contract["tariffPeriod"]:
        tariff.update({k: v for k, v in SOLARMAX["data"]["electricityContract"]["tariffPeriod"][0].items()
                       if k in ("dailySupplyCharge", "dailySupplyChargeType")})
    plan = convert(solarmax_cdr, TODAY).plans[0]
    assert validate_plan(plan).ok, validate_plan(plan).errors
    assert plan["seasons"] == {"nov_mar": [11, 12, 1, 2, 3], "jun_aug": [6, 7, 8], "apr_may": [4, 5],
                               "sep_oct": [9, 10]}
    periods = {p["id"]: p for p in plan["import"]["periods"]}
    assert set(periods) == {"peak", "free_period", "off_peak"}
    # Free is the same in every season: no seasons on its window
    assert periods["free_period"]["windows"] == [{"days": "all", "start": "11:00", "end": "14:00"}]
    assert periods["peak"]["windows"] == [{"days": "all", "start": "15:00", "end": "21:00",
                                           "seasons": ["jun_aug", "nov_mar"]}]
    resolver = Resolver(load_plan(plan))
    assert resolver.rate_at("import", at("2026-12-02 16:00")).period.id == "peak"
    assert resolver.rate_at("import", at("2026-07-01 16:00")).period.id == "peak"
    assert resolver.rate_at("import", at("2026-04-15 16:00")).period.id == "off_peak"  # no peak in autumn
    assert resolver.rate_at("import", at("2026-04-15 12:00")).period.id == "free_period"


def test_one_plan_per_network_when_it_has_several_names(solarmax_cdr):
    solarmax_cdr["data"]["geography"]["distributors"] = ["Essential Energy", "Essential Energy Far West"]
    plans = convert(solarmax_cdr, TODAY).plans
    assert [p["dnsp"] for p in plans] == ["Essential Energy"]


def test_retailer_name_is_trimmed(solarmax_cdr):
    solarmax_cdr["data"]["brandName"] = "GEE Energy "
    assert convert(solarmax_cdr, TODAY).plans[0]["retailer"] == "GEE Energy"


def test_seasonal_prices_are_named_by_season_not_price(solarmax_cdr):
    # Endeavour high/low season peak: same name, different price. Ids must survive next year's prices.
    contract = solarmax_cdr["data"]["electricityContract"]
    supply = {k: v for k, v in contract["tariffPeriod"][0].items() if k in ("dailySupplyCharge", "dailySupplyChargeType")}
    contract["tariffPeriod"] = [
        {**season("11-01", "03-31", tou("Peak", 0.465719, ("16:00", "19:59")),
                  tou("Off Peak", 0.356907, ("20:00", "15:59"), kind="OFF_PEAK")), **supply,
         "displayName": "Season 1 (1 Nov to 31 Mar)"},
        {**season("04-01", "10-31", tou("Peak", 0.471379, ("16:00", "19:59")),
                  tou("Off Peak", 0.356907, ("20:00", "15:59"), kind="OFF_PEAK")), **supply,
         "displayName": "Season 2 (1 Apr to 31 Oct)"},
    ]
    plan = convert(solarmax_cdr, TODAY).plans[0]
    assert validate_plan(plan).ok, validate_plan(plan).errors
    assert plan["seasons"] == {"nov_mar": [11, 12, 1, 2, 3], "apr_oct": [4, 5, 6, 7, 8, 9, 10]}
    ids = {p["id"]: p["name"] for p in plan["import"]["periods"]}
    assert ids == {"peak_nov_mar": "Peak (Nov – Mar)", "peak_apr_oct": "Peak (Apr – Oct)", "off_peak": "Off Peak"}


@pytest.mark.parametrize("text, name", [
    ("Ausgrid Solar Sharer Peak", "Peak"),
    ("Endeavour Energy Time of Use Off Peak AM", "Off Peak AM"),
    ("Essential Energy Solar Sharer Free Period", "Free Period"),
    ("Endeavour Energy Solar Sponge", "Solar Sponge"),
    ("TOU peak", "Peak"),
    ("TOU Rate Solar FiT", "Solar FiT"),
    ("Peak", "Peak"),
    ("Solar Sharer", "Solar Sharer"),  # nothing left: keep it as given
    ("Ausgrid", "Ausgrid"),
    ("", "Anytime"),
])
def test_rate_names_drop_the_network_and_tariff_words(text, name):
    assert rate_name(text, "Anytime") == name


def test_one_feed_in_period_is_called_feed_in():
    periods = [{"id": "single_rate_solar_fit", "name": "Single Rate Solar FIT", "default": True,
                "tiers": [{"up_to_kwh": 8, "rate": 0.08, "use_period": "single_rate_solar_fit"}, {"rate": 0.03}]}]
    assert cdr_module.name_feed_in(periods) == "Single Rate Solar FIT"
    assert periods[0]["id"] == "feed_in" and periods[0]["name"] == "Feed-in"
    assert periods[0]["tiers"][0]["use_period"] == "feed_in"
    tou = [{"id": "evening_fit", "name": "Evening FiT"}, {"id": "fit", "name": "FiT", "default": True}]
    assert cdr_module.name_feed_in(tou) is None and tou[0]["id"] == "evening_fit"  # TOU feed-in keeps its names


def test_source_links_the_plan_page():
    assert cdr_module.plan_page("ALI1151050MRE4@EME") == "https://www.energymadeeasy.gov.au/plan?id=ALI1151050MRE4"
    assert cdr_module.plan_page("ABC123@VEC") == "https://www.energymadeeasy.gov.au/"


# ----- demand charges (hand-written in the shapes Energy Made Easy data takes) ----------------------------------


def demand(amount, name="Demand", *, start="00:00", end="00:00", days=None, text="", **extra):
    charge = {"displayName": name, "description": text, "amount": amount, "startTime": start, "endTime": end,
              "measureUnit": "KWH", "chargePeriod": "DAY", "measurementPeriod": "DAY", "minDemand": "0.00", **extra}
    if days:
        charge["days"] = days
    return charge


def demand_period(start, end, *charges, name="Demand"):
    return {"displayName": name, "startDate": start, "endDate": end, "rateBlockUType": "demandCharges",
            "demandCharges": list(charges)}


WEEKDAYS = ALL[:5]


def with_demand(detail, *charges):
    detail["data"]["electricityContract"]["tariffPeriod"][0]["demandCharges"] = list(charges)
    return convert(detail, TODAY)


def test_demand_window_read_from_the_description(solarmax_cdr):
    # Red Energy's shape: times 00:00-00:00, the window only in the text, "per kVA"
    result = with_demand(solarmax_cdr, demand(
        ex(0.19613), "Residential Demand (CENT/kVA/DAY)",
        text="Based on the highest interval reading between 5pm and 8pm Mon to Fri. Per kVA, per day."))
    (plan,) = result.plans
    assert validate_plan(plan).ok, validate_plan(plan).errors
    assert plan["demand"] == [{
        "id": "residential_demand", "name": "Residential Demand", "rate": pytest.approx(0.19613),
        "unit": "per_kw_per_day", "windows": [{"days": "weekdays", "start": "17:00", "end": "20:00"}],
        "measure": {"interval_minutes": 30, "method": "max", "over": "month"},
    }]
    notes = plan["notes"]
    assert "times read from its description" in notes and "per kVA; counted as kW" in notes
    assert "measured over a month (not stated" in notes
    loaded = load_plan(plan)
    assert loaded.demand[0].measure.over == "month" and loaded.demand[0].windows[0].start == 17 * 60


@pytest.mark.parametrize(("text", "over"), [
    ("Highest half hour between 5pm and 8pm in a calendar month (or part thereof) in a billing period.", "month"),
    ("Highest reading between 5pm and 8pm on any day within your bill period.", "bill"),
    ("Maximum half hourly kW over the 12 months before the bill, 2pm to 8pm working weekdays.",
     "rolling_12_months"),
])
def test_demand_measured_over_what_the_text_says(solarmax_cdr, text, over):
    result = with_demand(solarmax_cdr, demand(ex(0.25), text=text))
    assert result.plans, result.skipped
    plan = result.plans[0]
    assert plan["demand"][0]["measure"]["over"] == over
    assert "measured over a month (not stated" not in plan["notes"]


def test_demand_structured_window_and_minimum(solarmax_cdr):
    plan = with_demand(solarmax_cdr, demand(ex(0.3), "Peak Demand", start="16:00", end="20:59", days=WEEKDAYS,
                                            minDemand="1.50", measurementPeriod="MONTH",
                                            text="Measured between 4:00 pm and 8:59 pm.")).plans[0]
    (charge,) = plan["demand"]
    assert charge["windows"] == [{"days": "weekdays", "start": "16:00", "end": "21:00"}]
    assert charge["measure"] == {"interval_minutes": 30, "method": "max", "over": "month", "minimum_kw": 1.5}
    assert "Peak Demand" not in plan["notes"]


def test_demand_seasons_from_the_charge_names(solarmax_cdr):
    # Alinta's shape: two charges beside an all-year usage rate, months only in the text
    plan = with_demand(
        solarmax_cdr,
        demand(ex(0.5), "Demand Summer (c/kW/day)", start="16:00", end="20:59", days=WEEKDAYS,
               text="November to March, Mon-Fri"),
        demand(ex(0.25), "Demand Non-Summer (c/kW/day)", start="16:00", end="20:59", days=WEEKDAYS,
               text="April to October, Mon-Fri"),
    ).plans[0]
    assert validate_plan(plan).ok, validate_plan(plan).errors
    assert plan["seasons"] == {"nov_mar": [11, 12, 1, 2, 3], "apr_oct": [4, 5, 6, 7, 8, 9, 10]}
    assert [(c["id"], c["windows"][0]["seasons"]) for c in plan["demand"]] == [
        ("demand_summer", ["nov_mar"]), ("demand_non_summer", ["apr_oct"])]


def test_demand_periods_with_dates_named_by_season(solarmax_cdr):
    # AusNet's shape: demandCharges tariff periods, the same name at two prices
    contract = solarmax_cdr["data"]["electricityContract"]
    contract["tariffPeriod"] += [
        demand_period("12-01", "03-31", demand(ex(0.35), start="15:00", end="21:00", days=WEEKDAYS,
                                               measurementPeriod="MONTH", text="Peak demand 3pm-9pm weekdays, Dec - Mar."),
                      name="Summer"),
        demand_period("04-01", "11-30", demand(ex(0.1), start="15:00", end="21:00", days=WEEKDAYS,
                                               measurementPeriod="MONTH"), name="Non-Summer"),
    ]
    plan = convert(solarmax_cdr, TODAY).plans[0]
    assert validate_plan(plan).ok, validate_plan(plan).errors
    assert {c["id"]: c["name"] for c in plan["demand"]} == {
        "demand_dec_mar": "Demand (Dec – Mar)", "demand_apr_nov": "Demand (Apr – Nov)"}


def test_one_demand_charge_listed_in_every_season_is_one_charge(solarmax_cdr):
    # United Energy's shape: four usage seasons, each listing the same rolling 12-month charge
    text = "Maximum half hourly kW over the 12 months before the bill, 2pm to 8pm working weekdays."
    contract = solarmax_cdr["data"]["electricityContract"]
    base = contract["tariffPeriod"][0]
    contract["tariffPeriod"] = []
    for start, end, label in (("11-01", "03-31", "Summer"), ("04-01", "05-31", "Non Summer/Non Winter"),
                              ("06-01", "08-31", "Winter"), ("09-01", "10-31", "Non Summer/Non Winter")):
        contract["tariffPeriod"].append({**copy.deepcopy(base), "startDate": start, "endDate": end,
                                         "displayName": label,
                                         "demandCharges": [demand(ex(0.54), f"{label} Demand Charge", text=text)]})
    plan = convert(solarmax_cdr, TODAY).plans[0]
    assert validate_plan(plan).ok, validate_plan(plan).errors
    (charge,) = plan["demand"]
    assert charge["name"] == "Demand Charge" and charge["id"] == "demand_charge"
    assert charge["windows"] == [{"days": "weekdays", "start": "14:00", "end": "20:00"}]
    assert charge["measure"]["over"] == "rolling_12_months"


@pytest.mark.parametrize(("charge", "reason"), [
    (demand("0.00", text="between 4pm and 8pm Mon to Fri"), "priced at $0"),
    (demand("9.24", "Capacity Charge (cents per kW pr day)"), "isn't a believable $/kW price"),
    (demand("0.000919", start="15:00", end="21:00"), "isn't a believable $/kW price"),
    (demand("0.62", chargePeriod="MONTH", start="10:00", end="18:00"), "looks like a daily price"),
    (demand("0.42", "All kVA", chargePeriod="MONTH", text="160.71 per annum"), "yearly figure"),
    (demand("0.13", text="Billable demand is the greater of this bill's demand and the network's peak."),
     "greater of"),
    (demand("0.35", start="15:00", end="21:00", text="between 3pm and 9pm during Summer (1 November to 31 March) "
                                                    "and Winter (1 June to 31 August)"), "several month ranges"),
    (demand("0.3", start="16:00", end="20:59", text="between 5pm and 8pm"), "other times than its data"),
])
def test_demand_charges_that_cant_be_read_skip_the_plan(solarmax_cdr, charge, reason):
    result = with_demand(solarmax_cdr, charge)
    assert not result.plans
    assert reason in result.skipped[0]
