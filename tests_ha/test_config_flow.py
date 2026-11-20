"""Config and options flow tests."""

from __future__ import annotations

import json

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.energy_tariff_assistant.const import (
    CONF_BILLING_DAY,
    CONF_IMPORT_SENSOR,
    CONF_LIBRARY_FILE,
    CONF_PLAN,
    CONF_PLAN_JSON,
    CONF_PLAN_SOURCE,
    CONF_SOURCE,
    DOMAIN,
    LIBRARY_BASE_URL,
    LIBRARY_INDEX,
    SOURCE_LIBRARY,
    SOURCE_PASTE,
)

from .conftest import IMPORT, PLAN_FILE


async def _start(hass, source: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Electricity", CONF_SOURCE: source}
    )


async def test_paste_flow_creates_entry(hass, sydney, plan_data):
    result = await _start(hass, SOURCE_PASTE)
    assert result["step_id"] == "paste"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PLAN_JSON: json.dumps(plan_data)}
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_SENSOR: IMPORT, CONF_BILLING_DAY: 15}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Electricity"
    options = result["options"]
    assert options[CONF_PLAN]["plan"] == "SolarMax Residential TOU"
    assert options[CONF_IMPORT_SENSOR] == IMPORT
    assert options["export_sensor"] is None
    assert options[CONF_BILLING_DAY] == 15


async def test_paste_bad_json(hass, sydney):
    result = await _start(hass, SOURCE_PASTE)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PLAN_JSON: "{not json"}
    )
    assert result["errors"] == {"base": "invalid_json"}


async def test_paste_invalid_plan(hass, sydney, plan_data):
    plan_data["import"]["periods"][-1]["default"] = False  # no default period
    result = await _start(hass, SOURCE_PASTE)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PLAN_JSON: json.dumps(plan_data)}
    )
    assert result["errors"] == {"base": "invalid_plan"}
    assert result["description_placeholders"]["detail"]


async def test_library_flow(hass, sydney, aioclient_mock):
    file = "plans/AU/NSW/Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json"
    aioclient_mock.get(
        LIBRARY_BASE_URL + LIBRARY_INDEX,
        text=json.dumps(
            {"plans": [{"file": file, "region": "NSW", "retailer": "Covau", "plan": "SolarMax"}]}
        ),
    )
    aioclient_mock.get(LIBRARY_BASE_URL + file, text=PLAN_FILE.read_text(encoding="utf-8"))
    result = await _start(hass, SOURCE_LIBRARY)
    assert result["step_id"] == "library"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LIBRARY_FILE: file}
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BILLING_DAY: 1}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_PLAN_SOURCE] == {
        CONF_SOURCE: SOURCE_LIBRARY,
        CONF_LIBRARY_FILE: file,
    }


async def test_library_unreachable(hass, sydney, aioclient_mock):
    aioclient_mock.get(LIBRARY_BASE_URL + LIBRARY_INDEX, status=500)
    result = await _start(hass, SOURCE_LIBRARY)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_options_change_meters(hass, sydney, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "meters"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMPORT_SENSOR: IMPORT, CONF_BILLING_DAY: 3}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_BILLING_DAY] == 3
    assert entry.options["export_sensor"] is None
    assert entry.options[CONF_PLAN]["retailer"] == "Covau"
