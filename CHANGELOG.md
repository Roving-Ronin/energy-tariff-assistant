# Changelog

Every release opens with a **TL;DR**: a handful of short bullets, rewritten as a whole
whenever an entry is added. Entries are direct: symptom, cause, fix, with the issue link and
credit where there is one.

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
