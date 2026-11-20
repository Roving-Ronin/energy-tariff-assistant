#!/usr/bin/env python3
"""Build plans/retailers.json: which retailers sell electricity in which distributor areas.

Source: the AER's Energy Product Reference Data (Energy Made Easy, Consumer Data
Right "Get Generic Plans"), which covers the National Energy Customer Framework
states (and Victoria, from DEECA). Public and anonymous; one call per retailer
brand, `x-v: 1`, up to 1000 plans a page.

Only derived facts are stored: retailer, distributor, state and plan counts. No
plan text or prices are copied.

    python scripts/fetch_eme_retailers.py            # fetch and write plans/retailers.json
    python scripts/fetch_eme_retailers.py --check    # fetch; exit 1 if the file would change

Retailer list: the ACCC CDR Register's energy data holder brands (their product
base URIs), falling back to scripts/eme_brands.json (the AER's published list)
if the Register can't be read. Distributor names in the plan data are mapped to
our DNSP names and states by DISTRIBUTORS; names it doesn't know are kept,
listed under "unmapped_distributors", and printed, so the map can be extended.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time
from typing import Any
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "plans" / "retailers.json"
FALLBACK_BRANDS = Path(__file__).resolve().parent / "eme_brands.json"

REGISTER_URL = "https://api.cdr.gov.au/cdr-register/v1/energy/data-holders/brands/summary"
EME_HOST = "https://cdr.energymadeeasy.gov.au"
PAGE_SIZE = 1000
USER_AGENT = "energy-tariff-assistant (+https://github.com/Roving-Ronin/energy-tariff-assistant)"
PAUSE_SECONDS = 0.5  # between requests: be polite to a public service
RETRIES = 4

# Lower-case keyword found in a distributor name -> (DNSP name as used in plans, state)
DISTRIBUTORS: list[tuple[str, str, str]] = [
    ("ausgrid", "Ausgrid", "NSW"),
    ("endeavour", "Endeavour Energy", "NSW"),
    ("essential", "Essential Energy", "NSW"),
    ("evoenergy", "Evoenergy", "ACT"),
    ("actewagl distribution", "Evoenergy", "ACT"),
    ("energex", "Energex", "QLD"),
    ("ergon", "Ergon Energy", "QLD"),
    ("citipower", "CitiPower", "VIC"),
    ("powercor", "Powercor", "VIC"),
    ("jemena", "Jemena", "VIC"),
    ("united", "United Energy", "VIC"),
    ("ausnet", "AusNet Services", "VIC"),
    ("sa power", "SA Power Networks", "SA"),
    ("sapn", "SA Power Networks", "SA"),
    ("tasnetworks", "TasNetworks", "TAS"),
    ("tas networks", "TasNetworks", "TAS"),
]

Fetcher = Callable[[str, dict[str, str]], Any]


def map_distributor(name: str) -> tuple[str, str] | None:
    """(DNSP, state) for a distributor name from the plan data, or None if unknown."""
    lower = name.lower()
    for keyword, dnsp, state in DISTRIBUTORS:
        if keyword in lower:
            return dnsp, state
    return None


def http_json(url: str, headers: dict[str, str]) -> Any:
    """GET JSON with retries on 429/5xx and network errors. https only."""
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT, **headers}
    )
    delay = 2.0
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https only
                return json.load(response)
        except urllib.error.HTTPError as err:
            if err.code not in (429, 500, 502, 503, 504) or attempt == RETRIES - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == RETRIES - 1:
                raise
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def register_brands(fetch: Fetcher) -> list[dict[str, str]]:
    """Energy brands from the CDR Register whose product data is on Energy Made Easy."""
    brands: list[dict[str, str]] = []
    page = 1
    while True:
        body = fetch(f"{REGISTER_URL}?page={page}&page-size=1000", {"x-v": "1"})
        for item in body.get("data", []):
            base = (item.get("productBaseUri") or "").rstrip("/")
            if base.startswith(EME_HOST + "/"):
                brands.append({"name": item.get("brandName") or base.rsplit("/", 1)[-1], "uri": base, "brand": ""})
        total = int((body.get("meta") or {}).get("totalPages") or 1)
        if page >= total:
            break
        page += 1
    return brands


def fallback_brands() -> list[dict[str, str]]:
    """The AER's list: several brands can share one path (e.g. energy-locals)."""
    data = json.loads(FALLBACK_BRANDS.read_text(encoding="utf-8"))
    return [{"name": b["name"], "uri": f"{EME_HOST}/{b['path']}", "brand": b["brand"]} for b in data["brands"]]


