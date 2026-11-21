<p align="center"><img src="images/banner.png" alt="Energy Tariff Assistant: Community Tariff Plans &amp; Utility Metering by Roving-Ronin" width="700"></p>

[![Marketplace][marketplace-badge]][marketplace-url]
[![release][release-badge]][release-url]
[![downloads][downloads-badge]][release-url]
[![stars][repo-stars-badge]][repo-url]
[![license][license-badge]][license-url]
[![installs][installs-badge]][installs-url]

[![maintained][maintained-badge]][commits-url]
[![activity][activity-badge]][commits-url]
[![Validate][validate-badge]][validate-url]
[![Tests][tests-badge]][tests-url]

___

> [!WARNING]
> **Under development: please don't install this yet.** Energy Tariff Assistant is being built and tested.
> Entities, settings and stored meter totals may change or be reset between versions without a migration.
> Watch or star the repository to hear when the first stable release is out.

A Home Assistant custom integration for Australian electricity tariff plans. Import the plans you're on (or
considering) from the shared library, a file or pasted JSON, choose the one you're on, and get **price sensors
with forecasts** that the Energy dashboard, HAEO, EMHASS and Nimbus read directly:

- `sensor.eta_grid_import_price` and `sensor.eta_grid_export_price`: the price now and the next 48 hours, for
  the plan you're on. Switch plans and these keep their names and history.
- **Capped rates** ("first 50 kWh free", "first 10 kWh at 15c") tracked against your grid energy sensors, so
  the price changes when the cap is used.
- Running **totals** for import cost, export compensation, the daily supply charge and demand charges, kept across
  plan switches.
- Each imported plan's own price sensors, and **library updates** under Settings → Updates.

It replaces the usual pile of `input_number` rates, time helpers, a template price sensor with a hand-written
forecast, and the automations that switch them at every period boundary.

> **Status: v0.1 (test builds).** Time-of-use import, feed-in (flat, time-of-use or capped), daily supply charge
> and public holidays are working and tested; demand charges are measured. Controlled-load circuits are in the
> plan format and the rate engine, but don't have entities yet.

## Installation

Energy Tariff Assistant isn't in the default list yet, so add it as a custom repository first.

### Marketplace (Home Assistant 2026.11 and newer)

[![Open this repository in the Marketplace][marketplace-button]][marketplace-url]

Or add it manually:

1. Go to **Settings → Marketplace**, open the ⋮ menu and choose **Custom repositories**
2. Add `https://github.com/Roving-Ronin/energy-tariff-assistant` as an **Integration**
3. Search for **Energy Tariff Assistant** and click **Download**
4. Restart Home Assistant, then go to **Settings → Devices & services → Add integration → Energy Tariff Assistant**

### HACS (Home Assistant 2026.10 and older)

[![Open this repository in HACS][hacs-button]][hacs-url]

Or add it manually:

1. Open **HACS**, open the ⋮ menu and choose **Custom repositories**
2. Add `https://github.com/Roving-Ronin/energy-tariff-assistant` as an **Integration**
3. Search for **Energy Tariff Assistant** and click **Download**
4. Restart Home Assistant, then go to **Settings → Devices & services → Add integration → Energy Tariff Assistant**

Needs Home Assistant 2026.2 or newer.

## Setup

