"""Price sensors (with forecasts), capped-rate usage, and Active tariff's grid sensors and totals."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.util import dt as dt_util
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TariffConfigEntry
from .active import ActiveRuntime
from .const import (
    ACTIVE_SLUG,
    ACTIVE_TITLE,
    ACTIVE_UNIQUE_ID,
    CONF_EXPORT_SENSOR,
    CONF_IMPORT_SENSOR,
    CONF_KIND,
    CONF_SLUG,
    DOMAIN,
    KIND_ACTIVE,
    STREAM_EXPORT,
    STREAM_IMPORT,
    signal_active,
)
from .engine import DemandCharge, Period
from .runtime import TariffRuntime

PARALLEL_UPDATES = 0

STREAM_LABEL = {STREAM_IMPORT: "Import", STREAM_EXPORT: "Export"}
FORECAST_ATTRIBUTES = frozenset({"forecast", "forecasts"})
# Totals change with every grid reading (some meters report every few seconds): write the state at most this often,
# so the history database stays small. Every reading is still counted.
TOTAL_WRITE_INTERVAL = 30.0  # seconds


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TariffConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if entry.data.get(CONF_KIND) == KIND_ACTIVE:
        runtime: ActiveRuntime = entry.runtime_data
        async_add_entities(
            [
                GridPriceSensor(runtime, STREAM_IMPORT),
                GridPriceSensor(runtime, STREAM_EXPORT),
                TotalSensor(runtime, "import_cost", "Grid - Import Cost - Total", "mdi:cash-minus"),
                TotalSensor(runtime, "export_compensation", "Grid - Export Compensation - Total", "mdi:cash-plus"),
                SupplyChargeSensor(runtime),
                TotalSensor(runtime, "supply_charge", "Grid - Supply Charge - Total", "mdi:transmission-tower"),
                TotalSensor(runtime, "demand_charge", "Grid - Demand Charge - Total", "mdi:flash-alert"),
            ]
        )
        return

    plan_runtime: TariffRuntime = entry.runtime_data
    entities: list[SensorEntity] = [PlanPriceSensor(plan_runtime, s) for s in (STREAM_IMPORT, STREAM_EXPORT)]
    for stream in (STREAM_IMPORT, STREAM_EXPORT):
        entities += [CapUsedSensor(plan_runtime, stream, p) for p in plan_runtime.capped_periods(stream)]
    entities += [DemandPeakSensor(plan_runtime, c) for c in plan_runtime.demand_charges()]
    async_add_entities(entities)


def price_attributes(runtime: TariffRuntime, stream: str) -> tuple[float, dict[str, Any]]:
    """The price now and the attributes optimisers read, laid out as Amber Express publishes them."""
    result = runtime.rate(stream)
    start, end = runtime.period_window(stream)
    plan = result.plan
    return round(result.rate, 6), {
        "start_time": start.isoformat() if start else None,
        "end_time": end.isoformat() if end else None,
        "period": result.period.id,
        "period_name": result.period.name,
        "charged_as": result.rate_period.id,
        "tier": result.tier_index,
        "retailer": plan.retailer,
        "plan": plan.plan,
        "plan_id": plan.plan_id,
        # Each price holds until the next point; without this HAEO interpolates `forecast` linearly
        "interpolation_mode": "previous",
        "forecast": runtime.forecast(stream),  # {time, value}: Nimbus, HAEO's own format
        "forecasts": runtime.intervals(stream),  # Amber's layout, 30-min slots: HAEO, EMHASS templates
    }


# ----- plan entries -----------------------------------------------------------


class PlanEntity(SensorEntity):
    """A plan entry's entity: one device per plan, updated by the plan's runtime."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: TariffRuntime, key: str, object_id: str) -> None:
        self.runtime = runtime
        entry = runtime.entry
        plan = runtime.plan
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        # The entity ID comes from the entry (fixed when it was created), never the plan's content
        self.entity_id = f"sensor.{entry.data[CONF_SLUG]}_{object_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=plan.retailer,
            model=plan.plan,
            model_id=plan.plan_id,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.runtime.signal, self._async_refresh))
        self._update()

    @callback
    def _async_refresh(self) -> None:
        self._update()
        self.async_write_ha_state()

    def _update(self) -> None:
        """Recompute the state from the runtime."""


