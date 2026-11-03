#!/usr/bin/env python3
"""Convert energy-manager-tariffs plan files into ha_tariff_plan files.

    python scripts/import_energy_manager.py path/to/Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json
    python scripts/import_energy_manager.py --write tariffs/*.json   # into plans/<country>/<region>/

Without --write the converted plan is printed. Each result is validated;
files that fail are reported and not written. Existing files are never
overwritten unless --force is given. Converted plans are marked
needs_review when the source format left something ambiguous.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import PLANS_DIR, dump_json, load_json, plan_relpath, validate_plan

from engine.importers.energy_manager import EnergyManagerImportError, convert


def source_slug(path: Path) -> str | None:
    """The plan slug from an Energy Manager file name (second '-' segment)."""
    parts = path.stem.split("-")
    return parts[1] if len(parts) >= 5 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--write", action="store_true", help="write into plans/")
    parser.add_argument("--force", action="store_true", help="overwrite existing plan files")
    args = parser.parse_args()

    failed = 0
    for path in args.files:
        try:
            plan = convert(load_json(path), path.name)
        except (EnergyManagerImportError, ValueError, KeyError) as err:
            print(f"SKIP  {path.name}: {err}", file=sys.stderr)
            failed += 1
            continue
        result = validate_plan(plan)
        if not result.ok:
            print(f"FAIL  {path.name}: " + "; ".join(result.errors), file=sys.stderr)
            failed += 1
            continue
        if not args.write:
            sys.stdout.write(dump_json(plan))
            continue
        target = PLANS_DIR / plan_relpath(plan, source_slug(path))
        if target.exists() and not args.force:
            print(f"EXISTS {target.relative_to(PLANS_DIR.parent)} (use --force to overwrite)", file=sys.stderr)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_json(plan), encoding="utf-8")
        flag = " (needs review)" if plan.get("needs_review") else ""
        print(f"WROTE {target.relative_to(PLANS_DIR.parent)}{flag}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
