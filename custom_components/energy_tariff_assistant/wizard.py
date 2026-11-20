"""Build a plan from the answers of the step-by-step wizard, and filter the library.

Pure Python (no Home Assistant imports), so the plan assembly and the
library filtering can be tested directly. The config flow collects the
answers; `assemble_plan` turns them into an `ha_tariff_plan` dict that the
normal validator then checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Any

COUNTRIES = ("AU",)
DEFAULT_COUNTRY = "AU"
DEFAULT_REGION = {"AU": "NSW"}

REGIONS = {"AU": ("NSW", "ACT", "VIC", "QLD", "SA", "TAS", "WA", "NT")}

TIMEZONES = {
    ("AU", "NSW"): "Australia/Sydney",
    ("AU", "ACT"): "Australia/Sydney",
    ("AU", "VIC"): "Australia/Melbourne",
    ("AU", "QLD"): "Australia/Brisbane",
    ("AU", "SA"): "Australia/Adelaide",
    ("AU", "TAS"): "Australia/Hobart",
    ("AU", "WA"): "Australia/Perth",
    ("AU", "NT"): "Australia/Darwin",
}
CURRENCIES = {"AU": "AUD"}

# Distribution network service providers by state (suggestions; any name can be typed)
DNSPS = {
    ("AU", "NSW"): ("Ausgrid", "Endeavour Energy", "Essential Energy"),
    ("AU", "ACT"): ("Evoenergy",),
    ("AU", "VIC"): ("AusNet Services", "CitiPower", "Jemena", "Powercor", "United Energy"),
    ("AU", "QLD"): ("Energex", "Ergon Energy"),
    ("AU", "SA"): ("SA Power Networks",),
    ("AU", "TAS"): ("TasNetworks",),
    ("AU", "WA"): ("Western Power", "Horizon Power"),
    ("AU", "NT"): ("Power and Water Corporation",),
}

DAY_CHOICES = ("all", "weekdays", "weekends", "custom")
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TIER_RESETS = ("day", "month", "bill")
AFTER_CAP_RATE = "__rate__"


# ----- wizard answers ----------------------------------------------------------


@dataclass
class DraftPeriod:
    """One period as entered. `windows` is empty for the default period."""

    name: str
    rate: float
    default: bool = False
    windows: list[dict[str, Any]] = field(default_factory=list)
    cap_kwh: float | None = None
    after_cap: str | None = None  # a period id, or AFTER_CAP_RATE
    rate_after_cap: float | None = None
    cap_reset: str = "day"
    id: str = ""


@dataclass
class DraftStream:
    periods: list[DraftPeriod] = field(default_factory=list)

    def add(self, period: DraftPeriod) -> DraftPeriod:
        period.id = unique_id(period.name, {p.id for p in self.periods})
        self.periods.append(period)
        return period

    def choices(self) -> list[tuple[str, str]]:
        """(id, name) of the periods entered so far, for 'after the cap' choices."""
        return [(p.id, p.name) for p in self.periods]


@dataclass
class Draft:
    country: str = DEFAULT_COUNTRY
    region: str = "NSW"
    dnsp: str = ""
    retailer: str = ""
    plan: str = ""
    plan_id: str = ""
    effective_from: str = ""
    supply_charge: float = 0.0
    streams: dict[str, DraftStream] = field(
        default_factory=lambda: {"import": DraftStream(), "export": DraftStream()}
    )


def slug_id(name: str) -> str:
    """Period id from a name: lowercase letters, digits and underscores."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "period"


def unique_id(name: str, taken: set[str]) -> str:
    base = slug_id(name)
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def window(days: str, custom_days: list[str] | None, start: str, end: str) -> dict[str, Any]:
    """A plan window from the wizard's fields. Times may come as HH:MM:SS."""
    value: Any = days
    if days == "custom":
        value = [d for d in DAY_NAMES if d in (custom_days or [])]
    return {"days": value, "start": start[:5], "end": end[:5]}


def _period_dict(period: DraftPeriod) -> dict[str, Any]:
    out: dict[str, Any] = {"id": period.id, "name": period.name}
    if period.cap_kwh:
        follow: dict[str, Any]
        if period.after_cap and period.after_cap != AFTER_CAP_RATE:
            follow = {"use_period": period.after_cap}
        else:
            follow = {"rate": period.rate_after_cap if period.rate_after_cap is not None else 0.0}
        out["tiers"] = [{"up_to_kwh": period.cap_kwh, "rate": period.rate}, follow]
        out["tier_reset"] = period.cap_reset
    else:
        out["rate"] = period.rate
    if period.default:
        out["default"] = True
    else:
        out["windows"] = period.windows
    return out


