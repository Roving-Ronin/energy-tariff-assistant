"""Build a plan step by step: the pure assembly and the flow."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from homeassistant.data_entry_flow import FlowResultType

from custom_components.energy_tariff_assistant import wizard
from custom_components.energy_tariff_assistant.const import (
    CONF_BILLING_DAY,
    CONF_PLAN,
    CONF_PLAN_PATH,
    CONF_PLAN_SOURCE,
    CONF_SOURCE,
    DOMAIN,
    SOURCE_BUILD,
)
from custom_components.energy_tariff_assistant.engine import Resolver, load_plan, validate_plan
from custom_components.energy_tariff_assistant.wizard import AFTER_CAP_RATE, Draft, DraftPeriod

TODAY = date(2026, 11, 20)


def _solarmax_like() -> Draft:
    draft = Draft(country="AU", region="NSW", dnsp="Essential Energy", retailer="Covau",
                  plan="SolarMax Residential TOU", plan_id="COV1117612MRE3",
                  effective_from="2026-07-01", supply_charge=2.3)
    imp = draft.streams["import"]
    imp.add(DraftPeriod(name="Shoulder", rate=0.27478, default=True))
    imp.add(DraftPeriod(name="Off-peak", rate=0.165, windows=[wizard.window("all", None, "00:00:00", "06:00:00")]))
    imp.add(DraftPeriod(name="Free", rate=0.0, windows=[wizard.window("all", None, "11:00", "14:00")],
                        cap_kwh=50, after_cap="shoulder"))
    imp.add(DraftPeriod(name="Peak", rate=0.45331, windows=[wizard.window("all", None, "15:00", "21:00")]))
    exp = draft.streams["export"]
    exp.add(DraftPeriod(name="Standard FiT", rate=0.05, default=True))
    exp.add(DraftPeriod(name="Super FiT", rate=0.15, windows=[wizard.window("all", None, "18:00", "21:00")],
                        cap_kwh=30, after_cap="standard_fit"))
    return draft


def test_assembled_plan_validates_and_prices_like_the_library_file():
    plan = wizard.assemble_plan(_solarmax_like(), TODAY)
    result = validate_plan(plan)
    assert result.ok, result.errors
    resolver = Resolver(load_plan(plan))
    from zoneinfo import ZoneInfo
    from datetime import datetime
    at = lambda t: datetime.fromisoformat(t).replace(tzinfo=ZoneInfo("Australia/Sydney"))  # noqa: E731
    assert resolver.rate_at("import", at("2026-11-04 16:00")).rate == pytest.approx(0.45331)
    assert resolver.rate_at("import", at("2026-11-04 03:00")).period.id == "off_peak"
    assert resolver.rate_at("import", at("2026-11-04 12:00"), {"free": 60}).rate == pytest.approx(0.27478)
    assert resolver.rate_at("export", at("2026-11-04 19:00"), {"super_fit": 31}).rate == pytest.approx(0.05)
    assert plan["import"]["periods"][-1]["default"] is True
    assert plan["timezone"] == "Australia/Sydney"


def test_flat_with_rate_after_cap_and_custom_days():
    draft = Draft(region="QLD", retailer="R", plan="P", supply_charge=1.0)
    draft.streams["import"].add(DraftPeriod(name="Anytime", rate=0.3, default=True))
    draft.streams["export"].add(DraftPeriod(name="Feed-in", rate=0.1, default=True, cap_kwh=10,
                                            after_cap=AFTER_CAP_RATE, rate_after_cap=0.02, cap_reset="bill"))
    plan = wizard.assemble_plan(draft, TODAY)
    assert validate_plan(plan).ok
    assert plan["export"]["periods"][0]["tiers"] == [{"up_to_kwh": 10, "rate": 0.1}, {"rate": 0.02}]
    assert plan["timezone"] == "Australia/Brisbane"
    assert plan["effective_from"] == "2026-11-20"
    assert "dnsp" not in plan
    assert wizard.window("custom", ["sat", "mon"], "07:00", "09:00")["days"] == ["mon", "sat"]


def test_ids_and_filename():
    assert wizard.slug_id("Off-peak (EV)") == "off_peak_ev"
    assert wizard.unique_id("Peak", {"peak"}) == "peak_2"
    plan = wizard.assemble_plan(_solarmax_like(), TODAY)
    assert wizard.plan_filename(plan) == "Covau-SolarMaxResidentialTOU-NSW-EssentialEnergy-20260701.json"


def test_library_filters():
    entries = [
        {"file": "a", "country": "AU", "region": "NSW", "dnsp": "Ausgrid", "retailer": "B", "plan": "P1", "effective_from": "2025-07-01"},
        {"file": "b", "country": "AU", "region": "NSW", "dnsp": "Ausgrid", "retailer": "B", "plan": "P2", "effective_from": "2026-07-01"},
        {"file": "c", "country": "AU", "region": "NSW", "dnsp": "Ausgrid", "retailer": "A", "plan": "P3", "effective_from": "2026-07-01"},
        {"file": "d", "country": "AU", "region": "VIC", "retailer": "C", "plan": "P4", "effective_from": "2026-07-01"},
    ]
    assert wizard.regions(entries, "AU") == ["NSW", "VIC"]
    assert wizard.dnsps(entries, "AU", "VIC") == [""]
    assert wizard.retailers(entries, "AU", "NSW", "Ausgrid") == ["A", "B"]
    picked = wizard.filter_entries(entries, country="AU", region="NSW", dnsp="Ausgrid", retailer="B")
    assert [e["file"] for e in wizard.plans_newest_first(picked)] == ["b", "a"]
    assert wizard.pick_default(["ACT", "NSW"], "NSW") == "NSW"
    assert wizard.pick_default(["VIC"], "NSW") == "VIC"
    typed = [{"file": "r", "customer_type": "residential"}, {"file": "b", "customer_type": "business"}, {"file": "n"}]
    assert [e["file"] for e in wizard.for_customer(typed, "business")] == ["b", "n"]


async def _configure(hass, flow_id, data):
    return await hass.config_entries.flow.async_configure(flow_id, data)


async def test_build_flow(hass, sydney):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await _configure(hass, result["flow_id"], {"name": "Electricity", CONF_SOURCE: SOURCE_BUILD})
    assert result["step_id"] == "build"
    fid = result["flow_id"]
    result = await _configure(hass, fid, {"country": "au", "region": "NSW"})
    assert result["step_id"] == "build_details"
    result = await _configure(hass, fid, {
        "dnsp": "Essential Energy", "retailer": "Covau", "plan_name": "SolarMax Residential TOU",
        "effective_from": "2026-07-01", "supply_charge": 2.3, "customer_type": "residential",
        "import_type": "tou", "export_type": "flat",
    })
    assert result["step_id"] == "build_import_default"
    result = await _configure(hass, fid, {"name": "Shoulder", "rate": 0.27478})
    assert result["step_id"] == "build_import_period"
    # Cap with "rate" after it but no rate: error
    bad = {"name": "Free", "rate": 0, "days": "all", "start": "11:00:00", "end": "14:00:00", "cap_kwh": 50,
           "after_cap": AFTER_CAP_RATE}
    result = await _configure(hass, fid, bad)
    assert result["errors"] == {"rate_after_cap": "rate_after_cap_required"}
    result = await _configure(hass, fid, {**bad, "after_cap": "shoulder"})
    assert result["type"] is FlowResultType.MENU
    result = await _configure(hass, fid, {"next_step_id": "build_import_period"})
    result = await _configure(hass, fid, {"name": "Peak", "rate": 0.45, "days": "weekdays",
                                          "start": "07:00:00", "end": "07:00:00"})
    assert result["errors"] == {"end": "times_equal"}
    result = await _configure(hass, fid, {"name": "Peak", "rate": 0.45, "days": "weekdays",
                                          "start": "07:00:00", "end": "09:00:00"})
    result = await _configure(hass, fid, {"next_step_id": "build_import_window"})
    assert result["description_placeholders"]["period"] == "Peak"
    result = await _configure(hass, fid, {"days": "weekdays", "start": "17:00:00", "end": "20:00:00"})
    result = await _configure(hass, fid, {"next_step_id": "build_import_done"})
    assert result["step_id"] == "build_export_flat"
    result = await _configure(hass, fid, {"name": "Feed-in", "rate": 0.05})
    assert result["step_id"] == "build_review"
    assert "Peak: 0.45" in result["description_placeholders"]["summary"]
    result = await _configure(hass, fid, {"action": "save"})
    assert result["step_id"] == "meters"
    result = await _configure(hass, fid, {CONF_BILLING_DAY: 1})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    options = result["options"]
    plan = options[CONF_PLAN]
    assert plan["customer_type"] == "residential"
    peak = next(p for p in plan["import"]["periods"] if p["id"] == "peak")
    assert peak["windows"] == [
        {"days": "weekdays", "start": "07:00", "end": "09:00"},
        {"days": "weekdays", "start": "17:00", "end": "20:00"},
    ]
    path = options[CONF_PLAN_SOURCE][CONF_PLAN_PATH]
    assert options[CONF_PLAN_SOURCE][CONF_SOURCE] == SOURCE_BUILD
    assert path == "energy_tariff_assistant/plans/Covau-SolarMaxResidentialTOU-NSW-EssentialEnergy-20260701.json"
    saved = json.loads((Path(hass.config.config_dir) / path).read_text(encoding="utf-8"))
    assert saved == plan


async def test_build_start_over(hass, sydney):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    fid = (await _configure(hass, result["flow_id"], {"name": "E", CONF_SOURCE: SOURCE_BUILD}))["flow_id"]
    await _configure(hass, fid, {"country": "au", "region": "VIC"})
    await _configure(hass, fid, {"retailer": "R", "plan_name": "P", "effective_from": "2026-07-01",
                                 "supply_charge": 1, "customer_type": "business",
                                 "import_type": "flat", "export_type": "flat"})
    await _configure(hass, fid, {"name": "Anytime", "rate": 0.3})
    result = await _configure(hass, fid, {"name": "Feed-in", "rate": 0.04})
    assert result["step_id"] == "build_review"
    result = await _configure(hass, fid, {"action": "start_over"})
    assert result["step_id"] == "build"


async def test_build_from_options_replaces_plan(hass, sydney, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    opt = hass.config_entries.options
    result = await opt.async_init(entry.entry_id)
    result = await opt.async_configure(result["flow_id"], {"next_step_id": "plan"})
    fid = result["flow_id"]
    result = await opt.async_configure(fid, {CONF_SOURCE: SOURCE_BUILD})
    assert result["step_id"] == "build"
    await opt.async_configure(fid, {"country": "au", "region": "NSW"})
    await opt.async_configure(fid, {"dnsp": "Ausgrid", "retailer": "R", "plan_name": "Flat",
                                    "effective_from": "2026-07-01", "supply_charge": 1.1,
                                    "customer_type": "residential", "import_type": "flat", "export_type": "flat"})
    await opt.async_configure(fid, {"name": "Anytime", "rate": 0.31})
    await opt.async_configure(fid, {"name": "Feed-in", "rate": 0.04})
    result = await opt.async_configure(fid, {"action": "save"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_PLAN]["plan"] == "Flat"
    assert entry.options[CONF_PLAN_SOURCE][CONF_SOURCE] == SOURCE_BUILD
    assert entry.options["billing_day"] == 15
    assert hass.states.get("sensor.electricity_import_price").state == "0.31"


def test_write_plan_file_never_overwrites(tmp_path):
    from custom_components.energy_tariff_assistant.plan_source import write_plan_file

    first = write_plan_file(str(tmp_path), "A-B-NSW-X-20260701.json", {"a": 1})
    second = write_plan_file(str(tmp_path), "A-B-NSW-X-20260701.json", {"a": 2})
    assert first == "energy_tariff_assistant/plans/A-B-NSW-X-20260701.json"
    assert second == "energy_tariff_assistant/plans/A-B-NSW-X-20260701-2.json"
    assert json.loads((tmp_path / first).read_text())["a"] == 1


def test_current_hides_ended_plans():
    entries = [{"plan": "a"}, {"plan": "b", "effective_to": "2026-11-20"}, {"plan": "c", "effective_to": "2026-11-21"},
               {"plan": "d", "effective_to": None}]
    assert [e["plan"] for e in wizard.current(entries, "2026-11-21")] == ["a", "c", "d"]
