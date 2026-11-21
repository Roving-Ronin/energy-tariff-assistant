"""Library updates for an imported plan: which library plan replaces it, and what changed. Pure.

A plan entry follows its library plan when the library has:
- the same plan ID with new prices (updated in place by the importer),
- a newer version of the same plan family (`COV1117612MRE3` -> `COV1117612MRE4`), or
- a replacement the importer linked with `replaced_by` (the retailer withdrew the plan and listed a new ID).
Period ids are kept across these by the importer (`keep_ids`), so meters and entities carry on.
"""

from __future__ import annotations

import re
from typing import Any

# Code + number + letters (MRE) + version. Victorian @VEC IDs have no version: each is its own family.
_VERSIONED = re.compile(r"^([A-Z0-9]*\d[A-Z]+)\d+$")
PRICING_KEYS = ("timezone", "holidays", "seasons", "supply_charge", "import", "export", "demand", "controlled_load")


def family(plan_id: str | None) -> str:
    base = (plan_id or "").split("@", 1)[0]
    match = _VERSIONED.match(base)
    return match.group(1) if match else base


def same_pricing(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in PRICING_KEYS)


def version_label(plan: dict[str, Any]) -> str:
    """'COV1117612MRE3 · from 2026-07-01': how the update entity shows a plan version."""
    plan_id = plan.get("plan_id")
    return f"{plan_id} · from {plan.get('effective_from')}" if plan_id else f"from {plan.get('effective_from')}"


def candidates(index: list[dict[str, Any]], plan: dict[str, Any], today: str) -> list[dict[str, Any]]:
    """Library entries that could replace `plan`: same network, same plan family, not ended; newest first."""
    fam = family(plan.get("plan_id"))
    if not fam:
        return []
    found = [
        e for e in index
        if e.get("dnsp") == plan.get("dnsp")
        and family(e.get("plan_id")) == fam
        and e.get("feed_in_option") == plan.get("feed_in_option")  # a feed-in option is a plan of its own
        and (not e.get("effective_to") or e["effective_to"] >= today)
    ]
    return sorted(found, key=lambda e: str(e.get("effective_from") or ""), reverse=True)


def _periods(plan: dict[str, Any], stream: str) -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in (plan.get(stream) or {}).get("periods", [])}


def _rate_text(period: dict[str, Any]) -> str:
    if period.get("tiers"):
        parts = []
        for tier in period["tiers"]:
            rate = tier.get("rate") if tier.get("rate") is not None else f"as {tier.get('use_period')}"
            parts.append(f"{rate} up to {tier['up_to_kwh']} kWh" if tier.get("up_to_kwh") is not None else str(rate))
        return ", then ".join(parts)
    return str(period.get("rate"))


def describe_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What an update changes, one line each: 'Import Peak: 0.45331 → 0.4621'."""
    lines = []
    if old.get("plan_id") != new.get("plan_id"):
        lines.append(f"Plan ID: {old.get('plan_id')} → {new.get('plan_id')}")
    if old.get("plan") != new.get("plan"):
        lines.append(f"Name: {old.get('plan')} → {new.get('plan')}")
    if old.get("effective_from") != new.get("effective_from"):
        lines.append(f"From: {old.get('effective_from')} → {new.get('effective_from')}")
    old_supply = (old.get("supply_charge") or {}).get("rate")
    new_supply = (new.get("supply_charge") or {}).get("rate")
    if old_supply != new_supply:
        lines.append(f"Supply charge: {old_supply} → {new_supply} per day")
    for stream, label in (("import", "Import"), ("export", "Export")):
        before, after = _periods(old, stream), _periods(new, stream)
        for pid, period in after.items():
            name = period.get("name", pid)
            if pid not in before:
                lines.append(f"{label} {name}: added ({_rate_text(period)})")
                continue
            if _rate_text(before[pid]) != _rate_text(period):
                lines.append(f"{label} {name}: {_rate_text(before[pid])} → {_rate_text(period)}")
            if before[pid].get("windows") != period.get("windows"):
                lines.append(f"{label} {name}: times changed")
        lines += [f"{label} {p.get('name', pid)}: removed" for pid, p in before.items() if pid not in after]
    for key, label in (("timezone", "Time zone"), ("holidays", "Public holidays"), ("seasons", "Seasons"),
                       ("demand", "Demand charges"), ("controlled_load", "Controlled load")):
        if old.get(key) != new.get(key):
            lines.append(f"{label}: changed")
    return lines
