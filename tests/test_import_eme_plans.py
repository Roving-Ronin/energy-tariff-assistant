"""scripts/import_eme_plans.py with a fake Energy Made Easy (no network)."""

from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import import_eme_plans as imp  # noqa: E402
from test_importer_cdr import SOLARMAX  # noqa: E402

from engine import validate_plan  # noqa: E402

TODAY = date(2026, 11, 21)
URI = "https://cdr.energymadeeasy.gov.au/covau"


def listed(plan_id, *, distributors=("Essential Energy",), customer="RESIDENTIAL", updated="2026-09-01"):
    return {"planId": plan_id, "fuelType": "ELECTRICITY", "customerType": customer, "lastUpdated": updated,
            "geography": {"distributors": list(distributors)}}


def detail_for(plan_id, *, distributors=("Essential Energy",), pricing="TIME_OF_USE", name=None):
    detail = copy.deepcopy(SOLARMAX)
    detail["data"]["planId"] = plan_id
    detail["data"]["geography"]["distributors"] = list(distributors)
    detail["data"]["electricityContract"]["pricingModel"] = pricing
    if name:
        detail["data"]["displayName"] = name
    return detail


@pytest.fixture
def library(tmp_path, monkeypatch):
    """An empty library of our own: tests never see (or end) the real plans."""
    plans_dir = tmp_path / "plans"
    monkeypatch.setattr(imp, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(imp, "plan_files", lambda: sorted(plans_dir.rglob("*.json")))
    return plans_dir


@pytest.fixture
def eme(monkeypatch, library):
    """A fake EME with one retailer path; set .plans (listing) and .details (by plan ID)."""
    monkeypatch.setattr(imp.eme, "fallback_brands", lambda: [{"name": "CovaU", "uri": URI, "brand": "covau"}])

    class Fake:
        def __init__(self):
            self.plans, self.details, self.fetched = [], {}, []
            self.down = False

        def __call__(self, url, headers):
            if self.down:
                raise OSError("EME is down")
            if url.endswith("page=1"):
                return {"data": {"plans": self.plans}}
            plan_id = url.rsplit("/", 1)[1]
            self.fetched.append(plan_id)
            return self.details[plan_id]

    return Fake()


def run(eme, **kwargs):
    args = {"dnsps": ["Ausgrid", "Essential Energy"], "customer": "residential", "pricing": "tou",
            "per_retailer": 3, "today": TODAY, "pause": 0, "log": lambda _: None, "write": True}
    return imp.run(eme, **{**args, **kwargs})


def plans_in(library):
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(library.rglob("*.json"))}


def offer(eme, plan_id, **kwargs):
    """List a plan and serve its detail; returns the detail to tweak."""
    eme.plans = [p for p in eme.plans if p["planId"] != plan_id] + [listed(plan_id)]
    eme.details[plan_id] = detail = detail_for(plan_id, **kwargs)
    return detail


def set_peak(detail, inc):
    """Change the plan's peak price (GST-inclusive dollars)."""
    for tariff in detail["data"]["electricityContract"]["tariffPeriod"]:
        for rate in tariff.get("timeOfUseRates") or []:
            if rate["type"] == "PEAK":
                rate["rates"] = [{"unitPrice": f"{inc / 1.1:.6f}"}]


def peak(plan):
    return next(p for p in plan["import"]["periods"] if p["name"].lower() == "peak")


def test_new_plans_are_converted_validated_and_written(eme, library):
    offer(eme, "COV2000001MRE1@EME", distributors=("Ausgrid", "Endeavour"), name="Solar Saver")
    eme.plans[-1]["geography"]["distributors"] = ["Ausgrid", "Endeavour Energy"]
    report = run(eme)
    assert [(p["dnsp"], p["plan_id"]) for p in report.written] == [("Ausgrid", "COV2000001MRE1")]
    plan = plans_in(library)["CovaU-SolarSaver-NSW-Ausgrid-20260701.json"]
    assert validate_plan(plan).ok and plan["needs_review"] is True
    assert plan["plan_id"] == "COV2000001MRE1"  # stored without the @EME suffix, like the library


