"""scripts/check_cdr_conversion.py with canned responses (no network)."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_cdr_conversion as chk  # noqa: E402
from test_importer_cdr import SOLARMAX  # noqa: E402

from engine.importers.cdr import convert  # noqa: E402

LIBRARY_FILE = (
    Path(__file__).resolve().parent.parent
    / "plans/AU/NSW/Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json"
)


def library_solarmax():
    return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))


def test_pick_plans_mixes_customer_types():
    plans = [
        {"planId": "r1", "fuelType": "ELECTRICITY", "customerType": "RESIDENTIAL", "lastUpdated": "2026-01-01"},
        {"planId": "r2", "fuelType": "DUAL", "customerType": "RESIDENTIAL", "lastUpdated": "2026-03-01"},
        {"planId": "g", "fuelType": "GAS", "customerType": "RESIDENTIAL", "lastUpdated": "2026-09-01"},
        {"planId": "b1", "fuelType": "ELECTRICITY", "customerType": "BUSINESS", "lastUpdated": "2026-02-01"},
    ]
    assert [p["planId"] for p in chk.pick_plans(plans, 3)] == ["r2", "b1", "r1"]


def test_reason_key_groups_reasons():
    assert chk.reason_key("CovaU X (ID@EME): unknown distributor 'Foo'") == "unknown distributor …"


def test_check(tmp_path, monkeypatch):
    monkeypatch.setattr(chk.eme, "fallback_brands", lambda: [
        {"name": "CovaU", "uri": "https://cdr.energymadeeasy.gov.au/covau", "brand": "covau"},
    ])
    flexible = copy.deepcopy(SOLARMAX)
    flexible["data"]["planId"] = "FLEX@EME"
    flexible["data"]["electricityContract"]["pricingModel"] = "FLEXIBLE"
    details = {"COV1117612MRE3@EME": SOLARMAX, "FLEX@EME": flexible}

    def fetch(url, headers):
        if url.endswith("page=1"):
            assert headers == {"x-v": "1"}
            return {"data": {"plans": [
                {"planId": pid, "fuelType": "ELECTRICITY", "customerType": "RESIDENTIAL", "displayName": pid}
                for pid in details
            ]}}
        assert headers == {"x-v": "3"}
        return details[url.rsplit("/", 1)[1]]

    report = chk.check(fetch, tmp_path, per_path=3, today=date(2026, 11, 20), pause=0, log=lambda *_: None,
                       library={})
    assert report["plans_tried"] == 2
    assert report["outcomes"] == {"converted": 1, "skipped": 1}
    assert report["reasons"] == {"skipped: FLEXIBLE pricing can't be expressed as fixed rates": 1}
    assert report["time_zones"] == {"LOCAL": 2}
    saved = json.loads((tmp_path / "samples" / "covau" / "COV1117612MRE3@EME.json").read_text())
    assert saved == SOLARMAX
    assert (tmp_path / "report.json").exists()


def test_base_id():
    assert chk.base_id("COV1117612MRE3@EME") == "COV1117612MRE3"
    assert chk.base_id("PLAIN") == "PLAIN"


def test_converted_solarmax_agrees_with_the_library():
    converted = convert(SOLARMAX, date(2026, 11, 20)).plans[0]
    assert chk.compare_plans(library_solarmax(), converted, date(2026, 11, 20)) == []


def test_differences_are_reported():
    detail = copy.deepcopy(SOLARMAX)
    peak = detail["data"]["electricityContract"]["tariffPeriod"][0]["timeOfUseRates"][3]
    peak["rates"] = [{"unitPrice": "0.5"}]  # 0.55 with GST, library has 0.45331
    converted = convert(detail, date(2026, 11, 20)).plans[0]
    diffs = chk.compare_plans(library_solarmax(), converted, date(2026, 11, 20))
    assert diffs and all("import" in d and "15:00-21:00" in d and "library 0.45331 vs converted 0.55" in d
                         for d in diffs)
    # One line per day (4) x (nothing used, caps used): the 12 half hours of peak are merged
    assert len(diffs) == 4 * 2
    assert diffs[0] == "import Wed 06 Jan 15:00-21:00: library 0.45331 vs converted 0.55"


def test_covau_stated_aest_now_agrees_with_the_library():
    detail = copy.deepcopy(SOLARMAX)
    detail["data"]["electricityContract"]["timeZone"] = "AEST"  # as in its Energy Made Easy data
    converted = convert(detail, date(2026, 11, 20)).plans[0]
    assert chk.compare_plans(library_solarmax(), converted, date(2026, 11, 20)) == []


def test_covau_feed_in_as_published_agrees_with_the_library():
    # Energy Made Easy splits SolarMax's feed-in over three entries; combined they price like the library plan
    from test_importer_cdr import fit_entry

    detail = copy.deepcopy(SOLARMAX)
    detail["data"]["electricityContract"]["timeZone"] = "AEST"  # as published; CovaU bills on local time
    detail["data"]["electricityContract"]["solarFeedInTariff"] = [
        fit_entry("0.05", times=[("21:00", "17:59")], kind="OFF_PEAK"),
        fit_entry("0.05", times=[("18:00", "20:59")]),
        fit_entry("0.15", volume=30, times=[("18:00", "20:59")]),
    ]
    converted = convert(detail, date(2026, 11, 20)).plans[0]
    assert chk.compare_plans(library_solarmax(), converted, date(2026, 11, 20)) == []


def test_daylight_saving_shift_shows_as_ranges():
    detail = copy.deepcopy(SOLARMAX)
    detail["data"]["electricityContract"]["timeZone"] = "AEST"
    detail["data"]["brand"], detail["data"]["brandName"] = "example", "Example Energy"  # no known clock rule
    converted = convert(detail, date(2026, 11, 20)).plans[0]
    diffs = chk.compare_plans(library_solarmax(), converted, date(2026, 11, 20))
    assert diffs and all(" Jan " in d for d in diffs)  # July is AEST in Sydney too: no differences
    assert "import Wed 06 Jan 15:00-16:00: library 0.45331 vs converted 0.27478" in diffs
    assert "import Wed 06 Jan 21:00-22:00: library 0.27478 vs converted 0.45331" in diffs


def test_check_compares_library_plans(tmp_path, monkeypatch):
    monkeypatch.setattr(chk.eme, "fallback_brands", lambda: [
        {"name": "CovaU", "uri": "https://cdr.energymadeeasy.gov.au/covau", "brand": "covau"},
    ])
    others = [{"planId": f"X{i}@EME", "fuelType": "ELECTRICITY", "customerType": "RESIDENTIAL",
               "lastUpdated": "2026-09-01"} for i in range(3)]
    listed = [*others, {"planId": "COV1117612MRE3@EME", "fuelType": "ELECTRICITY", "customerType": "RESIDENTIAL",
                        "lastUpdated": "2026-01-01"}]

    def fetch(url, headers):
        if url.endswith("page=1"):
            return {"data": {"plans": listed}}
        plan_id = url.rsplit("/", 1)[1]
        detail = copy.deepcopy(SOLARMAX)
        detail["data"]["planId"] = plan_id
        return detail

    logged = []
    report = chk.check(fetch, tmp_path, per_path=1, today=date(2026, 11, 20), pause=0, log=logged.append,
                       library={"COV1117612MRE3": library_solarmax()})
    # The library plan is checked even though only one plan per path was picked
    assert report["plans_tried"] == 2
    assert report["library_checks"] == [{"plan_id": "COV1117612MRE3@EME", "library": "SolarMax Residential TOU",
                                         "time_zone": "LOCAL", "agrees": True, "differences": []}]
    assert any(line.startswith("LIBRARY CHECK COV1117612MRE3@EME: agrees (plan time zone: LOCAL") for line in logged)
    assert report["notes"] == {}  # nothing left out of SolarMax
