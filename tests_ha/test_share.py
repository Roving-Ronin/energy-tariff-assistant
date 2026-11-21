"""Share this plan: the pre-filled GitHub link and the options flow step."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from homeassistant.data_entry_flow import FlowResultType

from custom_components.energy_tariff_assistant.const import (
    CONF_LIBRARY_FILE,
    CONF_SOURCE,
    SOURCE_LIBRARY,
    SOURCE_PASTE,
)
from custom_components.energy_tariff_assistant.share import (
    ISSUE_FORM_URL,
    KIND_CORRECTION,
    KIND_NEW,
    MAX_URL_LENGTH,
    build_share_link,
)


def _query(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == ISSUE_FORM_URL
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


def test_link_carries_the_plan(plan_data):
    link = build_share_link(plan_data, {CONF_SOURCE: SOURCE_PASTE}, "0.1.0b2")
    assert link.plan_in_link
    assert len(link.url) <= MAX_URL_LENGTH
    q = _query(link.url)
    assert q["template"] == "plan_submission.yml"
    assert q["title"] == "[Plan] CovaU SolarMax Residential TOU (NSW, Essential Energy)"
    assert q["kind"] == KIND_NEW
    assert q["state"] == "NSW"
    assert q["customer-type"] == "Residential"
    assert q["dnsp"] == "Essential Energy"
    assert q["plan-id"] == "COV1117612MRE3, from 2026-07-01"
    assert json.loads(q["plan-json"]) == plan_data
    assert "Energy Tariff Assistant 0.1.0b2" in q["source"]


def test_library_plan_is_a_correction(plan_data):
    source = {CONF_SOURCE: SOURCE_LIBRARY, CONF_LIBRARY_FILE: "plans/AU/NSW/x.json"}
    q = _query(build_share_link(plan_data, source, "0.1.0").url)
    assert q["kind"] == KIND_CORRECTION
    assert "plans/AU/NSW/x.json" in q["source"]


def test_long_plan_leaves_json_out_of_the_link(plan_data):
    plan_data["notes"] = "x" * 10000
    link = build_share_link(plan_data, None, "0.1.0")
    assert not link.plan_in_link
    assert len(link.url) <= MAX_URL_LENGTH
    assert "plan-json" not in _query(link.url)
    assert json.loads(link.plan_json) == plan_data


def test_unknown_region_not_prefilled(plan_data):
    plan_data["region"] = "Elsewhere"
    assert "state" not in _query(build_share_link(plan_data, None, "0.1.0").url)


async def test_share_step(hass, sydney, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "share" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "share"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "share"
    assert result["description_placeholders"]["url"].startswith(ISSUE_FORM_URL)
    options_before = dict(entry.options)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "share_done"
    assert dict(entry.options) == options_before


async def test_share_step_long_plan(hass, sydney, entry, plan_data):
    plan_data["notes"] = "x" * 10000
    hass.config_entries.async_update_entry(entry, options={**entry.options, "plan": plan_data})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "share"}
    )
    assert result["step_id"] == "share_paste"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["reason"] == "share_done"
