"""Loading and checking plan files from the library, a path or pasted text."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import BUILT_PLANS_DIR, LIBRARY_BASE_URL, LIBRARY_INDEX
from .engine import Resolver, load_plan, validate_plan

TIMEOUT = aiohttp.ClientTimeout(total=20)


class PlanError(Exception):
    """A plan could not be fetched, read or validated. `key` is a flow error key."""

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(detail or key)
        self.key = key
        self.detail = detail


async def _fetch_json(hass: HomeAssistant, path: str) -> Any:
    session = async_get_clientsession(hass)
    try:
        async with session.get(LIBRARY_BASE_URL + path, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            return json.loads(await resp.text())
    except (aiohttp.ClientError, TimeoutError) as err:
        raise PlanError("cannot_connect", str(err)) from err
    except json.JSONDecodeError as err:
        raise PlanError("invalid_json", str(err)) from err


async def async_fetch_index(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The library index's plan entries."""
    index = await _fetch_json(hass, LIBRARY_INDEX)
    plans = index.get("plans") if isinstance(index, dict) else None
    if not isinstance(plans, list):
        raise PlanError("invalid_json", "index has no plan list")
    return plans


# The library's own fields: who confirmed a plan, problems reported, the plan that replaced a withdrawn one
LIBRARY_ONLY_FIELDS = ("confirmations", "problems", "replaced_by")


async def async_fetch_library_plan(hass: HomeAssistant, file: str, keep_feedback: bool = False) -> dict[str, Any]:
    """A plan file from the library, by its index `file` path, without the library's own fields (unless asked)."""
    if not file.startswith("plans/") or ".." in file:
        raise PlanError("invalid_plan", "not a library path")
    plan = await _fetch_json(hass, file)
    if isinstance(plan, dict) and not keep_feedback:
        for key in LIBRARY_ONLY_FIELDS:  # the library's, not the entry's
            plan.pop(key, None)
    return plan


def library_label(entry: dict[str, Any]) -> str:
    """Human label for an index entry."""
    dnsp = f" ({entry['dnsp']})" if entry.get("dnsp") else ""
    return (
        f"{entry.get('region', '?')} · {entry.get('retailer', '?')} · "
        f"{entry.get('plan', '?')}{dnsp} · from {entry.get('effective_from', '?')}"
    )


def parse_plan_text(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise PlanError("invalid_json", str(err)) from err
    if not isinstance(data, dict):
        raise PlanError("invalid_json", "top level must be an object")
    return data


def read_plan_file(config_dir: str, path: str) -> dict[str, Any]:
    """Read a plan file inside the config directory. Blocking: run in an executor."""
    base = Path(config_dir).resolve()
    target = (base / path).resolve()
    if base not in target.parents and target != base:
        raise PlanError("file_outside_config")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as err:
        raise PlanError("file_not_found", str(err)) from err
    return parse_plan_text(text)


def write_plan_file(config_dir: str, filename: str, plan: dict[str, Any]) -> str:
    """Save a plan under the config folder; returns its path relative to it. Blocking.

    An existing file of the same name is kept: the new one gets a -2, -3...
    suffix, so a built plan never overwrites another.
    """
    folder = Path(config_dir) / BUILT_PLANS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = filename.rsplit(".", 1) if "." in filename else (filename, "json")
    target = folder / f"{stem}.{suffix}"
    n = 2
    while target.exists():
        target = folder / f"{stem}-{n}.{suffix}"
        n += 1
    try:
        target.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as err:
        raise PlanError("file_write_failed", str(err)) from err
    return str(target.relative_to(Path(config_dir)))


def check_plan(data: dict[str, Any]) -> Resolver:
    """Validate a plan and build its resolver.

    Blocking (time zone and holiday data are read from disk): run in an
    executor.
    """
    result = validate_plan(data)
    if not result.ok:
        raise PlanError("invalid_plan", "; ".join(result.errors[:3]))
    resolver = Resolver(load_plan(data))
    prewarm_holidays(resolver)
    return resolver


def prewarm_holidays(resolver: Resolver, year: int | None = None) -> None:
    """Load holiday calendars for `year` and the next. Blocking."""
    from datetime import date  # noqa: PLC0415

    year = year or date.today().year
    for plan in resolver.versions:
        for y in (year, year + 1):
            resolver.is_holiday(plan, date(y, 1, 1))