def test_unchanged_plans_are_left_alone(eme, library):
    offer(eme, "COV2@EME")
    run(eme)
    before = plans_in(library)
    report = run(eme, today=date(2026, 11, 28))
    assert report.already and not (report.written or report.updated or report.new_versions or report.withdrawn)
    assert plans_in(library) == before


def test_same_id_new_prices_from_a_later_date_is_a_new_file(eme, library):
    offer(eme, "COV3@EME")
    run(eme)
    detail = offer(eme, "COV3@EME")
    set_peak(detail, 0.5)
    detail["data"]["effectiveFrom"] = "2027-07-01T00:00:00Z"
    report = run(eme, today=date(2027, 7, 2))
    assert len(report.new_versions) == 1 and "new prices" in report.new_versions[0]
    files = plans_in(library)
    assert len(files) == 2
    old, new = (files[name] for name in sorted(files))
    assert old["effective_to"] == "2027-06-30" and new["effective_from"] == "2027-07-01"
    assert peak(new)["rate"] == 0.5
    assert [p["id"] for p in new["import"]["periods"]] == [p["id"] for p in old["import"]["periods"]]


def test_same_id_new_content_same_date_is_updated_in_place(eme, library):
    offer(eme, "COV4@EME")
    run(eme)
    set_peak(eme.details["COV4@EME"], 0.5)
    report = run(eme, today=date(2026, 11, 28))
    assert len(report.updated) == 1
    (plan,) = plans_in(library).values()
    assert peak(plan)["rate"] == 0.5


def test_new_version_of_a_plan_family_keeps_period_ids(eme, library):
    offer(eme, "COV5MRE1@EME")
    run(eme)
    eme.plans, eme.details = [], {}
    detail = offer(eme, "COV5MRE2@EME")
    detail["data"]["effectiveFrom"] = "2027-07-01T00:00:00Z"
    for tariff in detail["data"]["electricityContract"]["tariffPeriod"]:  # the retailer renames a rate
        for rate in tariff.get("timeOfUseRates") or []:
            if rate["type"] == "PEAK":
                rate["displayName"] = "Peak usage"
    report = run(eme, today=date(2027, 7, 2))
    assert len(report.new_versions) == 1 and "new version of COV5MRE1" in report.new_versions[0]
    old, new = sorted(plans_in(library).values(), key=lambda p: p["effective_from"])
    assert old["effective_to"] == "2027-06-30" and new["plan_id"] == "COV5MRE2"
    # "Peak usage" has the same times as "Peak", so it keeps peak's id: the meters carry on
    assert {p["id"] for p in new["import"]["periods"]} == {p["id"] for p in old["import"]["periods"]}
    assert not report.withdrawn


def test_plans_no_longer_listed_are_ended_not_deleted(eme, library):
    offer(eme, "COV6@EME")
    run(eme)
    eme.plans = []
    report = run(eme, today=date(2026, 12, 5))
    assert len(report.withdrawn) == 1
    (plan,) = plans_in(library).values()
    assert plan["effective_to"] == "2026-12-05" and "Not on Energy Made Easy any more" in plan["notes"]


def test_a_new_id_with_the_same_name_replaces_the_one_that_went(eme, library):
    offer(eme, "COV7@EME", name="Everyday Saver")
    run(eme)
    eme.plans, eme.details = [], {}
    detail = offer(eme, "COV8888@EME", name="Everyday Saver")
    detail["data"]["effectiveFrom"] = "2027-01-01T00:00:00Z"
    report = run(eme, today=date(2027, 1, 2))
    assert len(report.replaced) == 1 and "replaces COV7" in report.replaced[0]
    assert not report.written and not report.withdrawn
    old, new = sorted(plans_in(library).values(), key=lambda p: p["effective_from"])
    assert old["effective_to"] == "2026-12-31" and new["plan_id"] == "COV8888"


