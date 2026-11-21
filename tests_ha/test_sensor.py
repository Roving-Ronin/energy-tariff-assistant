"""Plan entries and Active tariff through real Home Assistant state changes."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.energy_tariff_assistant.const import (
    CONF_ACTIVE_ENTRY,
    CONF_KIND,
    CONF_PLAN,
    CONF_PLAN_SOURCE,
    CONF_SLUG,
    CONF_SOURCE,
    DOMAIN,
    KIND_ACTIVE,
    KIND_PLAN,
    SOURCE_PASTE,
)

from custom_components.energy_tariff_assistant import sensor as sensor_module

from .conftest import EXPORT, IMPORT, SLUG

pytest.importorskip("pytest_homeassistant_custom_component")
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_time_changed,
)

SYD = ZoneInfo("Australia/Sydney")


@pytest.fixture(autouse=True)
def write_totals_at_once(monkeypatch):
    """Totals normally write at most every 30 s; these tests read them straight after each reading."""
    monkeypatch.setattr(sensor_module, "TOTAL_WRITE_INTERVAL", 0.0)
KWH = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}


def syd(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SYD)


def num(hass, entity_id: str) -> float:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return float(state.state)


async def _setup(hass, freezer, when: str, *entries) -> None:
    freezer.move_to(syd(when))
    hass.states.async_set(IMPORT, "1000.0", KWH)
    hass.states.async_set(EXPORT, "500.0", KWH)
    for entry in entries:
        if entry.state is not ConfigEntryState.LOADED:
            assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _at(hass, freezer, when: str, **readings: str) -> None:
    freezer.move_to(syd(when))
    if "imp" in readings:
        hass.states.async_set(IMPORT, readings["imp"], KWH)
    if "exp" in readings:
        hass.states.async_set(EXPORT, readings["exp"], KWH)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def flat_plan(plan_data: dict, rate: float, feed_in: float, supply: float) -> dict:
    plan = json.loads(json.dumps(plan_data))
    plan.update(retailer="Flat Co", plan="Residential Flat", plan_id="FLT1000001MRE1")
    plan["supply_charge"] = {"rate": supply, "unit": "per_day"}
    plan["import"] = {"periods": [{"id": "anytime", "name": "Anytime", "rate": rate, "default": True}]}
    plan["export"] = {"periods": [{"id": "feed_in", "name": "Feed-in", "rate": feed_in, "default": True}]}
    return plan


@pytest.fixture
def flat_entry(hass, plan_data) -> MockConfigEntry:
    plan = flat_plan(plan_data, 0.30, 0.04, 1.0)
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="Flat Co - Flat",
        data={CONF_KIND: KIND_PLAN, CONF_SLUG: "eta_flatco_flat"},
        options={CONF_PLAN: plan, CONF_PLAN_SOURCE: {CONF_SOURCE: SOURCE_PASTE}},
    )
    entry.add_to_hass(hass)
    return entry


async def test_plan_price_sensors(hass, sydney, entry, hub, freezer):
    await _setup(hass, freezer, "2026-11-04 16:00", entry, hub)
    assert entry.state is ConfigEntryState.LOADED
    price = hass.states.get(f"sensor.{SLUG}_import_price")
    assert float(price.state) == pytest.approx(0.45331)
    attributes = price.attributes
    assert attributes["unit_of_measurement"] == "AUD/kWh"
    assert attributes["period"] == "peak" and attributes["period_name"] == "Peak"
    assert attributes["start_time"] == syd("2026-11-04 15:00").isoformat()
    assert attributes["end_time"] == syd("2026-11-04 21:00").isoformat()
    assert attributes["interpolation_mode"] == "previous"  # HAEO reads steps, not ramps
    assert attributes["forecast"][0] == {"time": syd("2026-11-04 16:00").isoformat(), "value": pytest.approx(0.45331)}
    assert len(attributes["forecasts"]) == 96  # Amber layout, 30-min slots, 48 h
    assert attributes["forecasts"][0]["start_time"] == syd("2026-11-04 16:00").isoformat()
    assert attributes["plan_id"] == "COV1117612MRE3"
    assert attributes["friendly_name"] == "CovaU - SolarMax TOU Import Price"  # Title Case names
    assert hass.states.get(f"sensor.{SLUG}_export_super_used").attributes["friendly_name"] == (
        "CovaU - SolarMax TOU Export Super FiT Used"
    )
    assert hass.states.get("sensor.eta_grid_export_compensation_total").attributes["friendly_name"] == (
        "Grid - Export Compensation - Total"
    )
    assert num(hass, f"sensor.{SLUG}_export_price") == pytest.approx(0.05)
    # Meters, period and bill sensors are gone
    assert hass.states.get(f"sensor.{SLUG}_import_peak_energy_today") is None
    # 2 energy sources + the plan's 2 prices and 2 capped-rate "used" sensors (Free, Super FiT) + Active tariff's 7
    assert len(hass.states.async_entity_ids("sensor")) == 2 + 4 + 7


async def test_grid_sensors_follow_the_active_plan(hass, sydney, entry, hub, flat_entry, freezer):
    await _setup(hass, freezer, "2026-11-04 16:00", entry, flat_entry, hub)
    grid = hass.states.get("sensor.eta_grid_import_price")
    assert float(grid.state) == pytest.approx(0.45331)
    assert grid.attributes["active_plan"] == "CovaU - SolarMax TOU"
    assert grid.attributes["friendly_name"] == "Grid - Import Price"
    assert hass.states.get("sensor.eta_grid_supply_charge_total").attributes["friendly_name"] == (
        "Grid - Supply Charge - Total"
    )
    assert grid.attributes["source_entity"] == f"sensor.{SLUG}_import_price"
    assert grid.attributes["forecast"] == hass.states.get(f"sensor.{SLUG}_import_price").attributes["forecast"]
    assert num(hass, "sensor.eta_grid_export_price") == pytest.approx(0.05)
    assert num(hass, "sensor.eta_grid_supply_charge") == pytest.approx(2.29999)

    select = hass.states.get("select.eta_grid_active_tariff_plan")
    assert select.state == "CovaU - SolarMax TOU"
    assert select.attributes["friendly_name"] == "Grid - Active Tariff Plan"
    assert select.attributes["options"] == ["CovaU - SolarMax TOU", "Flat Co - Flat"]
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.eta_grid_active_tariff_plan", "option": "Flat Co - Flat"}, blocking=True
    )
    await hass.async_block_till_done()
    assert num(hass, "sensor.eta_grid_import_price") == pytest.approx(0.30)
    assert num(hass, "sensor.eta_grid_export_price") == pytest.approx(0.04)
    assert hass.states.get("sensor.eta_grid_import_price").attributes["source_entity"] == (
        "sensor.eta_flatco_flat_import_price"
    )

    # The action, by plan entry
    await hass.services.async_call(DOMAIN, "set_active_plan", {"config_entry": entry.entry_id}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("select.eta_grid_active_tariff_plan").state == "CovaU - SolarMax TOU"
    assert num(hass, "sensor.eta_grid_import_price") == pytest.approx(0.45331)


async def test_cost_totals_split_at_boundaries_and_carry_across_a_switch(
    hass, sydney, entry, hub, flat_entry, freezer
):
    await _setup(hass, freezer, "2026-11-04 14:30", entry, flat_entry, hub)
    # 14:30 -> 15:30 crosses Shoulder -> Peak at 15:00: 1 kWh each side. 2 kWh exported at Standard FiT.
    await _at(hass, freezer, "2026-11-04 15:30", imp="1002.0", exp="502.0")
    cost = num(hass, "sensor.eta_grid_import_cost_total")
    assert cost == pytest.approx(0.27478 + 0.45331, abs=1e-4)
    assert num(hass, "sensor.eta_grid_export_compensation_total") == pytest.approx(2 * 0.05, abs=1e-4)
    state = hass.states.get("sensor.eta_grid_import_cost_total")
    assert state.attributes["state_class"] == "total" and state.attributes["device_class"] == "monetary"

    # Switch to the flat plan: from now, each kWh costs 0.30; the total carries on
    hub.runtime_data.async_set_active(flat_entry.entry_id)
    await hass.async_block_till_done()
    await _at(hass, freezer, "2026-11-04 16:30", imp="1003.0")
    assert num(hass, "sensor.eta_grid_import_cost_total") == pytest.approx(cost + 0.30, abs=1e-4)

    # Totals survive a restart of Active tariff
    assert await hass.config_entries.async_reload(hub.entry_id)
    await hass.async_block_till_done()
    assert num(hass, "sensor.eta_grid_import_cost_total") == pytest.approx(cost + 0.30, abs=1e-4)


async def test_free_cap_moves_price_and_used_sensor(hass, sydney, entry, hub, freezer):
    await _setup(hass, freezer, "2026-11-04 11:00", entry, hub)
    assert num(hass, "sensor.eta_grid_import_price") == pytest.approx(0.0)
    used = hass.states.get(f"sensor.{SLUG}_import_free_used")
    assert float(used.state) == 0.0
    assert used.attributes["cap_kwh"] == 50 and used.attributes["resets"] == "day"
    assert used.attributes["over_cap_rate_from"] == "shoulder"
    await _at(hass, freezer, "2026-11-04 12:00", imp="1055.0")
    assert num(hass, "sensor.eta_grid_import_price") == pytest.approx(0.27478)
    used = hass.states.get(f"sensor.{SLUG}_import_free_used")
    assert float(used.state) == pytest.approx(55.0)
    assert used.attributes["remaining_kwh"] == 0
    assert num(hass, "sensor.eta_grid_import_cost_total") == pytest.approx(5 * 0.27478, abs=1e-4)
    assert hass.states.get(f"sensor.{SLUG}_export_super_used").attributes["cap_kwh"] == 30


async def test_supply_charge_total_counts_whole_days(hass, sydney, entry, hub, freezer):
    await _setup(hass, freezer, "2026-11-04 16:00", entry, hub)
    assert num(hass, "sensor.eta_grid_supply_charge_total") == pytest.approx(2.29999)  # today counts
    await _at(hass, freezer, "2026-11-05 00:00:02")
    assert num(hass, "sensor.eta_grid_supply_charge_total") == pytest.approx(2 * 2.29999)
    # Home Assistant off for two days: the missed days are added at start
    assert await hass.config_entries.async_unload(hub.entry_id)
    freezer.move_to(syd("2026-11-07 09:00"))
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.eta_grid_supply_charge_total")
    assert float(state.state) == pytest.approx(4 * 2.29999)
    assert state.attributes["days_counted"] == 4


async def test_without_energy_sensors_totals_are_unavailable(hass, sydney, entry, hub, freezer):
    hass.config_entries.async_update_entry(hub, options={**hub.options, "import_sensor": None, "export_sensor": None})
    await _setup(hass, freezer, "2026-11-04 16:00", entry, hub)
    assert hass.states.get("sensor.eta_grid_import_cost_total").state == "unavailable"
    assert hass.states.get(f"sensor.{SLUG}_import_free_used").state == "unavailable"
    assert num(hass, "sensor.eta_grid_supply_charge_total") == pytest.approx(2.29999)
    assert num(hass, "sensor.eta_grid_import_price") == pytest.approx(0.45331)


async def test_removing_the_active_plan_leaves_no_plan(hass, sydney, entry, hub, flat_entry, freezer):
    await _setup(hass, freezer, "2026-11-04 16:00", entry, flat_entry, hub)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert hub.options[CONF_ACTIVE_ENTRY] is None  # never switched to another plan on its own
    assert hass.states.get("sensor.eta_grid_import_price").state == "unavailable"
    assert hass.states.get("select.eta_grid_active_tariff_plan").state == "No active plan"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "no_active_plan")
    hub.runtime_data.async_set_active(flat_entry.entry_id)
    await hass.async_block_till_done()
    assert num(hass, "sensor.eta_grid_import_price") == pytest.approx(0.30)
    assert not ir.async_get(hass).async_get_issue(DOMAIN, "no_active_plan")


async def test_plan_files_for_other_tools(hass, sydney, entry, hub, freezer):
    await _setup(hass, freezer, "2026-11-04 16:00", entry, hub)
    folder = Path(hass.config.config_dir) / "energy_tariff_assistant" / "active"
    assert json.loads((folder / f"{SLUG}.json").read_text())["plan_id"] == "COV1117612MRE3"
    assert json.loads((folder / "active.json").read_text())["plan_id"] == "COV1117612MRE3"
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not (folder / f"{SLUG}.json").exists()


async def test_version_1_entry_moves_to_active_tariff(hass, sydney, legacy_entry, freezer):
    registry = er.async_get(hass)
    # Entities the old version made: a meter (retired) and the price sensor (renamed)
    registry.async_get_or_create("sensor", DOMAIN, f"{legacy_entry.entry_id}_import_peak_energy_day",
                                 config_entry=legacy_entry, suggested_object_id="electricity_import_peak_energy_today")
    registry.async_get_or_create("sensor", DOMAIN, f"{legacy_entry.entry_id}_import_price",
                                 config_entry=legacy_entry, suggested_object_id="electricity_import_price")
    await _setup(hass, freezer, "2026-11-04 16:00", legacy_entry)
    await hass.async_block_till_done()
    assert legacy_entry.version == 2
    assert legacy_entry.data == {CONF_KIND: KIND_PLAN, CONF_SLUG: SLUG}
    assert registry.async_get("sensor.electricity_import_peak_energy_today") is None
    assert registry.async_get("sensor.electricity_import_price") is None
    assert num(hass, f"sensor.{SLUG}_import_price") == pytest.approx(0.45331)
    hub = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(CONF_KIND) == KIND_ACTIVE)
    assert hub.options[CONF_ACTIVE_ENTRY] == legacy_entry.entry_id
    assert hub.options["import_sensor"] == IMPORT  # taken from the old entry
    assert num(hass, "sensor.eta_grid_import_price") == pytest.approx(0.45331)


async def test_plan_loads_off_event_loop(hass, sydney, entry, hub, freezer, monkeypatch):
    """Validation, time zone and holiday loading read files: never on the loop.

    (HA's blocking-call detector doesn't fire in this test harness, so check the thread directly.)
    """
    import threading

    from custom_components import energy_tariff_assistant as integration

    threads = []
    real = integration.check_plan

    def spy(plan):
        threads.append(threading.current_thread())
        return real(plan)

    monkeypatch.setattr(integration, "check_plan", spy)
    await _setup(hass, freezer, "2026-11-04 16:00", entry, hub)
    assert threads
    assert threading.main_thread() not in threads


async def test_unload(hass, sydney, entry, hub, freezer):
    await _setup(hass, freezer, "2026-11-04 16:00", entry, hub)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("sensor.eta_grid_import_price").state == "unavailable"


async def test_diagnostics(hass, sydney, entry, hub, freezer):
    from custom_components.energy_tariff_assistant.diagnostics import async_get_config_entry_diagnostics

    await _setup(hass, freezer, "2026-11-04 16:00", entry, hub)
    plan = await async_get_config_entry_diagnostics(hass, entry)
    assert plan["plan"]["plan_id"] == "COV1117612MRE3" and set(plan["ledgers"]) == {"import", "export"}
    active = await async_get_config_entry_diagnostics(hass, hub)
    assert active["active_plan"] == "CovaU - SolarMax TOU" and active["days_counted"] == 1


@pytest.fixture
def demand_entry(hass, plan_data) -> MockConfigEntry:
    plan = flat_plan(plan_data, 0.30, 0.04, 1.0)
    plan.update(retailer="Peak Co", plan="Residential Demand", plan_id="PKC1000001MRE1")
    plan["demand"] = [{"id": "residential_demand", "name": "Residential Demand", "rate": 0.2,
                       "unit": "per_kw_per_day", "windows": [{"days": "weekdays", "start": "17:00", "end": "20:00"}],
                       "measure": {"interval_minutes": 30, "method": "max", "over": "month"}}]
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="Peak Co - Demand",
        data={CONF_KIND: KIND_PLAN, CONF_SLUG: "eta_peakco_demand"},
        options={CONF_PLAN: plan, CONF_PLAN_SOURCE: {CONF_SOURCE: SOURCE_PASTE}},
    )
    entry.add_to_hass(hass)
    return entry


async def test_demand_peak_and_charge_total(hass, sydney, entry, hub, demand_entry, freezer):
    await _setup(hass, freezer, "2026-11-04 16:00", entry, demand_entry, hub)
    hub.runtime_data.async_set_active(demand_entry.entry_id)
    await hass.async_block_till_done()
    peak_id = "sensor.eta_peakco_demand_demand_residential_demand_peak"
    assert num(hass, peak_id) == 0
    assert num(hass, "sensor.eta_grid_demand_charge_total") == 0
    assert hass.states.get("sensor.eta_grid_demand_charge_total").attributes["friendly_name"] == (
        "Grid - Demand Charge - Total"
    )

    await _at(hass, freezer, "2026-11-04 17:00", imp="1000.5")  # before the window: not demand
    await _at(hass, freezer, "2026-11-04 17:30", imp="1002.0")  # 1.5 kWh in 17:00-17:30: 3 kW
    peak = hass.states.get(peak_id)
    assert float(peak.state) == pytest.approx(3.0)
    assert peak.attributes["friendly_name"] == "Peak Co - Demand Residential Demand Peak"
    assert peak.attributes["unit_of_measurement"] == "kW"
    assert peak.attributes["peak_at"] == syd("2026-11-04 17:00").isoformat()
    assert peak.attributes["days_counted"] == 4 and peak.attributes["measured_over"] == "month"
    # 3 kW x $0.20 x 4 days (1-4 November): earlier days re-priced at the new peak
    assert num(hass, "sensor.eta_grid_demand_charge_total") == pytest.approx(2.4, abs=1e-4)

    await _at(hass, freezer, "2026-11-05 00:00:02")  # a new day adds a day's charge
    assert num(hass, "sensor.eta_grid_demand_charge_total") == pytest.approx(3.0, abs=1e-4)

    # The meter survives a restart
    assert await hass.config_entries.async_reload(demand_entry.entry_id)
    await hass.async_block_till_done()
    assert num(hass, peak_id) == pytest.approx(3.0)
    assert num(hass, "sensor.eta_grid_demand_charge_total") == pytest.approx(3.0, abs=1e-4)


async def test_demand_needs_the_import_sensor(hass, sydney, entry, hub, demand_entry, freezer):
    hass.config_entries.async_update_entry(hub, options={**hub.options, "import_sensor": None})
    await _setup(hass, freezer, "2026-11-04 16:00", entry, demand_entry, hub)
    hub.runtime_data.async_set_active(demand_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.eta_peakco_demand_demand_residential_demand_peak").state == "unavailable"
    assert hass.states.get("sensor.eta_grid_demand_charge_total").state == "unavailable"


async def test_totals_write_at_most_every_30_seconds(hass, sydney, entry, hub, freezer, monkeypatch):
    monkeypatch.setattr(sensor_module, "TOTAL_WRITE_INTERVAL", 30.0)
    await _setup(hass, freezer, "2026-11-04 15:00", entry, hub)
    await _at(hass, freezer, "2026-11-04 15:10", imp="1001.0")
    await _at(hass, freezer, "2026-11-04 15:10:05", imp="1002.0")  # counted at once, written within 30 s
    await _at(hass, freezer, "2026-11-04 15:10:10", imp="1003.0")
    assert num(hass, "sensor.eta_grid_import_cost_total") < 3 * 0.45331 - 1e-4
    await _at(hass, freezer, "2026-11-04 15:11:00")  # the last change is written when the 30 s are up
    assert num(hass, "sensor.eta_grid_import_cost_total") == pytest.approx(3 * 0.45331, abs=1e-4)


async def test_calibrate_a_total(hass, sydney, entry, hub, freezer):
    await _setup(hass, freezer, "2026-11-04 15:00", entry, hub)
    await _at(hass, freezer, "2026-11-04 15:10", imp="1001.0")
    state = hass.states.get("sensor.eta_grid_import_cost_total")
    assert state.attributes["energy_kwh"] == pytest.approx(1.0)
    assert state.attributes["average_rate"] == pytest.approx(0.45331)
    assert state.attributes["source"] == IMPORT
    assert state.attributes.get("last_reset") is None

    await hass.services.async_call(DOMAIN, "calibrate", {"entity_id": "sensor.eta_grid_import_cost_total",
                                                          "value": 100}, blocking=True)
    state = hass.states.get("sensor.eta_grid_import_cost_total")
    assert float(state.state) == 100
    assert datetime.fromisoformat(state.attributes["last_reset"]) == syd("2026-11-04 15:10")  # statistics restart
    assert state.attributes["energy_kwh"] == 0 and state.attributes["average_rate"] is None
    await _at(hass, freezer, "2026-11-04 15:20", imp="1002.0")  # carries on counting from 100
    state = hass.states.get("sensor.eta_grid_import_cost_total")
    assert float(state.state) == pytest.approx(100.45331, abs=1e-4)
    assert state.attributes["average_rate"] == pytest.approx(0.45331)

    # Survives a restart; price sensors can't be set
    assert await hass.config_entries.async_reload(hub.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.eta_grid_import_cost_total").state) == pytest.approx(100.45331, abs=1e-4)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "calibrate", {"entity_id": "sensor.eta_grid_import_price",
                                                              "value": 1}, blocking=True)
