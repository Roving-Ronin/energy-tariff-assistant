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
def eme(monkeypatch):
    """A fake EME with one retailer path; set .plans (listing) and .details (by plan ID)."""
    monkeypatch.setattr(imp.eme, "fallback_brands", lambda: [{"name": "CovaU", "uri": URI, "brand": "covau"}])

    class Fake:
        plans: list = []
        details: dict = {}
        fetched: list = []

        def __call__(self, url, headers):
            if url.endswith("page=1"):
                return {"data": {"plans": self.plans}}
            plan_id = url.rsplit("/", 1)[1]
            self.fetched.append(plan_id)
            return self.details[plan_id]

    return Fake()


def run(eme, **kwargs):
    args = {"dnsps": ["Ausgrid", "Essential Energy"], "customer": "residential", "pricing": "tou",
            "per_retailer": 3, "today": TODAY, "pause": 0, "log": lambda _: None, "write": False}
    return imp.run(eme, **{**args, **kwargs})


def test_plans_already_in_the_library_are_left_alone(eme):
    # Covau SolarMax on Essential Energy is in the library: reviewed plans keep their period ids
    eme.plans = [listed("COV1117612MRE3@EME")]
    report = run(eme)
    assert report.written == [] and eme.fetched == []
    assert report.already == ["COV1117612MRE3@EME (Essential Energy)"]


def test_new_plans_are_converted_validated_and_written(eme, tmp_path, monkeypatch):
    monkeypatch.setattr(imp, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(imp, "library_keys", lambda: set())
    eme.plans = [listed("COV2000001MRE1@EME", distributors=("Ausgrid", "Endeavour Energy"))]
    eme.details = {"COV2000001MRE1@EME": detail_for("COV2000001MRE1@EME", distributors=("Ausgrid", "Endeavour"),
                                                    name="Solar Saver")}
    report = run(eme, write=True)
    # Only the distributor asked for
    assert [(p["dnsp"], p["plan_id"]) for p in report.written] == [("Ausgrid", "COV2000001MRE1")]
    path = tmp_path / "plans" / "AU" / "NSW" / "CovaU-SolarSaver-NSW-Ausgrid-20260701.json"
    assert path.is_file()
    plan = json.loads(path.read_text(encoding="utf-8"))
    assert validate_plan(plan).ok and plan["needs_review"] is True
    assert plan["plan_id"] == "COV2000001MRE1"  # stored without the @EME suffix, like the library


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
    # Business and Jemena plans are never fetched; the newest TOU plan is taken, then the limit is reached
    assert eme.fetched == ["COV5@EME"]
    assert [p["plan_id"] for p in report.written] == ["COV5"]


def test_single_rate_plans_count_as_other_pricing(eme):
    eme.plans = [listed("COV8@EME")]
    eme.details = {"COV8@EME": detail_for("COV8@EME", pricing="SINGLE_RATE")}
    report = run(eme)
    assert report.other_pricing == 1 and report.written == []


def test_unconvertible_plans_are_reported(eme):
    eme.plans = [listed("COV9@EME")]
    detail = detail_for("COV9@EME")
    detail["data"]["electricityContract"]["solarFeedInTariff"] = [{
        "scheme": "OTHER", "payerType": "RETAILER", "tariffUType": "singleTariff",
        "singleTariff": {"rates": [{"unitPrice": "0.1", "volume": 10}], "period": "P1D"}}]
    eme.details = {"COV9@EME": detail}
    report = run(eme)
    assert report.written == []
    assert "10 kWh limit and no rate after it" in report.skipped[0]
    assert "Not converted (1)" in report.summary(["Essential Energy"], "residential", "tou")


def test_summary_lists_what_was_added(eme):
    eme.plans = [listed("COV10@EME")]
    eme.details = {"COV10@EME": detail_for("COV10@EME", name="Everyday Saver")}
    text = run(eme).summary(["Essential Energy"], "residential", "tou")
    assert "**1** plans added, all `needs_review: true`" in text
    assert "| CovaU | Everyday Saver | Essential Energy | 2026-07-01 | COV10 |" in text
