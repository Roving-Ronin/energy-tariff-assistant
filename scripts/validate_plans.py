#!/usr/bin/env python3
"""Validate every plan in plans/ (or the files given).

Checks each plan against the format rules, its folder and file name, and
looks for two files describing the same plan over overlapping dates.
Exits 1 if any error is found; warnings are printed but do not fail.

    python scripts/validate_plans.py
    python scripts/validate_plans.py plans/AU/NSW/CovaU-SolarMaxTOU-NSW-EssentialEnergy-20260701.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from _common import PLANS_DIR, filename_problems, load_json, plan_files, validate_plan


def _overlaps(a: dict, b: dict) -> bool:
    a_end = date.fromisoformat(a["effective_to"]) if a.get("effective_to") else date.max
    b_end = date.fromisoformat(b["effective_to"]) if b.get("effective_to") else date.max
    return date.fromisoformat(a["effective_from"]) <= b_end and date.fromisoformat(b["effective_from"]) <= a_end


@dataclass
class FileResult:
    path: Path
    rel: str
    plan: dict | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_files(paths: list[Path], today: date | None = None) -> list[FileResult]:
    """Validate each file: its contents, folder and name, and overlaps with other plans."""
    today = today or date.today()
    results: list[FileResult] = []
    for path in paths:
        rel = str(path.relative_to(PLANS_DIR.parent) if path.is_relative_to(PLANS_DIR.parent) else path)
        result = FileResult(path, rel)
        results.append(result)
        try:
            plan = load_json(path)
        except (OSError, json.JSONDecodeError) as err:
            result.errors.append(f"cannot read: {err}")
            continue
        result.plan = plan
        checked = validate_plan(plan, today=today)
        result.errors += list(checked.errors)
        if checked.ok and path.is_relative_to(PLANS_DIR):
            result.errors += [f"name: {p}" for p in filename_problems(path, plan)]
        result.warnings += list(checked.warnings)

    valid = [r for r in results if r.plan is not None and not r.errors]
    for i, a in enumerate(valid):
        for b in valid[i + 1:]:
            same = all(a.plan.get(k) == b.plan.get(k) for k in ("country", "region", "retailer", "plan", "dnsp"))
            # EME gives a retailer's variants (with or without controlled load...) one name: different
            # plan IDs are different plans
            if a.plan.get("plan_id") and b.plan.get("plan_id") and a.plan["plan_id"] != b.plan["plan_id"]:
                same = False
            if same and _overlaps(a.plan, b.plan):
                message = "same plan as {} with overlapping effective dates"
                a.warnings.append(message.format(b.path.name))
                b.warnings.append(message.format(a.path.name))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args()

    paths = [p.resolve() for p in args.files] or plan_files()
    results = check_files(paths)
    for result in results:
        for problem in result.errors:
            print(f"ERROR {result.rel}: {problem}")
        for warning in result.warnings:
            print(f"WARN  {result.rel}: {warning}")
    errors = sum(len(r.errors) for r in results)
    warnings = sum(len(r.warnings) for r in results)
    print(f"{len(paths)} plan(s) checked: {errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
