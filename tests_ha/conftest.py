"""Fixtures for the Home Assistant integration tests."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from custom_components.energy_tariff_assistant.const import (
    ACTIVE_TITLE,
    ACTIVE_UNIQUE_ID,
    CONF_ACTIVE_ENTRY,
    CONF_KIND,
    CONF_SLUG,
    KIND_ACTIVE,
    KIND_PLAN,
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


SLUG = "eta_covau_solarmaxtou"


@pytest.fixture
def entry(hass, plan_data) -> MockConfigEntry:
    """A plan entry (version 2): CovaU SolarMax, pasted."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="CovaU - SolarMax TOU",
        data={CONF_KIND: KIND_PLAN, CONF_SLUG: SLUG},
        options={CONF_PLAN: plan_data, CONF_PLAN_SOURCE: {CONF_SOURCE: SOURCE_PASTE}},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def hub(hass, entry) -> MockConfigEntry:
    """Active tariff, with `entry` in use and the grid energy sensors."""
    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title=ACTIVE_TITLE,
        unique_id=ACTIVE_UNIQUE_ID,
        data={CONF_KIND: KIND_ACTIVE},
        options={
            CONF_ACTIVE_ENTRY: entry.entry_id,
            CONF_IMPORT_SENSOR: IMPORT,
            CONF_EXPORT_SENSOR: EXPORT,
            CONF_BILLING_DAY: 15,
        },
    )
    hub.add_to_hass(hass)
    return hub


@pytest.fixture
def legacy_entry(hass, plan_data) -> MockConfigEntry:
    """A version 1 entry: metered its own energy sensors, named by the user."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
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
