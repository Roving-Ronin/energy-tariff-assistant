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

CUSTOMER_TYPES = ("residential", "business")
DEFAULT_CUSTOMER_TYPE = "residential"

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
    customer_type: str = DEFAULT_CUSTOMER_TYPE
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
    if draft.customer_type in CUSTOMER_TYPES:
        plan["customer_type"] = draft.customer_type
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


def for_customer(entries: list[dict[str, Any]], customer_type: str) -> list[dict[str, Any]]:
    """Entries for this customer type. Plans that don't say are offered to both."""
    return [e for e in entries if e.get("customer_type") in (None, "", customer_type)]


def current(entries: list[dict[str, Any]], today: str) -> list[dict[str, Any]]:
    """Entries that haven't ended (`effective_to` today or later, or none): ended versions and plans
    withdrawn from Energy Made Easy stay in the library but aren't offered. `today` is an ISO date."""
    return [e for e in entries if not e.get("effective_to") or str(e["effective_to"]) >= today]


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
    """'SolarMax Residential TOU · COV1117612MRE3 · from 2026-07-01' (the plan ID tells same-named variants apart)."""
    plan_id = f" · {entry['plan_id']}" if entry.get("plan_id") else ""
    return f"{entry.get('plan', '?')}{plan_id} · from {entry.get('effective_from', '?')}"


def pick_default(options: list[str], preferred: str | None) -> str | None:
    if preferred in options:
        return preferred
    return options[0] if options else None


# ----- names in Home Assistant ------------------------------------------

# Words a user already knows about their own supply: customer type, state, network. Left out of plan names so
# they stay short ("CovaU · SolarMax Residential TOU (Essential Energy)" -> "CovaU - SolarMax TOU").
_STATES = ("NSW", "QLD", "VIC", "SA", "ACT", "TAS", "WA", "NT")
_KNOWN_WORDS = ("Small Business", "Residential", "Resi", "Business")
# Networks as retailers shorten them in plan names ("Endeavour - RESI Time of Use")
_NETWORK_SHORT = ("Endeavour", "Essential", "Ergon", "AusNet", "Ausgrid", "Energex", "Evoenergy", "TasNetworks")


def _known_words() -> list[str]:
    from .engine.importers.cdr import DISTRIBUTORS  # pure; imported late to keep this module light

    networks = {dnsp for _, dnsp, _ in DISTRIBUTORS}
    return sorted(networks | set(_KNOWN_WORDS) | set(_STATES) | set(_NETWORK_SHORT), key=len, reverse=True)


def short_plan_name(plan: dict[str, Any]) -> str:
    """The plan's name without customer type, state, network or a repeated retailer name."""
    name = str(plan.get("plan") or "")
    words = "|".join(re.escape(w) for w in _known_words())
    name = re.sub(rf"(?<![A-Za-z0-9])(?:{words})(?![A-Za-z0-9])", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"(?<=\s)-(?=[A-Za-z])", "", " " + name).strip()  # "ENGIE -Home" -> "ENGIE Home"
    retailer = str(plan.get("retailer") or "").strip()
    for prefix in [p for p in (retailer, retailer.split(" ")[0]) if p]:  # full name first; "GEE" of GEE Energy
        if name.lower().startswith(prefix.lower() + " "):
            name = name[len(prefix) + 1:]
            break
    name = re.sub(r"\(\s*\)", " ", name)  # "()" left by a network in brackets
    name = re.sub(r"\s*-\s*(?=[-)]|$)", " ", name)  # a dash left dangling
    name = re.sub(r"^[\s\-–·,:]+|[\s\-–·,:]+$", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or str(plan.get("plan") or "Plan")


def display_name(plan: dict[str, Any]) -> str:
    """'CovaU - SolarMax TOU': what the plan is called in Home Assistant (the entry title; renamable)."""
    retailer = str(plan.get("retailer") or "").strip()
    short = short_plan_name(plan)
    return f"{retailer} - {short}" if retailer else short


def entity_slug(plan: dict[str, Any]) -> str:
    """'eta_covau_solarmaxtou': the start of the plan's entity IDs, fixed when the entry is created."""
    def compact(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    retailer = compact(str(plan.get("retailer") or "")) or "plan"
    name = compact(short_plan_name(plan))
    if option := compact(str(plan.get("feed_in_option") or "")):
        # Options of one plan differ only at the end of the name: keep that part
        suffix = f"fit{option}"
        name = compact(re.sub(r"\(\s*fit\s+option\s+\w+\s*\)", "", short_plan_name(plan), flags=re.IGNORECASE))
        return f"eta_{retailer}_{name[:32 - len(suffix)]}{suffix}"
    return f"eta_{retailer}_{name[:32] or 'plan'}"
