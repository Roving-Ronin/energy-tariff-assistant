# CLAUDE.md

Guidance for Claude Code when working in this repository. This file holds the rules and traps;
the README explains features to users.

**Start of a session: read `CLAUDE-TODO.md`.** It holds planned and waiting work, the design agreed for each,
the facts already checked and what was decided against and why. Update it as items move (dated status lines);
remove an item once it's released.

## Project

Energy Tariff Assistant is a Home Assistant custom integration (`custom_components/energy_tariff_assistant`)
for Australian electricity tariff plans: imported plans become price sensors with forecasts (Energy dashboard,
HAEO, EMHASS, Nimbus), "Active tariff" points stable grid sensors at the plan in use, with capped-rate tracking
and running cost, compensation and supply charge totals. Plans are JSON files (`"format": "ha_tariff_plan"`, v1) from the
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
python scripts/import_eme_plans.py --dnsp Ausgrid --dry-run   # convert EME plans (needs internet; the Import Plans - <state> workflows run it)
python scripts/fetch_synergy_plans.py --dry-run   # Synergy (WA) plans (home connection + pypdf; GitHub is refused)
python scripts/fetch_synergy_plans.py --from DIR  # ...or from saved .mhtml pages + the DEBS PDF
python scripts/fetch_horizon_plans.py --dry-run   # Horizon Power (regional WA) plans (needs internet; weekly workflow)
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
| `engine/forecast.py` | `build_forecast` (`{time, value}`: now, every :00/:30, changes) and `build_intervals` (Amber's `{start_time, end_time, per_kwh}`, 30-min slots). Pure |
| `engine/demand.py` | **`DemandMeter`: demand charges**: 30-min intervals on the local clock, peak per month / bill / rolling 12 months, charge = peak x rate x days (re-prices earlier days on a new peak), locked per period. Pure |
| `engine/meter.py` | **`Ledger`: all per-period kWh and cost maths**, day/month/cycle buckets, boundary split, tier split. Pure, unit-tested directly. Never re-implement this elsewhere |
| `engine/importers/` | Converters from other formats: `energy_manager.py` (energy-manager-tariffs), `cdr.py` (Energy Made Easy / CDR plan detail; also the shared CDR distributor → DNSP/state map). Pure |
| `schema/tariff-plan.schema.json` | JSON Schema for editors and CI; `validate.py` adds the cross-field rules |
| `plan_source.py` | Library fetch, file read, paste parse; `check_plan` (validate + build resolver + prewarm holidays) |
| `wizard.py` | Build-a-plan answers → plan dict (`assemble_plan`), file names, review summary; library filtering (country → state → DNSP → retailer → plan, newest first). Pure |
| `share.py` | Pre-filled GitHub plan submission link (plan only, never meter data). Pure apart from `const` |
| `runtime.py` | Per plan entry: prices, forecasts, period window, capped periods; counts Active tariff's grid sensors in its ledgers (caps), timers, `.storage` |
| `active.py` | Active tariff: the plan in use, totals (import cost, export compensation, supply charge), the active plan files for other tools |
| `sensor.py` | Entities only: plan price sensors and capped-rate `_used` sensors; Active tariff's grid prices and totals |
| `select.py` | `select.eta_grid_active_tariff_plan` |
| `update.py` / `updates.py` | Library updates for a plan entry (update entity; matching and change notes, pure) |
| `config_flow.py` | Add a plan (source → entry titled "Retailer - Plan"); `async_step_import` creates Active tariff; options: plan entries (replace the plan, share), Active tariff (`active`: plan in use, grid sensors, billing day). Plan sources: library (filtered steps), build step by step (`build_*` steps, one set per stream), paste, file |
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
- Demand meters (`runtime.demand`, one per demand charge, fed by grid import only) settle an interval only when a
  reading arrives after its end, never at a tick (same reason as below).
- **Never re-read an unchanged source value at a timer tick.** It moves the ledger's baseline and puts the next
  increase entirely in the new period instead of splitting it by time. Ticks only roll buckets.
- `dt_util` for times, never `datetime.now()`. Readings are timed by `state.last_updated`.
- Source units: Wh, kWh, MWh (`ENERGY_UNIT_FACTORS`); anything else is rejected with a warning.

### Plans and entries
- **Two kinds of entry** (`data.kind`): `plan` (one per imported plan) and `active` (Active tariff, unique id
  `active_tariff`, created by the first plan entry through `async_step_import`). The grid energy sensors and
  billing day live on Active tariff; every plan entry counts them in its own ledgers, so each plan's caps are right
  whether or not it's in use. Changing them reloads the plan entries; changing the plan in use doesn't.
- Active tariff follows the plan in use (`async_follow`): called from its own start and from the end of the plan
  entry's setup (state `SETUP_IN_PROGRESS` counts: entries set up concurrently). Removing the plan in use leaves
  no plan and a repair (`no_active_plan`); it never switches on its own.
- Totals (`active.py`): cost/compensation add each change in the active plan's ledger `total_cost` (a switch
  starts from the new plan's figure); demand adds each change in the plan's `demand_total()` (also at midnight: a new
  day adds a day's charge); supply charge adds whole days at 00:00, missed days at start.
- The chosen plan is **copied into the entry's options** (pinned). Library changes never reach an entry silently;
  the user replaces the plan in the options flow.
- Ledgers are keyed by period `id`. Keep ids stable across plan versions (`peak`, `shoulder`, `offpeak`...):
  a renamed id starts a new meter and new entities.
- **Released clients read the live library.** A new top-level field is only reported (a warning) by
  `validate_plan`, so older releases still load plans that gain one; the schema (CI) still rejects unknown
  fields. Anything that changes how prices are read bumps the plan format `version` instead (older releases
  refuse it). Library-only fields (`confirmations`, `problems`) are dropped when a plan is pinned
  (`LIBRARY_ONLY_FIELDS`).
- Changing what an entry stores = bump `VERSION` in `config_flow.py` + `async_migrate_entry` + a test with an
  old-format entry. Changing the ledger's storage format = bump `STORAGE_VERSION` with a migration.

### Entities
- Entity IDs come from the entry, never the plan's content: plan entries `sensor.<slug>_import_price` /
  `_export_price`, `sensor.<slug>_<stream>_<period>_used`, `sensor.<slug>_demand_<charge>_peak`, `update.<slug>_plan`, where `slug` (`data.slug`,
  `wizard.entity_slug`: `eta_covau_solarmaxtou`) is fixed at creation. Active tariff: `sensor.eta_grid_import_price`
  / `_export_price`, `…_import_cost_total`, `…_export_compensation_total`, `…_supply_charge`(`_total`), `…_demand_charge_total`,
  `select.eta_grid_active_tariff_plan`. Unique IDs `{entry_id}_{stream}_price`, `{entry_id}_{stream}_{period}_used`, `{entry_id}_demand_{charge}_peak`,
  `{entry_id}_plan_update`, `active_tariff_<key>`. **Renaming a key breaks users' dashboards and history**: avoid
  it, or migrate the registry entry (`_remove_retired_entities` drops what a version no longer makes and moves
  old price entity IDs).
- Price sensors carry Amber Express's layout: `start_time`/`end_time`, `interpolation_mode: previous` (HAEO
  interpolates linearly without it), `forecast` `{time, value}` (Nimbus; HAEO's own reader) and `forecasts`
  `{start_time, end_time, per_kwh}` (HAEO's Amber reader, which it tries first; EMHASS templates). Both unrecorded.
  Never add `detailedForecast` (Nimbus would read it as Amber Express). Price sensors have no device class
  (monetary allows only `total`); totals are `monetary` + `total`.
- Version 1 entries (own sensors, meters) migrate to 2 (`async_migrate_entry`): kind + slug; Active tariff, when
  created, takes their energy sensors.

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
  text → per day, with a note (1st Energy). Entry dates under a year that split the year (Red Energy: Nov-Mar, Apr-Oct)
  are seasons (must match usage seasons, or become the plan's if usage has none); one date range on every entry
  (Sumo: Apr-Dec) is when the rates apply, used all year with a note. Same rate and times in every season → one period. A cap with nothing
  after it, or entries that don't pair up, is skipped with a reason. Covau SolarMax combined matches the library.
- CDR window ends are inclusive (14:59, 23:59): they become 15:00, 24:00.
- **Feed-in options** (decided 2026-11-22): some plans offer a choice of feed-in, labelled in each retailer entry's
  description ("OPTION A: Red Energy FIT…", "OPTION B: Surplus FiT (First 7.5 kWh/day)…"). Each option is **its own
  plan**: name + " (FIT Option A)", `feed_in_option: "A"`, same plan ID, rates named from the description ("Surplus
  FiT", "Smart FiT"). Identity is plan ID + DNSP + option everywhere (importer matching, the library's same-plan
  check, update candidates; the file slug and entity slug keep the option when cut short). Some entries labelled
  and some not, or only one option: skipped. A library file from before options were told apart becomes option A.
- **Demand charges** (`_demand_charges`, surveyed 2026-11-21 with the CDR conversion check's `demand_plans`
  input: 40 plans, 67 charges): they sit in `demandCharges` tariff periods **and** in `demandCharges` lists beside
  usage rates; read both. The data is loose: times are often `00:00`-`00:00` with the window only in the description
  (Red Energy: "between 5pm and 8pm Mon to Fri"), `measurementPeriod` says DAY where the text says "calendar month",
  `measureUnit` says KWH. So: the data's times, else the text's, else all day (data and text disagreeing skips);
  `over` from the text (12 months / 365 days, calendar month, bill period), else `month` with a note; seasons from
  the tariff period's dates or the months in the name or text ("Demand Summer": "November to March"), made into plan
  seasons when usage has none. The same charge in every season (UE) is one charge (one measure); the same name at
  two prices is named by season. kVA counts as kW (note). Skipped with a reason: $0 (Red standing offers),
  implausible amounts (cents: 9.24; 0.000919), MONTH under $1 (a daily price), yearly figures, "greater of" rules,
  two month ranges, a maximum demand. Charge = peak kW x rate x days (EME's wording).
- Never guess: anything the plan format can't express exactly (FLEXIBLE/QUOTA pricing, banded supply, seasons not on
  month boundaries, per-year steps, differing supply charges) is skipped with a reason in `Conversion.skipped`;
  parts left out of a converted plan go in its `notes`. Converted plans are `needs_review: true`.
- Period ids come from the plan's own rate names (`free`, `peak`), so meters and entity ids read naturally.
  `rate_name` drops the network and tariff words retailers put first ("Ausgrid Solar Sharer Peak" → "Peak",
  "TOU Rate Solar FiT" → "Solar FiT"); season labels read "Peak (Nov – Mar)".
  One feed-in period for all times is always **Feed-in** (`feed_in`, `name_feed_in`; the retailer's name goes in the
  notes); TOU feed-in keeps its names. `source.url` is the plan's EME page (`plan_page`:
  `energymadeeasy.gov.au/plan?id=<plan ID>`); the plan browser shows the source as "Energy Made Easy", linked.
  EME lists each season's rates separately: the same name and price in several seasons is **one period** with
  each season's windows (Amber Solar Sharer: 10 CDR rates → peak, off_peak, free_period). Seasons take the plan's
  names when they tell them apart, else their months (`nov_mar`; also when the names have digits: "Season 1 (1 Nov
  to 31 Mar)"). The same name at different prices by season (high/low season peak) is named **by its seasons,
  never its price** (`peak_nov_mar`, "Peak (Nov – Mar)"): ids key the meters and prices change every year. Several CDR distributor names for one network
  ("Essential Energy", "... Far West") give one plan; retailer names are trimmed ("GEE Energy ").

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
  together. Its **Report a problem** link uses the same form field ids as `share.py`. The list's **Confirmed plans only** box shows plans with `needs_review: false`. Import and Export (selling) share a row, controlled loads the next
  (two per row). Rates show in dollars under "Rate ($/kWh)"; the period id is a hover title only; seasons show as month ranges ("Nov – Mar") from
  `seasons`. The library's same-plan check (`validate_plans`) treats different plan IDs as different plans
  (EME gives a retailer's variants one name). Only the library goes on
  the site: never raw CDR data or unchecked conversions.
- **Import plans** (manual workflows `Import Plans - NSW`/`QLD`/`VIC`/`SA`/`ACT`/`TAS`, each a small caller with its
  state's distributors pre-filled and a Residential/Business choice, run titled "Import Plans - NSW Residential";
  the work is in the shared `import-plans.yaml` (`workflow_call`, one import at a time); `scripts/import_eme_plans.py`): converts Energy Made Easy plans for chosen
  DNSPs, customer type and pricing, validates them and opens a PR with the summary. **EME lists only current plans
  and keeps no history** (checked 2026-11-21: 17,657 plans, none ended or future, one version per family), so
  changes are found by comparing each run with the library, in three phases (convert everything listed for those
  DNSPs, library plans included; reconcile; write). Family = plan ID without its version (`COV1117612MRE3` →
  `COV1117612MRE`, `family()`; Victorian `@VEC` IDs have no version). Same ID + same pricing: nothing. Same ID,
  new content: updated in place, or a new file when its effective date is later. New version of a family: new
  file. A library plan no longer listed: ended (`effective_to` = run date, note), never deleted; none are ended if
  any list couldn't be read. A new ID with the same retailer, name, DNSP and customer type as one that went: its
  replacement. New files end the old one the day before and **keep its period ids** (`keep_ids`: same name, else
  same times; tier `use_period` follows), so meters carry on. **Reviewed plans** (`needs_review: false`) **and
  plans not made by the importer** (source not `EME`, e.g. the hand-made CovaU plan) are never changed: their
  differences are listed in the PR. `--per-retailer` counts library plans too (a re-run with the same limit adds nothing). **Wanted plans** (`scripts/wanted_plans.json`,
  the maintainer's list of plans people use, taken from the energy-manager-tariffs file names: names only, no
  content) always come in for the DNSPs asked for, past the limit and whatever the pricing, up to 3 newest per entry
  (`WANTED_PER_ENTRY`; "Smart Saver" has many offers). Matched by words: the retailer starts the CDR brand, every word
  of the plan name is in the EME name. Ones not found are listed in the PR. **Wholesale-linked plans are skipped** (`WHOLESALE`: "estimate",
  "wholesale", "spot price", "variable prices" in the name or description; EME shows only the retailer's estimate,
  e.g. GloBird WHOLESAVE, Flow Power Variable Prices, Ezi Market): as fixed rates they'd price meters wrongly.
  **Wholesale retailers** (`WHOLESALE_RETAILERS`: Amber, Flow Power, Localvolts; maintainer 2026-11-21) keep only
  their standing offers (plan ID with `S`, e.g. `AMB1127895SRE1`: DMO and Solar Sharer, fixed prices); their market
  offers show a guide price (Flow Power's TOU "estimates" have one price in every period). EziPower and GloBird
  also sell fixed plans, so only their named wholesale plans go. HA's library steps and the plan
  browser offer only plans that haven't ended (`wizard.current`). The repo needs "Allow GitHub Actions to create
  and approve pull requests"; checks on a PR the bot opens need **Approve and run workflows** (GitHub, since
  2026-06; not settable for the bot). Retailer names come from the CDR `brandName`.
- **Synergy plans (WA)** (by-hand workflow **Import Plans - WA - Synergy**, `scripts/fetch_synergy_plans.py`): WA isn't on EME. **synergy.net.au
  answers GitHub's runners with HTTP 403 whatever the headers** (Fetch check, 2026-11-21), so it isn't scheduled:
  run it from home, or `--from <folder>` with the plan pages saved as `.mhtml` (their URL is inside) and the DEBS
  PDF (name contains "debs"); a missing saved page stops the run. Horizon's weekly run is the alarm: A2/K2 =
  A1/K1, and a mismatch with the library's current Synergy plans opens one issue ("WA tariffs changed: update
  the Synergy plans", only if none is open). Reads **only the
  "Item / Price inc. GST" tables** on Synergy's four plan pages (descriptions are ignored: K1's text disagrees with
  its table) and the stated times ("Peak (3pm to 9pm)"), against fixed per-plan label → period maps (`PLANS`).
  Feed-in: DEBS schedule PDF, the **Residential customers** table only, GST-free (2026-07-01: 10c 3pm-9pm, 2c
  otherwise); K1 isn't eligible (feed-in 0). Any missing/new label, changed time or unparsable number fails the run
  and writes nothing (every plan is read before any is written). A new effective date adds a file and sets the old
  one's `effective_to`; period ids never change. A1 is cross-checked against the WA Government page (warning only).
  Western Power, `Australia/Perth`.
- **Horizon Power plans (WA)** (weekly workflow **Import Plans - WA - Horizon Power**, `scripts/fetch_horizon_plans.py`, reuses the Synergy reader and
  `place_plan`): one pricing page. A2/K2 (= Synergy A1/K1 prices), DEBS and Buyback Bonus **per town** (37): per
  town an `A2 Residential (<town>)` (DEBS, peak 3pm-9pm) and `A2 Residential Buyback Bonus (<town>)` plan, plus
  `K2 Combined Use` (no feed-in, as K1). Buyback Bonus always has the four seasonal ids (`peak_nov_mar`,
  `peak_apr_oct`, `off_peak_nov_mar`, `off_peak_apr_oct`) even when a town's seasons match, so ids never change.
  Labels contain spans and info icons: lines split without `span`, non-ASCII dropped. REBS ignored (closed).
- **Plan feedback** (`plan-feedback.yaml`, `scripts/plan_feedback.py`): the plan browser's **Confirm this plan**
  opens a fully pre-filled issue (`plan_confirmation.yml`, title `[Confirm] …`); on opening, the workflow adds the
  author to the plan's `confirmations` (`date`, `by`, `issue`; once per account); at **2 people**
  (`CONFIRMATIONS_NEEDED`, maintainer 2026-11-21) it sets `needs_review: false` (badge **Confirmed** / **Confirmed by N**;
  before that "1 of 2 confirmations"), commits to main, runs the Pages workflow by name (a workflow-token push
  starts no workflows) and closes the issue. A **Report a problem** correction naming a library file sets
  `needs_review: true` again, adds it to `problems` and notes it; confirmations are kept, but only those after the latest
  problem count (`current_confirmations`). Issue text is read only in github-script;
  only a path matching `plans/<CC>/<REGION>/<name>.json` that exists, the login and the issue number go further.
  Confirmed plans count as reviewed, so the importer never changes them.
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