def assemble_plan(draft: Draft, today: date) -> dict[str, Any]:
    """The ha_tariff_plan dict for the answers. Validate it before use."""
    plan: dict[str, Any] = {
        "format": "ha_tariff_plan",
        "version": 1,
        "country": draft.country,
        "region": draft.region,
        "retailer": draft.retailer.strip(),
        "plan": draft.plan.strip(),
        "effective_from": draft.effective_from or today.isoformat(),
        "effective_to": None,
        "source": {"type": "manual", "url": None, "checked": today.isoformat()},
        "currency": CURRENCIES.get(draft.country, "AUD"),
        "timezone": TIMEZONES.get((draft.country, draft.region), "Australia/Sydney"),
        "holidays": {"treat_as": "none"},
        "supply_charge": {"rate": draft.supply_charge, "unit": "per_day"},
    }
    if draft.dnsp.strip():
        plan["dnsp"] = draft.dnsp.strip()
    if draft.plan_id.strip():
        plan["plan_id"] = draft.plan_id.strip()
    for key, stream in draft.streams.items():
        # Default period last, like the library files
        ordered = [p for p in stream.periods if not p.default] + [p for p in stream.periods if p.default]
        plan[key] = {"periods": [_period_dict(p) for p in ordered]}
    plan["demand"] = []
    plan["controlled_load"] = []
    plan["notes"] = "Built with the Energy Tariff Assistant wizard."
    return plan


def plan_filename(plan: dict[str, Any]) -> str:
    """Library-style file name: Retailer-PlanSlug-Region-DNSP-YYYYMMDD.json."""

    def camel(text: str) -> str:
        return "".join(w[:1].upper() + w[1:] for w in re.split(r"[^A-Za-z0-9]+", text or "") if w) or "Unknown"

    parts = [
        camel(plan.get("retailer", "")),
        camel(plan.get("plan", "")),
        plan.get("region", "XX"),
        camel(plan.get("dnsp", "")) if plan.get("dnsp") else "AnyDNSP",
        str(plan.get("effective_from", "")).replace("-", ""),
    ]
    return "-".join(parts) + ".json"


def summary(plan: dict[str, Any]) -> str:
    """Plain lines describing a plan's periods, for the review step. Data only."""
    lines = [f"{plan['retailer']} {plan['plan']} ({plan['region']}, {plan.get('dnsp', '-')})",
             f"supply {plan['supply_charge']['rate']}/day"]
    for key in ("import", "export"):
        lines.append(f"{key}:")
        for period in plan[key]["periods"]:
            if "tiers" in period:
                first, after = period["tiers"]
                tail = after.get("use_period") or after.get("rate")
                price = f"{first['rate']} up to {first['up_to_kwh']} kWh/{period['tier_reset']}, then {tail}"
            else:
                price = str(period["rate"])
            when = "other times" if period.get("default") else ", ".join(
                f"{w['days'] if isinstance(w['days'], str) else '+'.join(w['days'])} {w['start']}-{w['end']}"
                for w in period["windows"]
            )
            lines.append(f"- {period['name']}: {price} ({when})")
    return "\n".join(lines)


# ----- library filtering ------------------------------------------------------


def _values(entries: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({str(e.get(key) or "") for e in entries})


def filter_entries(entries: list[dict[str, Any]], **criteria: str) -> list[dict[str, Any]]:
    """Index entries matching every given field (dnsp "" matches entries without one)."""
    return [e for e in entries if all(str(e.get(k) or "") == v for k, v in criteria.items())]


def countries(entries: list[dict[str, Any]]) -> list[str]:
    return _values(entries, "country")


def regions(entries: list[dict[str, Any]], country: str) -> list[str]:
    return _values(filter_entries(entries, country=country), "region")


def dnsps(entries: list[dict[str, Any]], country: str, region: str) -> list[str]:
    return _values(filter_entries(entries, country=country, region=region), "dnsp")


def retailers(entries: list[dict[str, Any]], country: str, region: str, dnsp: str) -> list[str]:
    return _values(filter_entries(entries, country=country, region=region, dnsp=dnsp), "retailer")


def plans_newest_first(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest effective date first; plans starting the same day by name."""
    by_name = sorted(entries, key=lambda e: str(e.get("plan") or ""))
    return sorted(by_name, key=lambda e: str(e.get("effective_from") or ""), reverse=True)


def plan_label(entry: dict[str, Any]) -> str:
    label = f"{entry.get('plan', '?')} · from {entry.get('effective_from', '?')}"
    if entry.get("plan_id"):
        label += f" · {entry['plan_id']}"
    return label


def pick_default(options: list[str], preferred: str | None) -> str | None:
    if preferred in options:
        return preferred
    return options[0] if options else None
