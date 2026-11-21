#!/usr/bin/env python3
"""How Energy Made Easy versions plans: are price changes a new version of the same plan ID family?

Lists every retailer path's electricity plans with effective=ALL (past, current and future) and
groups them by family: the plan ID without its trailing version number (COV1117612MRE3 ->
COV1117612MRE). Logs counts and examples only (IDs, names, dates), no prices.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_eme_retailers as eme  # noqa: E402

VERSIONED = re.compile(r"^(.*?[A-Z])(\d+)@")


def main() -> int:
    families: dict[str, list[dict]] = defaultdict(list)
    names: dict[tuple[str, str], set[str]] = defaultdict(set)
    plans = unversioned = 0
    shapes: dict[str, list[str]] = defaultdict(list)
    ended = future = 0
    from datetime import date
    today = date.today().isoformat()
    for uri in eme.unique_paths(eme.fallback_brands()):
        try:
            listed = eme.brand_plans(eme.http_json, uri)
        except Exception as err:  # noqa: BLE001
            print(f"  {uri}: {err}")
            continue
        for p in listed:
            plans += 1
            if (p.get("effectiveTo") or "9999")[:10] < today:
                ended += 1
            if (p.get("effectiveFrom") or "")[:10] > today:
                future += 1
            m = VERSIONED.match(p.get("planId", ""))
            if not m:
                unversioned += 1
                shape = re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", p.get("planId", "")))
                if len(shapes[shape]) < 3:
                    shapes[shape].append(f"{p.get('planId')} {p.get('brand')} {p.get('displayName')!r}")
                continue
            families[m.group(1)].append(p)
            names[(p.get("brand") or "", p.get("displayName") or "")].add(m.group(1))
    multi = {f: v for f, v in families.items() if len({p["planId"] for p in v}) > 1}
    print(f"VERSIONS {plans} plans, {len(families)} families, {unversioned} IDs without a version number")
    print(f"VERSIONS {len(multi)} families listed with more than one version")
    print(f"VERSIONS {ended} plans already ended, {future} starting later")
    print(f"VERSIONS {len(shapes)} shapes of IDs without a version number:")
    for shape, examples in sorted(shapes.items(), key=lambda kv: -len(kv[1]))[:12]:
        print(f"  {shape}: " + " | ".join(examples))
    for family, versions in list(multi.items())[:25]:
        rows = sorted({(p["planId"], p.get("effectiveFrom", "")[:10], (p.get("effectiveTo") or "")[:10],
                        p.get("displayName")) for p in versions}, key=lambda r: r[1])
        print(f"  {family}: " + " | ".join(f"{i} {a}..{b or 'open'} {n!r}" for i, a, b, n in rows))
    renamed = {k: v for k, v in names.items() if len(v) > 1}
    print(f"VERSIONS {len(renamed)} (brand, plan name) pairs under more than one family")
    for (brand, name), fams in list(renamed.items())[:15]:
        print(f"  {brand} {name!r}: {sorted(fams)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
