#!/usr/bin/env python3
"""Add plans to the library from Energy Made Easy (CDR plan data).

For the distributors asked for, lists every retailer's current plans (Get Generic
Plans, x-v 1), fetches the matching ones (Get Generic Plan Detail, x-v 3), converts
them with engine/importers/cdr.py, validates them and writes the ones that pass to
plans/<country>/<region>/ with `needs_review: true`. Plans already in the library
(same plan ID and distributor) are left alone: a reviewed plan keeps its period ids,
and with them people's meters. Run scripts/build_index.py afterwards.

    python scripts/import_eme_plans.py --dnsp Ausgrid --dnsp "Endeavour Energy" --dnsp "Essential Energy"
    python scripts/import_eme_plans.py --dnsp Ausgrid --customer business --pricing all --per-retailer 0

Writes a Markdown summary (--summary) for the pull request the Import plans workflow opens.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, timedelta
import json
import re
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_eme_retailers as eme  # noqa: E402  (http_json, brand list; also puts the engine on sys.path)
from _common import PLANS_DIR, dump_json, plan_files, plan_relpath, slug  # noqa: E402
from engine import validate_plan  # noqa: E402
from engine.importers.cdr import DISTRIBUTORS, CdrImportError, convert, map_distributor  # noqa: E402

PRICING = {
    "tou": ("TIME_OF_USE", "TIME_OF_USE_CONT_LOAD"),
    "single": ("SINGLE_RATE", "SINGLE_RATE_CONT_LOAD"),
    "all": ("TIME_OF_USE", "TIME_OF_USE_CONT_LOAD", "SINGLE_RATE", "SINGLE_RATE_CONT_LOAD"),
}
SLUG_LENGTH = 40
# Plans whose prices follow the wholesale market: EME shows the retailer's estimate (GloBird WHOLESAVE
# "estimate", Flow Power "Variable Prices (estimate)"), so as fixed rates they'd price meters wrongly.
# They wait for a live price source (CLAUDE-TODO: wholesale-priced retailers).
WHOLESALE = re.compile(r"\bestimated?\b|\bwholesale\b|\bspot prices?\b|\bvariable prices\b", re.IGNORECASE)


def base_id(plan_id: str) -> str:
    """COV1117612MRE3@EME -> COV1117612MRE3 (the library stores plan ids without the source suffix)."""
    return plan_id.split("@", 1)[0]


@dataclass
class Report:
    written: list[dict[str, Any]] = field(default_factory=list)
    already: list[str] = field(default_factory=list)  # unchanged plans already in the library
    updated: list[str] = field(default_factory=list)
    new_versions: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    reviewed_differs: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    other_pricing: int = 0

    def summary(self, dnsps: list[str], customer: str, pricing: str) -> str:
        lines = [
            f"Imported from Energy Made Easy for **{', '.join(dnsps)}** ({customer}, {pricing} pricing).",
            "",
            f"- **{len(self.written)}** new plans, all `needs_review: true`",
            f"- **{len(self.new_versions)}** new versions and **{len(self.replaced)}** replacements of library plans "
            "(previous file ended the day before; period ids kept)",
            f"- **{len(self.updated)}** library plans updated in place (same plan ID, new content, not yet reviewed)",
            f"- **{len(self.withdrawn)}** library plans no longer on Energy Made Easy (ended, not deleted)",
            f"- **{len(self.reviewed_differs)}** reviewed plans that differ from Energy Made Easy (left alone)",
            f"- {len(self.already)} already in the library and unchanged",
            f"- {len(self.skipped)} not converted (the plan format can't express them yet)",
            f"- {len(self.invalid)} converted but failed validation (not added)",
            f"- {self.other_pricing} with other pricing (not asked for)",
        ]
        if self.failed:
            lines.append(f"- {len(self.failed)} couldn't be fetched (nothing was marked withdrawn)")
        if self.written:
            lines += ["", "| Retailer | Plan | Distributor | From | Plan ID |", "|---|---|---|---|---|"]
            for p in sorted(self.written, key=lambda p: (p["dnsp"], p["retailer"], p["plan"])):
                lines.append(f"| {p['retailer']} | {p['plan']} | {p['dnsp']} | {p['effective_from']} | {p['plan_id']} |")
        for title, items in (
            ("New versions", self.new_versions), ("Replacements (same name, new plan ID)", self.replaced),
            ("Updated in place", self.updated), ("No longer on Energy Made Easy", self.withdrawn),
            ("Reviewed plans that differ: check these", self.reviewed_differs),
            ("Not converted", self.skipped), ("Failed validation", self.invalid), ("Couldn't be fetched", self.failed),
        ):
            if items:
                lines += ["", f"<details><summary>{title} ({len(items)})</summary>", ""]
                lines += [f"- {item}" for item in items]
                lines += ["", "</details>"]
        return "\n".join(lines) + "\n"


# ----- the library ----------------------------------------------------------------

MAX_WITHDRAWN = 5  # a run may end at most this many plans, or WITHDRAWN_SHARE of them, whichever is more
WITHDRAWN_SHARE = 0.2
PRICING_KEYS = ("timezone", "holidays", "seasons", "supply_charge", "import", "export", "demand", "controlled_load")
VERSION = re.compile(r"^([A-Z0-9]*\d[A-Z]+)\d+$")  # code + number + letters (MRE) + version


def family(plan_id: str) -> str:
    """The plan ID without its version: COV1117612MRE3 -> COV1117612MRE. Victorian IDs (no version) are their own."""
    match = VERSION.match(base_id(plan_id))
    return match.group(1) if match else base_id(plan_id)


@dataclass
class LibraryPlan:
    path: Path
    plan: dict[str, Any]

    @property
    def reviewed(self) -> bool:
        """Checked by a person, or not made by this importer (hand-made, from a bill or fact sheet):
        never changed automatically; differences are listed instead."""
        return self.plan.get("needs_review") is False or (self.plan.get("source") or {}).get("type") != "EME"


def library_plans(dnsps: set[str], customer: str, today: date) -> list[LibraryPlan]:
    """Current library plans with a plan ID on these distributors, for this customer type."""
    out = []
    for path in plan_files():
        plan = json.loads(path.read_text(encoding="utf-8"))
        ended = plan.get("effective_to") and plan["effective_to"] < today.isoformat()
        if (plan.get("plan_id") and plan.get("dnsp") in dnsps and not ended
                and (plan.get("customer_type") or customer) == customer):
            out.append(LibraryPlan(path, plan))
    return out


def library_keys() -> set[tuple[str, str]]:
    """(plan ID, DNSP) of every library plan."""
    keys = set()
    for path in plan_files():
        plan = json.loads(path.read_text(encoding="utf-8"))
        if plan.get("plan_id"):
            keys.add((base_id(plan["plan_id"]), plan.get("dnsp") or ""))
    return keys


def _windows(period: dict[str, Any]) -> str:
    return json.dumps(period.get("windows"), sort_keys=True)


def keep_ids(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Give the new plan's periods the old plan's ids (same name, else same times), so people's meters carry
    on. Tier references follow. Returns the names of periods that start new meters."""
    fresh = []
    for stream in ("import", "export"):
        old_periods = (old.get(stream) or {}).get("periods") or []
        new_periods = (new.get(stream) or {}).get("periods") or []
        rename: dict[str, str] = {}
        free = list(old_periods)
        for period in new_periods:
            match = (next((o for o in free if o["name"].strip().lower() == period["name"].strip().lower()), None)
                     or next((o for o in free if _windows(o) == _windows(period) and o.get("default") == period.get("default")), None))
            if match:
                free.remove(match)
                rename[period["id"]] = match["id"]
            else:
                fresh.append(f"{stream} {period['name']}")
        taken = set(rename.values())
        for period in new_periods:
            if period["id"] in rename:
                period["id"] = rename[period["id"]]
            elif period["id"] in taken:  # a fresh period whose id an old one now uses
                n = 2
                while f"{period['id']}_{n}" in taken:
                    n += 1
                rename[period["id"]] = period["id"] = f"{period['id']}_{n}"
            taken.add(period["id"])
        for period in new_periods:
            for tier in period.get("tiers") or []:
                if tier.get("use_period") in rename:
                    tier["use_period"] = rename[tier["use_period"]]
    return fresh


