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
from engine.importers.cdr import CdrImportError, convert, map_distributor, with_gst

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


def test_demand_and_tou_controlled_load_noted(solarmax_cdr):
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
    assert "Demand charges left out" in plan["notes"] and "CL2" in plan["notes"]
