# Changelog

Every release opens with a **TL;DR**: a handful of short bullets, rewritten as a whole
whenever an entry is added. Entries are direct: symptom, cause, fix, with the issue link and
credit where there is one.

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