def unique_paths(brands: list[dict[str, str]]) -> list[str]:
    """Each plans endpoint once, even when several brands share it."""
    return sorted({b["uri"] for b in brands})


def known_names(brands: list[dict[str, str]]) -> dict[str, str]:
    """CDR brand id -> retailer name, where the brand list says."""
    return {b["brand"]: b["name"] for b in brands if b.get("brand")}


def brand_plans(fetch: Fetcher, uri: str, pause: float = PAUSE_SECONDS) -> list[dict[str, Any]]:
    """Every current or future electricity (or dual-fuel) plan at one path (all pages)."""
    plans: list[dict[str, Any]] = []
    page = 1
    while True:
        # fuelType=ALL: dual-fuel plans also sell electricity; gas-only plans are dropped below
        url = f"{uri}/cds-au/v1/energy/plans?fuelType=ALL&effective=ALL&page-size={PAGE_SIZE}&page={page}"
        body = fetch(url, {"x-v": "1"})
        plans += [
            p for p in (body.get("data") or {}).get("plans") or []
            if (p.get("fuelType") or "").upper() != "GAS"
        ]
        total = int((body.get("meta") or {}).get("totalPages") or 1)
        if page >= total:
            return plans
        page += 1
        time.sleep(pause)


def summarise(
    name: str, cdr_path: str, cdr_brand: str, plans: list[dict[str, Any]], unmapped: set[str]
) -> dict[str, Any] | None:
    """One retailer brand's coverage: DNSP -> state and residential/business plan counts."""
    coverage: dict[str, dict[str, Any]] = {}
    for plan in plans:
        customer = "business" if (plan.get("customerType") or "").upper() == "BUSINESS" else "residential"
        for raw in (plan.get("geography") or {}).get("distributors") or []:
            mapped = map_distributor(raw)
            if mapped is None:
                unmapped.add(raw)
                dnsp, state = raw, None
            else:
                dnsp, state = mapped
            entry = coverage.setdefault(dnsp, {"state": state, "residential": 0, "business": 0})
            entry[customer] += 1
    if not coverage:
        return None
    return {
        "name": name,
        "cdr_path": cdr_path,
        "cdr_brand": cdr_brand,
        "plans": len(plans),
        "states": sorted({c["state"] for c in coverage.values() if c["state"]}),
        "distributors": dict(sorted(coverage.items())),
    }


