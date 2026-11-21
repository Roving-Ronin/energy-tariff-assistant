"""Convert Consumer Data Right (Energy Made Easy) plan detail into ha_tariff_plan dicts.

Input: the response of CDR "Get Generic Plan Detail" (v3), as served by the AER
at https://cdr.energymadeeasy.gov.au/<path>/cds-au/v1/energy/plans/<planId>,
either the whole response or its `data` object.

Output: one plan per distributor in the plan's geography (a CDR plan can be
offered on several networks; ours have one DNSP each), plus the reasons for
anything left out. Every converted plan is `needs_review: true`.

Rules:
- **GST is added to charges.** CDR prices are "exclusive of GST" (unit prices,
  daily supply charges); our plans hold what the bill shows, GST included, so x1.1.
  Feed-in: households aren't paid GST, so residential (and unstated) plans use
  the published rate; business plans add GST, as paid to GST-registered
  businesses, with a note for those who aren't registered.
- Time zone: the plan's stated `timeZone` wins (LOCAL: the state's zone; AEST:
  Australia/Brisbane, so fixed-AEST windows stay right through daylight saving).
  **Absent** uses the network's clock, not the standard's AEST default: NSW, VIC
  and SA networks follow local time, Evoenergy and TasNetworks stay on AEST
  (`AEST_NETWORKS`). Absent, or stated against the network's clock, adds a
  review note (a retailer can apply its own rule, so neither is assumed wrong).
  A retailer known to bill on its own clock (`RETAILER_CLOCKS`, e.g. CovaU:
  local time) overrides what its plan data says, with a note.
- Window end times like 14:59 or 23:59:59 are inclusive ends: they become the
  next minute (15:00, 24:00). A whole day becomes `all_day`.
- Time of use: the rate covering the most time becomes the default period.
  Stepped rates (`volume`) become tiers, resetting per `period` (P1D day, P1M
  month); other periods are skipped.
- Tariff periods (mm-dd ranges) that follow month boundaries become seasons;
  others, banded supply charges, FLEXIBLE and QUOTA pricing are skipped with a
  reason: never guessed.
- Feed-in: the retailer-paid tariff (PREMIUM government schemes are closed to
  new customers and left out). None: feed-in 0 with a note.
- Demand charges and time-of-use controlled load: left out with a note for now.
- Discounts, incentives and fees are summarised in `notes`, not priced.

Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
import json
import re
from typing import Any

from ..model import FORMAT, VERSION, Window

GST = 1.1
SOURCE_URL = "https://www.energymadeeasy.gov.au/"

# Lower-case keyword in a CDR distributor name -> (DNSP name used in plans, state)
DISTRIBUTORS: list[tuple[str, str, str]] = [
    ("ausgrid", "Ausgrid", "NSW"),
    ("endeavour", "Endeavour Energy", "NSW"),
    ("essential", "Essential Energy", "NSW"),
    ("evoenergy", "Evoenergy", "ACT"),
    ("actewagl distribution", "Evoenergy", "ACT"),
    ("energex", "Energex", "QLD"),
    ("ergon", "Ergon Energy", "QLD"),
    ("citipower", "CitiPower", "VIC"),
    ("powercor", "Powercor", "VIC"),
    ("jemena", "Jemena", "VIC"),
    ("united", "United Energy", "VIC"),
    ("ausnet", "AusNet Services", "VIC"),
    ("sa power", "SA Power Networks", "SA"),
    ("sapn", "SA Power Networks", "SA"),
    ("tasnetworks", "TasNetworks", "TAS"),
    ("tas networks", "TasNetworks", "TAS"),
]

STATE_TIMEZONES = {
    "NSW": "Australia/Sydney",
    "ACT": "Australia/Sydney",
    "VIC": "Australia/Melbourne",
    "QLD": "Australia/Brisbane",
    "SA": "Australia/Adelaide",
    "TAS": "Australia/Hobart",
}
AEST_ZONE = "Australia/Brisbane"  # UTC+10 all year
# Networks whose time-of-use periods stay on AEST through daylight saving (an hour later on the clock in
# summer). ActewAGL: "Evoenergy does not adjust the tariff times to align with daylight saving"; TasNetworks
# publishes its peak as AEST 7-10am / AEDT 8-11am. NSW, VIC and SA networks follow local time; QLD, WA and the
# NT have no daylight saving.
AEST_NETWORKS = frozenset({"Evoenergy", "TasNetworks"})
# Retailers known to apply their own clock whatever their plan data says, by CDR brand (lower case), with
# where we know it from. CovaU: bills on local time during daylight saving in NSW, VIC and SA (maintainer,
# 2026-11-21), while its Energy Made Easy data says AEST.
RETAILER_CLOCKS = {"covau": "LOCAL"}
DST_STATES = frozenset({"NSW", "ACT", "VIC", "SA", "TAS"})


def observes_dst(state: str) -> bool:
    return state in DST_STATES

CDR_DAYS = {"MON": "mon", "TUE": "tue", "WED": "wed", "THU": "thu", "FRI": "fri", "SAT": "sat", "SUN": "sun"}
DAY_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TIER_RESET = {"P1D": "day", "P1M": "month"}
MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
NAME_BY_TYPE = {
    "PEAK": "Peak",
    "OFF_PEAK": "Off-peak",
    "SHOULDER": "Shoulder",
    "SHOULDER1": "Shoulder 1",
    "SHOULDER2": "Shoulder 2",
    "SOLAR_SPONGE": "Solar sponge",
}


class CdrImportError(ValueError):
    """The input is not usable CDR electricity plan detail."""


@dataclass
class Conversion:
    """Plans made from one CDR plan, and what couldn't be converted."""

    plans: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # whole plan or distributor not converted, with why
    notes: list[str] = field(default_factory=list)  # parts left out of every converted plan


