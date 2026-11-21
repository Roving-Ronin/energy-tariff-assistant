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

### Plan browser (GitHub Pages)
**Status (2026-11-21):** built (`site/`, `scripts/build_site.py`, Plan browser workflow); PR open. Needs the
maintainer to set Settings → Pages → Source: **GitHub Actions** before the first deploy, then run the Plan browser
workflow once (or merge). Later: a plan comparison (two plans' cost for a typical day), and listing plans
converted from Energy Made Easy separately once the library importer exists.

## Next (v0.2)
- **Demand charges:** in the format and `model.py` (`DemandCharge`), not in the resolver or ledger yet. Needs:
  interval kW from the import meter (`interval_minutes`, default 30), max or mean-of-top-n over month / bill /
  rolling 12 months, `minimum_kw`; sensors for demand now, peak this cycle (and when), cost to date; add the
  demand cost to the bill sensor.
- **Controlled load:** streams are in the format (`controlled_load`), the resolver handles them. Needs an energy
  sensor per controlled-load stream in the flow, ledgers, entities, and its supply charge in the bill.
- **Entity names in translations:** move `_attr_name` strings to `strings.json` (`translation_key` with
  `translation_placeholders` for the period name), keeping unique IDs.

### Retailer coverage: settled (2026-11-20)
**Status (2026-11-20):** diagnostics run: 38 of the 39 empty paths report `totalRecords: 0` on Energy Made Easy
itself (really no plans listed, e.g. Localvolts, whose prices follow the wholesale market live and so publish no
fixed plans); `perpetual` has gas plans only. The CDR Register's brand summary has 84 energy brands but no
`productBaseUri`, so the AER list is the only source of paths; brands in the Register but not the list are now
reported as `register_brands_not_in_list` (a warning in the workflow).

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
  - **First real run (2026-11-20):** 111 plans (3 per path), 108 converted and valid, 3 skipped (2 stepped
    rates on an unsupported reset period, 1 season off month boundaries); pricing models TOU 33, TOU+CL 29,
    single 28, single+CL 21; time zones LOCAL 82, AEST 15, absent 14. Artifacts can't be downloaded from this
    workspace (Azure blob host not allowed), so the check now logs full reasons, converter-note counts and a
    **library check** (library plans found in the listing are converted and priced against the library copy).
  - **Second run (2026-11-21):** 112 plans, 109 converted, 3 skipped. Library check, now logged in full as time
    ranges: Covau SolarMax **says AEST** on Energy Made Easy, so every January window is an hour later than the
    library (local time); July import agrees exactly. Essential Energy follows local time ("AEST or AEDST"), so
    either Covau's data or the library is wrong. **Settled:** CovaU bills on local time (maintainer, 2026-11-21),
    so the library is right and its EME data isn't: `RETAILER_CLOCKS` overrides it. Rule otherwise: the plan's
    stated zone wins, absent uses the network's clock (Evoenergy, TasNetworks: AEST; others local), and a
    disagreement adds a review note. **Feed-in:** converted pays 16.5c all day vs library 15c super FiT (18:00-21:00, 30 kWh/day) then
    5c: 15c x 1.1, so the converter adds GST to feed-in (import matches exactly after x1.1, so only feed-in is
    affected); and the cap and the 5c rate are missing: WATTever shows SolarMax's feed-in as a condition, "15c per
    kWh for the first 30kWh exported per day. 5c per kWh for exports at all other times", i.e. stepped FiT only in
    the description (CDS standards-maintenance #520). GST fixed (2026-11-21): residential and unstated feed-in as published, business feed-in +GST (bill amounts;
    claiming it back is the business's accounting). Still open: stepped FiT only in text: flag for review.
  - **Real samples:** Actions → **CDR conversion check** (`scripts/check_cdr_conversion.py`) converts a few plans per
    path and uploads `report.json` + raw details as an artifact (not committed). Use it to find what real data
    does that the hand-written samples don't, and add trimmed cases to `tests/test_importer_cdr.py`.

## Later (not started)
- **Wholesale-priced retailers** (Amber, Localvolts: prices follow the market, updated every 5 minutes): no fixed
  plan exists to convert. Would need a live price source (their APIs, or an existing HA integration's price
  sensor) as a plan's import/export price, metered by the same ledgers.
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
