#!/usr/bin/env python3
"""Try the CDR plan converter on real Energy Made Easy plans and report the results.

For every retailer path (the AER list), takes up to --per-path current electricity
plans (residential first, then business), fetches each plan's detail (Get Generic
Plan Detail, x-v 3), converts it with engine/importers/cdr.py and validates the
result. Writes to --out:

- report.json: totals, outcomes counted by reason, and one line per plan
- samples/<path>/<planId>.json: the raw plan detail, to build tests from

Nothing is committed: the workflow uploads --out as a run artifact (plan data is
for checking the converter, not for the repo).

    python scripts/check_cdr_conversion.py --out cdr-check --per-path 3
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_eme_retailers as eme  # noqa: E402  (http_json, brand list; also puts the engine on sys.path)
from engine import validate_plan  # noqa: E402
from engine.importers.cdr import CdrImportError, convert  # noqa: E402


def reason_key(text: str) -> str:
    """A skip reason without the plan label, so the same reason counts together."""
    reason = text.split("): ", 1)[-1] if "): " in text else text
    return re.sub(r"'[^']*'|\d+(\.\d+)?", "…", reason)


def pick_plans(plans: list[dict[str, Any]], per_path: int) -> list[dict[str, Any]]:
    """Up to per_path electricity plans: residential first, then business, newest first."""
    usable = [p for p in plans if (p.get("fuelType") or "").upper() in ("ELECTRICITY", "DUAL")]
    usable.sort(key=lambda p: str(p.get("lastUpdated") or ""), reverse=True)
    residential = [p for p in usable if (p.get("customerType") or "").upper() != "BUSINESS"]
    business = [p for p in usable if (p.get("customerType") or "").upper() == "BUSINESS"]
    picked: list[dict[str, Any]] = []
    while len(picked) < per_path and (residential or business):
        for pool in (residential, business):
            if pool and len(picked) < per_path:
                picked.append(pool.pop(0))
    return picked


def check(fetch, out: Path, per_path: int, today: date, pause: float = eme.PAUSE_SECONDS, log=print) -> dict:
    results: list[dict[str, Any]] = []
    for uri in eme.unique_paths(eme.fallback_brands()):
        path = uri.rsplit("/", 1)[-1]
        try:
            listing = fetch(f"{uri}/cds-au/v1/energy/plans?fuelType=ALL&effective=CURRENT&page-size=1000&page=1",
                            {"x-v": "1"})
        except Exception as err:  # noqa: BLE001
            results.append({"path": path, "outcome": "list_failed", "detail": str(err)})
            continue
        for summary in pick_plans((listing.get("data") or {}).get("plans") or [], per_path):
            plan_id = summary.get("planId", "")
            entry: dict[str, Any] = {"path": path, "plan_id": plan_id, "name": summary.get("displayName"),
                                     "customer": summary.get("customerType")}
            time.sleep(pause)
            try:
                detail = fetch(f"{uri}/cds-au/v1/energy/plans/{quote(plan_id, safe='@')}", {"x-v": "3"})
            except Exception as err:  # noqa: BLE001
                results.append({**entry, "outcome": "detail_failed", "detail": str(err)})
                continue
            sample = out / "samples" / path / f"{re.sub(r'[^A-Za-z0-9@._-]', '_', plan_id)}.json"
            sample.parent.mkdir(parents=True, exist_ok=True)
            sample.write_text(json.dumps(detail, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            contract = (detail.get("data") or {}).get("electricityContract") or {}
            entry["pricing_model"] = contract.get("pricingModel")
            entry["time_zone"] = contract.get("timeZone")
            try:
                conversion = convert(detail, today)
            except CdrImportError as err:
                results.append({**entry, "outcome": "error", "detail": str(err)})
                continue
            if not conversion.plans:
                results.append({**entry, "outcome": "skipped", "detail": "; ".join(conversion.skipped)})
                continue
            errors = []
            for plan in conversion.plans:
                errors += validate_plan(plan).errors
            results.append({
                **entry,
                "outcome": "invalid" if errors else "converted",
                "detail": "; ".join(errors[:5]) if errors else "",
                "plans": len(conversion.plans),
                "partly_skipped": conversion.skipped,
                "notes": conversion.notes,
            })
        log(f"{path}: done")
        time.sleep(pause)

    outcomes = Counter(r["outcome"] for r in results)
    reasons = Counter(
        f"{r['outcome']}: {reason_key(part)}"
        for r in results if r["outcome"] in ("skipped", "error", "invalid")
        for part in (r.get("detail") or "").split("; ") if part
    )
    report = {
        "checked": today.isoformat(),
        "plans_tried": len(results),
        "outcomes": dict(outcomes.most_common()),
        "pricing_models": dict(Counter(str(r.get("pricing_model")) for r in results).most_common()),
        "time_zones": dict(Counter(str(r.get("time_zone")) for r in results).most_common()),
        "reasons": dict(reasons.most_common()),
        "results": results,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("cdr-check"))
    parser.add_argument("--per-path", type=int, default=3)
    args = parser.parse_args()
    report = check(eme.http_json, args.out, args.per_path, date.today())
    print(json.dumps({k: report[k] for k in ("plans_tried", "outcomes", "pricing_models", "time_zones")}, indent=2))
    print("Top reasons:")
    for reason, count in list(report["reasons"].items())[:15]:
        print(f"  {count:4d}  {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
