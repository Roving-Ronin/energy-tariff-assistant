"""Test setup: import the engine as a top-level package.

The engine has no Home Assistant imports. Putting the integration folder
on sys.path lets the tests import `engine` without loading the
integration's own __init__.py (which needs Home Assistant).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "custom_components" / "energy_tariff_assistant"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PLANS = REPO_ROOT / "plans"
SOLARMAX = PLANS / "AU" / "NSW" / "Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json"


def read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def solarmax_data():
    return read_json(SOLARMAX)


@pytest.fixture
def solarmax(solarmax_data):
    from engine import Resolver, load_plan

    return Resolver(load_plan(solarmax_data))


def minimal_plan(**overrides):
    """A valid one-period plan to build test cases from."""
    plan = {
        "format": "ha_tariff_plan",
        "version": 1,
        "country": "AU",
        "region": "NSW",
        "retailer": "Test",
        "plan": "Test Plan",
        "effective_from": "2026-07-01",
        "source": {"type": "manual", "checked": "2026-07-01"},
        "currency": "AUD",
        "timezone": "Australia/Sydney",
        "supply_charge": {"rate": 1.0, "unit": "per_day"},
        "import": {"periods": [{"id": "anytime", "name": "Anytime", "rate": 0.30, "default": True}]},
        "export": {"periods": [{"id": "fit", "name": "FiT", "rate": 0.05, "default": True}]},
    }
    plan.update(overrides)
    return plan
