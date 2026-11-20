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

### Retailer coverage: first run
**Status (2026-11-20):** script and weekly workflow added; not yet run against the live API (this session's
workspace can't reach `cdr.energymadeeasy.gov.au`). After the first run (Actions → Retailer coverage → Run
workflow): check `unmapped_distributors` and `failed_brands` in `plans/retailers.json` and extend `DISTRIBUTORS`
(the plan data's exact distributor names are unknown until then), and check the plan counts look sane
(third-party API listings saw ~1,300 AGL and ~3,600 Origin plans in mid-2026, unfiltered; electricity-only
  counts will be lower).

## Next (after retailer coverage has data)
- **Wizard and library: suggest retailers by DNSP** from `plans/retailers.json` (fetched like `index.json`):
  in Build a plan, ask the DNSP before the retailer and offer the retailers selling there; in the library, show
  "retailers with no library plans here yet" as a prompt to share one.
- **Importer from Energy Made Easy:** Get Generic Plan Detail (`x-v: 3`) → `ha_tariff_plan`, so a user picks
  retailer + plan and gets the real rates; needs the tariff-period mapping worked out and its own tests.

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
