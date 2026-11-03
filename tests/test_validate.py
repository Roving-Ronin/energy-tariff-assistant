from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest
from conftest import PLANS, REPO_ROOT, minimal_plan, read_json

from engine import validate_plan

SCHEMA = REPO_ROOT / "custom_components" / "energy_tariff_assistant" / "schema" / "tariff-plan.schema.json"


def errors_of(plan) -> str:
    return " | ".join(validate_plan(plan).errors)


def with_import(*periods):
    return minimal_plan(**{"import": {"periods": list(periods)}})


DEFAULT = {"id": "anytime", "name": "Anytime", "rate": 0.30, "default": True}


def window(start, end, days="all"):
    return {"days": days, "start": start, "end": end}


def test_seed_plan_is_valid(solarmax_data):
    result = validate_plan(solarmax_data, today=date(2026, 11, 4))
    assert result.ok, result.errors
    assert result.warnings == []


def test_minimal_plan_is_valid():
    assert validate_plan(minimal_plan()).ok


def test_overlap_is_reported_with_first_minute():
    plan = with_import(
        {"id": "a", "name": "A", "rate": 0.1, "windows": [window("10:00", "12:00")]},
        {"id": "b", "name": "B", "rate": 0.2, "windows": [window("11:30", "13:00")]},
        DEFAULT,
    )
    assert "periods 'a' and 'b' overlap, first at mon 11:30" in errors_of(plan)


def test_wrapping_windows_overlap_after_midnight():
    plan = with_import(
        {"id": "a", "name": "A", "rate": 0.1, "windows": [window("22:00", "02:00")]},
        {"id": "b", "name": "B", "rate": 0.2, "windows": [window("01:00", "03:00")]},
        DEFAULT,
    )
    assert "first at mon 01:00" in errors_of(plan)


def test_same_times_on_different_days_do_not_overlap():
    plan = with_import(
        {"id": "a", "name": "A", "rate": 0.1, "windows": [window("10:00", "12:00", "weekdays")]},
        {"id": "b", "name": "B", "rate": 0.2, "windows": [window("10:00", "12:00", "weekends")]},
        DEFAULT,
    )
    assert validate_plan(plan).ok


@pytest.mark.parametrize(
    ("periods", "message"),
    [
        ([{"id": "a", "name": "A", "rate": 0.1, "windows": [window("10:00", "12:00")]}], "found 0"),
        ([DEFAULT, dict(DEFAULT, id="other")], "found 2"),
        ([dict(DEFAULT, windows=[window("10:00", "12:00")])], "must not have windows"),
        ([{"id": "a", "name": "A", "rate": 0.1}, DEFAULT], "required on every period except the default"),
        ([{"id": "a", "name": "A", "rate": 0.1, "windows": [window("00:00", "00:00")]}, DEFAULT], "use all_day"),
        ([{"id": "a", "name": "A", "rate": 0.1, "windows": [window("00:00", "24:00")]}, DEFAULT], "use all_day"),
        ([{"id": "a", "name": "A", "rate": 0.1, "windows": [window("9:00", "12:00")]}, DEFAULT], "must be HH:MM"),
        ([{"id": "a", "name": "A", "rate": 0.1, "windows": [window("10:00", "25:00")]}, DEFAULT], "must be HH:MM"),
        ([{"id": "A", "name": "A", "rate": 0.1, "windows": [window("10:00", "12:00")]}, DEFAULT], "lowercase"),
        ([{"id": "a", "name": "A", "windows": [window("10:00", "12:00")]}, DEFAULT], "exactly one of rate or tiers"),
        ([dict(DEFAULT, rate=0.1, tiers=[{"up_to_kwh": 1, "rate": 0}, {"rate": 1}])], "exactly one of rate or tiers"),
    ],
)
def test_period_rules(periods, message):
    assert message in errors_of(with_import(*periods))


