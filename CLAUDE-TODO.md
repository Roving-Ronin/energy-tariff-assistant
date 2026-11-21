# CLAUDE-TODO

Work that's planned, waiting on something, or decided against, with what's needed to pick it up again. Read
this at the start of a session (CLAUDE.md points here); update it when an item moves, and delete items once
they're released. Newest decisions win: date each status line.

## Waiting

### Active Tariff and price sensors (design agreed 2026-11-22)
**Status (2026-11-22):** built, in the v0.1.0b4 release PR. Remove this item once it's released.
Replaces the per-entry meters. Two entry kinds:
- **Plan entries** (one per imported plan), titled "Retailer - Plan" (customer type, network and state words
  dropped: "CovaU - SolarMax TOU"; renamable). Entities from the entry, never the plan's content, so library
  updates keep every name, ID and history: `sensor.eta_<retailer>_<planslug>_import_price` / `_export_price`
  (planslug from the library file name, e.g. `eta_covau_solarmaxtou`), one `…_<period>_used` sensor per capped
  period (kWh toward the cap, cap, remaining, reset), `update.eta_<…>_plan`.
- **Active tariff** (created with the first plan): `select.eta_grid_active_tariff_plan` ("Grid - Active Tariff Plan"; the Grid entities are named on their own: "Grid - Import Price", "Grid - Import Cost - Total"…), `sensor.eta_grid_import_price` and
  `sensor.eta_grid_export_price` (the "CNAME": mirror the active plan, stable for the Energy dashboard, HAEO,
  EMHASS, Nimbus), action `set_active_plan`, the grid import/export energy sensors (set once; every plan counts
  the same usage toward its own caps). Removing the active plan: "No active plan" + repair, never auto-switch.
- **Price sensor attributes** (copy Amber Express): `start_time`/`end_time` of the current period, `period_name`,
  `interpolation_mode: previous` (HAEO interpolates `forecast` linearly without it), `forecast` `{time, value}`
  (now, then every :00/:30, plus any off-grid change, 48 h; Nimbus reads this), `forecasts` in Amber core layout
  `{start_time, end_time, per_kwh}` (HAEO reads it first; EMHASS templates for Amber work unchanged). Export is
  positive (paid to you). No `detailedForecast` (Nimbus would read it as Amber Express).
- **Plan updates**: the update entity matches same plan ID with new content, a newer version of its family, or
  `replaced_by` (new information field the importer sets on withdrawn plans); shows the changes; Install replaces
  the plan in place. Period ids kept (`keep_ids`); added/removed capped periods add/remove their `_used` sensor.
- **Energy Manager**: the active plan's JSON at `/config/energy_tariff_assistant/active/active.json` (and per plan).
  EM writes its own converter later; document our format for other tools (library URLs, index, schema, versioning).
- **Totals on Active tariff** (agreed 2026-11-22): `sensor.eta_grid_import_cost_total` and
  `sensor.eta_grid_export_compensation_total` ($, `state_class: total`, for the Energy dashboard's "entity tracking
  the total costs"): each kWh costed when counted at the active plan's real rate (caps, boundary split), continuous
  across plan switches; unavailable without the grid energy sensors. `sensor.eta_grid_supply_charge` ($/d, active
  plan) and `sensor.eta_grid_supply_charge_total` (each day added at 00:00 at the plan active then; missed days added
  at start; from the day Active tariff is set up). "Supply charge", as bills and EME say (not "connection fee").
  Controlled load supply charges left out until a setting says the home has CL circuits.
- No period meters (today/week/month/quarter/year): the Energy dashboard and HA's Utility Meter helper (quarterly
  cycle + offset) already do that from the totals. Per-period meters and estimated bill: decide later.

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

### Library at scale (agreed 2026-11-21)
Order: (1) plan versions and expiry in the EME importer: built 2026-11-21 (see CLAUDE.md, Import plans), (2) index split per state and customer type
(`index/AU-NSW-residential.json`; HA and the plan browser load only what's picked; plan paths unchanged),
(3) weekly EME check + badges, (4) full import, scheduled and staggered per state/customer type (~22,000 files:
NSW 7,032 res / 4,719 bus, VIC 3,430 / 2,466, QLD 1,448 / 953, SA 604 / 430, ACT 559 / 207, TAS 75 / 72).
Open: approve-and-merge each import PR by hand, or auto-merge after checks pass. WA: Synergy and Horizon Power scrapers built
(2026-11-21); NT later.

### Location → postcode → distributor, retailers and plans (design 2026-11-21, with step 2 above)
Suggest the user's distributor and plans from Home Assistant's Home zone, **as a suggestion they confirm**.
- **Offline only:** HA's home latitude/longitude → nearest postcode centre from a small shipped table (~3,000
  postcodes, tens of KB). No reverse-geocoding service; the location never leaves HA (not to GitHub either).