def merge_by_name(retailers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per retailer name, even when its plans sit under several paths or brands."""
    merged: dict[str, dict[str, Any]] = {}
    for r in retailers:
        m = merged.get(r["name"])
        if m is None:
            merged[r["name"]] = {**r, "cdr_paths": [r["cdr_path"]], "cdr_brands": [r["cdr_brand"]],
                                 "distributors": {k: dict(v) for k, v in r["distributors"].items()}}
            continue
        m["plans"] += r["plans"]
        m["cdr_paths"] = sorted(set(m["cdr_paths"]) | {r["cdr_path"]})
        m["cdr_brands"] = sorted(set(m["cdr_brands"]) | {r["cdr_brand"]})
        m["states"] = sorted(set(m["states"]) | set(r["states"]))
        for dnsp, cov in r["distributors"].items():
            slot = m["distributors"].setdefault(dnsp, {"state": cov["state"], "residential": 0, "business": 0})
            slot["residential"] += cov["residential"]
            slot["business"] += cov["business"]
        m["distributors"] = dict(sorted(m["distributors"].items()))
    for m in merged.values():
        m.pop("cdr_path", None)
        m.pop("cdr_brand", None)
    return list(merged.values())


def build(fetch: Fetcher, pause: float = PAUSE_SECONDS, log=print) -> dict[str, Any]:
    try:
        brands = register_brands(fetch)
        brand_source = "ACCC CDR Register"
    except Exception as err:  # noqa: BLE001 - any failure: use the AER list instead
        log(f"CDR Register unavailable ({err}); using {FALLBACK_BRANDS.name}")
        brands, brand_source = [], ""
    if not brands:
        if brand_source:
            log(f"CDR Register listed no Energy Made Easy brands; using {FALLBACK_BRANDS.name}")
        brands, brand_source = fallback_brands(), "AER retailer base URI list"

    names = known_names(brands)
    path_names = {b["uri"]: b["name"] for b in brands}
    retailers, unmapped, failed, empty = [], set(), [], []
    for uri in unique_paths(brands):
        cdr_path = uri.rsplit("/", 1)[-1]
        try:
            plans = brand_plans(fetch, uri, pause)
        except Exception as err:  # noqa: BLE001 - one bad path mustn't stop the rest
            failed.append(f"{cdr_path}: {err}")
            log(f"! {cdr_path}: {err}")
            continue
        if not plans:
            empty.append(cdr_path)
            log(f"- {cdr_path}: no electricity plans")
        # One path can serve several brands: split by each plan's own brand
        groups: dict[str, list[dict[str, Any]]] = {}
        seen_ids: set[str] = set()
        for plan in plans:
            if (pid := plan.get("planId")) and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            groups.setdefault((plan.get("brand") or cdr_path).strip(), []).append(plan)
        for cdr_brand, group in sorted(groups.items()):
            brand_names = sorted({p["brandName"] for p in group if p.get("brandName")})
            name = names.get(cdr_brand) or (brand_names[0] if brand_names else path_names.get(uri, cdr_brand))
            summary = summarise(name, cdr_path, cdr_brand, group, unmapped)
            log(f"{name} ({cdr_path}/{cdr_brand}): {len(group)} electricity plans")
            if summary:
                retailers.append(summary)
        time.sleep(pause)

    retailers = merge_by_name(retailers)
    by_dnsp: dict[str, dict[str, Any]] = {}
    for retailer in retailers:
        for dnsp, cov in retailer["distributors"].items():
            slot = by_dnsp.setdefault(dnsp, {"state": cov["state"], "retailers": []})
            slot["retailers"].append(retailer["name"])
    for slot in by_dnsp.values():
        slot["retailers"].sort(key=str.lower)

    return {
        "format": "eta_retailers",
        "version": 1,
        "source": "AER Energy Product Reference Data (Energy Made Easy, Consumer Data Right), current electricity plans",
        "brand_list": brand_source,
        "retailers": sorted(retailers, key=lambda r: r["name"].lower()),
        "distributors": dict(sorted(by_dnsp.items())),
        "unmapped_distributors": sorted(unmapped),
        "failed_brands": failed,
        "paths_without_electricity_plans": sorted(empty),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if plans/retailers.json would change")
    args = parser.parse_args()

    data = build(http_json)
    old = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    changed = {k: v for k, v in old.items() if k != "updated_at"} != data
    data_out = {"format": data["format"], "version": data["version"],
                "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), **data}
    if data["unmapped_distributors"]:
        print("Unmapped distributor names (add them to DISTRIBUTORS):", ", ".join(data["unmapped_distributors"]))
    if not data["retailers"]:
        print("No retailers with electricity plans were found; not writing.", file=sys.stderr)
        return 2
    if args.check:
        print("changed" if changed else "unchanged")
        return 1 if changed else 0
    if changed:
        OUTPUT.write_text(json.dumps(data_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}: {len(data['retailers'])} retailers")
    else:
        print("plans/retailers.json is current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