1. **Add a plan:** Settings → Devices & services → Add integration → **Energy Tariff Assistant**, then choose
   where the plan comes from:
   - **Shared plan library:** browse it first in the [plan browser][plan-browser] if you like. Then narrow it down
     step by step (customer type and country, state, distributor (DNSP), retailer) and pick the plan, newest
     first. Plans that have ended aren't offered. Internet is needed only during setup; the plan is stored with
     the entry.
   - **Build a plan step by step:** enter it from your bill or the plan's fact sheet (BPID): state, distributor,
     retailer, plan name, start date, daily supply charge and customer type; then import and feed-in, each one
     rate all the time or time of use, with caps if the plan has them ("first 50 kWh a day free, then
     Shoulder"). You check a summary before saving. The plan is saved to `energy_tariff_assistant/plans/` in your
     config folder and can be shared with the library afterwards.
   - **Paste plan JSON** or a **plan file in my config folder** (see *Plan format* below).

   The plan is added as **"Retailer - Plan"** (e.g. *CovaU - SolarMax TOU*: customer type, state and network are
   left out, since you know them). Rename it in Home Assistant if you like.
2. **Active Tariff** is created with your first plan, with that plan in use. Open it (**Configure**) to choose
   your **grid import and export energy sensors** (kWh, Wh or MWh; `total` or `total_increasing`, the same ones
   the Energy dashboard uses) and the day your billing cycle starts. They're set once: every plan counts the same
   readings toward its own caps.
3. **Add more plans** the same way (a plan you're switching to, or comparing). They're added alongside; the plan
   in use only changes when you choose another.

## What you get

### Active Tariff: the plan you're on

| Entity | What it is |
| --- | --- |
| `select.eta_grid_active_tariff_plan` | The plan you're on, from your imported plans |
| `sensor.eta_grid_import_price` | Price now, `AUD/kWh`, with forecasts (below). **Energy dashboard:** Grid consumption → *Use an entity with current price*. **HAEO, Nimbus:** the import price sensor |
| `sensor.eta_grid_export_price` | Feed-in price now (positive: what you're paid), with forecasts |
| `sensor.eta_grid_import_cost_total` | $ for energy bought, ever-increasing. **Energy dashboard:** *Use an entity tracking the total costs* (instead of the price entity) |
| `sensor.eta_grid_export_compensation_total` | $ earned for energy fed in, ever-increasing. **Energy dashboard:** Return to grid → total compensation |
| `sensor.eta_grid_supply_charge` | The active plan's daily supply charge |
| `sensor.eta_grid_supply_charge_total` | Supply charges so far, a whole day at a time (attribute `days_counted`) |

**Price sensor attributes** (laid out as [Amber Express](https://github.com/hass-energy/amber-express) publishes
them, so tools built for it work):

| Attribute | |
| --- | --- |
| `start_time`, `end_time` | When the current period started and when it next changes |
| `period`, `period_name`, `tier`, `charged_as` | e.g. `free`, `Free`, `0`; `charged_as` is the period whose rate applies once a cap is used |
| `active_plan`, `source_entity`, `retailer`, `plan`, `plan_id` | Which plan the price comes from |
| `interpolation_mode` | `previous`: each price holds until the next point (HAEO needs this) |
| `forecast` | `[{time, value}]`: now, then every :00 and :30 for 48 hours, plus any change in between. Read by Nimbus and HAEO |
| `forecasts` | `[{start_time, end_time, per_kwh}]` in fixed 30-minute slots for 48 hours: Amber Electric's layout. Read by HAEO, and by EMHASS templates written for Amber |

The forecasts aren't recorded in history. For EMHASS, for example:

```yaml
"load_cost_forecast": {{ (state_attr('sensor.eta_grid_import_price', 'forecasts') | map(attribute='per_kwh') | list)[:48] }}
"prod_price_forecast": {{ (state_attr('sensor.eta_grid_export_price', 'forecasts') | map(attribute='per_kwh') | list)[:48] }}
```

**Switching plans:** choose another in `select.eta_grid_active_tariff_plan`, in Active Tariff → Configure, or with the
**Set active plan** action (`energy_tariff_assistant.set_active_plan`, by plan entry or device), e.g. from an
automation on the day you change retailer. From that moment the grid sensors follow the new plan; their names,
history and the totals carry on. If you remove the plan in use, Active Tariff shows *No active plan* (and a
repair) until you choose one: it never picks another on its own.

### Each imported plan

With a plan named *CovaU - SolarMax TOU*:

| Entity | What it is |
| --- | --- |
| `sensor.eta_covau_solarmaxtou_import_price` / `_export_price` | This plan's prices and forecasts (same attributes as above), whether or not it's the plan in use: handy for comparing |
| `sensor.eta_covau_solarmaxtou_import_free_used` | One per capped rate: kWh counted toward the cap since it last reset. Attributes `cap_kwh`, `remaining_kwh`, `resets`, `over_cap_rate_from`, `rate_now` |
| `update.eta_covau_solarmaxtou_plan` | Library plans only: on when the library has newer prices or a new version of this plan. It shows what changes (e.g. *Import Peak: 0.45331 → 0.4621*); **Install** replaces the plan in place |

Entity IDs come from the plan when you add it and never change after: installing an update, or replacing the plan
(**Configure → Replace the tariff plan**), keeps every name, ID and history.

### Plan files for other tools

Each plan's JSON is written to `energy_tariff_assistant/active/<entity prefix>.json` in your config folder, and
the plan in use to `energy_tariff_assistant/active/active.json`, kept up to date when you switch or update. Other
tools (Energy Manager, scripts) can read them; see [docs/using-plans.md](docs/using-plans.md).

## What the numbers mean (and don't)

**They are estimates measured in Home Assistant, not your bill.** They come from your own energy sensors, on
Home Assistant's clock, and won't match the retailer's figures to the cent. Smart-meter data is in 30-minute
intervals and the retailer rounds per line item; your inverter or CT clamp measures differently.

How energy is shared between periods: the energy between two readings is taken as used evenly over the time
between them. When a period boundary (or midnight) falls in between, the energy is split in proportion to the
time either side, and each part is priced at its own rate. (The Energy dashboard's price-entity method prices
each reading at the price when it arrives; the cost totals don't have that lag.) If Home Assistant was stopped,
the energy used meanwhile is spread over the time it was down.

Caps are split exactly: with "first 50 kWh/day free from 11:00 to 14:00, then shoulder", the 50th kWh is free and
the 51st is charged at shoulder, both counted toward the *Free* cap. The price sensor changes to the shoulder rate
as soon as the cap is used up. Without grid energy sensors, a capped rate is assumed to last its whole window.

A drop of under 1% in a source reading is ignored as sensor jitter; a bigger drop is taken as the meter
resetting to zero.

The cost and compensation totals show the kWh behind them (`energy_kwh`) and the average rate paid (`average_rate`),
and which grid sensor they count (`source`). Totals are written at most every 30 seconds (every reading still
counts), so a meter that reports every few seconds doesn't fill the history database.

**Set total** (`energy_tariff_assistant.calibrate`) sets one of the totals, e.g. to match a bill:

```yaml
action: energy_tariff_assistant.calibrate
target:
  entity_id: sensor.eta_grid_import_cost_total
data:
  value: 412.37
```

It carries on counting from that value. Its `last_reset` moves to that moment, so the Energy dashboard starts a new
cycle there instead of counting the jump as cost.

The supply charge total adds each day at midnight at the rate of the plan in use then, from the day Active Tariff
was set up. Controlled-load supply charges, discounts and concessions aren't included yet.

**Demand charges** (plans that charge for your highest half hour in a window, e.g. weekdays 5pm-8pm): each plan
gets a `sensor.<plan>_demand_<charge>_peak` (kW) per demand charge, measured from the grid import sensor: the
highest 30-minute reading in the charge's windows this month (or billing cycle, or rolling 12 months, as the plan
says), with the rate, days counted and the charge so far as attributes. `sensor.eta_grid_demand_charge_total` on
Active Tariff is the running $ total for the plan in use: peak kW x rate x days, as Energy Made Easy explains it. A
new, higher peak re-prices the earlier days of the month, so the total jumps by the difference. A rolling 12-month
charge only knows the months it has measured.

## Plan format in brief

A plan is one JSON file (`"format": "ha_tariff_plan"`, `"version": 1`). Rates include GST, per kWh, in dollars.
Each of `import`, `export` and any `controlled_load` has a list of periods; one period is the `default` and covers
any time no other window claims.

```json
{ "id": "free", "name": "Free",
  "windows": [{ "days": "all", "start": "11:00", "end": "14:00" }],
  "tiers": [{ "up_to_kwh": 50, "rate": 0.0 }, { "use_period": "shoulder" }],
  "tier_reset": "day" }
```

- **Windows**: `days` is `all`, `weekdays`, `weekends` or a list like `["mon","tue"]`. `start` is inclusive,
  `end` exclusive. An end before the start wraps past midnight, matched by calendar day: "weekdays 22:00-07:00"
  covers Monday 00:00-07:00 but not Saturday 00:00-07:00. Use `"all_day": true` rather than `00:00`-`00:00`.
- **Tiers** express caps and stepped rates. The last tier always says what happens after the cap (`rate`, which
  can be 0, or `use_period`). `tier_reset` is `day`, `month` or `bill` (your billing cycle).
- **Holidays**: `"holidays": {"treat_as": "sunday"}` treats public holidays (from `python-holidays`, by state)
  as Sundays.
- **Seasons**, **demand charges** and **controlled load** are supported in the format; see
  [`schema/tariff-plan.schema.json`](custom_components/energy_tariff_assistant/schema/tariff-plan.schema.json).

See [`plans/AU/NSW/CovaU-SolarMaxTOU-NSW-EssentialEnergy-20260701.json`](plans/AU/NSW/CovaU-SolarMaxTOU-NSW-EssentialEnergy-20260701.json)
for a complete example.

## Adding a plan to the library

The [plan browser][plan-browser] shows every library plan with its times on a 24-hour timeline, its rates and
caps, and anything the validator found. If you're on a plan, check it against your bill: **Confirm this plan**
opens a filled-in GitHub issue (just press Submit), and once two people on the plan have confirmed it, it shows as **Confirmed**; **Report a problem**
opens a correction with the plan filled in, and the plan goes back to **Needs review** until it's fixed.
Its **Coverage** page shows which distributors have no library plans yet.

**From Home Assistant:** Settings → Devices & services → Energy Tariff Assistant → **Configure** → **Share this
plan with the plan library**. It opens the [plan submission form][plan-form] on GitHub with the plan filled in
(rates, times, retailer and region only; nothing about your home, meters or usage). Nothing is sent from Home
Assistant: you check the form and submit it from your own GitHub account. A plan too long for a link is shown
for you to copy and paste into the form.

Don't want to write JSON? Use the [plan submission form][plan-form] with your plan's details (the BPID or
Energy Made Easy page is the easiest source).

Or, as a pull request:

1. Write the plan file (or convert one: `python scripts/import_energy_manager.py --write <file>`).
2. Put it at `plans/<country>/<region>/<Retailer>-<PlanSlug>-<Region>-<DNSP>-<YYYYMMDD>.json`.
3. Run `python scripts/validate_plans.py` and `python scripts/build_index.py`.
4. Open a pull request. The Plans workflow re-runs both checks.

## Development

```
pip install -r requirements_test.txt
bash scripts/fetch_fixtures.sh   # importer fixtures from energy-manager-tariffs
python -m pytest
```

| Path | What it is |
| --- | --- |
| `custom_components/energy_tariff_assistant/engine/` | Rate engine and metering: plan model, resolver, forecast, validator, ledger. No Home Assistant imports, so the plan scripts and CI use it directly. |
| `custom_components/energy_tariff_assistant/` | The integration: config flow, runtime (state tracking, timers, storage), sensors. |
| `plans/` | Shared plan library, plus a generated `index.json`. |
| `scripts/` | `validate_plans.py`, `build_index.py`, `import_energy_manager.py`, `fetch_eme_retailers.py` (see *Retailer coverage*). |
| `tests/` | Engine tests (run with just `holidays`, `jsonschema`, `pytest`). |
| `tests_ha/` | Integration tests with `pytest-homeassistant-custom-component`. |

The importer tests use plan files from energy-manager-tariffs. They are fetched rather than committed because
that repository has no licence file; without them those tests are skipped.

### Retailer coverage

`plans/retailers.json` lists which retailers sell electricity in which distributor (DNSP) areas and states, with
plan counts. The **Retailer coverage** workflow rebuilds it weekly with `scripts/fetch_eme_retailers.py` from the
AER's [Energy Product Reference Data](https://www.aer.gov.au/energy-product-reference-data) (Energy Made Easy,
Consumer Data Right; Victorian plans come from DEECA), and commits it when it changes. Only derived facts are
stored: retailer, distributor, state and counts, no plan text or prices. WA and the NT aren't in that data.

### Importing plans from Energy Made Easy

**Actions → Import Plans - NSW** (or QLD, VIC, SA, ACT, TAS) **→ Run workflow** converts current Energy Made Easy
plans for that state's distributors (all of them unless you remove some), customer type (Residential or Business;
each run is titled e.g. "Import Plans - NSW Residential") and pricing, validates them and opens a pull
request adding the ones that pass, each marked `needs_review: true`. Run again later, it also keeps the library
current: new prices or a new version of a plan get a new file (the previous one ends the day before, and the
plan's period names are kept so meters carry on), and plans no longer on Energy Made Easy are ended rather than
deleted. Reviewed and hand-made plans are never changed; any differences are listed in the pull request.
It needs **Settings → Actions → General → Allow GitHub Actions to create and approve pull requests**. The run
checks the import itself (plan validation, index, plan browser, tests) and merges its pull request when they pass;
untick **Merge the pull request once the checks pass** to review it first (GitHub then asks you to **Approve and run
workflows** before the pull request's own checks run).

### Synergy plans (WA)

WA isn't on Energy Made Easy. `scripts/fetch_synergy_plans.py` reads the price tables on Synergy's four plan pages
(Home Plan A1, Midday Saver, EV Add On, Home Business Plan K1) and the DEBS feed-in schedule. Every rate label and
stated time is checked against what the plan expects; anything unexpected stops it with the reason and changes
nothing.

synergy.net.au refuses GitHub's servers, so it isn't scheduled. Run it from a home connection when prices change
(normally 1 July; the Horizon Power workflow opens an issue when WA's tariffs change):

```bash
pip install -r requirements_test.txt pypdf
python scripts/fetch_synergy_plans.py                    # fetch from Synergy's site and write the plans
python scripts/fetch_synergy_plans.py --from ~/synergy   # or: pages saved from a browser
```

For `--from`, save each of the four plan pages with your browser's **Save page as… → Webpage, single file
(.mhtml)**, plus the DEBS pricing schedule PDF, into one folder. Then open a pull request with the new plan files.

### Horizon Power plans (regional WA)

The **Import Plans - WA - Horizon Power** workflow runs weekly on Horizon Power's pricing page. Prices are the WA
Government's uniform tariffs, but feed-in depends on the town, so each of the 37 towns gets an **A2 Residential**
plan with DEBS (solar up to 5 kW) and one with **Buyback Bonus** (over 5 kW, Community Wave members, summer and
winter rates), plus one **K2 Combined Use** plan (no feed-in). REBS (closed) and the other tariffs are left out.
A2 and K2 are the same government tariffs as Synergy's A1 and K1, so when they change the workflow opens one issue
asking for the Synergy plans to be updated.

### Plan browser

The [plan browser][plan-browser] is a static page in `site/` (plain HTML, CSS and JavaScript). The **Plan browser**
workflow builds it with `scripts/build_site.py` (the page, the library plans, their validation results and the
retailer coverage) and publishes it to GitHub Pages whenever `plans/`, `site/` or the scripts change on `main`.
To try it locally: `python scripts/build_site.py --out /tmp/site && python -m http.server -d /tmp/site`.

### Releasing

Merge a PR titled with its version (e.g. `v0.2.0: demand charges`) and with a `## v0.2.0 (YYYY-MM-DD)` section in
`CHANGELOG.md`: the Auto Release workflow publishes the release, writes the version into `manifest.json`, attaches
`energy_tariff_assistant.zip` and commits the version back to `main`. **`[skip release]`** in the PR title, or on a line of
its own in the description, merges without releasing (a sentence that only mentions it doesn't count).

## Licence

GPL-3.0. See `LICENSE`.

<!-- Badges -->
[marketplace-badge]: https://img.shields.io/badge/Marketplace%20-%20Custom-orange?style=flat-square
[release-badge]: https://img.shields.io/github/v/release/Roving-Ronin/energy-tariff-assistant?style=flat-square&color=blue&label=Release
[downloads-badge]: https://img.shields.io/github/downloads/Roving-Ronin/energy-tariff-assistant/total?style=flat-square&label=Downloads
[installs-badge]: https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=integration%20usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.energy_tariff_assistant.total&style=flat-square
[maintained-badge]: https://img.shields.io/badge/Maintained-yes-brightgreen?style=flat-square
[activity-badge]: https://img.shields.io/github/commit-activity/y/Roving-Ronin/energy-tariff-assistant?style=flat-square&label=Commit%20Activity
[license-badge]: https://img.shields.io/github/license/Roving-Ronin/energy-tariff-assistant?style=flat-square&label=License
[repo-stars-badge]: https://img.shields.io/github/stars/Roving-Ronin/energy-tariff-assistant?style=flat-square&logo=github&label=Repo%20Stars
[validate-badge]: https://img.shields.io/github/actions/workflow/status/Roving-Ronin/energy-tariff-assistant/validate.yaml?branch=main&label=Validate&style=flat-square
[tests-badge]: https://img.shields.io/github/actions/workflow/status/Roving-Ronin/energy-tariff-assistant/tests.yaml?branch=main&label=Tests&style=flat-square

<!-- Buttons -->
[marketplace-button]: https://my.home-assistant.io/badges/marketplace_repository.svg
[marketplace-url]: https://my.home-assistant.io/redirect/marketplace_repository/?owner=Roving-Ronin&repository=energy-tariff-assistant&category=integration
[hacs-button]: https://my.home-assistant.io/badges/hacs_repository.svg
[hacs-url]: https://my.home-assistant.io/redirect/hacs_repository/?owner=Roving-Ronin&repository=energy-tariff-assistant&category=integration

<!-- Links -->
[repo-url]: https://github.com/Roving-Ronin/energy-tariff-assistant
[release-url]: https://github.com/Roving-Ronin/energy-tariff-assistant/releases
[installs-url]: https://analytics.home-assistant.io/custom_integrations.json
[commits-url]: https://github.com/Roving-Ronin/energy-tariff-assistant/commits/main
[validate-url]: https://github.com/Roving-Ronin/energy-tariff-assistant/actions/workflows/validate.yaml
[tests-url]: https://github.com/Roving-Ronin/energy-tariff-assistant/actions/workflows/tests.yaml
[license-url]: https://github.com/Roving-Ronin/energy-tariff-assistant/blob/main/LICENSE
[plan-form]: https://github.com/Roving-Ronin/energy-tariff-assistant/issues/new?template=plan_submission.yml
[plan-browser]: https://roving-ronin.github.io/energy-tariff-assistant/