- **Postcode → distributor(s) and retailers** from a derived table built from Energy Made Easy plan geography
  (distributors, included/excluded postcodes): facts only, like `retailers.json`. One distributor: preselect it
  and the state. Two (boundary postcodes, e.g. Ausgrid/Endeavour): offer just those. Retailers and plans: only
  those sold there; plans that exclude the postcode are hidden.
- **Postcode first digit** narrows to a state as a sanity check and fallback: 0 NT (08xx-09xx) / ACT (02xx);
  1-2 NSW, except **ACT 2600-2618 and 2900-2920**; 3/8 VIC; 4/9 QLD; 5 SA; 6 WA; 7 TAS. Jervis Bay 2540 is an
  oddity; border towns follow their own state (Queanbeyan 2620 NSW, Albury 2640 NSW). 1xxx is PO boxes / large
  receivers, never a supply address: ask the user to check. A postcode that doesn't fit HA's state/time zone means
  the location guess is wrong: just ask for the postcode.
- **Why only a suggestion:** nearest-centre can pick a neighbouring postcode near an edge; the Home zone may never
  have been set (defaults elsewhere); network boundary files are partial and caveated (Ausgrid's Data.NSW area:
  not for "legal service boundaries"), and there's no single national DNSP-boundary dataset. Setup says "you look
  to be in 2580 (Essential Energy), is that right?" with the postcode editable; the bill names the distributor.
- **First task:** check how many EME plans fill in `includedPostcodes` / `excludedPostcodes` (diagnostic run on
  GitHub). If coverage is poor, the table needs distributor boundaries instead.

### WA scrapers (2026-11-21)
Horizon Power: works from GitHub; 75 plans merged (#26). Synergy: synergy.net.au answers GitHub's runners with
HTTP 403 for every header set (Fetch check), so it's run from home or with `--from` saved pages; Horizon's run
opens an issue when A2/K2 stop matching the library's A1/K1. First Synergy plans: from the maintainer's saved
pages, waiting on the DEBS PDF.

### Validating plans (design agreed 2026-11-21, after the importer)
**Status (2026-11-21):** 2 and 3 built as one-click: Confirm this plan → pre-filled issue → Plan feedback
workflow records it (no review PR, maintainer's choice: HACS users have GitHub accounts); 2 confirmations =
Confirmed (maintainer, 2026-11-21). Still to do: 1 (weekly EME check + badges), and Confirm this plan in HA's
Configure menu.
Statuses, shown as badges in the plan browser (and later in HA's library steps): **not checked** → **matches
Energy Made Easy** (or **differs from EME**) → **confirmed by N customers**. **Never one issue per plan**: the
library could hold thousands of plans.
1. **Automatic, weekly:** the library check (as proven on Covau) for every library plan with a plan ID. Results
   go in a generated `plans/checks.json` (per file: status matches / differs / not on EME any more, date,
   number of differing time ranges; no CDR plan text), committed like `retailers.json`; the plan browser reads it
   for the badges and a "Differs from EME" filter. Differences are fixed in batches by the maintainer (or a
   re-import), not by issues. "Matches EME" isn't "confirmed": EME can be wrong (Covau's AEST).
2. **People:** **Confirm this plan** on each plan page and in HA's Configure menu (next to Share this plan) opens a
   short pre-filled GitHub form: plan file, checked against (bill / fact sheet / EME) and its date, yes or no per
   part (rates, times, caps, supply, feed-in). A "no" is a correction, like Report a problem. Nothing sent from HA;
   no meter data or IDs.
3. **Recording confirmations:** a workflow reads the form (`github-script` only), checks the plan file exists, and
   adds the confirmation to an optional `checks` list on the plan (schema addition) in a PR the maintainer
   approves, batching confirmations so it isn't one PR each. One confirmation clears `needs_review`.

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
  - **Use 1, library importer:** built 2026-11-21 (`scripts/import_eme_plans.py`, Import plans workflow). New plans
    only; changed rates for plans already in the library are not handled yet (needs a plan-version step that
    keeps period ids). First run planned: residential TOU on Ausgrid, Endeavour Energy, Essential Energy.
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
    claiming it back is the business's accounting). Feed-in steps (2026-11-21): not text after all. The
    data splits them over separate entries (capped block + the rest for the same times, seasons as dated
    entries; seen in Covau, Flow Power, 1st Energy, Red Energy); the converter read only the first. Now combined
    (see CLAUDE.md); the check logs `FEED-IN TEXT` lines for entries whose text has numbers.
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
