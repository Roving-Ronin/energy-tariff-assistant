#!/usr/bin/env python3
"""Validate every plan in plans/ (or the files given).

Checks each plan against the format rules, its folder and file name, and
looks for two files describing the same plan over overlapping dates.
Exits 1 if any error is found; warnings are printed but do not fail.

    python scripts/validate_plans.py
    python scripts/validate_plans.py plans/AU/NSW/Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from _common import PLANS_DIR, filename_problems, load_json, plan_files, validate_plan


def _overlaps(a: dict, b: dict) -> bool:
    a_end = date.fromisoformat(a["effective_to"]) if a.get("effective_to") else date.max
    b_end = date.fromisoformat(b["effective_to"]) if b.get("effective_to") else date.max
    return date.fromisoformat(a["effective_from"]) <= b_end and date.fromisoformat(b["effective_from"]) <= a_end


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args()

    paths = [p.resolve() for p in args.files] or plan_files()
    errors = warnings = 0
    valid: list[tuple[Path, dict]] = []
    today = date.today()

    for path in paths:
        rel = path.relative_to(PLANS_DIR.parent) if path.is_relative_to(PLANS_DIR.parent) else path
        try:
            plan = load_json(path)
        except (OSError, json.JSONDecodeError) as err:
            print(f"ERROR {rel}: cannot read: {err}")
            errors += 1
            continue
        result = validate_plan(plan, today=today)
        problems = list(result.errors)
        if result.ok and path.is_relative_to(PLANS_DIR):
            problems += [f"name: {p}" for p in filename_problems(path, plan)]
        for problem in problems:
            print(f"ERROR {rel}: {problem}")
        for warning in result.warnings:
            print(f"WARN  {rel}: {warning}")
        errors += len(problems)
        warnings += len(result.warnings)
        if not problems:
            valid.append((path, plan))

    for i, (path_a, a) in enumerate(valid):
        for path_b, b in valid[i + 1:]:
            same = all(a.get(k) == b.get(k) for k in ("country", "region", "retailer", "plan", "dnsp"))
            if same and _overlaps(a, b):
                print(f"WARN  {path_a.name} and {path_b.name}: same plan with overlapping effective dates")
                warnings += 1

    print(f"{len(paths)} plan(s) checked: {errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