class PlanPriceSensor(PlanEntity):
    """This plan's price now, per kWh, with forecasts. Export is positive: what you're paid."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    _unrecorded_attributes = FORECAST_ATTRIBUTES

    def __init__(self, runtime: TariffRuntime, stream: str) -> None:
        super().__init__(runtime, f"{stream}_price", f"{stream}_price")
        self.stream = stream
        self._attr_name = f"{STREAM_LABEL[stream]} Price"
        self._attr_native_unit_of_measurement = f"{runtime.currency}/{UnitOfEnergy.KILO_WATT_HOUR}"
        self._attr_icon = "mdi:transmission-tower-import" if stream == STREAM_IMPORT else "mdi:transmission-tower-export"

    def _update(self) -> None:
        self._attr_native_value, self._attr_extra_state_attributes = price_attributes(self.runtime, self.stream)


class CapUsedSensor(PlanEntity):
    """kWh counted toward a capped rate ("first 50 kWh free") since its last reset."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:gauge"

    def __init__(self, runtime: TariffRuntime, stream: str, period: Period) -> None:
        super().__init__(runtime, f"{stream}_{period.id}_used", f"{stream}_{period.id}_used")
        self.stream = stream
        self.period = period
        self._attr_name = f"{STREAM_LABEL[stream]} {period.name} Used"

    @property
    def available(self) -> bool:
        return self.stream in self.runtime.ledgers

    def _cap(self) -> tuple[float | None, Period]:
        current = next(
            (p for p in self.runtime.plan.stream(self.stream).periods if p.id == self.period.id), self.period
        )
        return next((t.up_to_kwh for t in current.tiers if t.up_to_kwh is not None), None), current

    def _update(self) -> None:
        used = self.runtime.used(self.stream, self.period.id)
        cap, period = self._cap()
        self._attr_native_value = round(used, 3) if used is not None else None
        ledger = self.runtime.ledgers.get(self.stream)
        self._attr_last_reset = None
        if ledger is not None and period.tier_reset in ledger.buckets:
            self._attr_last_reset = ledger.bucket_start_time(period.tier_reset)
        result = self.runtime.rate(self.stream)
        self._attr_extra_state_attributes = {
            "cap_kwh": cap,
            "remaining_kwh": round(max(cap - used, 0.0), 3) if cap is not None and used is not None else None,
            "resets": period.tier_reset,
            "over_cap_rate_from": next((t.use_period for t in period.tiers if t.use_period), None),
            "rate_now": round(result.rate, 6) if result.period.id == self.period.id else None,
            "source": self.runtime.sources.get(self.stream),
        }


DEMAND_OVER = {"month": "month", "bill": "bill", "rolling_12_months": "rolling 12 months"}


class DemandPeakSensor(PlanEntity):
    """A demand charge's measured demand (kW): the peak 30-minute reading in its windows this month (or bill, or
    12 months), and what the charge comes to so far."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:flash-alert"

    def __init__(self, runtime: TariffRuntime, charge: DemandCharge) -> None:
        super().__init__(runtime, f"demand_{charge.id}_peak", f"demand_{charge.id}_peak")
        self.charge = charge
        self._attr_name = f"{charge.name} Peak"

    @property
    def available(self) -> bool:
        return self.charge.id in self.runtime.demand

    def _update(self) -> None:
        meter = self.runtime.demand.get(self.charge.id)
        if meter is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {"source": None}
            return
        now = dt_util.utcnow()
        today = self.runtime.resolver.local(now).date()
        current = meter.applies_on(today) or self.charge
        peak = meter.peak(today)
        start, end = meter.period_bounds(today)
        charge_so_far, days = meter.period_charge(today, today)
        self._attr_native_value = round(meter.demand_kw(today), 3)
        self._attr_extra_state_attributes = {
            "peak_at": self.runtime.resolver.local(peak.at).isoformat() if peak else None,
            "measured_over": DEMAND_OVER.get(meter.over, meter.over),
            "minimum_kw": current.measure.minimum_kw,
            "billable_kw": round(max(meter.demand_kw(today), current.measure.minimum_kw or 0.0), 3),
            "rate": current.rate,
            "unit": current.unit,
            "applies_today": meter.applies_on(today) is not None,
            "period_start": start.isoformat(),
            "period_end": (end - timedelta(days=1)).isoformat(),
            "days_counted": days,
            "charge_so_far": round(charge_so_far, 4),
            "source": self.runtime.sources.get(STREAM_IMPORT),
        }


# ----- Active tariff ----------------------------------------------------------


class ActiveEntity(SensorEntity):
    """An Active tariff entity: follows whichever plan is in use.

    Named on its own ("Grid - Import Price"), not after the device: these are the names people pick in the Energy
    dashboard and optimisers."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, runtime: ActiveRuntime, key: str) -> None:
        self.runtime = runtime
        self._attr_unique_id = f"{ACTIVE_UNIQUE_ID}_{key}"
        self.entity_id = f"sensor.{ACTIVE_SLUG}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ACTIVE_UNIQUE_ID)},
            name=ACTIVE_TITLE,
            manufacturer="Energy Tariff Assistant",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, signal_active(), self._async_refresh))
        self._update()

    @callback
    def _async_refresh(self) -> None:
        self._update()
        self.async_write_ha_state()

    def _update(self) -> None:
        """Recompute the state."""

    def _plan_attributes(self) -> dict[str, Any]:
        entry = self.runtime.active_entry
        return {"active_plan": entry.title if entry else None}


