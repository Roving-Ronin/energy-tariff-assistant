# Using these plans in other tools

Energy Tariff Assistant's plans are plain JSON files (`"format": "ha_tariff_plan"`, version 1). Other tools
(Energy Manager, scripts, other integrations) can read them, from the shared library or from a Home Assistant
install, and convert them to their own format. This page is what such a converter needs.

## Where the plans are

**The shared library** (every plan, published on GitHub):

| What | Where |
| --- | --- |
| Index of every plan | `https://raw.githubusercontent.com/Roving-Ronin/energy-tariff-assistant/main/plans/index.json` |
| A plan | `https://raw.githubusercontent.com/Roving-Ronin/energy-tariff-assistant/main/` + the index entry's `file` |
| JSON Schema | `custom_components/energy_tariff_assistant/schema/tariff-plan.schema.json` in the repository |
| Plan browser (people) | <https://roving-ronin.github.io/energy-tariff-assistant/> |

Each index entry has `file`, `country`, `region` (state), `dnsp` (network), `retailer`, `plan`, `plan_id`,
`customer_type`, `effective_from`, `effective_to` and `needs_review`. Skip entries whose `effective_to` has
passed: they're kept for history.

**In a Home Assistant install** (the plans that user imported), under the config folder:

| File | What it is |
| --- | --- |
| `energy_tariff_assistant/active/active.json` | The plan the user is on now (Active Tariff). Rewritten when they switch plans or install an update |
| `energy_tariff_assistant/active/eta_<retailer>_<plan>.json` | Each imported plan, e.g. `eta_covau_solarmaxtou.json` |

These hold exactly what Home Assistant prices with: the library's own fields (`confirmations`, `problems`,
`replaced_by`) are left out.

## Reading a plan

The essentials (the schema has every field and its rules):

- **Prices include GST** and are dollars per kWh (`rate`). Feed-in is positive: what the customer is paid.
- `timezone`: an IANA zone (`Australia/Sydney`, or `Australia/Brisbane` for plans on fixed AEST all year).
  All window times are local wall-clock time in that zone.
- `supply_charge`: `{"rate": 1.23, "unit": "per_day"}`.
- `import` and `export`: `{"periods": [...]}`. Each period has an `id` (stable across plan versions: use it as
  the key for anything you store), a `name`, and either a `rate` or `tiers`. Exactly one period is `default`
  (all times no other period covers); the others have `windows`.
- A window: `{"days": "all" | "weekdays" | "weekends" | [ "mon", ... ], "start": "15:00", "end": "21:00"}`
  (or `"all_day": true` instead of the times),
  optionally with `seasons` (season ids from the plan's `seasons`: `{"nov_mar": [11, 12, 1, 2, 3], ...}`).
  `end` may be `24:00`. The first non-default period whose window covers a moment wins.
- `tiers` (capped rates): `[{"up_to_kwh": 50, "rate": 0.0}, {"use_period": "shoulder"}]`: the first 50 kWh in
  this period's windows at 0.0, then priced as the `shoulder` period. `tier_reset` says when the count restarts:
  `day`, `month` or `cycle` (the customer's billing cycle).
- `holidays.treat_as`: `none` (public holidays priced like any day), `saturday` or `sunday`.
- `feed_in_option`: set ("A", "B") when the retailer offers a choice of feed-in and this plan is one of them. Each
  option is a separate plan file with the same `plan_id`: identify a plan by `plan_id` + `dnsp` + `feed_in_option`.
- `effective_from` / `effective_to`: when the plan applies (a later version is a separate file).
- `demand`: `[{"id", "name", "rate", "unit": "per_kw_per_day" | "per_kw_per_month", "windows": [...],
  "measure": {"interval_minutes": 30, "method": "max" | "mean_of_top_n", "n", "over": "month" | "bill" |
  "rolling_12_months", "minimum_kw"}}]`. Demand is the highest interval reading (kWh x 60 / interval minutes)
  starting inside the windows over the `over` period; the charge is max(demand, `minimum_kw`) x `rate` x days (a
  monthly rate counts each day as a share of the month). Home Assistant measures it from the grid import sensor.
- `controlled_load`: in the format, not yet used by the Home Assistant entities.
- `notes`: anything the format couldn't express exactly, and where the plan came from.

## Keeping up with changes

- **Additions are safe to ignore.** New information-only fields can appear at the top level of a plan (e.g.
  `confirmations`). Home Assistant reports and ignores top-level fields it doesn't know; a converter should too.
- **A change to how prices are read bumps `version`.** If a plan's `version` isn't one you know, don't convert
  it: refuse it, so you never misprice.
- **Plans change in the library.** The importer updates a plan in place when its prices change (same `plan_id`),
  adds a new file for a new version (`COV1117612MRE3` → `COV1117612MRE4`, the old file gets `effective_to`), and
  sets `replaced_by` (a library `file` path) on a plan the retailer withdrew when it finds its replacement.
  Period `id`s are kept across all of these.
- `needs_review: true` means nobody has checked the plan against a bill yet (converted from Energy Made Easy).
  It becomes `false` once two people on the plan confirm it in the plan browser.
