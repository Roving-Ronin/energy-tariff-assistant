#!/usr/bin/env python3
"""Horizon Power (regional WA) plans from Horizon Power's pricing page.

Prices are the WA Government's uniform tariffs (A2 = Synergy's A1, K2 = Synergy's K1), but
feed-in depends on the town: DEBS (systems up to 5 kW) and Buyback Bonus (over 5 kW, Community
Wave members) are priced per town. So each town gets its own A2 plan for each buyback scheme,
plus one K2 plan (not eligible for feed-in). REBS (closed to new customers) and the other
tariffs on the page are left out.

Only the page's tables are read. Every label, column heading and town row is checked; anything
unexpected stops the run with the reason and nothing is written.

    python scripts/fetch_horizon_plans.py            # fetch, write changed plans
    python scripts/fetch_horizon_plans.py --dry-run  # fetch and check only
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_synergy_plans import SynergyError, _Text, _w, http_get, page_lines, place_plan  # noqa: E402

from engine import validate_plan  # noqa: E402

URL = "https://www.horizonpower.com.au/utilities/pricing/"
BLOCKS = _Text.BLOCKS - {"span"}  # Horizon's labels have spans inside them ("Supply charge (<span>per day</span>)")
SUMMER = [11, 12, 1, 2, 3]  # "Summer months (1 Nov to 31 March)"
WINTER = [4, 5, 6, 7, 8, 9, 10]


class HorizonError(SynergyError):
    """The page isn't what this script expects: change nothing."""


def clean(line: str) -> str:
    """Drop info icons and other non-ASCII marks around labels and values."""
    return re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7E]", " ", line)).strip()


NUMBER = re.compile(r"^\$?(\d+(?:\.\d+)?)$")


def number(line: str, what: str) -> float:
    if not (match := NUMBER.match(clean(line))):
        raise HorizonError(f"{what}: expected a number, found {line!r}")
    return float(match.group(1))


def after(lines: list[str], heading: str, start: int = 0) -> int:
    """Index just after the first line equal to `heading` (at or after `start`)."""
    for i in range(start, len(lines)):
        if clean(lines[i]) == heading:
            return i + 1
    raise HorizonError(f"no {heading!r} section")


def rate_block(lines: list[str], heading: str, labels: list[str]) -> list[float]:
    """The values under `heading`, label by label, in order: each label line, then its value."""
    i = after(lines, heading)
    for _ in range(2):  # a description line may come between the heading and the first label
        if clean(lines[i]) == labels[0]:
            break
        i += 1
    values = []
    for label in labels:
        if clean(lines[i]) != label:
            raise HorizonError(f"{heading}: expected {label!r}, found {clean(lines[i])!r}")
        values.append(number(lines[i + 1], f"{heading} {label}"))
        i += 2
    return values


def town_table(lines: list[str], heading: str, header: list[str], columns: int) -> dict[str, list[float]]:
    """Town rows (town, then `columns` numbers) after `heading`'s column header, up to the prices note."""
    i = after(lines, heading)
    while i < len(lines) and clean(lines[i]) != header[0]:
        i += 1
    if [clean(x) for x in lines[i:i + len(header)]] != header:
        raise HorizonError(f"{heading}: the table's column headings changed")
    i += len(header)
    towns: dict[str, list[float]] = {}
    while not clean(lines[i]).startswith("All prices are correct as at"):
        town = clean(lines[i])
        if not town or NUMBER.match(town):
            raise HorizonError(f"{heading}: expected a town name, found {lines[i]!r}")
        if town in towns:
            raise HorizonError(f"{heading}: {town} appears twice")
        towns[town] = [number(x, f"{heading} {town}") for x in lines[i + 1:i + 1 + columns]]
        i += 1 + columns
    if len(towns) < 10:
        raise HorizonError(f"{heading}: only {len(towns)} towns found")
    return towns