def map_distributor(name: str) -> tuple[str, str] | None:
    """(DNSP, state) for a CDR distributor name, or None if unknown."""
    lower = name.lower()
    for keyword, dnsp, state in DISTRIBUTORS:
        if keyword in lower:
            return dnsp, state
    return None


# ----- small helpers -----------------------------------------------------------


def with_gst(value: Any) -> float:
    """CDR price string (ex GST) -> float including GST, rounded to 6 places."""
    try:
        return round(float(value) * GST, 6)
    except (TypeError, ValueError) as err:
        raise CdrImportError(f"not a price: {value!r}") from err


def _hhmm(value: str, end: bool) -> str:
    """ISO time ('07:00', '07:00:00', 'T0700', '14:59:59+10:00') -> 'HH:MM'.

    An inclusive end (minute 59) becomes the next minute; '24:00' at midnight.
    """
    match = re.match(r"^T?(\d{2}):?(\d{2})", value or "")
    if not match:
        raise CdrImportError(f"not a time: {value!r}")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if end and minutes == 59:
        hours, minutes = hours + 1, 0
    if end and hours == 0 and minutes == 0:
        hours = 24
    if hours > 24 or minutes > 59:
        raise CdrImportError(f"not a time: {value!r}")
    return f"{hours:02d}:{minutes:02d}"


def _days(values: list[str]) -> tuple[Any, bool]:
    """CDR day list -> our days value, and whether PUBLIC_HOLIDAYS was in it."""
    holidays = "PUBLIC_HOLIDAYS" in (values or [])
    days = sorted({CDR_DAYS[d] for d in values or [] if d in CDR_DAYS}, key=DAY_ORDER.index)
    if not days:
        raise CdrImportError("a time window has no days")
    if len(days) == 7:
        return "all", holidays
    if days == list(DAY_ORDER[:5]):
        return "weekdays", holidays
    if days == ["sat", "sun"]:
        return "weekends", holidays
    return days, holidays


def _window(days: Any, start: str | None, end: str | None, seasons: list[str] | None) -> dict[str, Any]:
    s = _hhmm(start, end=False) if start else "00:00"
    e = _hhmm(end, end=True) if end else "24:00"
    out: dict[str, Any] = {"days": days}
    if (s, e) in (("00:00", "24:00"), ("00:00", "00:00")):
        out["all_day"] = True
    else:
        out["start"], out["end"] = s, e
    if seasons:
        out["seasons"] = seasons
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "rate"


