"""Shared helpers for the repo scripts. Not part of the integration."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PLANS_DIR = REPO_ROOT / "plans"
INDEX_PATH = PLANS_DIR / "index.json"
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "energy_tariff_assistant"

# Import the engine as a top-level package so Home Assistant is not needed.
sys.path.insert(0, str(INTEGRATION_DIR))

from engine import validate_plan  # noqa: E402,F401  (re-exported for scripts)


def slug(text: str | None) -> str:
    """Letters and digits only, keeping case: 'Essential Energy' -> 'EssentialEnergy'."""
    return re.sub(r"[^A-Za-z0-9]", "", text or "")


def plan_filename(plan: dict[str, Any], plan_slug: str | None = None) -> str:
    """<Retailer>-<PlanSlug>-<Region>-<DNSP>-<YYYYMMDD>.json (DNSP omitted if none)."""
    parts = [slug(plan["retailer"]), plan_slug or slug(plan["plan"]), slug(plan["region"])]
    if plan.get("dnsp"):
        parts.append(slug(plan["dnsp"]))
    parts.append(plan["effective_from"].replace("-", ""))
    return "-".join(parts) + ".json"


def plan_relpath(plan: dict[str, Any], plan_slug: str | None = None) -> Path:
    return Path(plan["country"]) / plan["region"] / plan_filename(plan, plan_slug)


def filename_problems(path: Path, plan: dict[str, Any]) -> list[str]:
    """Check a library file's folder and name against its contents.

    The plan slug is free (contributors may shorten long plan names), so
    only retailer, region, DNSP and date are compared.
    """
    problems = []
    rel = path.relative_to(PLANS_DIR)
    if len(rel.parts) != 3:
        return [f"must sit at plans/<country>/<region>/<file>, not plans/{rel}"]
    country, region, name = rel.parts
    if country != plan.get("country"):
        problems.append(f"folder country {country!r} does not match country {plan.get('country')!r}")
    if region != plan.get("region"):
        problems.append(f"folder region {region!r} does not match region {plan.get('region')!r}")

    dnsp = rf"-{re.escape(slug(plan.get('dnsp')))}" if plan.get("dnsp") else ""
    pattern = (
        rf"^{re.escape(slug(plan.get('retailer')))}-[A-Za-z0-9]+-{re.escape(slug(plan.get('region')))}"
        rf"{dnsp}-{re.escape(str(plan.get('effective_from', '')).replace('-', ''))}\.json$"
    )
    if not re.match(pattern, name):
        problems.append(f"file name should be {plan_filename(plan, '<PlanSlug>')}")
    return problems


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def plan_files() -> list[Path]:
    return sorted(p for p in PLANS_DIR.rglob("*.json") if p != INDEX_PATH)
