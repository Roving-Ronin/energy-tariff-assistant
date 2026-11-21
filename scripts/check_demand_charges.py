#!/usr/bin/env python3
"""How Energy Made Easy plans lay out their demand charges: a sample to build the converter on.

For every retailer path, takes up to --per-path current electricity plans with "demand" in their name (plus any
--plan IDs asked for), fetches each plan's detail (x-v 3) and records where its demand charges are (a tariff period
of type `demandCharges`, or a `demandCharges` list beside the usage rates) and their fields. Writes to --out:

- demand.json: every demand charge found, with its plan, and the values each field takes, counted
- samples/<path>/<planId>.json: the raw plan detail

Nothing is committed: the workflow uploads --out as a run artifact.

    python scripts/check_demand_charges.py --out demand-check --per-path 3 --plan RED552428MRE21@EME
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_eme_retailers as eme  # noqa: E402

FIELDS = ("measureUnit", "measurementPeriod", "chargePeriod", "days", "startTime", "endTime", "minDemand",
          "maxDemand", "timeOfUseType")


def demand_charges(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Every demand charge in a contract, with where it was found."""
    found = []
    for i, period in enumerate(contract.get("tariffPeriod") or []):
        where = f"tariffPeriod[{i}] ({period.get('rateBlockUType')}, {period.get('displayName')!r}, " \
                f"{period.get('startDate')}..{period.get('endDate')})"
        for charge in period.get("demandCharges") or []:
            found.append({"where": where, **charge})
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("demand-check"))
    parser.add_argument("--per-path", type=int, default=3)
    parser.add_argument("--plan", action="append", default=[], help="plan ID to include, e.g. RED552428MRE21@EME")
    args = parser.parse_args()
    wanted = {p if "@" in p else f"{p}@EME" for p in args.plan}

    charges: list[dict[str, Any]] = []
    plans_seen = 0
    for uri in eme.unique_paths(eme.fallback_brands()):
        path = uri.rsplit("/", 1)[-1]
        try:
            listing = eme.http_json(
                f"{uri}/cds-au/v1/energy/plans?fuelType=ALL&effective=CURRENT&page-size=1000&page=1", {"x-v": "1"})
        except Exception as err:  # noqa: BLE001
            print(f"{path}: plan list failed: {err}")
            continue
        plans = [p for p in (listing.get("data") or {}).get("plans") or []
                 if (p.get("fuelType") or "").upper() in ("ELECTRICITY", "DUAL")]
        named = [p for p in plans if "demand" in (p.get("displayName") or "").lower()][: args.per_path]
        picked = {p["planId"]: p for p in named} | {p["planId"]: p for p in plans if p["planId"] in wanted}
        for plan_id, summary in picked.items():
            try:
                detail = eme.http_json(f"{uri}/cds-au/v1/energy/plans/{quote(plan_id, safe='@')}", {"x-v": "3"})
            except Exception as err:  # noqa: BLE001
                print(f"{path} {plan_id}: detail failed: {err}")
                continue
            plans_seen += 1
            sample = args.out / "samples" / path / f"{plan_id}.json"
            sample.parent.mkdir(parents=True, exist_ok=True)
            sample.write_text(json.dumps(detail, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            contract = (detail.get("data") or {}).get("electricityContract") or {}
            found = demand_charges(contract)
            print(f"{path} {plan_id} {summary.get('displayName')!r}: {len(found)} demand charge(s), "
                  f"timeZone {contract.get('timeZone')!r}")
            for charge in found:
                print("   ", json.dumps(charge, ensure_ascii=False))
                charges.append({"path": path, "plan_id": plan_id, "plan": summary.get("displayName"), **charge})
            time.sleep(eme.PAUSE_SECONDS)

    values = {f: dict(Counter(json.dumps(c.get(f)) for c in charges).most_common()) for f in FIELDS}
    where = dict(Counter(c["where"].split(" (")[1].split(",")[0] for c in charges).most_common())
    report = {"plans_checked": plans_seen, "demand_charges": len(charges), "found_in": where,
              "field_values": values, "charges": charges}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "demand.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("plans_checked", "demand_charges", "found_in", "field_values")},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
