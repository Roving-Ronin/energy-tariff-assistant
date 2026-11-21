"""Fixtures for the Home Assistant integration tests."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from custom_components.energy_tariff_assistant.const import (
    CONF_BILLING_DAY,
    CONF_EXPORT_SENSOR,
    CONF_IMPORT_SENSOR,
    CONF_PLAN,
    CONF_PLAN_SOURCE,
    CONF_SOURCE,
    DOMAIN,
    SOURCE_PASTE,
)

pytest.importorskip("pytest_homeassistant_custom_component")
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

PLAN_FILE = (
    Path(__file__).resolve().parent.parent
    / "plans/AU/NSW/CovaU-SolarMaxTOU-NSW-EssentialEnergy-20260701.json"
)
IMPORT = "sensor.grid_import"
EXPORT = "sensor.grid_export"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def clean_built_plans(hass):
    """The wizard saves plans under the shared test config folder: start and end empty."""
    folder = Path(hass.config.config_dir) / "energy_tariff_assistant"
    shutil.rmtree(folder, ignore_errors=True)
    yield
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def plan_data() -> dict:
    return json.loads(PLAN_FILE.read_text(encoding="utf-8"))


@pytest.fixture
async def sydney(hass):
    await hass.config.async_set_time_zone("Australia/Sydney")
    hass.config.currency = "AUD"
    hass.config.country = "AU"


@pytest.fixture
def entry(hass, plan_data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity",
        data={},
        options={
            CONF_PLAN: plan_data,
            CONF_PLAN_SOURCE: {CONF_SOURCE: SOURCE_PASTE},
            CONF_IMPORT_SENSOR: IMPORT,
            CONF_EXPORT_SENSOR: EXPORT,
            CONF_BILLING_DAY: 15,
        },
    )
    entry.add_to_hass(hass)
    return entry