def same_pricing(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in PRICING_KEYS)


def day_before(iso: str) -> str:
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()


# ----- running ----------------------------------------------------------------------


def wanted(summary: dict[str, Any], dnsps: set[str], customer: str) -> list[str]:
    """The asked-for DNSPs a listed plan is offered on, if it's for this customer type."""
    plan_customer = (summary.get("customerType") or "").lower()
    if plan_customer and plan_customer != customer:
        return []
    mapped = [map_distributor(d) for d in (summary.get("geography") or {}).get("distributors") or []]
    return sorted({m[0] for m in mapped if m and m[0] in dnsps})


def target_path(plan: dict[str, Any], taken: set[Path]) -> Path:
    """Where a new plan goes, never over another file."""
    plan_slug = slug(plan["plan"])[:SLUG_LENGTH] or "Plan"
    path = PLANS_DIR / plan_relpath(plan, plan_slug)
    if path.exists() or path in taken:
        path = PLANS_DIR / plan_relpath(plan, (plan_slug[:SLUG_LENGTH - 12] + slug(plan["plan_id"]))[:SLUG_LENGTH + 12])
    return path


def run(
    fetch, dnsps: list[str], customer: str, pricing: str, per_retailer: int, today: date,
    pause: float = eme.PAUSE_SECONDS, log=print, write: bool = True,
) -> Report:
    """Convert what's listed for these distributors, reconcile it with the library, then write.

    Energy Made Easy lists only current plans and keeps no history, so changes are found by comparing
    with the library: same ID with new content, a new version of a plan family, a plan gone from the
    list, or a new ID with the same name as a plan that went. Nothing is written until the end.
    """
    report = Report()
    wanted_dnsps = set(dnsps)
    library = library_plans(wanted_dnsps, customer, today)
    by_id = {(base_id(lp.plan["plan_id"]), lp.plan["dnsp"]): lp for lp in library}
    by_family = {(family(lp.plan["plan_id"]), lp.plan["dnsp"]): lp for lp in library}
    allowed = PRICING[pricing]
    listed: set[tuple[str, str]] = set()  # (family, DNSP) of everything on the list, converted or not
    incoming: list[dict[str, Any]] = []
    for uri in eme.unique_paths(eme.fallback_brands()):
        path_name = uri.rsplit("/", 1)[-1]
        try:
            listing = fetch(f"{uri}/cds-au/v1/energy/plans?fuelType=ALL&effective=CURRENT&page-size=1000&page=1",
                            {"x-v": "1"})
        except Exception as err:  # noqa: BLE001
            report.failed.append(f"{path_name}: plan list: {err}")
            continue
        summaries = [p for p in (listing.get("data") or {}).get("plans") or []
                     if (p.get("fuelType") or "").upper() in ("ELECTRICITY", "DUAL")]
        summaries.sort(key=lambda p: str(p.get("lastUpdated") or ""), reverse=True)  # newest first
        counts: dict[str, int] = {}
        for summary in summaries:
            plan_id = summary.get("planId", "")
            on = wanted(summary, wanted_dnsps, customer)
            listed.update((family(plan_id), d) for d in on)
            known = [d for d in on if (family(plan_id), d) in by_family]
            new = [d for d in on if d not in known and (not per_retailer or counts.get(d, 0) < per_retailer)]
            if not known and not new:
                continue
            time.sleep(pause)
            try:
                detail = fetch(f"{uri}/cds-au/v1/energy/plans/{quote(plan_id, safe='@')}", {"x-v": "3"})
            except Exception as err:  # noqa: BLE001
                report.failed.append(f"{plan_id}: {err}")
                continue
            contract = (detail.get("data") or {}).get("electricityContract") or {}
            if (contract.get("pricingModel") or "").upper() not in allowed:
                report.other_pricing += 1
                continue
            data = detail.get("data") or {}
            if WHOLESALE.search(f"{data.get('displayName') or ''} {data.get('description') or ''}"):
                report.skipped.append(f"{plan_id}: {data.get('displayName')!r} follows wholesale prices (EME shows "
                                      "an estimate): not a fixed-rate plan")
                listed.update((family(plan_id), d) for d in on)
                continue
            try:
                conversion = convert(detail, today)
            except CdrImportError as err:
                report.skipped.append(f"{plan_id}: {err}")
                continue
            report.skipped += [reason for reason in conversion.skipped if "unknown distributor" not in reason]
            for plan in conversion.plans:
                if plan["dnsp"] not in known + new:
                    continue
                result = validate_plan(plan, today=today)
                if not result.ok:
                    report.invalid.append(f"{plan_id} ({plan['dnsp']}): {'; '.join(result.errors[:3])}")
                    continue
                plan["plan_id"] = base_id(plan["plan_id"])
                incoming.append(plan)
                if plan["dnsp"] in new:
                    counts[plan["dnsp"]] = counts.get(plan["dnsp"], 0) + 1
        log(f"{path_name}: listed")

    writes: dict[Path, dict[str, Any]] = {}
    taken: set[Path] = set()

    def add(plan: dict[str, Any]) -> Path:
        target = target_path(plan, taken)
        taken.add(target)
        writes[target] = plan
        return target

    def supersede(old: LibraryPlan, new: dict[str, Any], why: str, bucket: list[str]) -> None:
        fresh = keep_ids(old.plan, new)
        old.plan["effective_to"] = day_before(new["effective_from"]) if new["effective_from"] > old.plan[
            "effective_from"] else today.isoformat()
        writes[old.path] = old.plan
        if fresh:
            new["notes"] = f"{new['notes']} New meters (no matching period in the previous plan): {', '.join(fresh)}."
        target = add(new)
        bucket.append(f"{new['retailer']} {new['plan']} ({new['dnsp']}): {why}, from {new['effective_from']} "
                      f"→ {target.name}" + (f"; new meters: {', '.join(fresh)}" if fresh else ""))

    matched: set[Path] = set()
    unmatched: list[dict[str, Any]] = []
    for plan in incoming:
        key = (plan["plan_id"], plan["dnsp"])
        old = by_id.get(key) or by_family.get((family(plan["plan_id"]), plan["dnsp"]))
        if old is None:
            unmatched.append(plan)
            continue
        matched.add(old.path)
        label = f"{plan['retailer']} {plan['plan']} ({plan['dnsp']}, {plan['plan_id']})"
        same_id = base_id(old.plan["plan_id"]) == plan["plan_id"]
        candidate = json.loads(json.dumps(plan))
        keep_ids(old.plan, candidate)
        if same_id and same_pricing(old.plan, candidate):
            report.already.append(label)
        elif old.reviewed:
            report.reviewed_differs.append(f"{label}: Energy Made Easy now differs from the reviewed plan "
                                           f"{old.path.name}")
        elif same_id and plan["effective_from"] <= old.plan["effective_from"]:
            keep_ids(old.plan, plan)
            plan["effective_from"] = old.plan["effective_from"]
            writes[old.path] = plan
            report.updated.append(f"{label}: {old.path.name}")
        else:
            why = "new prices" if same_id else f"new version of {old.plan['plan_id']}"
            supersede(old, plan, why, report.new_versions)

    gone = [lp for lp in library if lp.path not in matched and (family(lp.plan["plan_id"]), lp.plan["dnsp"]) not in listed]
    if report.failed:
        gone = []  # part of the list is missing: don't end plans that might still be there
    elif len(gone) > max(MAX_WITHDRAWN, WITHDRAWN_SHARE * len(library)):
        # Far more than a real week's changes: an empty or partial list, not retailers withdrawing plans
        report.failed.append(f"{len(gone)} of {len(library)} library plans looked withdrawn: too many to be real, "
                             "so none were ended")
        gone = []
    for old in gone:
        replacement = next((p for p in unmatched if all(
            p.get(k) == old.plan.get(k) for k in ("retailer", "plan", "dnsp", "customer_type"))), None)
        if old.reviewed:
            report.reviewed_differs.append(f"{old.plan['retailer']} {old.plan['plan']} ({old.plan['dnsp']}, "
                                           f"{old.plan['plan_id']}): not on Energy Made Easy any more ({old.path.name})")
            continue
        if replacement is not None:
            unmatched.remove(replacement)
            supersede(old, replacement, f"replaces {old.plan['plan_id']} (same name, new plan ID)", report.replaced)
            continue
        old.plan["effective_to"] = today.isoformat()
        old.plan["notes"] = (f"{old.plan.get('notes') or ''} Not on Energy Made Easy any more "
                             f"(checked {today.isoformat()}).").strip()
        writes[old.path] = old.plan
        report.withdrawn.append(f"{old.plan['retailer']} {old.plan['plan']} ({old.plan['dnsp']}, "
                                f"{old.plan['plan_id']}): {old.path.name}")

    for plan in unmatched:
        target = add(plan)
        report.written.append({**{k: plan[k] for k in ("retailer", "plan", "dnsp", "effective_from", "plan_id")},
                               "file": str(target.relative_to(PLANS_DIR.parent))})
    if write:
        for path, plan in writes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump_json(plan), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dnsp", action="append", required=True, help="distributor, as in the library (repeat)")
    parser.add_argument("--customer", choices=("residential", "business"), default="residential")
    parser.add_argument("--pricing", choices=tuple(PRICING), default="tou")
    parser.add_argument("--per-retailer", type=int, default=3,
                        help="newest plans per retailer per distributor (0: all)")
    parser.add_argument("--summary", type=Path, help="write a Markdown summary here")
    parser.add_argument("--dry-run", action="store_true", help="convert and report, write no plan files")
    args = parser.parse_args()

    known_dnsps = {dnsp for _, dnsp, _ in DISTRIBUTORS}
    unknown = [d for d in args.dnsp if d not in known_dnsps]
    if unknown:
        parser.error(f"unknown distributor(s) {unknown}; use one of: {', '.join(sorted(known_dnsps))}")

    report = run(eme.http_json, args.dnsp, args.customer, args.pricing, args.per_retailer, date.today(),
                 write=not args.dry_run)
    text = report.summary(args.dnsp, args.customer, args.pricing)
    print(text)
    if args.summary:
        args.summary.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
