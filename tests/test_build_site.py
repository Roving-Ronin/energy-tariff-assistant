"""scripts/build_site.py and validate_plans.check_files."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_site  # noqa: E402
from _common import PLANS_DIR, load_json, plan_files  # noqa: E402
from validate_plans import check_files  # noqa: E402


def test_site_has_every_library_plan(tmp_path):
    out = tmp_path / "site"
    lib = build_site.build(out)

    index = load_json(PLANS_DIR / "index.json")
    assert sorted(p["file"] for p in lib["plans"]) == sorted(p["file"] for p in index["plans"])
    for entry in lib["plans"]:
        assert (out / entry["file"]).is_file()
        assert entry["errors"] == []
        assert set(build_site.INDEX_FIELDS) <= set(entry)
        assert isinstance(entry["confirmations"], int)  # real confirmations arrive on main
    for name in ("index.html", "app.js", "style.css", "icon.png", ".nojekyll"):
        assert (out / name).is_file()
    assert json.loads((out / "data" / "library.json").read_text(encoding="utf-8")) == lib


def test_coverage_keeps_derived_facts_only(tmp_path):
    out = tmp_path / "site"
    build_site.build(out)
    coverage = json.loads((out / "data" / "coverage.json").read_text(encoding="utf-8"))
    assert set(coverage) == {"updated_at", "source", "retailers", "distributors"}


def test_errors_and_overlaps_reach_the_site(tmp_path):
    plan = load_json(plan_files()[0])
    broken = dict(plan, effective_from="not a date")
    first, second, bad = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "c.json"
    for path, data in ((first, plan), (second, plan), (bad, broken)):
        path.write_text(json.dumps(data), encoding="utf-8")

    results = check_files([first, second, bad], today=date(2026, 11, 21))
    assert any("overlapping" in w for w in results[0].warnings)
    assert any("overlapping" in w for w in results[1].warnings)
    assert results[2].errors


    entries = build_site.library(results)["plans"]
    assert entries[2]["errors"] == results[2].errors
    assert entries[0]["needs_review"] is plan.get("needs_review", False)


def test_same_name_different_plan_ids_are_different_plans(tmp_path):
    # EME names a retailer's variants (TOU, + controlled load) the same: only the plan ID tells them apart
    plan = load_json(plan_files()[0])
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps(dict(plan, plan_id="TAN1128298SRE2")), encoding="utf-8")
    second.write_text(json.dumps(dict(plan, plan_id="TAN1128299SRE2")), encoding="utf-8")
    results = check_files([first, second], today=date(2026, 11, 21))
    assert not any("overlapping" in w for r in results for w in r.warnings)


def test_plan_types_for_the_filter():
    one = {"import": {"periods": [{"id": "anytime", "rate": 0.3, "default": True}]}}
    tou = {"import": {"periods": [{"id": "peak"}, {"id": "off_peak", "default": True}]},
           "demand": [{"id": "demand"}], "controlled_load": [{"id": "cl1"}]}
    assert build_site.plan_types(one) == ["Single rate"]
    assert build_site.plan_types(tou) == ["Time of use", "Demand", "Controlled load"]
