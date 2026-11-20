# CLAUDE-TODO

Work that's planned, waiting on something, or decided against, with what's needed to pick it up again. Read
this at the start of a session (CLAUDE.md points here); update it when an item moves, and delete items once
they're released. Newest decisions win: date each status line.

## Waiting

### v0.1.0: first release
**Status (2026-11-20):** v0.1.0b1 released (pre-release) for the maintainer to test; v0.1.0b2 adds Share this plan, Build a plan step by step and the filtered library.
README warns not to install while under development: remove that warning for v0.1.0.
Repo topics are set (HACS Validate passes). Before releasing v0.1.0:
- Run it on a live Home Assistant 2026.11 (tests only ran on 2026.2.3, the `hacs.json` minimum): setup from the
  library, the maintainer's own Sigenergy import/export sensors, check the Covau plan's prices and a day's meters
  against the Energy dashboard.
- Google Drive backup secrets (`GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN`), or that
  workflow fails on every push to main.
- Feedback from testing v0.1.0b1; then a `## v0.1.0 (YYYY-MM-DD)` CHANGELOG section (TL;DR rewritten) and a PR
  titled `v0.1.0: …`.

### Covau SolarMax plan: needs review
**Status (2026-11-20):** `needs_review: true` (converted from energy-manager-tariffs). Check against EME: supply
charge 2.29999 (probably 2.30), whether the 30 kWh super FiT cap resets daily, public holidays.

## Next (v0.2)
- **Demand charges:** in the format and `model.py` (`DemandCharge`), not in the resolver or ledger yet. Needs:
  interval kW from the import meter (`interval_minutes`, default 30), max or mean-of-top-n over month / bill /
  rolling 12 months, `minimum_kw`; sensors for demand now, peak this cycle (and when), cost to date; add the
  demand cost to the bill sensor.
- **Controlled load:** streams are in the format (`controlled_load`), the resolver handles them. Needs an energy
  sensor per controlled-load stream in the flow, ledgers, entities, and its supply charge in the bill.
- **Entity names in translations:** move `_attr_name` strings to `strings.json` (`translation_key` with
  `translation_placeholders` for the period name), keeping unique IDs.

### Retailer coverage: first run done (2026-11-20)
**Status (2026-11-20):** first run committed `plans/retailers.json`: 40 retailers, all 13 NEM distributors
mapped, none unmapped or failed, 17,635 electricity plans. Fixes since (PR): dual-fuel plans counted, brand names
trimmed (`amber `), a retailer under two paths merged (Indigo Power: `energy-locals` and `next-business`), and
the paths with no plans listed in `paths_without_electricity_plans` (39 in the first run, e.g. shell-energy,
telstra-energy, tesla, localvolts: check after the re-run whether they're really empty). The CDR Register listed
no Energy Made Easy brands, so the AER fallback list was used; now logged. Worth a look at what the Register's
brand summary actually returns.

## Next (after retailer coverage has data)
- **Wizard and library: suggest retailers by DNSP** from `plans/retailers.json` (fetched like `index.json`):
  in Build a plan, ask the DNSP before the retailer and offer the retailers selling there; in the library, show
  "retailers with no library plans here yet" as a prompt to share one.
- **Importer from Energy Made Easy** (design agreed 2026-11-20; converter in PR #11, `engine/importers/cdr.py`;
  next: a script/workflow fetching real plan detail to convert and add as test samples):
  - **Source:** CDR Get Generic Plan Detail (`/energy/plans/{planId}`, `x-v: 3`, no auth) for one plan at a time;
    plan ids from Get Generic Plans. Current plans only (no history or future), NEM states incl. VIC (DEECA).
  - **Converter** (`engine/importers/cdr.py`, pure, tested on real plan-detail samples): `electricityContract`
    → `ha_tariff_plan`: `tariffPeriod` dates → `effective_from`/`to` (several periods → seasons or plan versions);
    `dailySupplyCharge` → `supply_charge`; `singleRate` / `timeOfUseRates` (days, start/end times) → import periods
    and windows; rate blocks (`unitPrice` + `volume`) → `tiers`; `solarFeedInTariff` single / time-varying (with
    volume caps) → export periods; `demandCharges` → `demand`; `controlledLoad` → `controlled_load`;
    `geography.distributors` → `dnsp` (via the retailer-coverage DNSP map), `customerType` → `customer_type`.
    **Add GST**: check the standard's wording, but CDR prices are understood to be GST-exclusive. Discounts,
    pay-on-time conditions, incentives and fees go in `notes`, not prices. Plans that can't be represented
    (wholesale-linked e.g. Amber, `FLEXIBLE` pricing) are skipped with a reason, never guessed.
  - **Use 1, library importer** (script + workflow): convert chosen retailers/DNSPs, validate, open a PR with new or
    changed plans (`needs_review: true` until checked against the BPID).
  - **Use 2, live in Home Assistant**: state → DNSP → retailer → plan from Energy Made Easy, converted on the spot
    (`async_add_executor_job` for the conversion if it gets heavy), with the library as fallback.
  - ~17,600 electricity plans: fetch on demand or per DNSP, not all weekly.

## Later (not started)
- **Wizard: edit the current plan** (pre-fill the steps from the entry's plan, e.g. after a price change) rather
  than building from scratch.
- **Wizard: more of the format:** public holidays as Sunday/Saturday, seasons, demand charges, controlled load.
- **Check shared plans automatically:** a workflow on `plan`-labelled issues that runs `validate_plans.py` on the
  pasted Plan JSON and comments the errors/warnings (read the body only via `github-script` or `env:`).
- **Plan library growth:** more AU plans (Ausgrid, Endeavour, Evoenergy, Energex/Ergon, CitiPower/Powercor,
  SA Power Networks...), ideally through the Energy Made Easy importer above.
- **Library update notice:** a Repairs issue when the library has a newer version of the entry's plan
  (`plan_source.library_file` is stored for this), showing the changed rates; never update silently.
- **Statistics import:** backfill per-period kWh/cost from long-term statistics of the source sensors, so a new
  setup isn't empty.
- **Discounts / concessions / GST-exclusive plans** in the bill estimate.

## Decided against (and why)
- **MIT licence** (2026-11-20): the repo was already GPL-3.0 from Phase 1; maintainer chose to keep GPL-3.0.
- **Adding tariff plans to utility_meter_next_gen (cabberley/utility_meter_evolved)** (2026-11-20): a second
  integration's worth of code in someone else's project, and Abode Power Tariffs (Apache-2.0) couldn't be copied
  into MIT code anyway. Built here instead, written fresh (Abode used only as a reference).
- **Driving a `utility_meter` select at period boundaries** (Abode's approach) (2026-11-20): the ledger meters
  directly, splitting increases across boundaries by time instead of crediting whichever tariff is selected.
- **Re-reading the source at boundary ticks** (2026-11-20): moves the baseline forward and defeats the time split
  (see CLAUDE.md).