def test_reviewed_and_hand_made_plans_are_never_changed(eme, library):
    offer(eme, "COV9@EME")
    run(eme)
    (path,) = library.rglob("*.json")
    plan = json.loads(path.read_text())
    plan["needs_review"] = False
    path.write_text(json.dumps(plan))
    set_peak(eme.details["COV9@EME"], 0.5)
    report = run(eme, today=date(2026, 11, 28))
    assert len(report.reviewed_differs) == 1 and json.loads(path.read_text()) == plan
    eme.plans = []  # gone from EME: still left alone, and listed
    report = run(eme, today=date(2026, 11, 28))
    assert "not on Energy Made Easy any more" in report.reviewed_differs[0]
    assert json.loads(path.read_text()) == plan


def test_nothing_is_ended_when_part_of_the_list_couldnt_be_read(eme, library):
    offer(eme, "COV10@EME")
    run(eme)
    eme.down = True
    report = run(eme, today=date(2026, 12, 5))
    assert report.failed and not report.withdrawn
    assert all(p["effective_to"] is None for p in plans_in(library).values())


def test_keep_ids_follows_tier_references():
    old = {"import": {"periods": [
        {"id": "free", "name": "Free", "tiers": [{"up_to_kwh": 50, "rate": 0}, {"use_period": "shoulder"}],
         "windows": [{"days": "all", "start": "11:00", "end": "14:00"}]},
        {"id": "shoulder", "name": "Shoulder", "rate": 0.27, "default": True}]}}
    new = {"import": {"periods": [
        {"id": "free_period", "name": "Free period", "tiers": [{"up_to_kwh": 50, "rate": 0},
                                                                {"use_period": "off_peak"}],
         "windows": [{"days": "all", "start": "11:00", "end": "14:00"}]},
        {"id": "off_peak", "name": "Shoulder", "rate": 0.28, "default": True}]}}
    assert imp.keep_ids(old, new) == []
    assert [p["id"] for p in new["import"]["periods"]] == ["free", "shoulder"]
    assert new["import"]["periods"][0]["tiers"][1] == {"use_period": "shoulder"}


def test_filters_customer_type_pricing_and_count(eme):
    eme.plans = [
        listed("COV3@EME", customer="BUSINESS"),
        listed("COV4@EME", distributors=("Jemena",)),
        listed("COV5@EME", updated="2026-10-01"),
        listed("COV6@EME", updated="2026-09-01"),
        listed("COV7@EME", updated="2026-08-01"),
    ]
    eme.details = {pid: detail_for(pid, name=f"Plan {pid[:4]}") for pid in ("COV5@EME", "COV6@EME", "COV7@EME")}
    eme.details["COV6@EME"]["data"]["electricityContract"]["pricingModel"] = "SINGLE_RATE"
    report = run(eme, per_retailer=1)
    assert eme.fetched == ["COV5@EME"]
    assert [p["plan_id"] for p in report.written] == ["COV5"]


def test_single_rate_plans_count_as_other_pricing(eme):
    offer(eme, "COV8@EME", pricing="SINGLE_RATE")
    report = run(eme)
    assert report.other_pricing == 1 and report.written == []


def test_unconvertible_plans_are_reported(eme):
    detail = offer(eme, "COV9@EME")
    detail["data"]["electricityContract"]["solarFeedInTariff"] = [{
        "scheme": "OTHER", "payerType": "RETAILER", "tariffUType": "singleTariff",
        "singleTariff": {"rates": [{"unitPrice": "0.1", "volume": 10}], "period": "P1D"}}]
    report = run(eme)
    assert report.written == []
    assert "10 kWh limit and no rate after it" in report.skipped[0]
    assert "Not converted (1)" in report.summary(["Essential Energy"], "residential", "tou")


def test_summary_lists_what_was_added(eme):
    offer(eme, "COV10@EME", name="Everyday Saver")
    text = run(eme).summary(["Essential Energy"], "residential", "tou")
    assert "**1** new plans, all `needs_review: true`" in text
    assert "| CovaU | Everyday Saver | Essential Energy | 2026-07-01 | COV10 |" in text
