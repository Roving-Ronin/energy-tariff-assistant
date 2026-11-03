#!/usr/bin/env python3
"""Rebuild plans/index.json from the plan files.

    python scripts/build_index.py          # write the index
    python scripts/build_index.py --check  # exit 1 if the index is out of date
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from _common import INDEX_PATH, PLANS_DIR, dump_json, load_json, plan_files

INDEX_FIELDS = (
    "country", "region", "dnsp", "retailer", "plan", "plan_id",
    "effective_from", "effective_to",
)


def build_entries() -> list[dict]:
    entries = []
    for path in plan_files():
        plan = load_json(path)
        entry = {"file": f"plans/{path.relative_to(PLANS_DIR).as_posix()}"}
        entry.update({key: plan.get(key) for key in INDEX_FIELDS})
        entry["source_checked"] = (plan.get("source") or {}).get("checked")
        entries.append(entry)
    entries.sort(key=lambda e: (e["country"], e["region"], e["retailer"], e["plan"], e["dnsp"] or "", e["effective_from"]))
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="only check the index is current")
    args = parser.parse_args()

    entries = build_entries()
    if args.check:
        current = load_json(INDEX_PATH) if INDEX_PATH.exists() else {}
        if current.get("plans") != entries:
            print("plans/index.json is out of date; run scripts/build_index.py", file=sys.stderr)
            return 1
        print(f"plans/index.json is current ({len(entries)} plans)")
        return 0

    index = {
        "format": "ha_tariff_index",
        "version": 1,
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "plans": entries,
    }
    INDEX_PATH.write_text(dump_json(index), encoding="utf-8")
    print(f"wrote {INDEX_PATH.relative_to(PLANS_DIR.parent)} ({len(entries)} plans)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
