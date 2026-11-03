"""Data model for ha_tariff_plan v1 files.

`load_plan` turns a plan dict into dataclasses. It assumes the dict has
already passed `validate_plan`; it does not re-check the rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

FORMAT = "ha_tariff_plan"
VERSION = 1

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_SETS = {
    "all": frozenset(range(7)),
    "weekdays": frozenset(range(5)),
    "weekends": frozenset((5, 6)),
}
MINUTES_PER_DAY = 1440


def parse_hhmm(value: str) -> int:
    """'HH:MM' -> minutes after midnight. '24:00' is allowed as an end."""
    hours, minutes = value.split(":")
    total = int(hours) * 60 + int(minutes)
    if not 0 <= total <= MINUTES_PER_DAY or int(minutes) > 59:
        raise ValueError(f"time out of range: {value!r}")
    return total


def parse_days(value: Any) -> frozenset[int]:
    """'all' / 'weekdays' / 'weekends' / ['mon', ...] -> weekday numbers (Mon=0)."""
    if isinstance(value, str):
        if value not in DAY_SETS:
            raise ValueError(f"unknown days value: {value!r}")
        return DAY_SETS[value]
    days = set()
    for item in value:
        if item not in DAY_NAMES:
            raise ValueError(f"unknown day: {item!r}")
        days.add(DAY_NAMES.index(item))
    return frozenset(days)


@dataclass(frozen=True)
class Window:
    """When a period applies. `start` inclusive, `end` exclusive, in minutes.

    An `end` at or before `start` wraps past midnight. Days are matched by
    calendar day: on each listed day the window covers clock times from
    `start` to midnight and from midnight to `end`. So "weekdays 22:00-07:00"
    covers Monday 00:00-07:00 and Friday 22:00-24:00, but not Saturday
    00:00-07:00. This matches how retailers word plans.
    """

    days: frozenset[int]
    start: int
    end: int
    all_day: bool = False
    seasons: frozenset[str] | None = None

    @property
    def wraps(self) -> bool:
        return not self.all_day and self.end <= self.start

    def segments(self) -> list[tuple[int, int]]:
        """(start_minute, end_minute) pieces within one listed day, end exclusive."""
        if self.all_day:
            return [(0, MINUTES_PER_DAY)]
        if not self.wraps:
            return [(self.start, self.end)]
        pieces = [(self.start, MINUTES_PER_DAY)]
        if self.end > 0:
            pieces.append((0, self.end))
        return pieces

    def covers(self, weekday: int, minute: int, season: str | None) -> bool:
        """True if this window applies on `weekday` at `minute` in `season`."""
        if weekday not in self.days:
            return False
        if self.seasons is not None and season not in self.seasons:
            return False
        return any(lo <= minute < hi for lo, hi in self.segments())

    def boundaries(self) -> set[int]:
        """Minutes of the day at which this window starts or stops applying."""
        if self.all_day:
            return {0}
        return {self.start, self.end % MINUTES_PER_DAY, 0}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Window:
        seasons = data.get("seasons")
        if data.get("all_day"):
            return cls(
                days=parse_days(data.get("days", "all")),
                start=0,
                end=MINUTES_PER_DAY,
                all_day=True,
                seasons=frozenset(seasons) if seasons else None,
            )
        return cls(
            days=parse_days(data.get("days", "all")),
            start=parse_hhmm(data["start"]),
            end=parse_hhmm(data["end"]),
            seasons=frozenset(seasons) if seasons else None,
        )


@dataclass(frozen=True)
class Tier:
    up_to_kwh: float | None
    rate: float | None = None
    use_period: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tier:
        return cls(
            up_to_kwh=data.get("up_to_kwh"),
            rate=data.get("rate"),
            use_period=data.get("use_period"),
        )


@dataclass(frozen=True)
class Period:
    id: str
    name: str
    rate: float | None
    tiers: tuple[Tier, ...] = ()
    tier_reset: str = "day"
    windows: tuple[Window, ...] = ()
    default: bool = False

    @property
    def tiered(self) -> bool:
        return bool(self.tiers)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Period:
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            rate=data.get("rate"),
            tiers=tuple(Tier.from_dict(t) for t in data.get("tiers", ())),
            tier_reset=data.get("tier_reset", "day"),
            windows=tuple(Window.from_dict(w) for w in data.get("windows", ())),
            default=bool(data.get("default", False)),
        )


@dataclass(frozen=True)
class Stream:
    """One metered flow with its own periods: import, export or a controlled load."""

    key: str
    periods: tuple[Period, ...]
    name: str | None = None
    supply_charge: float | None = None

    def period(self, period_id: str) -> Period:
        for period in self.periods:
            if period.id == period_id:
                return period
        raise KeyError(period_id)

    @property
    def default_period(self) -> Period:
        for period in self.periods:
            if period.default:
                return period
        raise ValueError(f"stream {self.key!r} has no default period")

    @classmethod
    def from_dict(cls, key: str, data: dict[str, Any]) -> Stream:
        supply = data.get("supply_charge")
        return cls(
            key=key,
            name=data.get("name"),
            periods=tuple(Period.from_dict(p) for p in data.get("periods", ())),
            supply_charge=supply.get("rate") if isinstance(supply, dict) else None,
        )


@dataclass(frozen=True)
class DemandMeasure:
    interval_minutes: int = 30
    method: str = "max"
    over: str = "month"
    n: int | None = None
    minimum_kw: float | None = None


@dataclass(frozen=True)
class DemandCharge:
    id: str
    name: str
    rate: float
    unit: str
    windows: tuple[Window, ...]
    measure: DemandMeasure

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DemandCharge:
        measure = data.get("measure", {})
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            rate=data["rate"],
            unit=data["unit"],
            windows=tuple(Window.from_dict(w) for w in data.get("windows", ())),
            measure=DemandMeasure(
                interval_minutes=measure.get("interval_minutes", 30),
                method=measure.get("method", "max"),
                over=measure.get("over", "month"),
                n=measure.get("n"),
                minimum_kw=measure.get("minimum_kw"),
            ),
        )


@dataclass(frozen=True)
class Plan:
    country: str
    region: str
    retailer: str
    plan: str
    effective_from: date
    effective_to: date | None
    currency: str
    timezone: str
    supply_charge: float
    import_: Stream
    export: Stream
    dnsp: str | None = None
    plan_id: str | None = None
    customer_type: str | None = None
    holidays_treat_as: str = "none"
    holidays_calendar: tuple[str, str | None] | None = None
    seasons: dict[str, frozenset[int]] = field(default_factory=dict)
    demand: tuple[DemandCharge, ...] = ()
    controlled_load: tuple[Stream, ...] = ()
    source: dict[str, Any] = field(default_factory=dict)
    notes: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def stream(self, key: str) -> Stream:
        if key == "import":
            return self.import_
        if key == "export":
            return self.export
        for load in self.controlled_load:
            if load.key == key:
                return load
        raise KeyError(key)

    @property
    def streams(self) -> tuple[Stream, ...]:
        return (self.import_, self.export, *self.controlled_load)

    def season_for_month(self, month: int) -> str | None:
        for name, months in self.seasons.items():
            if month in months:
                return name
        return None

    def covers_date(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to


def load_plan(data: dict[str, Any]) -> Plan:
    """Build a Plan from a validated plan dict."""
    holidays = data.get("holidays") or {}
    calendar = holidays.get("calendar")
    if calendar:
        holidays_calendar = (calendar["country"], calendar.get("subdivision"))
    else:
        holidays_calendar = (data["country"], data.get("region"))

    effective_to = data.get("effective_to")
    return Plan(
        country=data["country"],
        region=data["region"],
        retailer=data["retailer"],
        plan=data["plan"],
        effective_from=date.fromisoformat(data["effective_from"]),
        effective_to=date.fromisoformat(effective_to) if effective_to else None,
        currency=data["currency"],
        timezone=data["timezone"],
        supply_charge=data["supply_charge"]["rate"],
        import_=Stream.from_dict("import", data["import"]),
        export=Stream.from_dict("export", data["export"]),
        dnsp=data.get("dnsp"),
        plan_id=data.get("plan_id"),
        customer_type=data.get("customer_type"),
        holidays_treat_as=holidays.get("treat_as", "none"),
        holidays_calendar=holidays_calendar,
        seasons={k: frozenset(v) for k, v in (data.get("seasons") or {}).items()},
        demand=tuple(DemandCharge.from_dict(d) for d in data.get("demand") or ()),
        controlled_load=tuple(
            Stream.from_dict(c["id"], c) for c in data.get("controlled_load") or ()
        ),
        source=data.get("source") or {},
        notes=data.get("notes"),
        raw=data,
    )
