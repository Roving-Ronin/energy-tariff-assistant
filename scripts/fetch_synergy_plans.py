#!/usr/bin/env python3
"""Synergy (WA) plans from Synergy's own plan pages, and DEBS feed-in from its pricing schedule.

WA isn't on Energy Made Easy. Synergy's four plans on the Western Power network have one
"Item / Price inc. GST" table each; this reads **only those tables** (descriptions are ignored)
and the times stated in words ("Peak (3pm to 9pm)"), checks them against what each plan
expects, and writes library plans. Anything unexpected (a missing or new rate label, changed
times, a number that won't parse) stops the run with the reason: nothing is guessed.

New prices with a new effective date become a new plan file; the previous file gets an
`effective_to` the day before. Period ids never change, so people's meters carry on.

    python scripts/fetch_synergy_plans.py            # fetch, write changed plans
    python scripts/fetch_synergy_plans.py --dry-run  # fetch and check only
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import sys
import urllib.error
from typing import Any
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_eme_retailers as eme  # noqa: E402  (USER_AGENT; also puts the engine on sys.path)
from _common import PLANS_DIR, dump_json, plan_files, plan_relpath  # noqa: E402
from engine import validate_plan  # noqa: E402

BASE = "https://www.synergy.net.au/Your-home/Energy-plans"
DEBS_URL = "https://www.synergy.net.au/-/media/Files/PDF-Library/DEBS_Pricing_schedule.pdf"
GOV_PRICES_URL = "https://www.wa.gov.au/organisation/energy-policy-wa/household-electricity-pricing"


class SynergyError(ValueError):
    """The page isn't what this script expects: change nothing."""


@dataclass
class Rate:
    period: str  # period id (stable: keys people's meters)
    name: str
    windows: list[dict[str, Any]] | None = None  # None: the default period
    up_to_kwh: float | None = None  # a daily step of a stepped rate


@dataclass
class PlanSpec:
    url: str
    plan: str
    plan_id: str | None
    customer_type: str
    rates: dict[str, Rate]  # table label -> rate
    stated_times: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # "Peak" -> [("3pm","9pm")]
    debs: bool = True  # eligible for DEBS feed-in

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", self.plan)


def _w(start: str, end: str) -> dict[str, Any]:
    return {"days": "all", "start": start, "end": end}


SUPPLY = "Supply charge"
PLANS = [
    PlanSpec(
        f"{BASE}/Home-Plan-A1", "Home Plan (A1)", "A1", "residential",
        {"Electricity charge": Rate("anytime", "Anytime")},
    ),
    PlanSpec(
        f"{BASE}/Midday-Saver", "Midday Saver", None, "residential",
        {
            "Everyday Super Off Peak electricity charge": Rate("super_off_peak", "Super Off Peak", [_w("09:00", "15:00")]),
            "Peak electricity charge": Rate("peak", "Peak", [_w("15:00", "21:00")]),
            "Everyday Off-peak electricity charge": Rate("off_peak", "Off Peak"),
        },
        {"Super Off Peak": [("9am", "3pm")], "Peak": [("3pm", "9pm")], "Off Peak": [("9pm", "9am")]},
    ),
    PlanSpec(
        f"{BASE}/Electric-Vehicle-Add-On", "EV Add On", None, "residential",
        {
            "Super Off Peak electricity charge": Rate("super_off_peak", "Super Off Peak", [_w("09:00", "15:00")]),
            "Standard home electricity charge": Rate("peak", "Peak", [_w("15:00", "21:00")]),
            "Overnight electricity charge": Rate("overnight", "Overnight", [_w("23:00", "06:00")]),
            "Everyday EV off peak electricity charge": Rate("off_peak", "Off Peak"),
        },
        {"Overnight": [("11pm", "6am")], "Peak": [("3pm", "9pm")], "Super Off Peak": [("9am", "3pm")],
         "Off Peak": [("6am", "9am"), ("9pm", "11pm")]},
    ),
    PlanSpec(
        f"{BASE}/Home-Business-Plan-K1-Resi", "Home Business Plan (K1)", "K1", "business",
        {
            "Electricity charge - first 20 units per day": Rate("anytime", "Anytime", up_to_kwh=20),
            "Electricity charge - next 20 - 1,650 units per day": Rate("anytime", "Anytime", up_to_kwh=1650),
            "Electricity charge - more than 1,650 units per day": Rate("anytime", "Anytime"),
        },
        debs=False,
    ),
]


