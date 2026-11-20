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

    report = chk.check(fetch, tmp_path, per_path=3, today=date(2026, 11, 20), pause=0, log=lambda *_: None)
    assert report["plans_tried"] == 2
    assert report["outcomes"] == {"converted": 1, "skipped": 1}
    assert report["reasons"] == {"skipped: FLEXIBLE pricing can't be expressed as fixed rates": 1}
    assert report["time_zones"] == {"LOCAL": 2}
    saved = json.loads((tmp_path / "samples" / "covau" / "COV1117612MRE3@EME.json").read_text())
    assert saved == SOLARMAX
    assert (tmp_path / "report.json").exists()
