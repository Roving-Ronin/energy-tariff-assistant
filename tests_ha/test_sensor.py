"""Setup, prices and metering through real Home Assistant state changes."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from homeassistant.config_entries import ConfigEntryState

from custom_components.energy_tariff_assistant.const import DOMAIN

from .conftest import EXPORT, IMPORT

SYD = ZoneInfo("Australia/Sydney")
KWH = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}


def syd(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SYD)


def num(hass, entity_id: str) -> float:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return float(state.state)


async def _setup(hass, entry, freezer, when: str):
    freezer.move_to(syd(when))
    hass.states.async_set(IMPORT, "1000.0", KWH)
    hass.states.async_set(EXPORT, "500.0", KWH)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_prices_and_periods(hass, sydney, entry, freezer):
    await _setup(hass, entry, freezer, "2026-11-04 16:00")
    assert entry.state is ConfigEntryState.LOADED
    price = hass.states.get("sensor.electricity_import_price")
    assert float(price.state) == pytest.approx(0.45331)
    assert price.attributes["unit_of_measurement"] == "AUD/kWh"
    assert price.attributes["period"] == "peak"
    assert price.attributes["forecast"][0]["value"] == pytest.approx(0.45331)
    assert hass.states.get("sensor.electricity_import_period").state == "Peak"
    assert num(hass, "sensor.electricity_export_price") == pytest.approx(0.05)
    nxt = hass.states.get("sensor.electricity_import_next_period_change")
    assert datetime.fromisoformat(nxt.state) == syd("2026-11-04 21:00")
    assert num(hass, "sensor.electricity_daily_supply_charge") == pytest.approx(2.29999)


async def test_metering_split_and_cost(hass, sydney, entry, freezer):
    await _setup(hass, entry, freezer, "2026-11-04 14:30")
    # 14:30 -> 15:30 crosses shoulder -> peak at 15:00: 1 kWh each side.
    freezer.move_to(syd("2026-11-04 15:30"))
    hass.states.async_set(IMPORT, "1002.0", KWH)
    await hass.async_block_till_done()

    assert num(hass, "sensor.electricity_import_shoulder_energy_today") == pytest.approx(1.0)
    assert num(hass, "sensor.electricity_import_peak_energy_today") == pytest.approx(1.0)
    assert num(hass, "sensor.electricity_import_energy_today") == pytest.approx(2.0)
    assert num(hass, "sensor.electricity_import_cost_today") == pytest.approx(
        0.27478 + 0.45331, abs=1e-4
    )
    assert num(hass, "sensor.electricity_import_energy_this_billing_cycle") == pytest.approx(2.0)
    bill = hass.states.get("sensor.electricity_estimated_bill_this_billing_cycle")
    # Cycle started 2026-10-15: 21 days of supply so far.
    assert bill.attributes["supply"] == pytest.approx(21 * 2.29999, abs=1e-3)


async def test_free_cap_moves_price(hass, sydney, entry, freezer):
    await _setup(hass, entry, freezer, "2026-11-04 11:00")
    assert num(hass, "sensor.electricity_import_price") == pytest.approx(0.0)
    freezer.move_to(syd("2026-11-04 12:00"))
    hass.states.async_set(IMPORT, "1055.0", KWH)
    await hass.async_block_till_done()
    assert num(hass, "sensor.electricity_import_price") == pytest.approx(0.27478)
    assert num(hass, "sensor.electricity_import_free_cost_today") == pytest.approx(
        5 * 0.27478, abs=1e-4
    )


async def test_wh_source_and_unavailable(hass, sydney, entry, freezer):
    await _setup(hass, entry, freezer, "2026-11-04 03:00")
    freezer.move_to(syd("2026-11-04 03:10"))
    hass.states.async_set(IMPORT, "unavailable", {})
    await hass.async_block_till_done()
    energy = hass.states.get("sensor.electricity_import_energy_today")
    assert energy.attributes["source_available"] is False
    freezer.move_to(syd("2026-11-04 04:00"))
    hass.states.async_set(IMPORT, "1001000", {**KWH, "unit_of_measurement": "Wh"})
    await hass.async_block_till_done()
    assert num(hass, "sensor.electricity_import_off_peak_energy_today") == pytest.approx(1.0)


async def test_calibrate_and_reset(hass, sydney, entry, freezer):
    await _setup(hass, entry, freezer, "2026-11-04 03:00")
    freezer.move_to(syd("2026-11-04 04:00"))
    hass.states.async_set(IMPORT, "1002.0", KWH)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        "calibrate",
        {"entity_id": "sensor.electricity_import_off_peak_energy_this_billing_cycle", "energy": 10},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert num(hass, "sensor.electricity_import_off_peak_energy_this_billing_cycle") == 10
    assert num(hass, "sensor.electricity_import_off_peak_cost_this_billing_cycle") == pytest.approx(
        10 * 0.165, abs=1e-4
    )

    await hass.services.async_call(
        DOMAIN, "reset_meters", {"config_entry": entry.entry_id, "bucket": "cycle"}, blocking=True
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.electricity_import_energy_this_billing_cycle")
    assert float(state.state) == 0
    assert state.attributes["last_period"] == pytest.approx(10.0)


async def test_state_survives_reload(hass, sydney, entry, freezer):
    await _setup(hass, entry, freezer, "2026-11-04 03:00")
    freezer.move_to(syd("2026-11-04 04:00"))
    hass.states.async_set(IMPORT, "1003.0", KWH)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert num(hass, "sensor.electricity_import_energy_today") == pytest.approx(3.0)
    # Energy used while unloaded is counted from the saved baseline.
    freezer.move_to(syd("2026-11-04 05:00"))
    hass.states.async_set(IMPORT, "1004.0", KWH)
    await hass.async_block_till_done()
    assert num(hass, "sensor.electricity_import_energy_today") == pytest.approx(4.0)


async def test_midnight_rollover(hass, sydney, entry, freezer):
    await _setup(hass, entry, freezer, "2026-11-04 23:00")
    freezer.move_to(syd("2026-11-05 00:00:01"))
    async_fire = __import__(
        "pytest_homeassistant_custom_component.common", fromlist=["async_fire_time_changed"]
    ).async_fire_time_changed
    async_fire(hass)
    await hass.async_block_till_done()
    freezer.move_to(syd("2026-11-05 01:00"))
    hass.states.async_set(IMPORT, "1002.0", KWH)
    await hass.async_block_till_done()
    today = hass.states.get("sensor.electricity_import_energy_today")
    assert float(today.state) == pytest.approx(1.0)
    assert today.attributes["last_period"] == pytest.approx(1.0)


async def test_plan_loads_off_event_loop(hass, sydney, entry, freezer, monkeypatch):
    """Validation, time zone and holiday loading read files: never on the loop.

    (HA's blocking-call detector doesn't fire in this test harness, so check
    the thread directly.)
    """
    import threading

    from custom_components import energy_tariff_assistant as integration

    threads = []
    real = integration.check_plan

    def spy(plan):
        threads.append(threading.current_thread())
        return real(plan)

    monkeypatch.setattr(integration, "check_plan", spy)
    await _setup(hass, entry, freezer, "2026-11-04 16:00")
    assert threads
    assert threading.main_thread() not in threads


async def test_unload(hass, sydney, entry, freezer):
    await _setup(hass, entry, freezer, "2026-11-04 16:00")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
