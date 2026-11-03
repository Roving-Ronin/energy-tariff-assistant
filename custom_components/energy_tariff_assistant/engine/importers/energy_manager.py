"""Import energy_manager_custom_tariff v1 files.

These are the plan files in ha-energymanager/energy-manager-tariffs: a
flat dump of Energy Manager's input_number / input_datetime /
input_boolean helpers with fixed slots (3 peak, 3 offpeak, 3 shoulder,
1 additional, 1 free, 3 export).

Conversion rules (see the design spec, "Importers"):
- Each enabled slot becomes a window of a period named after the slot.
- An enabled slot with 00:00-00:00 means all day.
- The import or export period covering the most time becomes the default
  period (its windows are dropped, it covers whatever is left). Any time
  no slot covered then falls to it, and the plan is marked needs_review.
- The free slot is rate 0, capped at other_free_kwh, then the default
  import period's rate.
- An export slot with other_export_N_kwh > 0 is capped: after the cap
  the default export period's rate applies, or $0 if the capped slot is
  itself the only export slot. Capped exports are marked needs_review
  because the source format does not say what happens after the cap.
- Battery and control settings (*_battery_*, *_restrict*, *_force_import,
  *_limit, *_export toggles) are dropped: they are not tariff data.
"""

from __future__ import annotations

from typing import Any

from ..model import FORMAT, MINUTES_PER_DAY, VERSION, Window

SOURCE_FORMAT = "energy_manager_custom_tariff"

STATE_TIMEZONES = {
    "NSW": "Australia/Sydney",
    "ACT": "Australia/Sydney",
    "VIC": "Australia/Melbourne",
    "QLD": "Australia/Brisbane",
    "SA": "Australia/Adelaide",
    "TAS": "Australia/Hobart",
    "WA": "Australia/Perth",
    "NT": "Australia/Darwin",
}

DNSP_NAMES = {
    "EssentialEnergy": "Essential Energy",
    "Endeavour": "Endeavour Energy",
    "EndeavourEnergy": "Endeavour Energy",
    "Ausgrid": "Ausgrid",
    "Energex": "Energex",
    "Ergon": "Ergon Energy",
    "TasNetworks": "TasNetworks",
    "SAPN": "SA Power Networks",
    "Evoenergy": "Evoenergy",
    "Citipower": "CitiPower",
    "Powercor": "Powercor",
    "Jemena": "Jemena",
    "UnitedEnergy": "United Energy",
    "AusNet": "AusNet Services",
}

IMPORT_SLOTS = (
    # (slot prefix, slot numbers, period id, period name)
    ("peak", (1, 2, 3), "peak", "Peak"),
    ("shoulder", (1, 2, 3), "shoulder", "Shoulder"),
    ("offpeak", (1, 2, 3), "offpeak", "Off-peak"),
    ("additional", (1,), "additional", "Additional"),
)


class EnergyManagerImportError(ValueError):
    """The input is not an energy_manager_custom_tariff v1 file."""


def _entity(entities: dict[str, Any], kind: str, name: str, default: Any = None) -> Any:
    return entities.get(f"input_{kind}.{name}", default)


def _window(start: str, end: str) -> dict[str, Any]:
    start_hm, end_hm = start[:5], end[:5]
    if start_hm == end_hm:
        return {"days": "all", "all_day": True}
    if end_hm == "00:00":
        end_hm = "24:00"
    return {"days": "all", "start": start_hm, "end": end_hm}


def _covered_minutes(windows: list[dict[str, Any]]) -> set[int]:
    minutes: set[int] = set()
    for data in windows:
        window = Window.from_dict(data)
        for lo, hi in window.segments():
            minutes.update(range(lo, hi))
    return minutes


def _choose_default(
    periods: list[dict[str, Any]], also_covered: set[int] | None = None
) -> tuple[dict[str, Any], bool]:
    """Make the period covering the most time the default.

    Returns the default period and whether some minutes were not covered
    by any slot (and so fell to the default). `also_covered` adds minutes
    claimed by periods that cannot be the default (the free window).
    """
    coverage = {p["id"]: _covered_minutes(p["windows"]) for p in periods}
    all_covered = set().union(also_covered or set(), *coverage.values())
    gaps = len(all_covered) < MINUTES_PER_DAY
    best = max(periods, key=lambda p: len(coverage[p["id"]]))
    best.pop("windows")
    best["default"] = True
    return best, gaps


def _ordered(period: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "name", "rate", "tiers", "tier_reset", "windows", "default")
    return {k: period[k] for k in keys if k in period}


