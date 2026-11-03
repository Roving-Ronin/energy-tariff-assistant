from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import FIXTURES, REPO_ROOT, read_json

from engine import Resolver, load_plan, validate_plan
from engine.importers.energy_manager import EnergyManagerImportError, convert

SOURCE_DIR = FIXTURES / "energy_manager"
SCHEMA = REPO_ROOT / "custom_components" / "energy_tariff_assistant" / "schema" / "tariff-plan.schema.json"
SYD = ZoneInfo("Australia/Sydney")

# Known bad source data: effective_from is "202x-10-03" in the community file.
BAD_SOURCE = {"SolsticeEnergy-SolsticeCircle-TAS-TasNetworks-20261003.json"}


def sources() -> list[Path]:
    return sorted(SOURCE_DIR.glob("*.json"))


needs_fixtures = pytest.mark.skipif(
    not sources(), reason="run scripts/fetch_fixtures.sh to fetch the energy-manager-tariffs files"
)


def converted(name: str) -> dict:
    return convert(read_json(SOURCE_DIR / name), name)


@needs_fixtures
def test_fixture_set_has_the_known_files():
    names = {p.name for p in sources()}
    assert len(names) >= 17
    assert "Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json" in names


@pytest.mark.parametrize("path", sources(), ids=lambda p: p.name)
def test_every_library_file_converts_and_validates(path):
    plan = convert(read_json(path), path.name)
    result = validate_plan(plan)
    if path.name in BAD_SOURCE and not result.ok:
        # Fails on the source data, not the importer; passes once upstream fixes it.
        assert result.errors == ["effective_from: not a valid date: '202x-10-03'"]
        return
    assert result.ok, result.errors
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(plan, json.loads(SCHEMA.read_text()))


@pytest.mark.parametrize("path", sources(), ids=lambda p: p.name)
def test_control_settings_are_dropped(path):
    text = json.dumps(convert(read_json(path), path.name))
    for word in ("battery", "restrict", "force_import", "input_"):
        assert word not in text


@needs_fixtures
def test_solarmax_conversion_charges_the_same_as_the_seed_plan(solarmax):
    """The converted file and the hand-tidied seed plan differ in ids and
    names, but must charge the same rate at every half hour of a week,
    with tiers both under and over their caps."""
    imported = Resolver(load_plan(converted("Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json")))
    start = datetime(2026, 11, 2, tzinfo=SYD)
    for usage_seed, usage_conv in (({}, {}), ({"free": 60, "super": 40}, {"free": 60, "export_1": 40})):
        for step in range(7 * 48):
            moment = start + timedelta(minutes=30 * step)
            for stream in ("import", "export"):
                expected = solarmax.rate_at(stream, moment, usage_seed).rate
                actual = imported.rate_at(stream, moment, usage_conv).rate
                assert actual == pytest.approx(expected), (stream, moment)


@needs_fixtures
def test_capped_all_day_export_pays_nothing_after_the_cap():
    plan = converted("AGL-SmartSaver-QLD-Energex-20260701.json")
    (period,) = plan["export"]["periods"]
    assert period["tiers"] == [{"up_to_kwh": 8.0, "rate": 0.02}, {"rate": 0.0}]
    assert period["default"] is True
    assert plan["needs_review"] is True


@needs_fixtures
def test_capped_window_export_falls_back_to_other_export_rate():
    plan = converted("Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json")
    periods = {p["id"]: p for p in plan["export"]["periods"]}
    assert periods["export_1"]["tiers"] == [{"up_to_kwh": 30.0, "rate": 0.15}, {"use_period": "export_2"}]
    assert periods["export_2"]["default"] is True


@needs_fixtures
def test_metadata_mapping():
    plan = converted("Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json")
    assert plan["retailer"] == "Covau"
    assert plan["plan"] == "SolarMax Residential TOU"
    assert plan["region"] == "NSW"
    assert plan["dnsp"] == "Essential Energy"
    assert plan["timezone"] == "Australia/Sydney"
    assert plan["supply_charge"] == {"rate": 2.29999, "unit": "per_day"}
    assert plan["source"]["checked"] == "2026-08-25"
    assert converted("Covau-Basics-QLD-Energex-20260701.json")["timezone"] == "Australia/Brisbane"


@needs_fixtures
def test_full_coverage_is_not_flagged_as_a_gap():
    for name in ("Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json", "GloBird-FOUR4FREE-NSW-EssentialEnergy-20260701.json"):
        assert "no import slot" not in converted(name)["notes"]


@needs_fixtures
def test_plan_without_caps_needs_no_review():
    assert converted("Ovo-BasicEVPlan-NSW-Endeavour-20260701.json")["needs_review"] is False


def test_rejects_other_formats():
    with pytest.raises(EnergyManagerImportError):
        convert({"format": "ha_tariff_plan", "version": 1})
