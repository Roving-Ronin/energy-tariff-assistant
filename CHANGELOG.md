# Changelog

Every release opens with a **TL;DR**: a handful of short bullets, rewritten as a whole
whenever an entry is added. Entries are direct: symptom, cause, fix, with the issue link and
credit where there is one.

## v0.1.0b4 (2026-11-22)

Fourth test build (pre-release). **Breaking:** the per-period meters, supply charge, period and estimated bill
sensors are gone, and entity IDs change. Your entry is migrated (see *Changed*); dashboards using the old
entities need updating.

### TL;DR
- Import several plans and choose the one you're on: `sensor.eta_grid_import_price` and `_export_price` follow it,
  so the Energy dashboard, HAEO, EMHASS and Nimbus never need changing when you switch
- Price sensors publish forecasts in Amber Express's layout (`forecast` and Amber's `forecasts`), and HAEO now
  reads them as steps, not ramps
- Running totals for import cost, export compensation and the daily supply charge, kept across plan switches
- Capped rates tracked with a "used" sensor each; library plan updates under Settings → Updates
- Fixes "unknown fields ['confirmations']" when choosing a plan someone had confirmed

### Added
- **Active Tariff**: created with your first plan. `select.eta_grid_active_tariff_plan` chooses the plan you're on (also
  Configure, or the **Set active plan** action, e.g. from an automation on the day you switch retailer).
  `sensor.eta_grid_import_price` / `sensor.eta_grid_export_price` follow it. It holds the grid import and export
  energy sensors (set once) and the billing day.
- Totals on Active Tariff: `sensor.eta_grid_import_cost_total` and `sensor.eta_grid_export_compensation_total`
  ($, for the Energy dashboard's "entity tracking the total costs": each kWh costed at the real rate when it was
  counted, caps and period changes included), `sensor.eta_grid_supply_charge` and
  `sensor.eta_grid_supply_charge_total` (each day added at midnight).
- Price sensor attributes, laid out as Amber Express publishes them: `start_time` / `end_time` of the current
  period, `forecast` (`{time, value}`: now, every :00 and :30 for 48 hours, plus changes between), `forecasts`
  (Amber Electric's `{start_time, end_time, per_kwh}` in 30-minute slots, so EMHASS templates for Amber work
  unchanged), `plan_id`.
- One `…_<period>_used` sensor per capped rate ("first 50 kWh free"): kWh toward the cap, cap, remaining, reset.
- `update.eta_<plan>_plan`: when the library has newer prices, a new version, or a replacement for a withdrawn
  plan (new `replaced_by` field, set by the importer), it shows what changes; Install replaces the plan in place.
- The plan files other tools read: `energy_tariff_assistant/active/active.json` (the plan in use) and one per
  plan. [docs/using-plans.md](docs/using-plans.md) describes the format for converters (Energy Manager).

### Changed
- One entry per imported plan, named "Retailer - Plan" (*CovaU - SolarMax TOU*), with entity IDs from it
  (`sensor.eta_covau_solarmaxtou_import_price`), fixed when the plan is added: updates and replacing the plan
  keep every name, ID and history. Setup no longer asks for a name or energy sensors.
- Existing entries are migrated: the old meter, period, supply and bill entities are removed, the price sensors
  move to the new entity IDs, and Active Tariff is created with the entry's energy sensors and that plan in use.
- Price forecasts: no 5-minute steps up to the next half hour; after "now" every point is on :00 or :30.
- Plan library: plans that have ended (`effective_to` in the past) aren't offered; each reads "name · plan ID ·
  from date" ("SolarMax Residential TOU · COV1117612MRE3 · from 2026-07-01").