# ----- reading pages -----------------------------------------------------------


class _Text(HTMLParser):
    BLOCKS = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "br", "section", "table", "span"}

    def __init__(self, blocks: set[str] | None = None) -> None:
        super().__init__()
        self.blocks = self.BLOCKS if blocks is None else blocks
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        if tag in self.blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self.skip -= 1
        if tag in self.blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def page_lines(html: str, blocks: set[str] | None = None) -> list[str]:
    """The page's visible text, one line per block element (`blocks`: which tags start a line)."""
    parser = _Text(blocks)
    parser.feed(html)
    text = "".join(parser.parts).replace("\xa0", " ")
    return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]


PRICE = re.compile(r"^([\d,]+(?:\.\d+)?) cents per (day|unit)$")


def price_table(lines: list[str]) -> dict[str, tuple[float, str]]:
    """The first 'Item / Price inc. GST' table: label -> (cents, 'day' or 'unit')."""
    for i in range(len(lines) - 1):
        if lines[i] == "Item" and lines[i + 1] == "Price inc. GST":
            rows: dict[str, tuple[float, str]] = {}
            j = i + 2
            while j + 1 < len(lines) and (match := PRICE.match(lines[j + 1])):
                if lines[j] in rows:
                    raise SynergyError(f"rate {lines[j]!r} appears twice in the price table")
                rows[lines[j]] = (float(match.group(1).replace(",", "")), match.group(2))
                j += 2
            if not rows:
                raise SynergyError("the price table has no rates")
            return rows
    raise SynergyError("no 'Item / Price inc. GST' price table")


EFFECTIVE = re.compile(r"(?:effective(?: at)?|correct as at) (\d{1,2} [A-Z][a-z]+ \d{4})", re.IGNORECASE)


def effective_date(lines: list[str]) -> str:
    found = {datetime.strptime(m.group(1), "%d %B %Y").date().isoformat()
             for line in lines for m in EFFECTIVE.finditer(line)}
    if len(found) != 1:
        raise SynergyError(f"expected one effective date on the page, found {sorted(found) or 'none'}")
    return found.pop()


TIME = r"(\d{1,2}(?::\d{2})?[ap]m)"
STATED = re.compile(rf"^(.+?) \({TIME} to {TIME}(?: / {TIME} to {TIME})?\)$")


def stated_times(lines: list[str]) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for line in lines:
        if match := STATED.match(line):
            name, *times = match.groups()
            pairs = [(times[k], times[k + 1]) for k in (0, 2) if times[k]]
            out.setdefault(name.strip(), pairs)
    return out


# ----- building plans -----------------------------------------------------------


