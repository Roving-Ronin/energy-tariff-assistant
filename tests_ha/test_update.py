"""Plan updates from the library: found, described, installed in place."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest

from homeassistant.helpers import entity_registry as er

from custom_components.energy_tariff_assistant import updates
from custom_components.energy_tariff_assistant.const import (
    CONF_LIBRARY_FILE,
    CONF_PLAN,
    CONF_PLAN_SOURCE,
    CONF_SOURCE,
    LIBRARY_BASE_URL,
    LIBRARY_INDEX,
    SOURCE_LIBRARY,
)

from .conftest import EXPORT, IMPORT, SLUG

pytest.importorskip("pytest_homeassistant_custom_component")
from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: E402

SYD = ZoneInfo("Australia/Sydney")
OLD = "plans/AU/NSW/CovaU-SolarMaxTOU-NSW-EssentialEnergy-20260701.json"
NEW = "plans/AU/NSW/CovaU-SolarMaxTOU-NSW-EssentialEnergy-20270101.json"


def index_entry(file: str, plan: dict) -> dict:
    return {"file": file, "country": "AU", "region": "NSW", "dnsp": plan["dnsp"], "retailer": plan["retailer"],
            "plan": plan["plan"], "plan_id": plan["plan_id"], "effective_from": plan["effective_from"],
            "effective_to": plan.get("effective_to")}


def newer(plan_data: dict) -> dict:
    plan = json.loads(json.dumps(plan_data))
    plan.update(plan_id="COV1117612MRE4", effective_from="2027-01-01")
    peak = next(p for p in plan["import"]["periods"] if p["id"] == "peak")
    peak["rate"] = 0.4621
    plan["confirmations"] = [{"date": "2027-01-02", "by": "octocat", "issue": 99}]  # library field: not copied
    return plan


# ----- pure ---------------------------------------------------------------------


def test_family_and_candidates(plan_data):
    assert updates.family("COV1117612MRE3") == "COV1117612MRE"
    assert updates.family("COV1117612MRE3@EME") == "COV1117612MRE"
    assert updates.family("VIC123456@VEC") == "VIC123456"
    later = newer(plan_data)
    other_network = {**later, "dnsp": "Ausgrid"}
    ended = {**later, "plan_id": "COV1117612MRE2", "effective_from": "2025-07-01", "effective_to": "2026-06-30"}
    index = [index_entry(OLD, plan_data), index_entry(NEW, later), index_entry("x", other_network),
             index_entry("y", ended)]
    assert [e["file"] for e in updates.candidates(index, plan_data, "2026-11-22")] == [NEW, OLD]


def test_describe_changes(plan_data):
    lines = updates.describe_changes(plan_data, newer(plan_data))
    assert "Plan ID: COV1117612MRE3 → COV1117612MRE4" in lines
    assert "Import Peak: 0.45331 → 0.4621" in lines
    assert any(line.startswith("From: 2026-07-01") for line in lines)
    capped = json.loads(json.dumps(plan_data))
    free = next(p for p in capped["import"]["periods"] if p["id"] == "free")
    free["tiers"][0]["up_to_kwh"] = 40
    assert updates.describe_changes(plan_data, capped) == [
        "Import Free: 0.0 up to 50 kWh, then as shoulder → 0.0 up to 40 kWh, then as shoulder"
    ]


# ----- in Home Assistant -------------------------------------------------------


async def test_update_found_and_installed_in_place(hass, sydney, entry, hub, plan_data, aioclient_mock, freezer):
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PLAN_SOURCE: {CONF_SOURCE: SOURCE_LIBRARY, CONF_LIBRARY_FILE: OLD}}
    )
    later = newer(plan_data)
    aioclient_mock.get(
        LIBRARY_BASE_URL + LIBRARY_INDEX,
        text=json.dumps({"plans": [index_entry(OLD, plan_data), index_entry(NEW, later)]}),
    )
    aioclient_mock.get(LIBRARY_BASE_URL + OLD, text=json.dumps(plan_data))
    aioclient_mock.get(LIBRARY_BASE_URL + NEW, text=json.dumps(later))

    freezer.move_to(datetime(2026, 11, 22, 16, 0, tzinfo=SYD))
    hass.states.async_set(IMPORT, "1000.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set(EXPORT, "500.0", {"unit_of_measurement": "kWh"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    update_id = f"update.{SLUG}_plan"
    assert hass.states.get(update_id).state == "off"  # not checked yet: Home Assistant starts first

    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(update_id)
    assert state.state == "on"
    assert state.attributes["installed_version"] == "COV1117612MRE3 · from 2026-07-01"
    assert state.attributes["latest_version"] == "COV1117612MRE4 · from 2027-01-01"
    assert "Import Peak: 0.45331 → 0.4621" in state.attributes["release_summary"]

    registry = er.async_get(hass)
    before = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    await hass.services.async_call("update", "install", {"entity_id": update_id}, blocking=True)
    await hass.async_block_till_done()
    assert entry.options[CONF_PLAN]["plan_id"] == "COV1117612MRE4"
    assert "confirmations" not in entry.options[CONF_PLAN]
    assert entry.options[CONF_PLAN_SOURCE][CONF_LIBRARY_FILE] == NEW
    assert entry.title == "CovaU - SolarMax TOU"
    assert {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)} == before
    assert hass.states.get(update_id).attributes["installed_version"] == "COV1117612MRE4 · from 2027-01-01"


async def test_replaced_by_is_followed(hass, sydney, entry, hub, plan_data, aioclient_mock, freezer):
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PLAN_SOURCE: {CONF_SOURCE: SOURCE_LIBRARY, CONF_LIBRARY_FILE: OLD}}
    )
    replacement = json.loads(json.dumps(plan_data))
    replacement.update(plan_id="COV2220000MRE1", plan="SolarMax Residential TOU 2")
    withdrawn = {**plan_data, "effective_to": "2026-11-20", "replaced_by": NEW}
    aioclient_mock.get(LIBRARY_BASE_URL + OLD, text=json.dumps(withdrawn))
    aioclient_mock.get(LIBRARY_BASE_URL + NEW, text=json.dumps(replacement))
    freezer.move_to(datetime(2026, 11, 22, 16, 0, tzinfo=SYD))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(f"update.{SLUG}_plan")
    assert state.state == "on"
    assert state.attributes["latest_version"] == "COV2220000MRE1 · from 2026-07-01"


async def test_pasted_plans_have_nothing_to_follow(hass, sydney, entry, hub, freezer):
    freezer.move_to(datetime(2026, 11, 22, 16, 0, tzinfo=SYD))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(f"update.{SLUG}_plan").state == "off"