def convert(data: dict[str, Any], filename: str | None = None) -> dict[str, Any]:
    """Convert one Energy Manager plan dict to an ha_tariff_plan dict."""
    if data.get("format") != SOURCE_FORMAT or data.get("version") != 1:
        raise EnergyManagerImportError(f"not an {SOURCE_FORMAT} v1 file")
    entities = data.get("entities")
    if not isinstance(entities, dict):
        raise EnergyManagerImportError("missing entities block")

    state = data.get("state", "")
    review: list[str] = []

    # ----- import --------------------------------------------------------
    import_periods: list[dict[str, Any]] = []
    for prefix, numbers, pid, name in IMPORT_SLOTS:
        windows = []
        for number in numbers:
            if _entity(entities, "boolean", f"other_{prefix}_{number}"):
                windows.append(
                    _window(
                        _entity(entities, "datetime", f"other_{prefix}_{number}_start", "00:00:00"),
                        _entity(entities, "datetime", f"other_{prefix}_{number}_end", "00:00:00"),
                    )
                )
        if windows:
            import_periods.append(
                {
                    "id": pid,
                    "name": name,
                    "rate": float(_entity(entities, "number", f"other_{prefix}_tariff", 0)),
                    "windows": windows,
                }
            )

    free_period = None
    if _entity(entities, "boolean", "other_free_1"):
        free_period = {
            "id": "free",
            "name": "Free",
            "windows": [
                _window(
                    _entity(entities, "datetime", "other_free_1_start", "00:00:00"),
                    _entity(entities, "datetime", "other_free_1_end", "00:00:00"),
                )
            ],
        }
        import_periods.append(free_period)

    if not import_periods:
        raise EnergyManagerImportError("no import slots are enabled")

    candidates = [p for p in import_periods if p is not free_period]
    if not candidates:
        raise EnergyManagerImportError("only a free slot is enabled; no rate for the rest of the day")
    free_minutes = _covered_minutes(free_period["windows"]) if free_period else set()
    default_import, gaps = _choose_default(candidates, free_minutes)
    if gaps:
        review.append(f"some times had no import slot and now use {default_import['name']}")

    if free_period is not None:
        cap = float(_entity(entities, "number", "other_free_kwh", 0) or 0)
        if cap > 0:
            free_period["tiers"] = [
                {"up_to_kwh": cap, "rate": 0.0},
                {"use_period": default_import["id"]},
            ]
            free_period["tier_reset"] = "day"
            review.append(
                f"free import is capped at {cap:g} kWh/day, then {default_import['name']} rate is assumed"
            )
        else:
            free_period["rate"] = 0.0

    # ----- export --------------------------------------------------------
    export_periods: list[dict[str, Any]] = []
    caps: dict[str, float] = {}
    for number in (1, 2, 3):
        if not _entity(entities, "boolean", f"other_export_{number}"):
            continue
        pid = f"export_{number}"
        export_periods.append(
            {
                "id": pid,
                "name": f"Export {number}",
                "rate": float(_entity(entities, "number", f"other_export_{number}_tariff", 0)),
                "windows": [
                    _window(
                        _entity(entities, "datetime", f"other_export_{number}_start", "00:00:00"),
                        _entity(entities, "datetime", f"other_export_{number}_end", "00:00:00"),
                    )
                ],
            }
        )
        kwh = float(_entity(entities, "number", f"other_export_{number}_kwh", 0) or 0)
        if kwh > 0 and export_periods[-1]["rate"] != 0:
            caps[pid] = kwh

    if not export_periods:
        export_periods.append({"id": "export", "name": "Export", "rate": 0.0, "windows": [{"days": "all", "all_day": True}]})
        review.append("no export slot was enabled; export is set to 0")

    default_export, gaps = _choose_default(export_periods)
    if gaps:
        review.append(f"some times had no export slot and now use {default_export['name']}")

    for period in export_periods:
        cap = caps.get(period["id"])
        if cap is None:
            continue
        rate = period.pop("rate")
        if period is default_export:
            after: dict[str, Any] = {"rate": 0.0}
            after_text = "$0"
        else:
            after = {"use_period": default_export["id"]}
            after_text = f"{default_export['name']} rate"
        period["tiers"] = [{"up_to_kwh": cap, "rate": rate}, after]
        period["tier_reset"] = "day"
        review.append(f"{period['name']} is capped at {cap:g} kWh/day, then {after_text} is assumed")

    # ----- assemble -----------------------------------------------------
    note = f"Imported from Energy Manager file {filename}." if filename else "Imported from an Energy Manager file."
    if review:
        note += " Check: " + "; ".join(review) + "."

    plan: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "country": "AU",
        "region": state,
        "dnsp": DNSP_NAMES.get(data.get("dnsp"), data.get("dnsp")),
        "retailer": data.get("provider"),
        "plan": data.get("plan"),
        "plan_id": None,
        "customer_type": "residential",
        "effective_from": data.get("effective_from"),
        "effective_to": None,
        "source": {"type": "manual", "url": None, "checked": data.get("source_checked") or data.get("effective_from")},
        "currency": "AUD",
        "timezone": STATE_TIMEZONES.get(state, "Australia/Sydney"),
        "holidays": {"treat_as": "none"},
        "supply_charge": {"rate": float(_entity(entities, "number", "electricity_provider_daily_charge", 0)), "unit": "per_day"},
        "import": {"periods": [_ordered(p) for p in import_periods]},
        "export": {"periods": [_ordered(p) for p in export_periods]},
        "demand": [],
        "controlled_load": [],
        "needs_review": bool(review),
        "notes": note,
    }
    return plan