def build_plan(spec: PlanSpec, lines: list[str], debs: dict[str, Any], today: date) -> dict[str, Any]:
    table = price_table(lines)
    expected = {SUPPLY, *spec.rates}
    if set(table) != expected:
        missing, extra = sorted(expected - set(table)), sorted(set(table) - expected)
        raise SynergyError(f"{spec.plan}: price table labels changed (missing {missing}, new {extra})")
    times = stated_times(lines)
    for name, windows in spec.stated_times.items():
        if times.get(name) != windows:
            raise SynergyError(f"{spec.plan}: {name} times changed: page says {times.get(name)}, expected {windows}")
    supply, unit = table[SUPPLY]
    if unit != "day":
        raise SynergyError(f"{spec.plan}: supply charge isn't per day")

    periods: dict[str, dict[str, Any]] = {}
    for label, rate in spec.rates.items():
        cents, unit = table[label]
        if unit != "unit":
            raise SynergyError(f"{spec.plan}: {label!r} isn't per unit")
        dollars = round(cents / 100, 6)
        period = periods.setdefault(rate.period, {"id": rate.period, "name": rate.name})
        if rate.up_to_kwh is not None or "tiers" in period:
            period.setdefault("tiers", []).append(
                {"up_to_kwh": rate.up_to_kwh, "rate": dollars} if rate.up_to_kwh is not None else {"rate": dollars})
            period["tier_reset"] = "day"
        else:
            period["rate"] = dollars
        if rate.windows:
            period["windows"] = rate.windows
        else:
            period["default"] = True
    import_periods = sorted(periods.values(), key=lambda p: p.get("default", False))  # default last

    if spec.debs:
        export = [
            {"id": "peak", "name": "DEBS Peak", "rate": debs["peak"], "windows": [_w("15:00", "21:00")]},
            {"id": "off_peak", "name": "DEBS Off Peak", "rate": debs["off_peak"], "default": True},
        ]
        export_note = (f"Feed-in is the Distributed Energy Buyback Scheme (DEBS), from Synergy's DEBS pricing "
                       f"schedule (correct as at {debs['effective']}).")
    else:
        export = [{"id": "feed_in", "name": "Feed-in", "rate": 0.0, "default": True}]
        export_note = "Not eligible for DEBS feed-in (stated on the plan's page): feed-in set to 0."

    return {
        "format": "ha_tariff_plan",
        "version": 1,
        "country": "AU",
        "region": "WA",
        "dnsp": "Western Power",
        "retailer": "Synergy",
        "plan": spec.plan,
        "plan_id": spec.plan_id,
        "customer_type": spec.customer_type,
        "effective_from": effective_date(lines),
        "effective_to": None,
        "source": {"type": "retailer_factsheet", "url": spec.url, "checked": today.isoformat()},
        "currency": "AUD",
        "timezone": "Australia/Perth",
        "holidays": {"treat_as": "none"},
        "supply_charge": {"rate": round(supply / 100, 6), "unit": "per_day"},
        "import": {"periods": import_periods},
        "export": {"periods": export},
        "demand": [],
        "controlled_load": [],
        "needs_review": True,
        "notes": f"From Synergy's price table at {spec.url} (prices include GST). {export_note}",
    }


DEBS_ROW = {
    "off_peak": re.compile(r"Off[ -]?Peak\s*(?:[-–:]\s*)?Before 3pm or after 9pm\s*(?:[-–:]\s*)?([\d.]+)", re.I),
    "peak": re.compile(r"(?<!Off )(?<!Off-)\bPeak\s*(?:[-–:]\s*)?Between 3pm and 9pm\s*(?:[-–:]\s*)?([\d.]+)", re.I),
}


DEBS_DATE = re.compile(r"correct as at \[?(\d{1,2} [A-Z][a-z]+ \d{4})\]?", re.IGNORECASE)


def debs_rates(text: str) -> dict[str, Any]:
    """DEBS peak and off-peak (dollars, GST-free for households) and their date, from the
    schedule's **Residential customers** table only (the other table is for not-for-profits)."""
    flat = re.sub(r"\s+", " ", text)
    start = flat.rfind("Residential customers")
    if start < 0:
        raise SynergyError("the DEBS schedule has no 'Residential customers' table")
    section = flat[start:]
    out: dict[str, Any] = {}
    for key, pattern in DEBS_ROW.items():
        values = {float(m) for m in pattern.findall(section)}
        if len(values) != 1:
            raise SynergyError(f"DEBS {key}: expected one rate, found {sorted(values) or 'none'}")
        out[key] = round(values.pop() / 100, 6)
    dates = {datetime.strptime(m, "%d %B %Y").date().isoformat() for m in DEBS_DATE.findall(section)}
    if len(dates) != 1:
        raise SynergyError(f"DEBS: expected one 'correct as at' date, found {sorted(dates) or 'none'}")
    out["effective"] = dates.pop()
    return out


# ----- the library --------------------------------------------------------------


