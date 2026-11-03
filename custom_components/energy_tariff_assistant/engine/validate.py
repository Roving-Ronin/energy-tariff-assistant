"""Validation for ha_tariff_plan v1 files.

Runs without third-party packages so the integration can use it inside
Home Assistant. The JSON Schema in ../schema describes the same structure
for editors and CI; this module also checks the cross-field rules the
schema cannot express.

Errors block loading or merging a plan. Warnings are reported only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import DAY_NAMES, DAY_SETS, FORMAT, MINUTES_PER_DAY, VERSION, Window, parse_hhmm

HHMM = re.compile(r"^\d{2}:\d{2}$")
ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")
SOURCE_TYPES = {"EME", "CDR", "retailer_factsheet", "bill", "manual"}
TREAT_AS = {"none", "saturday", "sunday"}
TIER_RESETS = {"day", "month", "bill"}
DEMAND_UNITS = {"per_kw_per_day", "per_kw_per_month"}
DEMAND_METHODS = {"max", "mean_of_top_n"}
DEMAND_OVER = {"month", "bill", "rolling_12_months"}
TOP_LEVEL_FIELDS = {
    "format", "version", "country", "region", "dnsp", "retailer", "plan", "plan_id",
    "customer_type", "effective_from", "effective_to", "source", "currency", "timezone",
    "holidays", "seasons", "supply_charge", "import", "export", "demand",
    "controlled_load", "needs_review", "notes",
}
HIGH_RATE_WARNING = 2.0
STALE_DAYS = 365


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(f"{path}: {message}")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_date(result: ValidationResult, path: str, value: Any) -> date | None:
    if not isinstance(value, str):
        result.error(path, "must be a YYYY-MM-DD string")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        result.error(path, f"not a valid date: {value!r}")
        return None


def _check_id(result: ValidationResult, path: str, value: Any) -> bool:
    if not isinstance(value, str) or not value or not set(value) <= ID_CHARS:
        result.error(path, "must be lowercase letters, digits and underscores")
        return False
    return True


def _check_window(
    result: ValidationResult, path: str, data: Any, seasons: dict[str, Any]
) -> Window | None:
    if not isinstance(data, dict):
        result.error(path, "must be an object")
        return None
    unknown = set(data) - {"days", "start", "end", "all_day", "seasons"}
    if unknown:
        result.error(path, f"unknown fields {sorted(unknown)}")

    days = data.get("days")
    if isinstance(days, str):
        if days not in DAY_SETS:
            result.error(f"{path}.days", f"must be all, weekdays, weekends or a list, not {days!r}")
            return None
    elif isinstance(days, list) and days:
        bad = [d for d in days if d not in DAY_NAMES]
        if bad:
            result.error(f"{path}.days", f"unknown day names {bad}")
            return None
        if len(set(days)) != len(days):
            result.error(f"{path}.days", "repeats a day")
    else:
        result.error(f"{path}.days", "is required")
        return None

    for name in data.get("seasons") or ():
        if name not in seasons:
            result.error(f"{path}.seasons", f"season {name!r} is not defined in seasons")

    if data.get("all_day"):
        if data["all_day"] is not True:
            result.error(f"{path}.all_day", "must be true when present")
        if "start" in data or "end" in data:
            result.error(path, "all_day windows must not also have start/end")
            return None
    else:
        if "start" not in data or "end" not in data:
            result.error(path, "needs start and end, or all_day: true")
            return None
        try:
            if not all(isinstance(data[k], str) and HHMM.match(data[k]) for k in ("start", "end")):
                raise ValueError
            start = parse_hhmm(data["start"])
            end = parse_hhmm(data["end"])
        except (ValueError, AttributeError):
            result.error(path, f"start/end must be HH:MM, got {data.get('start')!r}-{data.get('end')!r}")
            return None
        if start == MINUTES_PER_DAY:
            result.error(f"{path}.start", "cannot be 24:00")
            return None
        if start == end % MINUTES_PER_DAY:
            result.error(path, f"{data['start']}-{data['end']} is ambiguous; use all_day: true")
            return None

    try:
        return Window.from_dict(data)
    except (ValueError, KeyError) as err:
        result.error(path, str(err))
        return None


def _check_tiers(result: ValidationResult, path: str, period: dict[str, Any]) -> None:
    tiers = period.get("tiers")
    if not isinstance(tiers, list) or len(tiers) < 2:
        result.error(f"{path}.tiers", "needs at least two tiers (a capped tier and what follows it)")
        return
    previous = 0.0
    for index, tier in enumerate(tiers):
        tpath = f"{path}.tiers[{index}]"
        if not isinstance(tier, dict):
            result.error(tpath, "must be an object")
            continue
        unknown = set(tier) - {"up_to_kwh", "rate", "use_period"}
        if unknown:
            result.error(tpath, f"unknown fields {sorted(unknown)}")
        has_rate = "rate" in tier
        has_use = "use_period" in tier
        if has_rate == has_use:
            result.error(tpath, "needs exactly one of rate or use_period")
        if has_rate and not _is_number(tier["rate"]):
            result.error(f"{tpath}.rate", "must be a number")
        last = index == len(tiers) - 1
        if last:
            if "up_to_kwh" in tier:
                result.error(tpath, "the last tier must not have up_to_kwh; it says what applies after the cap")
        else:
            limit = tier.get("up_to_kwh")
            if not _is_number(limit) or limit <= 0:
                result.error(f"{tpath}.up_to_kwh", "required, a number above 0, on every tier but the last")
            elif limit <= previous:
                result.error(f"{tpath}.up_to_kwh", "must be larger than the previous tier's")
            else:
                previous = float(limit)
    reset = period.get("tier_reset", "day")
    if reset not in TIER_RESETS:
        result.error(f"{path}.tier_reset", f"must be one of {sorted(TIER_RESETS)}")


def _check_periods(
    result: ValidationResult,
    path: str,
    periods: Any,
    seasons: dict[str, Any],
) -> None:
    if not isinstance(periods, list) or not periods:
        result.error(path, "needs at least one period")
        return

    ids: dict[str, dict[str, Any]] = {}
    windows: dict[str, list[Window]] = {}
    defaults = []
    for index, period in enumerate(periods):
        ppath = f"{path}[{index}]"
        if not isinstance(period, dict):
            result.error(ppath, "must be an object")
            continue
        unknown = set(period) - {"id", "name", "rate", "tiers", "tier_reset", "windows", "default"}
        if unknown:
            result.error(ppath, f"unknown fields {sorted(unknown)}")
        pid = period.get("id")
        if _check_id(result, f"{ppath}.id", pid):
            if pid in ids:
                result.error(f"{ppath}.id", f"duplicate period id {pid!r}")
            ids[pid] = period
            ppath = f"{path}[{pid}]"
        if not isinstance(period.get("name"), str) or not period.get("name"):
            result.error(f"{ppath}.name", "is required")

        has_rate = "rate" in period
        has_tiers = "tiers" in period
        if has_rate == has_tiers:
            result.error(ppath, "needs exactly one of rate or tiers")
        if has_rate:
            if not _is_number(period["rate"]):
                result.error(f"{ppath}.rate", "must be a number")
            else:
                _rate_warnings(result, f"{ppath}.rate", period["rate"])
        if has_tiers:
            _check_tiers(result, ppath, period)
            for tindex, tier in enumerate(period.get("tiers") or ()):
                if isinstance(tier, dict) and _is_number(tier.get("rate")):
                    _rate_warnings(result, f"{ppath}.tiers[{tindex}].rate", tier["rate"])

        is_default = period.get("default", False)
        if not isinstance(is_default, bool):
            result.error(f"{ppath}.default", "must be true or false")
        raw_windows = period.get("windows")
        if is_default:
            defaults.append(pid)
            if raw_windows:
                result.error(ppath, "the default period must not have windows; it covers all unclaimed time")
        else:
            if not isinstance(raw_windows, list) or not raw_windows:
                result.error(f"{ppath}.windows", "required on every period except the default")
            else:
                parsed = [
                    _check_window(result, f"{ppath}.windows[{w}]", window, seasons)
                    for w, window in enumerate(raw_windows)
                ]
                if isinstance(pid, str):
                    windows[pid] = [w for w in parsed if w is not None]

    if len(defaults) != 1:
        result.error(path, f"needs exactly one period with default: true, found {len(defaults)}")

    # use_period references and loops.
    for pid, period in ids.items():
        for tindex, tier in enumerate(period.get("tiers") or ()):
            if not isinstance(tier, dict) or "use_period" not in tier:
                continue
            target = tier["use_period"]
            if target not in ids:
                result.error(f"{path}[{pid}].tiers[{tindex}].use_period", f"no period {target!r} in this list")
    for start in ids:
        if _has_loop(start, ids):
            result.error(f"{path}[{start}]", "use_period references loop back to this period")

    _check_overlaps(result, path, windows, seasons)


def _has_loop(start: str, ids: dict[str, dict[str, Any]]) -> bool:
    stack = [(start, frozenset())]
    while stack:
        pid, seen = stack.pop()
        period = ids.get(pid)
        if period is None:
            continue
        for tier in period.get("tiers") or ():
            target = tier.get("use_period") if isinstance(tier, dict) else None
            if target is None:
                continue
            if target == start or target in seen:
                return True
            stack.append((target, seen | {pid}))
    return False


def _check_overlaps(
    result: ValidationResult,
    path: str,
    windows: dict[str, list[Window]],
    seasons: dict[str, Any],
) -> None:
    """Every minute of every day type and season maps to at most one period."""
    season_keys: list[str | None] = []
    for month in range(1, 13):
        name = next((n for n, months in seasons.items() if month in (months or ())), None)
        if name not in season_keys:
            season_keys.append(name)

    reported: set[tuple[str, str]] = set()
    for season in season_keys:
        for weekday in range(7):
            owner: list[str | None] = [None] * MINUTES_PER_DAY
            for pid, plist in windows.items():
                for window in plist:
                    if weekday not in window.days:
                        continue
                    if window.seasons is not None and season not in window.seasons:
                        continue
                    for lo, hi in window.segments():
                        for minute in range(lo, hi):
                            other = owner[minute]
                            if other is not None and other != pid:
                                pair = tuple(sorted((other, pid)))
                                if pair not in reported:
                                    reported.add(pair)
                                    when = (
                                        f"{DAY_NAMES[weekday]} {minute // 60:02d}:{minute % 60:02d}"
                                        + (f" ({season})" if season else "")
                                    )
                                    result.error(path, f"periods {pair[0]!r} and {pair[1]!r} overlap, first at {when}")
                            else:
                                owner[minute] = pid


def _rate_warnings(result: ValidationResult, path: str, rate: float) -> None:
    if rate > HIGH_RATE_WARNING:
        result.warn(path, f"rate {rate} is above {HIGH_RATE_WARNING} per kWh; check the units")
    elif rate < 0:
        result.warn(path, f"rate {rate} is negative; correct only if the plan really charges for export")


def _check_supply(result: ValidationResult, path: str, data: Any) -> None:
    if not isinstance(data, dict):
        result.error(path, "must be an object with rate and unit")
        return
    if not _is_number(data.get("rate")) or data["rate"] < 0:
        result.error(f"{path}.rate", "must be a number of 0 or more")
    if data.get("unit") != "per_day":
        result.error(f"{path}.unit", "must be per_day")


def validate_plan(data: Any, today: date | None = None) -> ValidationResult:
    """Check a plan dict against the format and the cross-field rules."""
    result = ValidationResult()
    if not isinstance(data, dict):
        result.error("$", "plan must be a JSON object")
        return result

    unknown = set(data) - TOP_LEVEL_FIELDS
    if unknown:
        result.error("$", f"unknown fields {sorted(unknown)}")
    if data.get("format") != FORMAT:
        result.error("format", f"must be {FORMAT!r}")
    if data.get("version") != VERSION:
        result.error("version", f"must be {VERSION}")

    for key in ("country", "region", "retailer", "plan", "currency", "timezone"):
        if not isinstance(data.get(key), str) or not data.get(key):
            result.error(key, "is required")
    country = data.get("country")
    if isinstance(country, str) and (len(country) != 2 or not country.isupper()):
        result.error("country", "must be a 2-letter ISO code such as AU")
    currency = data.get("currency")
    if isinstance(currency, str) and (len(currency) != 3 or not currency.isupper()):
        result.error("currency", "must be a 3-letter ISO code such as AUD")
    tz = data.get("timezone")
    if isinstance(tz, str) and tz:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            result.error("timezone", f"unknown time zone {tz!r}")

    start = _check_date(result, "effective_from", data.get("effective_from"))
    end = None
    if data.get("effective_to") is not None:
        end = _check_date(result, "effective_to", data["effective_to"])
    if start and end and end < start:
        result.error("effective_to", "is before effective_from")

    source = data.get("source")
    if not isinstance(source, dict):
        result.error("source", "is required")
    else:
        if source.get("type") not in SOURCE_TYPES:
            result.error("source.type", f"must be one of {sorted(SOURCE_TYPES)}")
        checked = _check_date(result, "source.checked", source.get("checked"))
        if checked and today and (today - checked).days > STALE_DAYS:
            result.warn("source.checked", f"last checked {checked}, more than 12 months ago")

    holidays = data.get("holidays") or {}
    if not isinstance(holidays, dict):
        result.error("holidays", "must be an object")
    elif holidays.get("treat_as", "none") not in TREAT_AS:
        result.error("holidays.treat_as", f"must be one of {sorted(TREAT_AS)}")

    seasons = data.get("seasons") or {}
    if not isinstance(seasons, dict):
        result.error("seasons", "must be an object of name -> month list")
        seasons = {}
    month_owner: dict[int, str] = {}
    for name, months in seasons.items():
        if not isinstance(months, list) or not months:
            result.error(f"seasons.{name}", "must be a non-empty list of months")
            continue
        for month in months:
            if not isinstance(month, int) or not 1 <= month <= 12:
                result.error(f"seasons.{name}", f"month {month!r} must be 1-12")
            elif month in month_owner:
                result.error(f"seasons.{name}", f"month {month} is already in season {month_owner[month]!r}")
            else:
                month_owner[month] = name

    _check_supply(result, "supply_charge", data.get("supply_charge"))

    for stream in ("import", "export"):
        block = data.get(stream)
        if not isinstance(block, dict):
            result.error(stream, "is required")
            continue
        _check_periods(result, f"{stream}.periods", block.get("periods"), seasons)

    for index, load in enumerate(data.get("controlled_load") or ()):
        lpath = f"controlled_load[{index}]"
        if not isinstance(load, dict):
            result.error(lpath, "must be an object")
            continue
        _check_id(result, f"{lpath}.id", load.get("id"))
        _check_supply(result, f"{lpath}.supply_charge", load.get("supply_charge"))
        _check_periods(result, f"{lpath}.periods", load.get("periods"), seasons)

    for index, charge in enumerate(data.get("demand") or ()):
        dpath = f"demand[{index}]"
        if not isinstance(charge, dict):
            result.error(dpath, "must be an object")
            continue
        _check_id(result, f"{dpath}.id", charge.get("id"))
        if not _is_number(charge.get("rate")) or charge["rate"] < 0:
            result.error(f"{dpath}.rate", "must be a number of 0 or more")
        if charge.get("unit") not in DEMAND_UNITS:
            result.error(f"{dpath}.unit", f"must be one of {sorted(DEMAND_UNITS)}")
        raw_windows = charge.get("windows")
        if not isinstance(raw_windows, list) or not raw_windows:
            result.error(f"{dpath}.windows", "needs at least one window")
        else:
            for w, window in enumerate(raw_windows):
                _check_window(result, f"{dpath}.windows[{w}]", window, seasons)
        measure = charge.get("measure") or {}
        if measure.get("method", "max") not in DEMAND_METHODS:
            result.error(f"{dpath}.measure.method", f"must be one of {sorted(DEMAND_METHODS)}")
        if measure.get("method") == "mean_of_top_n" and not isinstance(measure.get("n"), int):
            result.error(f"{dpath}.measure.n", "required for mean_of_top_n")
        if measure.get("over", "month") not in DEMAND_OVER:
            result.error(f"{dpath}.measure.over", f"must be one of {sorted(DEMAND_OVER)}")
        if measure.get("interval_minutes", 30) not in (5, 15, 30, 60):
            result.error(f"{dpath}.measure.interval_minutes", "must be 5, 15, 30 or 60")

    return result