class GridPriceSensor(ActiveEntity):
    """The active plan's price now, with its forecasts: one fixed name for the Energy dashboard and optimisers."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    _unrecorded_attributes = FORECAST_ATTRIBUTES

    def __init__(self, runtime: ActiveRuntime, stream: str) -> None:
        super().__init__(runtime, f"{stream}_price")
        self.stream = stream
        self._attr_name = f"Grid - {STREAM_LABEL[stream]} Price"
        self._attr_icon = "mdi:transmission-tower-import" if stream == STREAM_IMPORT else "mdi:transmission-tower-export"

    @property
    def available(self) -> bool:
        return self.runtime.plan_runtime is not None

    def _update(self) -> None:
        plan_runtime = self.runtime.plan_runtime
        if plan_runtime is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = self._plan_attributes()
            return
        self._attr_native_unit_of_measurement = f"{plan_runtime.currency}/{UnitOfEnergy.KILO_WATT_HOUR}"
        value, attributes = price_attributes(plan_runtime, self.stream)
        self._attr_native_value = value
        self._attr_extra_state_attributes = {
            **self._plan_attributes(),
            "source_entity": self.runtime.source_entity(self.stream),
            **attributes,
        }


class TotalSensor(ActiveEntity):
    """A running $ total kept across plan switches (import cost, export compensation, supply and demand charges).

    Settable with the `calibrate` action (to match a bill); `last_reset` then moves to that moment, so the
    statistics start a new cycle instead of counting the jump."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _unrecorded_attributes = frozenset({"energy_kwh", "average_rate"})

    def __init__(self, runtime: ActiveRuntime, key: str, name: str, icon: str) -> None:
        super().__init__(runtime, f"{key}_total")
        self.key = key
        self._attr_name = name
        self._attr_icon = icon
        self._last_write: datetime | None = None
        self._pending: Callable[[], None] | None = None

    @callback
    def _async_refresh(self) -> None:
        """Write at most every TOTAL_WRITE_INTERVAL seconds; a change in between is written when it's up."""
        now = dt_util.utcnow()
        wait = TOTAL_WRITE_INTERVAL - (now - self._last_write).total_seconds() if self._last_write else 0.0
        if wait <= 0:
            self._write(now)
        elif self._pending is None:
            self._pending = async_call_later(self.hass, wait, self._async_write_later)

    @callback
    def _async_write_later(self, _now: datetime) -> None:
        self._pending = None
        self._write(dt_util.utcnow())

    @callback
    def _write(self, now: datetime) -> None:
        self._last_write = now
        self._update()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._pending:
            self._pending()
            self._pending = None

    @callback
    def async_calibrate(self, value: float) -> None:
        """Set this total (the `calibrate` action)."""
        self.runtime.async_calibrate(self.key, value)
        if self._pending:  # the new value goes out now, not after the wait
            self._pending()
            self._pending = None
        self._write(dt_util.utcnow())

    @property
    def available(self) -> bool:
        if self.key == "supply_charge":
            return True
        # Cost and compensation need the grid energy sensors: without them there's nothing counted
        plan_runtime = self.runtime.plan_runtime
        if self.key == "demand_charge":
            return plan_runtime is not None and STREAM_IMPORT in plan_runtime.ledgers
        stream = STREAM_IMPORT if self.key == "import_cost" else STREAM_EXPORT
        return plan_runtime is not None and stream in plan_runtime.ledgers

    def _update(self) -> None:
        plan_runtime = self.runtime.plan_runtime
        # Keep the currency while no plan is in use, so the total's statistics don't break
        self._attr_native_unit_of_measurement = plan_runtime.currency if plan_runtime else "AUD"
        self._attr_native_value = round(self.runtime.totals[self.key], 6)
        reset = self.runtime.last_reset.get(self.key)
        self._attr_last_reset = dt_util.parse_datetime(reset) if reset else None
        attributes = self._plan_attributes()
        if self.key == "supply_charge":
            attributes["days_counted"] = self.runtime.days_counted
        if self.key in self.runtime.energy:
            stream = STREAM_IMPORT if self.key == "import_cost" else STREAM_EXPORT
            kwh = self.runtime.energy[self.key]
            counted = self.runtime.totals[self.key] - self.runtime.base[self.key]
            attributes["energy_kwh"] = round(kwh, 3)
            attributes["average_rate"] = round(counted / kwh, 6) if kwh > 0 else None
            attributes["source"] = self.runtime.entry.options.get(
                CONF_IMPORT_SENSOR if stream == STREAM_IMPORT else CONF_EXPORT_SENSOR
            )
        self._attr_extra_state_attributes = attributes


class SupplyChargeSensor(ActiveEntity):
    """The active plan's daily supply charge."""

    _attr_suggested_display_precision = 4
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, runtime: ActiveRuntime) -> None:
        super().__init__(runtime, "supply_charge")
        self._attr_name = "Grid - Supply Charge"

    @property
    def available(self) -> bool:
        return self.runtime.plan_runtime is not None

    def _update(self) -> None:
        plan_runtime = self.runtime.plan_runtime
        if plan_runtime is not None:
            self._attr_native_unit_of_measurement = f"{plan_runtime.currency}/d"
        self._attr_native_value = self.runtime.supply_rate()
        self._attr_extra_state_attributes = self._plan_attributes()
