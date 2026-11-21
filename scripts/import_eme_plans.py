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
from datetime import date
import json
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


def base_id(plan_id: str) -> str:
    """COV1117612MRE3@EME -> COV1117612MRE3 (the library stores plan ids without the source suffix)."""
    return plan_id.split("@", 1)[0]


@dataclass
class Report:
    written: list[dict[str, Any]] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    other_pricing: int = 0

    def summary(self, dnsps: list[str], customer: str, pricing: str) -> str:
        lines = [
            f"Imported from Energy Made Easy for **{', '.join(dnsps)}** ({customer}, {pricing} pricing).",
            "",
            f"- **{len(self.written)}** plans added, all `needs_review: true`",
            f"- {len(self.already)} already in the library (left as they are)",
            f"- {len(self.skipped)} not converted (the plan format can't express them yet)",
            f"- {len(self.invalid)} converted but failed validation (not added)",
            f"- {self.other_pricing} with other pricing (not asked for)",
        ]
        if self.failed:
            lines.append(f"- {len(self.failed)} couldn't be fetched")
        if self.written:
            lines += ["", "| Retailer | Plan | Distributor | From | Plan ID |", "|---|---|---|---|---|"]
            for p in sorted(self.written, key=lambda p: (p["dnsp"], p["retailer"], p["plan"])):
                lines.append(f"| {p['retailer']} | {p['plan']} | {p['dnsp']} | {p['effective_from']} | {p['plan_id']} |")
        for title, items in (("Not converted", self.skipped), ("Failed validation", self.invalid),
                             ("Couldn't be fetched", self.failed)):
            if items:
                lines += ["", f"<details><summary>{title} ({len(items)})</summary>", ""]
                lines += [f"- {item}" for item in items]
                lines += ["", "</details>"]
        return "\n".join(lines) + "\n"


def library_keys() -> set[tuple[str, str]]:
    """(plan ID, DNSP) of every library plan."""
    keys = set()
    for path in plan_files():
        plan = json.loads(path.read_text(encoding="utf-8"))
        if plan.get("plan_id"):
            keys.add((base_id(plan["plan_id"]), plan.get("dnsp") or ""))
    return keys


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
    report = Report()
    known = library_keys()
    allowed = PRICING[pricing]
    taken: set[Path] = set()
    for uri in eme.unique_paths(eme.fallback_brands()):
        path_name = uri.rsplit("/", 1)[-1]
        try:
            listing = fetch(f"{uri}/cds-au/v1/energy/plans?fuelType=ALL&effective=CURRENT&page-size=1000&page=1",
                            {"x-v": "1"})
        except Exception as err:  # noqa: BLE001
            report.failed.append(f"{path_name}: plan list: {err}")
            continue
        listed = [p for p in (listing.get("data") or {}).get("plans") or []
                  if (p.get("fuelType") or "").upper() in ("ELECTRICITY", "DUAL")]
        listed.sort(key=lambda p: str(p.get("lastUpdated") or ""), reverse=True)  # newest first
        counts: dict[str, int] = {}
        for summary in listed:
            on = [d for d in wanted(summary, set(dnsps), customer)
                  if (base_id(summary.get("planId", "")), d) not in known]
            already = [d for d in wanted(summary, set(dnsps), customer) if d not in on]
            report.already += [f"{summary.get('planId')} ({d})" for d in already]
            if per_retailer:
                on = [d for d in on if counts.get(d, 0) < per_retailer]
            if not on:
                continue
            plan_id = summary["planId"]
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
            try:
                conversion = convert(detail, today)
            except CdrImportError as err:
                report.skipped.append(f"{plan_id}: {err}")
                continue
            # Distributors we didn't ask for don't matter here
            report.skipped += [reason for reason in conversion.skipped if "unknown distributor" not in reason]
            for plan in conversion.plans:
                if plan["dnsp"] not in on:
                    continue
                result = validate_plan(plan, today=today)
                if not result.ok:
                    report.invalid.append(f"{plan_id} ({plan['dnsp']}): {'; '.join(result.errors[:3])}")
                    continue
                plan["plan_id"] = base_id(plan["plan_id"])
                target = target_path(plan, taken)
                taken.add(target)
                if write:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(dump_json(plan), encoding="utf-8")
                known.add((plan["plan_id"], plan["dnsp"]))
                counts[plan["dnsp"]] = counts.get(plan["dnsp"], 0) + 1
                report.written.append({**{k: plan[k] for k in ("retailer", "plan", "dnsp", "effective_from",
                                                                 "plan_id")},
                                       "file": str(target.relative_to(PLANS_DIR.parent))})
        log(f"{path_name}: done ({sum(counts.values())} added)")
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
