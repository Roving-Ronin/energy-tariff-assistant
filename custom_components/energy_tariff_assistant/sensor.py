"""Price, period, metering and bill sensors."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TariffConfigEntry
from .const import (
    ATTR_COST,
    ATTR_ENERGY,
    DOMAIN,
    EXPOSED_BUCKETS,
    SERVICE_CALIBRATE,
    STREAM_EXPORT,
    STREAM_IMPORT,
)
from .engine import Period
from .runtime import TariffRuntime

PARALLEL_UPDATES = 0

STREAM_LABEL = {STREAM_IMPORT: "Import", STREAM_EXPORT: "Export"}
BUCKET_LABEL = {"day": "today", "cycle": "this billing cycle"}
COST_LABEL = {STREAM_IMPORT: "cost", STREAM_EXPORT: "credit"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TariffConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    entities: list[SensorEntity] = []

    for stream in (STREAM_IMPORT, STREAM_EXPORT):
        entities += [
            PriceSensor(runtime, stream),
            PeriodSensor(runtime, stream),
            NextChangeSensor(runtime, stream),
        ]
    entities.append(DailySupplySensor(runtime))

    for stream in runtime.ledgers:
        periods = _periods(runtime, stream)
        for bucket in EXPOSED_BUCKETS:
            entities += [
                EnergySensor(runtime, stream, bucket, None),
                CostSensor(runtime, stream, bucket, None),
            ]
            for period in periods:
                entities += [
                    EnergySensor(runtime, stream, bucket, period),
                    CostSensor(runtime, stream, bucket, period),
                ]
    if STREAM_IMPORT in runtime.ledgers:
        entities += [SupplyAccruedSensor(runtime, b) for b in EXPOSED_BUCKETS]
        entities.append(BillSensor(runtime))

    async_add_entities(entities)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_CALIBRATE,
        {
            vol.Required(ATTR_ENERGY): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(ATTR_COST): vol.Coerce(float),
        },
        "async_calibrate",
    )


def _periods(runtime: TariffRuntime, stream: str) -> list[Period]:
    """Every period of `stream` across plan versions, first seen wins."""
    seen: dict[str, Period] = {}
    for plan in runtime.resolver.versions:
        for period in plan.stream(stream).periods:
            seen.setdefault(period.id, period)
    return list(seen.values())


class TariffEntity(SensorEntity):
    """Base: one device per entry, updated by the runtime's signal."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: TariffRuntime, key: str) -> None:
        self.runtime = runtime
        entry = runtime.entry
        plan = runtime.plan
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=plan.retailer,
            model=plan.plan,
            model_id=plan.plan_id,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.runtime.signal, self._async_refresh)
        )
        self._update()

    @callback
    def _async_refresh(self) -> None:
        self._update()
        self.async_write_ha_state()

    def _update(self) -> None:
        """Recompute the state from the runtime."""

    async def async_calibrate(self, energy: float, cost: float | None = None) -> None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_calibratable"
        )


# ----- prices ---------------------------------------------------------------


class PriceSensor(TariffEntity):
    """Price in force now, per kWh. Use as the Energy dashboard price entity."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(self, runtime: TariffRuntime, stream: str) -> None:
        super().__init__(runtime, f"{stream}_price")
        self.stream = stream
        self._attr_name = f"{STREAM_LABEL[stream]} price"
        self._attr_native_unit_of_measurement = f"{runtime.currency}/{UnitOfEnergy.KILO_WATT_HOUR}"
        self._attr_icon = "mdi:cash-plus" if stream == STREAM_IMPORT else "mdi:cash-minus"

    def _update(self) -> None:
        result = self.runtime.rate(self.stream)
        self._attr_native_value = round(result.rate, 6)
        self._attr_extra_state_attributes = {
            "period": result.period.id,
            "period_name": result.period.name,
            "charged_as": result.rate_period.id,
            "tier": result.tier_index,
            "retailer": result.plan.retailer,
            "plan": result.plan.plan,
            "forecast": self.runtime.forecast(self.stream),
        }


class PeriodSensor(TariffEntity):
    """Name of the period in force now (Peak, Shoulder, Off-peak...)."""

    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, runtime: TariffRuntime, stream: str) -> None:
        super().__init__(runtime, f"{stream}_period")
        self.stream = stream
        self._attr_name = f"{STREAM_LABEL[stream]} period"
        self._attr_icon = "mdi:clock-outline"
        self._attr_options = [p.name for p in _periods(runtime, stream)]

    def _update(self) -> None:
        result = self.runtime.rate(self.stream)
        self._attr_native_value = result.period.name
        self._attr_extra_state_attributes = {"period_id": result.period.id}


class NextChangeSensor(TariffEntity):
    """When the period in force next changes."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, runtime: TariffRuntime, stream: str) -> None:
        super().__init__(runtime, f"{stream}_next_change")
        self.stream = stream
        self._attr_name = f"{STREAM_LABEL[stream]} next period change"
        self._attr_icon = "mdi:clock-end"

    def _update(self) -> None:
        change = self.runtime.next_change(self.stream)
        self._attr_native_value = change
        if change is None:
            self._attr_extra_state_attributes = {}
            return
        nxt = self.runtime.resolver.period_at(self.stream, change)
        self._attr_extra_state_attributes = {"next_period": nxt.id, "next_period_name": nxt.name}