def _unique(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    taken.add(candidate)
    return candidate


def _price(value: Any, gst: bool) -> float:
    if gst:
        return with_gst(value)
    try:
        return round(float(value), 6)
    except (TypeError, ValueError) as err:
        raise CdrImportError(f"not a price: {value!r}") from err


def _rates(rates: list[dict[str, Any]], period: str | None, what: str, gst: bool = True) -> dict[str, Any]:
    """CDR rates (in order of usage volume) -> {'rate': x} or tiers. Volumes are block sizes.

    `gst`: add GST (charges, and business feed-in); residential feed-in is paid as published.
    """
    if not rates:
        raise CdrImportError(f"{what} has no rates")
    for r in rates:
        if (r.get("measureUnit") or "KWH").upper() != "KWH":
            raise CdrImportError(f"{what} is priced per {r.get('measureUnit')}, not per kWh")
    if len(rates) == 1:
        return {"rate": _price(rates[0]["unitPrice"], gst)}
    reset = TIER_RESET.get(period or "P1Y")
    if reset is None:
        raise CdrImportError(f"{what} has stepped rates per {period or 'year'}, which plans can't express")
    tiers, cumulative = [], 0.0
    for r in rates[:-1]:
        if not r.get("volume"):
            raise CdrImportError(f"{what} has a stepped rate without a volume")
        cumulative += float(r["volume"])
        tiers.append({"up_to_kwh": round(cumulative, 6), "rate": _price(r["unitPrice"], gst)})
    tiers.append({"rate": _price(rates[-1]["unitPrice"], gst)})
    return {"tiers": tiers, "tier_reset": reset}


def _covered(windows: list[dict[str, Any]]) -> int:
    total = 0
    for w in windows:
        window = Window.from_dict(w)
        per_day = sum(hi - lo for lo, hi in window.segments())
        total += per_day * len(window.days)
    return total


def _make_default(periods: list[dict[str, Any]]) -> None:
    """The period covering the most time becomes the default (its windows are dropped)."""
    best = max(periods, key=lambda p: _covered(p["windows"]))
    best.pop("windows")
    best["default"] = True
    periods.remove(best)
    periods.append(best)  # default last, like the library files


# ----- seasons -----------------------------------------------------------------


def _months(start: str, end: str) -> list[int] | None:
    """Months of an mm-dd range that follows month boundaries (may wrap the year), else None."""
    try:
        sm, sd = (int(x) for x in start.split("-"))
        em, ed = (int(x) for x in end.split("-"))
    except (AttributeError, ValueError):
        return None
    last_day = calendar.monthrange(2023, em)[1]
    if sd != 1 or (ed != last_day and not (em == 2 and ed == 29)):
        return None
    months, m = [], sm
    while True:
        months.append(m)
        if m == em:
            return months
        m = m % 12 + 1
        if len(months) > 12:
            return None


# ----- conversion --------------------------------------------------------------


def _import_periods(
    usage: list[dict[str, Any]], season_names: list[str | None], holidays: set[str]
) -> list[dict[str, Any]]:
    """Import periods for all usage tariff periods (one per season, or one for the year)."""
    periods: list[dict[str, Any]] = []
    taken: set[str] = set()
    single_rate_periods = []
    for tariff, season in zip(usage, season_names, strict=True):
        seasons = [season] if season else None
        kind = tariff.get("rateBlockUType")
        if kind == "singleRate":
            single = tariff.get("singleRate") or {}
            name = single.get("displayName") or "Anytime"
            period = {"id": _unique(_slug(name if not season else f"{name} {season}"), taken), "name": name,
                      **_rates(single.get("rates") or [], single.get("period"), name)}
            period["windows"] = [{"days": "all", "all_day": True, **({"seasons": seasons} if seasons else {})}]
            single_rate_periods.append(period)
            periods.append(period)
        elif kind == "timeOfUseRates":
            for tou in tariff.get("timeOfUseRates") or []:
                name = tou.get("displayName") or NAME_BY_TYPE.get(tou.get("type"), "Rate")
                windows = []
                for slot in tou.get("timeOfUse") or []:
                    days, has_holidays = _days(slot.get("days"))
                    if has_holidays:
                        holidays.add(tou.get("type") or name)
                    windows.append(_window(days, slot.get("startTime"), slot.get("endTime"), seasons))
                if not windows:
                    raise CdrImportError(f"time of use rate {name!r} has no time windows")
                # Ids come from the plan's own names ("Free"), the CDR type only if unnamed:
                # they key the meters and entity ids, so they should mean something.
                base = tou.get("displayName") or NAME_BY_TYPE.get(tou.get("type")) or "rate"
                periods.append({
                    "id": _unique(_slug(base if not season else f"{base} {season}"), taken),
                    "name": name,
                    **_rates(tou.get("rates") or [], tou.get("period"), name),
                    "windows": windows,
                })
        else:
            raise CdrImportError(f"tariff period type {kind!r} isn't a usage rate")
    if len(periods) == 1 and len(single_rate_periods) == 1:
        only = periods[0]
        only.pop("windows")
        only["default"] = True
        return periods
    _make_default(periods)
    return periods


def _clock(clock: dict[str, Any], dnsp: str, state: str, brand: str) -> tuple[str, str | None]:
    """The plan's time zone on this distributor, and a review note when it's in doubt.

    A retailer known to bill on its own clock (RETAILER_CLOCKS) wins. Otherwise the plan's
    stated zone wins; with none, the network's clock is used. When the plan and the network
    disagree, the plan's word is kept and the note says so (a retailer can apply its own rule
    on top of the network's, so neither is assumed wrong).
    """
    network = "AEST" if dnsp in AEST_NETWORKS else "LOCAL"
    stated = clock["stated"]
    known = RETAILER_CLOCKS.get(brand.strip().lower())
    used = known or stated or network
    timezone = AEST_ZONE if used == "AEST" else STATE_TIMEZONES[state]
    if not clock["timed"] or not observes_dst(state):
        return timezone, None  # no times of day, or no daylight saving: AEST and local agree
    words = {"AEST": "AEST all year (an hour later on the clock in summer)",
             "LOCAL": "local time, following daylight saving"}
    if known:
        if stated and stated != known:
            return timezone, (f"The plan data gives its times in {words[stated]}, but {brand} bills on "
                              f"{words[known]}: that was used.")
        return timezone, None
    if stated is None:
        return timezone, (f"The plan doesn't say which time zone its times are in: {dnsp}'s clock "
                          f"({words[network]}) was used. Check against the plan's fact sheet.")
    if stated != network:
        return timezone, (f"The plan gives its times in {words[stated]}, but {dnsp}'s time-of-use periods "
                          f"follow {words[network]}. The plan's own time zone was used: check against the "
                          "plan's fact sheet.")
    return timezone, None


def _has_windows(usage: list[dict[str, Any]], contract: dict[str, Any]) -> bool:
    """True if any rate depends on the time of day (so the time zone matters)."""
    if any(t.get("rateBlockUType") == "timeOfUseRates" for t in usage):
        return True
    return any(fit.get("timeVaryingTariffs") for fit in contract.get("solarFeedInTariff") or [])


FEED_IN_GST_NOTES = {
    "business": ("Feed-in includes GST, as paid to GST-registered businesses; if you aren't registered for GST, "
                 "your feed-in is the published rate (divide by 1.1)."),
    "": ("Feed-in is the published rate, without GST (households aren't paid GST on feed-in); a GST-registered "
         "business is paid 10% more."),
}


@dataclass
class _FeedInPiece:
    """One retailer feed-in rate for one set of times (and maybe months), from one CDR entry."""

    months: tuple[int, ...] | None  # None: all year
    windows: list[dict[str, Any]] | None  # None: all times
    rates: list[dict[str, Any]]
    period: str | None
    name: str
    text: str  # displayName + description, to settle a cap's reset when the data's period can't
    type_name: str | None = None  # Peak, Off-peak...: used when entries share a name

    @property
    def key(self) -> tuple[Any, ...]:
        return (self.months, json.dumps(self.windows, sort_keys=True))

    @property
    def capped(self) -> bool:
        return len(self.rates) == 1 and bool(self.rates[0].get("volume"))


PER_DAY_TEXT = re.compile(r"(per|each|a)\s+day|/\s*day", re.IGNORECASE)


def _export_periods(
    contract: dict[str, Any], notes: list[str], customer: str, seasons: dict[str, list[int]]
) -> list[dict[str, Any]]:
    """Retailer feed-in, from every retailer feed-in entry.

    Retailers often split one feed-in over several entries (CDS standards-maintenance #520): a
    capped block (`volume`) and the rate after it as separate entries for the same times, other
    times as more entries, and seasons as entries with their own dates. Entries for the same
    times and months are combined into one period (a capped entry + one uncapped entry -> tiers).
    `seasons` (the plan's, from usage) may gain the feed-in's own when usage has none.

    GST: households aren't paid GST on feed-in, so residential (and unstated) plans use the
    published rate; GST-registered businesses are, so business plans add it, as their bill shows.
    """
    gst = customer == "business"
    feed_ins = [
        f for f in contract.get("solarFeedInTariff") or []
        if (f.get("payerType") or "").upper() == "RETAILER" and (f.get("scheme") or "").upper() != "PREMIUM"
    ]
    if not feed_ins:
        notes.append("No retailer feed-in tariff in the plan data: feed-in set to 0.")
        return [{"id": "feed_in", "name": "Feed-in", "rate": 0.0, "default": True}]

    pieces: list[_FeedInPiece] = []
    for fit in feed_ins:
        months = _entry_months(fit)
        text = f"{fit.get('displayName') or ''} {fit.get('description') or ''}"
        if fit.get("tariffUType") == "singleTariff":
            single = fit.get("singleTariff") or {}
            pieces.append(_FeedInPiece(months, None, single.get("rates") or [], single.get("period"),
                                       fit.get("displayName") or "Feed-in", text))
            continue
        variations = fit.get("timeVaryingTariffs") or []
        if not variations:
            raise CdrImportError("time-varying feed-in has no variations")
        for variation in variations:
            windows = []
            for slot in variation.get("timeVariations") or []:
                days, _ = _days(slot.get("days"))
                windows.append(_window(days, slot.get("startTime"), slot.get("endTime"), None))
            type_name = NAME_BY_TYPE.get(variation.get("type"))
            name = variation.get("displayName") or type_name or "Feed-in"
            pieces.append(_FeedInPiece(months, windows or None, variation.get("rates") or [],
                                       variation.get("period"), name, text, type_name))

    if len({p.months for p in pieces}) == 1 and pieces[0].months is not None:
        # Every entry has the same dates: when the rates apply (e.g. until a price review), not seasons
        for piece in pieces:
            piece.months = None
        note = ("Feed-in rates are listed for " + ", ".join(sorted({f"{f.get('startDate')} to {f.get('endDate')}"
                for f in feed_ins})) + " only; used all year.")
        if note not in notes:
            notes.append(note)
    season_of = _feed_in_seasons(pieces, seasons)
    groups: dict[tuple[Any, ...], list[_FeedInPiece]] = {}
    for piece in pieces:
        groups.setdefault(piece.key, []).append(piece)

    periods: list[dict[str, Any]] = []
    taken: set[str] = set()
    type_names: dict[int, str | None] = {}
    for group in groups.values():
        first = next((p for p in group if p.capped), group[0])  # the capped block names a stepped rate
        label = first.name
        rate = _combine(group, gst, label, notes)
        windows = first.windows or [{"days": "all", "all_day": True}]
        if first.months is not None:
            windows = [{**w, "seasons": [season_of[first.months]]} for w in windows]
        period = {"id": "", "name": label, **rate, "windows": windows}
        type_names[id(period)] = first.type_name
        periods.append(period)

    periods = _merge_same_rates(periods, set(seasons))
    names = [p["name"] for p in periods]
    if len(set(names)) < len(names):  # entries share a name: try their types, else say when they apply
        typed = [type_names.get(id(p)) or p["name"] for p in periods]
        for period, name in zip(periods, typed, strict=True):
            period["name"] = name if typed.count(name) == 1 else _describe(period)
    for period in periods:
        period["id"] = _unique(_slug(period["name"]), taken)
    if len(periods) == 1:
        periods[0].pop("windows")
        periods[0]["default"] = True
    else:
        _make_default(periods)
    _feed_in_note(notes, customer)
    return periods


def _entry_months(fit: dict[str, Any]) -> tuple[int, ...] | None:
    """Months an entry applies to, from its dates (yyyy-mm-dd), or None for all year."""
    start, end = fit.get("startDate"), fit.get("endDate")
    if not start or not end:
        return None
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as err:
        raise CdrImportError(f"feed-in dates {start!r} to {end!r} aren't dates") from err
    if (last - first).days >= 364:
        return None  # a year or more: not a season
    months = _months(start[5:], end[5:])
    if months is None:
        raise CdrImportError(f"feed-in dates {start} to {end} don't follow month boundaries")
    return tuple(months)


def _feed_in_seasons(pieces: list[_FeedInPiece], seasons: dict[str, list[int]]) -> dict[tuple[int, ...], str]:
    """Season name for each set of feed-in months: the usage's own seasons, or new ones if usage has none."""
    month_sets = sorted({p.months for p in pieces if p.months is not None})
    if not month_sets:
        return {}
    if sorted(m for ms in month_sets for m in ms) != list(range(1, 13)):
        raise CdrImportError("feed-in seasons don't cover the whole year exactly once")
    out: dict[tuple[int, ...], str] = {}
    if seasons:
        for months in month_sets:
            name = next((n for n, ms in seasons.items() if sorted(ms) == sorted(months)), None)
            if name is None:
                raise CdrImportError("feed-in seasons differ from the usage seasons")
            out[months] = name
        return out
    for months in month_sets:
        name = _unique(f"{MONTH_NAMES[months[0] - 1]}_{MONTH_NAMES[months[-1] - 1]}", set(seasons))
        seasons[name] = list(months)
        out[months] = name
    return out


def _combine(group: list[_FeedInPiece], gst: bool, label: str, notes: list[str]) -> dict[str, Any]:
    """One period's rate from the entries for the same times: a lone entry, or a capped block + the rest."""
    if len(group) == 1:
        piece = group[0]
        if piece.capped:
            raise CdrImportError(f"feed-in {label!r} has a {piece.rates[0]['volume']} kWh limit and no rate after it")
        return _rates(piece.rates, piece.period, label, gst)
    capped = [p for p in group if p.capped]
    rest = [p for p in group if not p.capped]
    if len(capped) != 1 or len(rest) != 1 or len(rest[0].rates) != 1:
        raise CdrImportError(f"feed-in entries for the same times ({label!r}) can't be combined")
    cap, after = capped[0], rest[0]
    reset = TIER_RESET.get(cap.period or "")
    if reset is None:
        if not PER_DAY_TEXT.search(cap.text):
            raise CdrImportError(f"feed-in {label!r} has a limit per {cap.period or 'year'}, which plans can't express")
        reset = "day"
        note = (f"Feed-in {label!r}: the data gives its {cap.rates[0]['volume']} kWh limit per "
                f"{cap.period or 'year'}, but its text says per day: per day was used.")
        if note not in notes:
            notes.append(note)
    return {
        "tiers": [
            {"up_to_kwh": round(float(cap.rates[0]["volume"]), 6), "rate": _price(cap.rates[0]["unitPrice"], gst)},
            {"rate": _price(after.rates[0]["unitPrice"], gst)},
        ],
        "tier_reset": reset,
    }


def _merge_same_rates(periods: list[dict[str, Any]], all_seasons: set[str]) -> list[dict[str, Any]]:
    """Periods with the same rate and times in different seasons become one period; a window
    that ends up in every season needs no seasons."""
    out: list[dict[str, Any]] = []
    keys: list[tuple[str, str]] = []
    for period in periods:
        key = (json.dumps({k: period[k] for k in ("rate", "tiers", "tier_reset") if k in period}),
               json.dumps([{k: v for k, v in w.items() if k != "seasons"} for w in period["windows"]]))
        if key not in keys:
            keys.append(key)
            out.append(period)
            continue
        match = out[keys.index(key)]
        for mine, theirs in zip(match["windows"], period["windows"], strict=True):
            if "seasons" in mine and "seasons" in theirs:
                mine["seasons"] = sorted(set(mine["seasons"]) | set(theirs["seasons"]))
    for period in out:
        for window in period["windows"]:
            if all_seasons and set(window.get("seasons", ())) == all_seasons:
                window.pop("seasons")
    return out


def _describe(period: dict[str, Any]) -> str:
    """A name for a period whose CDR name is shared: its name plus when it applies."""
    window = (period.get("windows") or [{}])[0]
    when = "all day" if window.get("all_day") else f"{window.get('start')}-{window.get('end')}"
    season = f" {'/'.join(window['seasons'])}" if window.get("seasons") else ""
    return f"{period['name']} {when}{season}"


def _feed_in_note(notes: list[str], customer: str) -> None:
    note = FEED_IN_GST_NOTES.get(customer)
    if note and note not in notes:
        notes.append(note)


def _controlled_loads(contract: dict[str, Any], notes: list[str]) -> list[dict[str, Any]]:
    loads = []
    taken: set[str] = set()
    for load in contract.get("controlledLoad") or []:
        name = load.get("displayName") or "Controlled load"
        if load.get("rateBlockUType") != "singleRate":
            notes.append(f"Controlled load {name!r} has time-of-use rates: left out for now.")
            continue
        single = load.get("singleRate") or {}
        supply = with_gst(single["dailySupplyCharge"]) if single.get("dailySupplyCharge") else 0.0
        pid = _unique(_slug(name), taken)
        loads.append({
            "id": pid,
            "name": name,
            "supply_charge": {"rate": supply, "unit": "per_day"},
            "periods": [{"id": "anytime", "name": name,
                         **_rates(single.get("rates") or [], single.get("period"), name), "default": True}],
        })
    return loads


def _extras_note(contract: dict[str, Any]) -> list[str]:
    """Discounts, incentives and fees in words: they change the bill, not the per-kWh price."""
    out = []
    for d in contract.get("discounts") or []:
        method = d.get("methodUType")
        value = (d.get(method) or {}) if method else {}
        amount = value.get("rate") or value.get("amount")
        out.append(f"Discount: {d.get('displayName')} ({d.get('type', '').lower()}, {method} {amount}).")
    for i in contract.get("incentives") or []:
        out.append(f"Incentive: {i.get('displayName')}.")
    fees = [f.get("type") or f.get("displayName") for f in contract.get("fees") or []]
    if fees:
        out.append("Fees: " + ", ".join(str(f) for f in fees) + ".")
    return out


def convert(detail: dict[str, Any], today: date) -> Conversion:
    """Convert one CDR plan detail into ha_tariff_plan dicts (one per known distributor)."""
    data = detail.get("data", detail) if isinstance(detail, dict) else None
    if not isinstance(data, dict) or "planId" not in data:
        raise CdrImportError("not CDR plan detail (no planId)")
    out = Conversion()
    plan_id = data["planId"]
    label = f"{data.get('brandName')} {data.get('displayName')} ({plan_id})"

    if (data.get("fuelType") or "").upper() == "GAS":
        out.skipped.append(f"{label}: gas only")
        return out
    contract = data.get("electricityContract")
    if not isinstance(contract, dict):
        out.skipped.append(f"{label}: no electricity contract")
        return out
    model = (contract.get("pricingModel") or "").upper()
    if model.startswith("FLEXIBLE") or model == "QUOTA":
        out.skipped.append(f"{label}: {model} pricing can't be expressed as fixed rates")
        return out

    try:
        built = _build(data, contract, today, out.notes)
    except CdrImportError as err:
        out.skipped.append(f"{label}: {err}")
        return out

    for raw in (data.get("geography") or {}).get("distributors") or []:
        mapped = map_distributor(raw)
        if mapped is None:
            out.skipped.append(f"{label}: unknown distributor {raw!r}")
            continue
        dnsp, state = mapped
        plan = dict(built)
        plan["region"] = state
        plan["dnsp"] = dnsp
        plan["timezone"], note = _clock(plan.pop("_clock"), dnsp, state, plan.get("retailer") or "")
        if note:
            plan["notes"] = f"{plan['notes']} {note}"
            if note not in out.notes:
                out.notes.append(note)
        out.plans.append(_ordered(plan))
    if not out.plans and not out.skipped:
        out.skipped.append(f"{label}: no distributors")
    return out


def _build(data: dict[str, Any], contract: dict[str, Any], today: date, notes: list[str]) -> dict[str, Any]:
    """Everything but region, DNSP and timezone (set per distributor from `_clock`)."""
    tariffs = contract.get("tariffPeriod") or []
    usage = [t for t in tariffs if t.get("rateBlockUType") in ("singleRate", "timeOfUseRates")]
    demand = [t for t in tariffs if t.get("rateBlockUType") == "demandCharges"]
    if not usage:
        raise CdrImportError("no usage rates")
    if demand:
        notes.append("Demand charges left out for now (not yet converted).")
    for t in usage:
        if (t.get("dailySupplyChargeType") or "SINGLE").upper() != "SINGLE":
            raise CdrImportError("banded daily supply charges can't be expressed")
    supplies = {t.get("dailySupplyCharge") for t in usage}
    if len(supplies) != 1 or None in supplies:
        raise CdrImportError("the daily supply charge differs between tariff periods")

    seasons: dict[str, list[int]] = {}
    season_names: list[str | None] = []
    if len(usage) == 1:
        season_names = [None]
    else:
        for t in usage:
            months = _months(t.get("startDate", ""), t.get("endDate", ""))
            if months is None:
                raise CdrImportError(
                    f"tariff period {t.get('displayName')!r} ({t.get('startDate')} to {t.get('endDate')}) "
                    "doesn't follow month boundaries"
                )
            name = _unique(_slug(t.get("displayName") or "season"), set(seasons))
            seasons[name] = months
            season_names.append(name)
        if sorted(m for ms in seasons.values() for m in ms) != list(range(1, 13)):
            raise CdrImportError("the tariff periods don't cover the whole year exactly once")

    stated = {(t.get("timeZone") or contract.get("timeZone") or "").upper() for t in usage}
    if len(stated) != 1:
        raise CdrImportError("tariff periods use different time zones")
    # The zone is settled per distributor in convert(): the plan's word, else the network's clock
    clock = {"stated": next(iter(stated)) or None, "timed": _has_windows(usage, contract)}

    holiday_rates: set[str] = set()
    import_periods = _import_periods(usage, season_names, holiday_rates)
    export_periods = _export_periods(contract, notes, (data.get("customerType") or "").lower(), seasons)
    controlled = _controlled_loads(contract, notes)
    if holiday_rates:
        notes.append(
            "Public holidays are listed for " + ", ".join(sorted(holiday_rates))
            + "; holidays are treated as Sundays. Check against the plan's fact sheet."
        )
    extras = _extras_note(contract)

    effective_from = (data.get("effectiveFrom") or "")[:10] or today.isoformat()
    effective_to = (data.get("effectiveTo") or "")[:10] or None
    plan: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "country": "AU",
        "retailer": data.get("brandName") or data.get("brand"),
        "plan": data.get("displayName") or data["planId"],
        "plan_id": data["planId"],
        "effective_from": effective_from,
        "effective_to": effective_to if effective_to and effective_to >= effective_from else None,
        "source": {"type": "EME", "url": data.get("applicationUri") or SOURCE_URL, "checked": today.isoformat()},
        "currency": "AUD",
        "timezone": None,  # set per distributor (_clock)
        "holidays": {"treat_as": "sunday" if holiday_rates else "none"},
        "supply_charge": {"rate": with_gst(next(iter(supplies))), "unit": "per_day"},
        "import": {"periods": import_periods},
        "export": {"periods": export_periods},
        "demand": [],
        "controlled_load": controlled,
        "needs_review": True,
        "notes": " ".join(
            [f"Converted from Energy Made Easy (CDR) plan {data['planId']}; charges include GST (CDR ex GST x1.1)."]
            + notes + extras
        ),
    }
    plan["_clock"] = clock
    if seasons:
        plan["seasons"] = seasons
    customer = (data.get("customerType") or "").lower()
    if customer in ("residential", "business"):
        plan["customer_type"] = customer
    return plan


TOP_ORDER = (
    "format", "version", "country", "region", "dnsp", "retailer", "plan", "plan_id", "customer_type",
    "effective_from", "effective_to", "source", "currency", "timezone", "holidays", "seasons",
    "supply_charge", "import", "export", "demand", "controlled_load", "needs_review", "notes",
)


def _ordered(plan: dict[str, Any]) -> dict[str, Any]:
    return {k: plan[k] for k in TOP_ORDER if k in plan}


__all__ = ["CdrImportError", "Conversion", "DISTRIBUTORS", "convert", "map_distributor", "with_gst"]