def comparable(plan: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in plan.items() if k not in ("source", "effective_to")}


def place(plan: dict[str, Any], spec: PlanSpec, write: bool) -> str:
    """Write the plan if it's new or changed. Returns what happened."""
    return place_plan(plan, spec.slug, write)


def place_plan(plan: dict[str, Any], plan_slug: str, write: bool) -> str:
    """Write a WA plan if it's new or changed (same retailer, plan name and DNSP = the same plan)."""
    def same(path: Path) -> bool:
        other = json.loads(path.read_text(encoding="utf-8"))
        return all(other.get(k) == plan[k] for k in ("retailer", "plan", "dnsp"))

    mine = sorted((p for p in plan_files() if p.parent.name == "WA" and same(p)), key=lambda p: p.name)
    target = PLANS_DIR / plan_relpath(plan, plan_slug)
    latest = mine[-1] if mine else None
    if latest is not None:
        old = json.loads(latest.read_text(encoding="utf-8"))
        if comparable(old) == comparable(plan):
            return "unchanged"
        if old["effective_from"] > plan["effective_from"]:
            raise SynergyError(f"{plan['plan']}: the page's prices are older ({plan['effective_from']}) than the library's")
        if old["effective_from"] < plan["effective_from"]:
            old["effective_to"] = (date.fromisoformat(plan["effective_from"]) - timedelta(days=1)).isoformat()
            if write:
                latest.write_text(dump_json(old), encoding="utf-8")
            action = f"new prices from {plan['effective_from']}"
        else:
            action = "corrected (same effective date)"
    else:
        action = "added"
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_json(plan), encoding="utf-8")
    return action


# ----- running ------------------------------------------------------------------


def http_get(url: str) -> bytes:
    if not url.startswith("https://"):
        raise SynergyError(f"refusing a non-https url: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": eme.USER_AGENT})  # noqa: S310 (https only)
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (https only, checked above)
        return response.read()


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader  # only needed on the runner

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def run(get, today: date, write: bool = True, log=print) -> list[str]:
    debs = debs_rates(pdf_text(get(DEBS_URL)))
    log(f"DEBS: peak {debs['peak']}, off-peak {debs['off_peak']}")
    changes = []
    plans = []
    for spec in PLANS:
        lines = page_lines(get(spec.url).decode("utf-8", "replace"))
        plan = build_plan(spec, lines, debs, today)
        result = validate_plan(plan, today=today)
        if not result.ok:
            raise SynergyError(f"{spec.plan}: invalid plan: {'; '.join(result.errors)}")
        plans.append((spec, plan))
    for spec, plan in plans:  # only after every plan read cleanly
        action = place(plan, spec, write)
        log(f"{plan['plan']}: {action}")
        if action != "unchanged":
            changes.append(f"{plan['plan']}: {action}")
    try:
        gov = " ".join(page_lines(get(GOV_PRICES_URL).decode("utf-8", "replace")))
        a1 = next(p for s, p in plans if s.plan_id == "A1")
        figures = [f"{a1['supply_charge']['rate'] * 100:.4f}", f"{a1['import']['periods'][0]['rate'] * 100:.4f}"]
        missing = [f for f in figures if f not in gov]
        if missing:
            log(f"::warning::WA Government pricing page doesn't show A1 figure(s) {missing}: check A1 by hand")
    except Exception as err:  # noqa: BLE001  (a cross-check only)
        log(f"::warning::couldn't cross-check A1 against the WA Government page: {err}")
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
    except urllib.error.HTTPError as err:  # a refused download: say which and why, not a traceback
        print(f"::error::{err.url} answered HTTP {err.code} {err.reason}: nothing was changed")
        return 1
    except OSError as err:
        print(f"::error::download failed ({err}): nothing was changed")
        return 1
    text = ("Synergy plans updated from Synergy's price tables:\n\n" + "\n".join(f"- {c}" for c in changes) + "\n"
            if changes else "No changes.\n")
    print(text)
    if args.summary:
        args.summary.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