EFFECTIVE = re.compile(r"correct as at (\d{1,2} [A-Z][a-z]+ \d{4})")


def effective_date(lines: list[str]) -> str:
    found = {datetime.strptime(m, "%d %B %Y").date().isoformat() for line in lines for m in EFFECTIVE.findall(line)}
    if len(found) != 1:
        raise HorizonError(f"expected one 'correct as at' date, found {sorted(found) or 'none'}")
    return found.pop()


def read_page(lines: list[str]) -> dict[str, Any]:
    lines = [c for c in (clean(line) for line in lines) if c]  # icon-only lines drop out
    a2 = rate_block(lines, "A2 tariff rates", [
        "Supply charge (per day)", "Supply charge (additional dwelling) (per day)", "Electricity unit charge (cents/kWh)"])
    k2 = rate_block(lines, "K2 Combined Use Tariff", [
        "Supply charge (per day)", "0-20 kWh/day (cents/kWh)", "20-1,650 kWh/day (cents/kWh)", ">1,650 kWh/day (cents/kWh)"])
    debs = town_table(lines, "Distributed Energy Buyback Scheme (DEBS)",
                      ["Before 3pm", "cents/kWh", "Between 3pm and 9pm", "cents/kWh"], 2)
    bonus = town_table(lines, "Buyback Bonus", [
        "Summer months (1 Nov to 31 March)", "cents/kWh", "Winter months (1 April to 31 October)", "cents/kWh",
        "Off-peak", "Peak", "Off-peak", "Peak"], 4)
    if set(debs) != set(bonus):
        raise HorizonError(f"DEBS and Buyback Bonus list different towns: {sorted(set(debs) ^ set(bonus))}")
    return {"a2": a2, "k2": k2, "debs": debs, "bonus": bonus, "effective": effective_date(lines)}


# ----- plans --------------------------------------------------------------------


def base_plan(name: str, plan_id: str, customer: str, effective: str, today: date) -> dict[str, Any]:
    return {
        "format": "ha_tariff_plan", "version": 1, "country": "AU", "region": "WA", "dnsp": "Horizon Power",
        "retailer": "Horizon Power", "plan": name, "plan_id": plan_id, "customer_type": customer,
        "effective_from": effective, "effective_to": None,
        "source": {"type": "retailer_factsheet", "url": URL, "checked": today.isoformat()},
        "currency": "AUD", "timezone": "Australia/Perth", "holidays": {"treat_as": "none"},
    }


def cents(value: float) -> float:
    return round(value / 100, 6)


def a2_import(page: dict[str, Any]) -> dict[str, Any]:
    supply, _additional, unit = page["a2"]
    return {"supply_charge": {"rate": round(supply, 6), "unit": "per_day"},
            "import": {"periods": [{"id": "anytime", "name": "Anytime", "rate": cents(unit), "default": True}]}}


def feed_in(off_peak: float, peak: float) -> list[dict[str, Any]]:
    return [{"id": "peak", "name": "Peak", "rate": cents(peak), "windows": [_w("15:00", "21:00")]},
            {"id": "off_peak", "name": "Off Peak", "rate": cents(off_peak), "default": True}]


def bonus_feed_in(rates: list[float]) -> tuple[list[dict[str, Any]], dict[str, list[int]]]:
    """Buyback Bonus: always the same four seasonal periods, whatever this year's rates, so the ids
    (and the meters) don't change when a town's summer and winter rates start or stop differing."""
    s_off, s_peak, w_off, w_peak = rates
    periods = [
        {"id": "peak_nov_mar", "name": "Peak (Nov-Mar)", "rate": cents(s_peak),
         "windows": [{**_w("15:00", "21:00"), "seasons": ["nov_mar"]}]},
        {"id": "peak_apr_oct", "name": "Peak (Apr-Oct)", "rate": cents(w_peak),
         "windows": [{**_w("15:00", "21:00"), "seasons": ["apr_oct"]}]},
        {"id": "off_peak_nov_mar", "name": "Off Peak (Nov-Mar)", "rate": cents(s_off),
         "windows": [{**_w("21:00", "15:00"), "seasons": ["nov_mar"]}]},
        {"id": "off_peak_apr_oct", "name": "Off Peak (Apr-Oct)", "rate": cents(w_off), "default": True},
    ]
    return periods, {"nov_mar": SUMMER, "apr_oct": WINTER}


