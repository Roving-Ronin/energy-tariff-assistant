# CLAUDE.md

Guidance for Claude Code when working in this repository. This file holds the rules and traps;
the README explains features to users.

**Start of a session: read `CLAUDE-TODO.md`.** It holds planned and waiting work, the design agreed for each,
the facts already checked and what was decided against and why. Update it as items move (dated status lines);
remove an item once it's released.

## Project

Energy Tariff Assistant is a Home Assistant custom integration (`custom_components/energy_tariff_assistant`)
for Australian electricity tariff plans with per-rate metering built in: prices, periods, forecasts, kWh and $
per period, supply charge and an estimated bill. Plans are JSON files (`"format": "ha_tariff_plan"`, v1) from the
shared library in `plans/` (published as
`https://raw.githubusercontent.com/Roving-Ronin/energy-tariff-assistant/main/plans/index.json`), a file in the
user's config folder, or pasted text. Licence: **GPL-3.0** (decided 2026-11-20; keep it).

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
bash scripts/fetch_fixtures.sh           # importer fixtures (energy-manager-tariffs has no licence: fetched, never committed)
python -m pytest -q                      # tests/ (engine) + tests_ha/ (real in-process Home Assistant, no network)
pip install ruff && ruff check .        # lint incl. security rules (ruff.toml); Tests workflow runs it
pip install zizmor actionlint-py && zizmor . && actionlint   # workflow security + syntax; Security workflow runs both
python scripts/validate_plans.py && python scripts/build_index.py --check   # plan library; Plans workflow runs both
python scripts/build_site.py --out /tmp/site && python -m http.server -d /tmp/site   # plan browser, locally
```

CI on every push and PR: Tests (Ruff, pytest), Pull Requests (compile, strings = en.json, engine has no HA
imports, manifest), Security (zizmor, actionlint, CodeQL; also weekly), Plans (when plans/site/engine/scripts
change; also builds the plan browser), hassfest and the HACS validator. Plan browser publishes to GitHub Pages on
main.

Tests in `tests_ha/` run against a real test Home Assistant (pytest-homeassistant-custom-component), never a
`MagicMock` hass: a mock accepts calls real HA rejects. The library is mocked with `aioclient_mock`; tests never
touch the network. `tests/` runs with only `holidays`, `jsonschema` and `pytest` (the root `conftest.py` loads
the HA plugin only if it's installed). Time-dependent tests use `freezer` and `Australia/Sydney`.

## Module map

| File | Role |
|---|---|
| `engine/model.py` | Plan dataclasses and `load_plan`. Pure |
| `engine/validate.py` | Plan validation (errors block, warnings report). Runs without third-party packages. Pure |
| `engine/resolver.py` | **Moment → period and rate**, tiers (`used_kwh`), holidays, plan versions, `boundaries`, `next_change`. Pure |
| `engine/forecast.py` | HAEO/EMHASS-shaped price forecast. Pure |
| `engine/meter.py` | **`Ledger`: all per-period kWh and cost maths**, day/month/cycle buckets, boundary split, tier split. Pure, unit-tested directly. Never re-implement this elsewhere |
| `engine/importers/` | Converters from other formats: `energy_manager.py` (energy-manager-tariffs), `cdr.py` (Energy Made Easy / CDR plan detail; also the shared CDR distributor → DNSP/state map). Pure |
| `schema/tariff-plan.schema.json` | JSON Schema for editors and CI; `validate.py` adds the cross-field rules |
| `plan_source.py` | Library fetch, file read, paste parse; `check_plan` (validate + build resolver + prewarm holidays) |
| `wizard.py` | Build-a-plan answers → plan dict (`assemble_plan`), file names, review summary; library filtering (country → state → DNSP → retailer → plan, newest first). Pure |
| `share.py` | Pre-filled GitHub plan submission link (plan only, never meter data). Pure apart from `const` |
| `runtime.py` | Per entry: tracks source sensors, converts units, feeds the ledgers, timers, `.storage`, signals entities |
| `sensor.py` | Entities only: values come from the runtime / ledgers |
| `config_flow.py` | Setup (plan source → meters) and options flow (meters, replace the plan, share). Plan sources: library (filtered steps), build step by step (`build_*` steps, one set per stream), paste, file |
| `diagnostics.py` | Plan, options and ledger state |

## Rules

### Engine
- **`engine/` never imports Home Assistant** (the Pull Requests workflow checks). The scripts and CI use it directly.
- All matching is in the plan's local wall-clock time; step through time in UTC (adding minutes to a zone-aware
  local time is wall-clock arithmetic and breaks across DST).
- Tier `used_kwh` is counted by the **window** period (the free window's kWh includes what overflowed to shoulder).

### Home Assistant
- **No file I/O on the event loop.** `validate_plan`, `Resolver(...)` (ZoneInfo) and holiday calendars read
  from disk: run them via `hass.async_add_executor_job` (`check_plan`, `prewarm_holidays`).
  `test_plan_loads_off_event_loop` checks the thread directly, because HA's blocking-call detector doesn't fire in
  the test harness.
- **Never re-read an unchanged source value at a timer tick.** It moves the ledger's baseline and puts the next
  increase entirely in the new period instead of splitting it by time. Ticks only roll buckets.
- `dt_util` for times, never `datetime.now()`. Readings are timed by `state.last_updated`.
- Source units: Wh, kWh, MWh (`ENERGY_UNIT_FACTORS`); anything else is rejected with a warning.

### Plans and entries
- The chosen plan is **copied into the entry's options** (pinned). Library changes never reach an entry silently;
  the user replaces the plan in the options flow.
- Ledgers are keyed by period `id`. Keep ids stable across plan versions (`peak`, `shoulder`, `offpeak`...):
  a renamed id starts a new meter and new entities.
- Changing what an entry stores = bump `VERSION` in `config_flow.py` + `async_migrate_entry` + a test with an
  old-format entry. Changing the ledger's storage format = bump `STORAGE_VERSION` with a migration.

### Entities
- Unique IDs are `{entry_id}_{stream}_{period|total}_{energy|cost}_{day|cycle}` and similar. **Renaming a key
  breaks users' dashboards and history**: avoid it, or migrate the registry entry.
- Meters are `state_class: total` with `last_reset` at the bucket start; the price sensor's `forecast` attribute
  is unrecorded.

### Converting CDR (Energy Made Easy) plans
- CDR prices are **ex GST**: `cdr.with_gst` adds 10%. Our plans hold what the bill shows, GST included (a
  business claiming GST back is its own accounting). **Feed-in** (decided 2026-11-21): households aren't paid GST,
  so residential and unstated plans use the published rate; business plans add GST, as paid to GST-registered
  businesses, with a note for those who aren't registered (unstated plans get a note too).
- Time zone (decided 2026-11-21): the plan's stated `timeZone` wins (AEST → `Australia/Brisbane`, so fixed-AEST
  windows stay right through daylight saving; LOCAL → the state's zone). **Absent uses the network's clock**, not
  the standard's AEST default: NSW, VIC and SA networks follow local time, Evoenergy and TasNetworks stay on AEST
  (`AEST_NETWORKS`). Absent, or a stated zone that disagrees with the network (Covau SolarMax on Essential Energy
  says AEST), adds a review note: retailers can apply their own rule, so neither side is assumed wrong. A retailer
  known to bill on its own clock overrides its data (`RETAILER_CLOCKS`, with the source: CovaU bills on local time,
  maintainer 2026-11-21, though its EME data says AEST).
- **Feed-in is spread over several entries** (CDS standards-maintenance #520): every retailer (non-PREMIUM)
  entry is read, not just the first. Entries for the same times and months combine: a capped block (one rate with
  `volume`) + one uncapped entry → tiers, reset from the capped entry's period; P1Y with "per day"/"/day" in its
  text → per day, with a note (1st Energy). Entry dates under a year are seasons (must match usage seasons, or
  become the plan's if usage has none). Same rate and times in every season → one period. A cap with nothing
  after it, or entries that don't pair up, is skipped with a reason. Covau SolarMax combined matches the library.
- CDR window ends are inclusive (14:59, 23:59): they become 15:00, 24:00.
- Never guess: anything the plan format can't express exactly (FLEXIBLE/QUOTA pricing, banded supply, seasons not on
  month boundaries, per-year steps, differing supply charges) is skipped with a reason in `Conversion.skipped`;
  parts left out of a converted plan go in its `notes`. Converted plans are `needs_review: true`.
- Period ids come from the plan's own rate names (`free`, `peak`), so meters and entity ids read naturally.

### Building plans
- Wizard output goes through `check_plan` like any other plan; the wizard never writes a plan the validator rejects.
- Built plans are saved under `energy_tariff_assistant/plans/` in the config folder and **never overwrite** a file
  (`-2`, `-3`... suffix). Tests clean that folder (`clean_built_plans`): the test config folder is shared.
- Step ids are per stream (`build_import_period`, `build_export_period`...) so each has its own strings; the
  methods delegate to shared helpers. Keep both streams' strings in step when wording changes.

### Sharing plans
- **Share this plan** only builds a link; Home Assistant never sends anything to GitHub and holds no token.
- Only the plan goes in the link. Never add meter totals, sensor or entity IDs, entry IDs or anything else
  install-specific. The form's field ids (`plan_submission.yml`) and the link's query keys must match; the issue
  validator's `TEMPLATES` lists the form's headings.

### UI text
- Wording goes in `strings.json`; copy it to `translations/en.json` (the Pull Requests workflow checks they
  match). Description placeholders carry data only.
- Known gap: entity names are still built in Python (`_attr_name`); see CLAUDE-TODO.

## Plan library
- Path: `plans/<country>/<region>/<Retailer>-<PlanSlug>-<Region>-<DNSP>-<YYYYMMDD>.json`; regenerate
  `plans/index.json` with `scripts/build_index.py` in the same commit.
- Rates include GST, per kWh, in dollars. `needs_review: true` until checked against EME / the BPID.
- Never commit anything from energy-manager-tariffs (no licence): convert, check, and record the source in `notes`.
- Files directly in `plans/` (`index.json`, `retailers.json`) are generated data; plans are only read from
  `plans/<country>/<region>/` (`_common.plan_files`).
- `plans/retailers.json` is written by the weekly Retailer coverage workflow (`scripts/fetch_eme_retailers.py`) from
  the AER's Energy Product Reference Data: CDR Get Generic Plans, `x-v: 1`, no auth, page-size up to 1000,
  `fuelType=ALL&effective=ALL` (dual-fuel plans sell electricity too; gas-only plans are dropped), one call per
  **path**. Brands are trimmed (the data has `amber ` with a space), plans deduped by `planId`, and a retailer
  under several paths is merged into one entry (`cdr_paths`, `cdr_brands`). Several brands share a path (`energy-locals`: Energy Locals, ARCLINE, Cooperative, RAA;
  `ovo-energy`: OVO, MYOB, OVO for CTM; `radian`: Radian, iO; `future-x`: Future X, Sunswitch), so plans are split
  by each plan's `brand`. Brands come from the ACCC CDR Register, with `scripts/eme_brands.json` (the AER's
  January 2026 list) as fallback. Store derived facts only (retailer, DNSP, state, counts), never plan text or
  prices. Unknown distributor names show as `unmapped_distributors`: add them to `DISTRIBUTORS` (in
  `engine/importers/cdr.py`, shared with the converter). The Register's brand summary has no `productBaseUri`, so
  paths come from `scripts/eme_brands.json`; Register brands missing from it show as `register_brands_not_in_list`.
- **Plan browser** (`site/`, GitHub Pages, `https://roving-ronin.github.io/energy-tariff-assistant/`): plain
  HTML/CSS/JS, no framework or build tools. `scripts/build_site.py` copies it with the library plans,
  `data/library.json` (index fields + `validate_plans.check_files` errors and warnings, so the page never
  re-validates) and `data/coverage.json` (a slim `retailers.json`). Plan files are contributor text: the page
  builds the DOM with text nodes only (`h()`), never `innerHTML`, and links only `http(s)` source URLs. Its
  timeline mirrors `Window.segments`/`covers` and `Resolver._period` (first non-default match wins): change them
  together. Its **Report a problem** link uses the same form field ids as `share.py`. Only the library goes on
  the site: never raw CDR data or unchecked conversions.