class DailySupplySensor(TariffEntity):
    """The plan's daily supply charge."""

    _attr_suggested_display_precision = 4
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, runtime: TariffRuntime) -> None:
        super().__init__(runtime, "supply_charge_daily")
        self._attr_name = "Daily supply charge"

    def _update(self) -> None:
        self._attr_native_unit_of_measurement = f"{self.runtime.currency}/d"
        self._attr_native_value = self.runtime.plan.supply_charge


# ----- metering -------------------------------------------------------------


class MeterEntity(TariffEntity):
    """Common to energy and cost meters: a ledger bucket, optionally one period."""

    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        runtime: TariffRuntime,
        stream: str,
        bucket: str,
        period: Period | None,
        metric: str,
    ) -> None:
        part = period.id if period else "total"
        super().__init__(runtime, f"{stream}_{part}_{metric}_{bucket}")
        self.stream = stream
        self.bucket = bucket
        self.period = period
        self.ledger = runtime.ledgers[stream]

    @property
    def available(self) -> bool:
        return self.bucket in self.ledger.buckets

    def _pick(self, values: dict[str, float] | None) -> float:
        if values is None:
            return 0.0
        if self.period is None:
            return sum(values.values())
        return values.get(self.period.id, 0.0)

    def _common_attributes(self, previous: float | None) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "source": self.runtime.sources[self.stream],
            "source_available": self.runtime.source_ok[self.stream],
            "last_period": previous,
        }
        if (prev := self.ledger.previous.get(self.bucket)) is not None:
            attrs["last_period_start"] = prev.start.isoformat()
        return attrs

    @property
    def last_reset(self) -> datetime | None:
        if self.bucket not in self.ledger.buckets:
            return None
        return self.ledger.bucket_start_time(self.bucket)


class EnergySensor(MeterEntity):
    """kWh used (or exported) in a period, today or this billing cycle."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(self, runtime, stream, bucket, period) -> None:
        super().__init__(runtime, stream, bucket, period, "energy")
        what = f"{period.name} energy" if period else "energy"
        self._attr_name = f"{STREAM_LABEL[stream]} {what} {BUCKET_LABEL[bucket]}"
        self._attr_icon = "mdi:transmission-tower-import" if stream == STREAM_IMPORT else "mdi:transmission-tower-export"

    def _update(self) -> None:
        bucket = self.ledger.buckets.get(self.bucket)
        self._attr_native_value = round(self._pick(bucket.kwh if bucket else None), 6)
        prev = self.ledger.previous.get(self.bucket)
        self._attr_extra_state_attributes = self._common_attributes(
            round(self._pick(prev.kwh), 6) if prev else None
        )

    async def async_calibrate(self, energy: float, cost: float | None = None) -> None:
        if self.period is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="calibrate_total"
            )
        self.ledger.calibrate(self.bucket, self.period.id, energy, cost)
        self.runtime.async_changed(forecasts=True)


class CostSensor(MeterEntity):
    """Cost of import (or credit for export) in a period, today or this cycle."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(self, runtime, stream, bucket, period) -> None:
        super().__init__(runtime, stream, bucket, period, "cost")
        label = COST_LABEL[stream]
        what = f"{period.name} {label}" if period else label
        self._attr_name = f"{STREAM_LABEL[stream]} {what} {BUCKET_LABEL[bucket]}"
        self._attr_native_unit_of_measurement = runtime.currency
        self._attr_icon = "mdi:cash"

    def _update(self) -> None:
        bucket = self.ledger.buckets.get(self.bucket)
        self._attr_native_value = round(self._pick(bucket.cost if bucket else None), 4)
        prev = self.ledger.previous.get(self.bucket)
        attrs = self._common_attributes(round(self._pick(prev.cost), 4) if prev else None)
        if bucket is not None:
            kwh = self._pick(bucket.kwh)
            attrs["average_rate"] = round(self._attr_native_value / kwh, 5) if kwh > 0 else None
        self._attr_extra_state_attributes = attrs


class SupplyAccruedSensor(TariffEntity):
    """Supply charge for the days so far, today or this billing cycle."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, runtime: TariffRuntime, bucket: str) -> None:
        super().__init__(runtime, f"supply_charge_{bucket}")
        self.bucket = bucket
        self._attr_name = f"Supply charge {BUCKET_LABEL[bucket]}"
        self._attr_native_unit_of_measurement = runtime.currency

    @property
    def last_reset(self) -> datetime | None:
        ledger = self.runtime.ledgers[STREAM_IMPORT]
        if self.bucket not in ledger.buckets:
            return None
        return ledger.bucket_start_time(self.bucket)

    def _update(self) -> None:
        value = self.runtime.supply_for_bucket(self.bucket)
        self._attr_native_value = round(value, 4) if value is not None else None


class BillSensor(TariffEntity):
    """Estimated bill so far this cycle: usage + supply - export credit."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:receipt-text-outline"

    def __init__(self, runtime: TariffRuntime) -> None:
        super().__init__(runtime, "bill_cycle")
        self._attr_name = "Estimated bill this billing cycle"
        self._attr_native_unit_of_measurement = runtime.currency

    @property
    def last_reset(self) -> datetime | None:
        ledger = self.runtime.ledgers[STREAM_IMPORT]
        return ledger.bucket_start_time("cycle") if "cycle" in ledger.buckets else None

    def _update(self) -> None:
        bill = self.runtime.bill_to_date()
        if bill is None:
            self._attr_native_value = None
            return
        self._attr_native_value = round(bill["total"], 2)
        self._attr_extra_state_attributes = {
            "usage": round(bill["usage"], 4),
            "supply": round(bill["supply"], 4),
            "export_credit": round(bill["export_credit"], 4),
            "estimate": True,
        }
