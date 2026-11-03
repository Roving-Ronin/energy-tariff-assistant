# Energy Tariff Assistant

A Home Assistant custom integration for electricity tariff plans. Load a
plan once, from a file, the shared library in `plans/`, or (later) an
official source such as Australia's Energy Made Easy, and get standard
import/export price, period, forecast and cost entities. No more
hand-written template sensors, `utility_meter` tariff selects or
automations to switch them.

> **Status: Phase 1 (engine and plan format).** The rate engine, plan
> format, validator and Energy Manager importer are done and tested.
> The Home Assistant integration itself (config flow, entities) is not
> built yet, so this repo is not installable through HACS yet.

## What's here

| Path | What it is |
| --- | --- |
| `custom_components/energy_tariff_assistant/engine/` | Rate engine: plan model, resolver, forecast, validator. No Home Assistant imports. |
| `custom_components/energy_tariff_assistant/engine/importers/energy_manager.py` | Converts `energy_manager_custom_tariff` v1 files from [energy-manager-tariffs](https://github.com/ha-energymanager/energy-manager-tariffs). |
| `custom_components/energy_tariff_assistant/schema/tariff-plan.schema.json` | JSON Schema for plan files. |
| `plans/` | Shared plan library: `plans/<country>/<region>/<Retailer>-<PlanSlug>-<Region>-<DNSP>-<YYYYMMDD>.json`, plus a generated `index.json`. |
| `scripts/` | `validate_plans.py`, `build_index.py`, `import_energy_manager.py`. |
| `tests/` | Pytest suite; importer tests run against the energy-manager-tariffs files (fetched, see Development). |

## Plan format in brief

A plan is one JSON file (`"format": "ha_tariff_plan"`, `"version": 1`).
Rates include tax, per kWh in the plan's currency. Each of `import`,
`export` and any `controlled_load` has a list of periods; one period is
the `default` and covers any time no other window claims.

```json
{ "id": "free", "name": "Free",
  "windows": [{ "days": "all", "start": "11:00", "end": "14:00" }],
  "tiers": [{ "up_to_kwh": 50, "rate": 0.0 }, { "use_period": "shoulder" }],
  "tier_reset": "day" }
```

- **Windows**: `days` is `all`, `weekdays`, `weekends` or a list like
  `["mon","tue"]`. `start` is inclusive, `end` exclusive. An end before
  the start wraps past midnight, matched by calendar day: "weekdays
  22:00-07:00" covers Monday 00:00-07:00 but not Saturday 00:00-07:00.
  Use `"all_day": true` rather than `00:00`-`00:00`.
- **Tiers** express caps and stepped rates. The last tier always says
  what happens after the cap (`rate`, which can be 0, or `use_period`).
- **Holidays**: `"holidays": {"treat_as": "sunday"}` treats public
  holidays (from `python-holidays`, by country and region) as Sundays.
- **Seasons**, **demand charges** and **controlled load** are supported
  in the format; see the full spec.

See `plans/AU/NSW/Covau-SolarMaxTOU-NSW-EssentialEnergy-20260701.json`
for a complete example.

## Adding a plan to the library

1. Write the plan file (or convert one: `python scripts/import_energy_manager.py --write <file>`).
2. Put it at `plans/<country>/<region>/<Retailer>-<PlanSlug>-<Region>-<DNSP>-<YYYYMMDD>.json`.
3. Run `python scripts/validate_plans.py` and `python scripts/build_index.py`.
4. Open a pull request. The Plans workflow re-runs both checks.

## Development

```
pip install -r requirements_test.txt
bash scripts/fetch_fixtures.sh   # importer fixtures from energy-manager-tariffs
python -m pytest tests
```

The importer tests use the plan files from energy-manager-tariffs. They are
fetched rather than committed because that repository has no licence file;
without them those tests are skipped.

## Licence

GPL-3.0. See `LICENSE`.