- **CDR conversion check** (manual workflow, `scripts/check_cdr_conversion.py`): real plan details are an uploaded
  artifact only, never committed. Tests use hand-written or trimmed samples.

## Releases and changelog
- **A PR titled `vX.Y.Z: …` releases `vX.Y.Z` when it's merged** (Auto Release), with its CHANGELOG section as the
  notes. Never put a version at the start of a PR that isn't a release. `[skip release]` in a PR's title, or on a line of its own in the
  description, stops it releasing. **Don't write the marker in a release PR's description** at all, not even to
  explain it (say "the skip marker" instead): v0.1.0b2's merge was skipped that way (2026-11-20) before the
  own-line rule. To release after a skip: Actions → Auto Release → Run workflow with the version.
- Release writes the version into `manifest.json`, attaches `energy_tariff_assistant.zip` and commits the version
  back. Don't edit the manifest version by hand.
- `CHANGELOG.md`: every release opens with a `### TL;DR`, rewritten as a whole when entries are added. Head a
  release's section `## vX.Y.Z (YYYY-MM-DD)` with the date it's prepared.

## Workflow security
- Never put `${{ ... }}` expressions inside `run:` scripts: pass them through `env:` and use `"$VAR"`.
  Issue titles, bodies and comments are attacker-controlled; read them only in `github-script` or via `env:`.
- `permissions: {}` (or `contents: read`) at workflow level; grant each job only what it needs.
- `actions/checkout` with `persist-credentials: false`, unless the job pushes with git.
- Actions are pinned by tag (`.github/zizmor.yml`), kept current by Dependabot.

## Repo conventions
- Issue templates use GitHub's default labels (`bug`, `enhancement`, `question`) plus `plan` for plan
  submissions; the issue validator's `TEMPLATES` must list every template's headings, or its issues get closed.
- Never commit or push without the maintainer's explicit approval.
