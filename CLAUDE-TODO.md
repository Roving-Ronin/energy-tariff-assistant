# CLAUDE-TODO

Work that's planned, waiting on something, or decided against, with what's needed to pick it up again. Read
this at the start of a session (CLAUDE.md points here); update it when an item moves, and delete items once
they're released. Newest decisions win: date each status line.

## Waiting

### v0.1.0: first release
**Status (2026-11-20):** Phase 2 merged to main. Before releasing:
- Run it on a live Home Assistant 2026.11 (tests only ran on 2026.2.3, the `hacs.json` minimum): setup from the
  library, the maintainer's own Sigenergy import/export sensors, check the Covau plan's prices and a day's meters
  against the Energy dashboard.
- Repo topics and description (HACS Validate fails without topics): the maintainer sets them on GitHub (this
  session's token can't change repo settings). Suggested topics: `home-assistant`, `hacs`,
  `home-assistant-custom-component`, `homeassistant-integration`, `energy`, `electricity-tariff`, `utility-meter`,
  `australia`.
- Google Drive backup secrets (`GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN`), or that
  workflow fails on every push to main.
- Date the CHANGELOG section (`## v0.1.0 (YYYY-MM-DD)`, currently "(unreleased)") and release with a PR titled
  `v0.1.0: …`.

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

## Later (not started)
- **Plan library growth:** more AU plans (Ausgrid, Endeavour, Evoenergy, Energex/Ergon, CitiPower/Powercor,
  SA Power Networks...). An importer from Energy Made Easy / CDR (Consumer Data Right, product reference data)
  would beat hand entry: check the CDR energy plan API's licence and shape first.
- **Library update notice:** a Repairs issue when the library has a newer version of the entry's plan
  (`plan_source.library_file` is stored for this), showing the changed rates; never update silently.
- **Region step in the library flow** once there are more than a handful of plans (today one dropdown, labels
  start with the state).
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