@pytest.mark.parametrize(
    ("tiers", "message"),
    [
        ([{"up_to_kwh": 10, "rate": 0}], "at least two tiers"),
        ([{"up_to_kwh": 10, "rate": 0}, {"up_to_kwh": 20, "rate": 1}], "last tier must not have up_to_kwh"),
        ([{"rate": 0}, {"rate": 1}], "required, a number above 0"),
        ([{"up_to_kwh": 20, "rate": 0}, {"up_to_kwh": 10, "rate": 1}, {"rate": 2}], "larger than the previous"),
        ([{"up_to_kwh": 10, "rate": 0, "use_period": "anytime"}, {"rate": 1}], "exactly one of rate or use_period"),
        ([{"up_to_kwh": 10, "rate": 0}, {"use_period": "missing"}], "no period 'missing'"),
    ],
)
def test_tier_rules(tiers, message):
    capped = {"id": "free", "name": "Free", "tiers": tiers, "windows": [window("11:00", "14:00")]}
    assert message in errors_of(with_import(capped, DEFAULT))


def test_use_period_loop():
    a = {"id": "a", "name": "A", "tiers": [{"up_to_kwh": 1, "rate": 0}, {"use_period": "b"}], "windows": [window("10:00", "11:00")]}
    b = {"id": "b", "name": "B", "tiers": [{"up_to_kwh": 1, "rate": 0}, {"use_period": "a"}], "windows": [window("12:00", "13:00")]}
    assert "loop" in errors_of(with_import(a, b, DEFAULT))


def test_seasons_rules():
    plan = with_import(
        {"id": "a", "name": "A", "rate": 0.1, "windows": [dict(window("10:00", "12:00"), seasons=["winter"])]},
        DEFAULT,
    )
    plan["seasons"] = {"summer": [12, 1, 2], "shoulder": [2, 3]}
    errors = errors_of(plan)
    assert "season 'winter' is not defined" in errors
    assert "month 2 is already in season 'summer'" in errors


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("timezone", "Australia/Gotham", "unknown time zone"),
        ("country", "Australia", "2-letter ISO"),
        ("effective_to", "2026-01-01", "before effective_from"),
        ("effective_from", "202x-10-03", "not a valid date"),
        ("colour", "blue", "unknown fields ['colour']"),
        ("format", "energy_manager_custom_tariff", "must be 'ha_tariff_plan'"),
    ],
)
def test_top_level_rules(field, value, message):
    assert message in errors_of(minimal_plan(**{field: value}))


def test_warnings_do_not_fail():
    plan = minimal_plan()
    plan["import"]["periods"][0]["rate"] = 2.5
    plan["export"]["periods"][0]["rate"] = -0.02
    result = validate_plan(plan, today=date(2028, 1, 1))
    assert result.ok
    text = " | ".join(result.warnings)
    assert "above 2.0" in text and "negative" in text and "more than 12 months" in text


def test_demand_rules():
    plan = minimal_plan(
        demand=[
            {
                "id": "peak_demand",
                "name": "Peak demand",
                "rate": 0.23452,
                "unit": "per_kw_per_day",
                "windows": [window("14:00", "20:00", "weekdays")],
                "measure": {"interval_minutes": 30, "method": "max", "over": "month"},
            }
        ]
    )
    assert validate_plan(plan).ok
    bad = copy.deepcopy(plan)
    bad["demand"][0]["measure"] = {"method": "mean_of_top_n", "interval_minutes": 7}
    errors = errors_of(bad)
    assert "required for mean_of_top_n" in errors and "must be 5, 15, 30 or 60" in errors


# ----- JSON Schema agreement ------------------------------------------------

jsonschema = pytest.importorskip("jsonschema")


def library_plans() -> list[Path]:
    return sorted(p for p in PLANS.rglob("*.json") if p.name != "index.json")


def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(json.loads(SCHEMA.read_text()))


@pytest.mark.parametrize("path", library_plans(), ids=lambda p: p.name)
def test_library_plans_match_schema(path):
    schema = json.loads(SCHEMA.read_text())
    jsonschema.validate(read_json(path), schema)
