"""Repo-level pytest setup.

The Home Assistant test plugin is loaded only if installed, so the engine
tests in tests/ still run with just requirements_test.txt's engine deps.
"""

import importlib.util

if importlib.util.find_spec("pytest_homeassistant_custom_component"):
    pytest_plugins = ["pytest_homeassistant_custom_component"]