def town_slug(town: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", town)


def build_plans(page: dict[str, Any], today: date) -> list[tuple[dict[str, Any], str]]:
    """(plan, file slug) for every plan the page gives."""
    effective = page["effective"]
    tail = {"demand": [], "controlled_load": [], "needs_review": True}
    out = []
    for town in sorted(page["debs"]):
        off_peak, peak = page["debs"][town]
        plan = {**base_plan(f"A2 Residential ({town})", "A2", "residential", effective, today), **a2_import(page),
                "export": {"periods": feed_in(off_peak, peak)}, **tail,
                "notes": (f"From Horizon Power's pricing page (prices include GST). Feed-in: DEBS for {town}, for "
                          "systems up to 5 kW (GST-free): peak 3pm-9pm, off-peak 9pm-3pm.")}
        out.append((plan, f"A2Residential{town_slug(town)}"))
        periods, seasons = bonus_feed_in(page["bonus"][town])
        plan = {**base_plan(f"A2 Residential Buyback Bonus ({town})", "A2", "residential", effective, today),
                "seasons": seasons, **a2_import(page),
                "export": {"periods": periods}, **tail,
                "notes": (f"From Horizon Power's pricing page (prices include GST). Feed-in: Buyback Bonus for {town}, "
                          "for systems over 5 kW and Community Wave members only (GST-free): peak 3pm-9pm, "
                          "summer 1 Nov-31 Mar, winter 1 Apr-31 Oct.")}
        out.append((plan, f"A2BuybackBonus{town_slug(town)}"))
    supply, first, middle, last = page["k2"]
    k2 = {**base_plan("K2 Combined Use", "K2", "business", effective, today),
          "supply_charge": {"rate": round(supply, 6), "unit": "per_day"},
          "import": {"periods": [{"id": "anytime", "name": "Anytime", "default": True, "tier_reset": "day", "tiers": [
              {"up_to_kwh": 20, "rate": cents(first)}, {"up_to_kwh": 1650, "rate": cents(middle)}, {"rate": cents(last)}]}]},
          "export": {"periods": [{"id": "feed_in", "name": "Feed-in", "rate": 0.0, "default": True}]}, **tail,
          "notes": "From Horizon Power's pricing page (prices include GST). Not eligible for DEBS feed-in: set to 0."}
    out.append((k2, "K2CombinedUse"))
    return out


def run(get, today: date, write: bool = True, log=print) -> list[str]:
    page = read_page(page_lines(get(URL).decode("utf-8", "replace"), BLOCKS))
    log(f"Horizon Power: {len(page['debs'])} towns, prices from {page['effective']}")
    plans = build_plans(page, today)
    for plan, _ in plans:
        result = validate_plan(plan, today=today)
        if not result.ok:
            raise HorizonError(f"{plan['plan']}: invalid plan: {'; '.join(result.errors)}")
    changes = []
    for plan, plan_slug in plans:  # only after every plan read and validated
        action = place_plan(plan, plan_slug, write)
        if action != "unchanged":
            changes.append(f"{plan['plan']}: {action}")
    log(f"{len(changes)} plan(s) added or changed")
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    try:
        changes = run(http_get, date.today(), write=not args.dry_run)
    except SynergyError as err:
        print(f"::error::{err}")
        return 1
    text = ("Horizon Power plans updated from its pricing page:\n\n" + "\n".join(f"- {c}" for c in changes) + "\n"
            if changes else "No changes.\n")
    print(text)
    if args.summary:
        args.summary.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
