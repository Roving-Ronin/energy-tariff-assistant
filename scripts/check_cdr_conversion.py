#!/usr/bin/env python3
"""Try the CDR plan converter on real Energy Made Easy plans and report the results.

For every retailer path (the AER list), takes up to --per-path current electricity
plans (residential first, then business), fetches each plan's detail (Get Generic
Plan Detail, x-v 3), converts it with engine/importers/cdr.py and validates the
result. Writes to --out:

- report.json: totals, outcomes counted by reason, converter notes counted, one line
  per plan, and the library check
- samples/<path>/<planId>.json: the raw plan detail, to build tests from

Library check: a library plan (plans/) whose plan_id is in a retailer's Energy
Made Easy listing is always converted too, and priced against the library copy
(weekday and Sunday, summer and winter, before and after any caps). Differences
are printed: the library plans were checked by hand, so this is a real-world test
of the converter.

Nothing is committed: the workflow uploads --out as a run artifact (plan data is
for checking the converter, not for the repo).

    python scripts/check_cdr_conversion.py --out cdr-check --per-path 3
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_eme_retailers as eme  # noqa: E402  (http_json, brand list; also puts the engine on sys.path)
from _common import plan_files  # noqa: E402
from engine import Resolver, load_plan, validate_plan  # noqa: E402
from engine.importers.cdr import CdrImportError, convert  # noqa: E402


def reason_key(text: str) -> str:
    """A skip reason without the plan label, so the same reason counts together."""
    reason = text.split("): ", 1)[-1] if "): " in text else text
    return re.sub(r"'[^']*'|\d+(\.\d+)?", "…", reason)


MAX_DIFF_LINES = 200  # per library plan; differences are already merged into time ranges


def base_id(plan_id: str) -> str:
    """'COV1117612MRE3@EME' -> 'COV1117612MRE3' (library plans keep the id without the suffix)."""
    return (plan_id or "").split("@", 1)[0]


def library_plans() -> dict[str, dict[str, Any]]:
    """Library plans by plan_id."""
    out: dict[str, dict[str, Any]] = {}
    for path in plan_files():
        plan = json.loads(path.read_text(encoding="utf-8"))
        if plan.get("plan_id"):
            out[base_id(plan["plan_id"])] = plan
    return out


def compare_plans(library: dict[str, Any], converted: dict[str, Any], today: date) -> list[str]:
    """Where the converted plan prices differently from the library plan (empty: they agree).

    Every half hour of a Wednesday and a Sunday, in January and July, import and export,
    with nothing used and with every cap used up. Times are in the library plan's zone.
    Neighbouring half hours with the same two rates are one line ("15:00-21:00"), so the
    whole comparison fits in the log.
    """
    diffs: list[str] = []
    if abs(library["supply_charge"]["rate"] - converted["supply_charge"]["rate"]) > 0.0005:
        diffs.append(f"supply charge {library['supply_charge']['rate']} vs {converted['supply_charge']['rate']}")
    lib, conv = Resolver(load_plan(library)), Resolver(load_plan(converted))
    zone = ZoneInfo(library["timezone"])
    days = []
    for month in (1, 7):
        first = date(today.year + 1, month, 1)
        days.append(first + timedelta(days=(2 - first.weekday()) % 7))  # Wednesday
        days.append(first + timedelta(days=(6 - first.weekday()) % 7))  # Sunday
    for stream in ("import", "export"):
        for day in days:
            for label, used in (("", None), (" (caps used)", "all")):
                lib_used = dict.fromkeys(_period_ids(library, stream), 1e9) if used else None
                conv_used = dict.fromkeys(_period_ids(converted, stream), 1e9) if used else None
                run: tuple[int, int, float, float] | None = None  # start, end (half hours), rates
                runs = []
                for half_hour in range(48):
                    moment = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(minutes=30 * half_hour + 15)
                    a = lib.rate_at(stream, moment, lib_used).rate
                    b = conv.rate_at(stream, moment, conv_used).rate
                    if abs(a - b) <= 0.0005:
                        run = None
                        continue
                    if run and run[1] == half_hour and (run[2], run[3]) == (a, b):
                        run = (run[0], half_hour + 1, a, b)
                        runs[-1] = run
                    else:
                        run = (half_hour, half_hour + 1, a, b)
                        runs.append(run)
                for start, end, a, b in runs:
                    diffs.append(
                        f"{stream} {day:%a %d %b} {_hhmm(start)}-{_hhmm(end)}{label}: library {a} vs converted {b}"
                    )
    return diffs


def _hhmm(half_hours: int) -> str:
    return f"{half_hours // 2:02d}:{30 * (half_hours % 2):02d}"


def _period_ids(plan: dict[str, Any], stream: str) -> list[str]:
    return [p["id"] for p in plan[stream]["periods"]]


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


def check(
    fetch, out: Path, per_path: int, today: date, pause: float = eme.PAUSE_SECONDS, log=print,
    library: dict[str, dict[str, Any]] | None = None,
) -> dict:
    library = library_plans() if library is None else library
    results: list[dict[str, Any]] = []
    library_checks: list[dict[str, Any]] = []
    for uri in eme.unique_paths(eme.fallback_brands()):
        path = uri.rsplit("/", 1)[-1]
        try:
            listing = fetch(f"{uri}/cds-au/v1/energy/plans?fuelType=ALL&effective=CURRENT&page-size=1000&page=1",
                            {"x-v": "1"})
        except Exception as err:  # noqa: BLE001
            results.append({"path": path, "outcome": "list_failed", "detail": str(err)})
            continue
        listed = (listing.get("data") or {}).get("plans") or []
        picked = pick_plans(listed, per_path)
        # Library plans listed here are always checked, and compared below
        picked += [p for p in listed if base_id(p.get("planId", "")) in library and p not in picked]
        for summary in picked:
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
            for fit in contract.get("solarFeedInTariff") or []:
                # Feed-in whose steps may be only in its text (CDS standards-maintenance #520): log it, so the
                # converter's reading of that text can be checked against real wording.
                if (fit.get("payerType") or "").upper() == "RETAILER" and re.search(r"\d", json.dumps(
                        {k: v for k, v in fit.items() if k in ("description", "displayName")})):
                    log(f"FEED-IN TEXT {plan_id}: {json.dumps(fit, ensure_ascii=False)}")
            try:
                conversion = convert(detail, today)
            except CdrImportError as err:
                results.append({**entry, "outcome": "error", "detail": str(err)})
                continue
            if not conversion.plans:
                results.append({**entry, "outcome": "skipped", "detail": "; ".join(conversion.skipped)})
                log(f"  skipped {plan_id}: {'; '.join(conversion.skipped)}")
                if any("feed-in" in reason for reason in conversion.skipped):
                    for fit in contract.get("solarFeedInTariff") or []:
                        log(f"FEED-IN SKIPPED {plan_id}: {json.dumps(fit, ensure_ascii=False)}")
                continue
            lib_plan = library.get(base_id(plan_id))
            if lib_plan is not None:
                match = next((p for p in conversion.plans if p.get("dnsp") == lib_plan.get("dnsp")), None)
                diffs = (compare_plans(lib_plan, match, today) if match
                         else [f"no converted plan for {lib_plan.get('dnsp')}"])
                zone_text = contract.get("timeZone") or "not given"
                library_checks.append({"plan_id": plan_id, "library": lib_plan.get("plan"),
                                       "time_zone": zone_text, "agrees": not diffs,
                                       "differences": diffs[:MAX_DIFF_LINES]})
                verdict = "agrees" if not diffs else f"{len(diffs)} differences"
                log(f"LIBRARY CHECK {plan_id}: {verdict} (plan time zone: {zone_text}, "
                    f"converted as {match['timezone'] if match else '-'}, library {lib_plan.get('timezone')})")
                for diff in diffs[:MAX_DIFF_LINES]:
                    log(f"    {diff}")
                if len(diffs) > MAX_DIFF_LINES:
                    log(f"    ... and {len(diffs) - MAX_DIFF_LINES} more")
            errors = []
            for plan in conversion.plans:
                errors += validate_plan(plan).errors
            if errors:
                log(f"  invalid {plan_id}: {'; '.join(errors[:5])}")
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
    notes = Counter(reason_key(n) for r in results for n in r.get("notes") or [])
    report = {
        "checked": today.isoformat(),
        "plans_tried": len(results),
        "outcomes": dict(outcomes.most_common()),
        "pricing_models": dict(Counter(str(r.get("pricing_model")) for r in results).most_common()),
        "time_zones": dict(Counter(str(r.get("time_zone")) for r in results).most_common()),
        "reasons": dict(reasons.most_common()),
        "notes": dict(notes.most_common()),
        "library_checks": library_checks,
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
    print("Converter notes:")
    for note, count in list(report["notes"].items())[:20]:
        print(f"  {count:4d}  {note}")
    checks = report["library_checks"]
    print(f"Library check: {sum(c['agrees'] for c in checks)} of {len(checks)} library plans agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
