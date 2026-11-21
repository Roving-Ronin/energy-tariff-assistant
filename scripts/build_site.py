#!/usr/bin/env python3
"""Build the plan browser (GitHub Pages) into a folder.

Copies the static page in site/, the library plans, and writes
data/library.json: every plan's index entry with its validation result,
so the page shows what the validator found without re-checking. Coverage
comes from plans/retailers.json (derived facts only).

    python scripts/build_site.py            # into _site/
    python scripts/build_site.py --out /tmp/site
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from _common import INTEGRATION_DIR, PLANS_DIR, REPO_ROOT, dump_json, load_json, plan_files
from validate_plans import check_files

SITE_DIR = REPO_ROOT / "site"
INDEX_FIELDS = (
    "country", "region", "dnsp", "retailer", "plan", "plan_id", "customer_type",
    "effective_from", "effective_to",
)


def library(results) -> dict:
    plans = []
    for result in results:
        plan = result.plan or {}
        entry = {key: plan.get(key) for key in INDEX_FIELDS}
        entry.update(
            file=result.rel,
            source_checked=(plan.get("source") or {}).get("checked"),
            needs_review=bool(plan.get("needs_review")),
            errors=result.errors,
            warnings=result.warnings,
        )
        plans.append(entry)
    return {
        "format": "ha_tariff_site",
        "version": 1,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plans": plans,
    }


def build(out: Path) -> dict:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SITE_DIR, out)
    shutil.copyfile(INTEGRATION_DIR / "brand" / "icon.png", out / "icon.png")
    results = check_files(plan_files())
    for result in results:
        target = out / result.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(result.path, target)
    data = out / "data"
    data.mkdir(exist_ok=True)
    lib = library(results)
    (data / "library.json").write_text(dump_json(lib), encoding="utf-8")
    retailers = PLANS_DIR / "retailers.json"
    if retailers.exists():
        coverage = load_json(retailers)
        slim = {k: coverage.get(k) for k in ("updated_at", "source", "retailers", "distributors")}
        (data / "coverage.json").write_text(json.dumps(slim, ensure_ascii=False), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return lib


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "_site")
    args = parser.parse_args()
    lib = build(args.out.resolve())
    errors = sum(len(p["errors"]) for p in lib["plans"])
    print(f"Site built in {args.out}: {len(lib['plans'])} plan(s), {errors} validation error(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
