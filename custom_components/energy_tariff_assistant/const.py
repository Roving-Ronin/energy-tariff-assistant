"""Constants for Energy Tariff Assistant."""

from __future__ import annotations

DOMAIN = "energy_tariff_assistant"

CONF_PLAN = "plan"
CONF_PLAN_SOURCE = "plan_source"
CONF_SOURCE = "source"
CONF_LIBRARY_FILE = "library_file"
CONF_PLAN_JSON = "plan_json"
CONF_PLAN_PATH = "plan_path"
CONF_IMPORT_SENSOR = "import_sensor"
CONF_EXPORT_SENSOR = "export_sensor"
CONF_BILLING_DAY = "billing_day"

SOURCE_LIBRARY = "library"
SOURCE_PASTE = "paste"
SOURCE_FILE = "file"
SOURCE_BUILD = "build"
SOURCES = [SOURCE_LIBRARY, SOURCE_BUILD, SOURCE_PASTE, SOURCE_FILE]

# Where the wizard saves the plans it builds, relative to the config folder
BUILT_PLANS_DIR = "energy_tariff_assistant/plans"

# Plan browser (GitHub Pages): the library with filters, a timeline per plan and validation results.
PLAN_BROWSER_URL = "https://roving-ronin.github.io/energy-tariff-assistant/"
LIBRARY_BASE_URL = (
    "https://raw.githubusercontent.com/Roving-Ronin/energy-tariff-assistant/main/"
)
LIBRARY_INDEX = "plans/index.json"

STREAM_IMPORT = "import"
STREAM_EXPORT = "export"
STREAM_SENSORS = {STREAM_IMPORT: CONF_IMPORT_SENSOR, STREAM_EXPORT: CONF_EXPORT_SENSOR}

# Buckets exposed as entities (the ledger also keeps "month" for tier resets).
EXPOSED_BUCKETS = ("day", "cycle")

FORECAST_DAYS = 2
STORAGE_VERSION = 1
SAVE_DELAY = 30

SERVICE_CALIBRATE = "calibrate"
SERVICE_RESET_METERS = "reset_meters"
ATTR_CONFIG_ENTRY = "config_entry"
ATTR_BUCKET = "bucket"
ATTR_ENERGY = "energy"
ATTR_COST = "cost"

ENERGY_UNIT_FACTORS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}


def signal_update(entry_id: str) -> str:
    """Dispatcher signal fired when an entry's figures change."""
    return f"{DOMAIN}_{entry_id}_update"