- The library now holds plans converted from Energy Made Easy for NSW, QLD and ACT networks, and Synergy and
  Horizon Power plans for WA. Converted plans say **Needs review** until two people on them confirm them in the
  [plan browser](https://roving-ronin.github.io/energy-tariff-assistant/).

### Removed
- Per-period energy and cost meters, the supply charge today/cycle and estimated bill sensors, the period and next
  period change sensors, and the `calibrate` and `reset_meters` actions. Which of these come back is still to
  be decided.

### Fixed
- Price forecasts in HAEO: the price sensors' `forecast` had no `interpolation_mode`, so HAEO drew straight lines
  between its points and priced the half hour before each period change as a blend (14:30–15:00 at ~0.36 instead
  of Shoulder then Peak). The price sensors now say `interpolation_mode: previous`: each price holds until the
  next point.
- Plan library: choosing a plan someone had confirmed failed with "$: unknown fields ['confirmations']": the
  plan browser's **Confirm this plan** (added after v0.1.0b3) records confirmations in the library's plan files,
  which v0.1.0b3 didn't know. Unknown top-level fields are now reported as a warning and ignored (inside a plan
  they still fail), and the library's own fields aren't copied into your entry.

## v0.1.0b3 (2026-11-20)

Third test build (pre-release).

### TL;DR
- Residential or business: the plan library and Build a plan ask which, Residential by default

### Added
- Plan library: **Customer type** (Residential by default) on the first step; later steps only offer states,
  distributors, retailers and plans for that type. Plans that don't say are shown for both.
- Build a plan: **Customer type** on the details step, saved in the plan (`customer_type`).
- Share this plan and the plan submission form: **Customer type**.
- `plans/index.json` lists each plan's `customer_type`.

## v0.1.0b2 (2026-11-20)

Second test build (pre-release).

### TL;DR
- Build a plan step by step: flat or time-of-use import and feed-in, with caps; saved to your config folder
- Plan library: choose country, state, distributor and retailer, then the plan, newest first
- Share your plan with the plan library from Home Assistant: Configure → Share this plan
- README says not to install yet, while it's under development

### Added
- **Build a plan step by step** (setup, and Configure → Replace the tariff plan): state, distributor (suggested
  per state, or typed), retailer, plan name and ID, start date and daily supply charge; import and feed-in each
  flat or time of use. Time of use: the rate for all other times, then any number of timed periods, each with
  one or more times (every day, weekdays, weekends or chosen days; an end before the start runs past midnight).
  Any rate can have a cap, followed by another period's rate or a rate of its own, resetting each day, month or
  billing cycle. A summary is shown before saving. The plan is validated like any other, saved to
  `energy_tariff_assistant/plans/<Retailer>-<Plan>-<State>-<DNSP>-<YYYYMMDD>.json` in the config folder (an existing
  file gets a `-2`, `-3`... copy, never overwritten), and used for the entry.
- Options flow: **Share this plan with the plan library** opens the GitHub plan submission form with the plan
  filled in (rates, times, retailer, region, DNSP and plan ID; never meter totals, sensor names or usage). Nothing
  is sent from Home Assistant: the user checks the form and submits it from their own account. A plan from the
  library is marked as a correction. A plan too long for a link (over ~7.5 KB) is shown to copy into the form.
- Plan submission form: a **Plan JSON** field.

### Changed
- Plan library: instead of one long list, choose the country (Australia first), state (NSW first), distributor
  and retailer, then the plan, newest first. Only choices with plans in the library are offered.
- README: a warning not to install while the integration is under development.

## v0.1.0b1 (2026-11-20)

First test build (pre-release): please report anything odd with the bug template.

### TL;DR
- First installable release: pick a plan, choose your import/export energy sensors, get prices and per-rate meters
- kWh and $ per period, today and per billing cycle, plus supply charge and an estimated bill
- Energy that spans a period boundary is split by time, and capped rates are split exactly at the cap

### Added
- Config flow: plan from the shared library, a file in the config folder, or pasted JSON; then the grid
  import and export energy sensors (Wh, kWh or MWh) and the billing cycle start day. Options flow to change
  the sensors or billing day, or to replace the plan (meters carry on).
- Sensors: import and export price (with a 2-day forecast attribute in the HAEO/EMHASS format), period in
  force, next period change, daily supply charge.
- Metering: energy and cost (credit for export) per period and in total, today and this billing cycle; supply
  charge accrued; estimated bill (import cost + supply − feed-in credit). Totals survive restarts, and energy
  used while Home Assistant was stopped is counted from the saved baseline.
- Actions: `calibrate` (set a per-period meter to match a bill) and `reset_meters` (start a new day or cycle).
- Diagnostics download with the plan and meter state.
- Engine: `Ledger` (pure Python) for per-period, tier-aware accounting with day, month and billing-cycle
  buckets.
