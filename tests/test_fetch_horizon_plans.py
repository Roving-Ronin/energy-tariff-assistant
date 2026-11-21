"""scripts/fetch_horizon_plans.py on a hand-written page shaped like Horizon Power's (no network)."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_horizon_plans as hp  # noqa: E402
import fetch_synergy_plans as syn  # noqa: E402

from engine import validate_plan  # noqa: E402

TODAY = date(2026, 11, 21)
TOWNS = {  # town: (DEBS off-peak, peak), (Bonus summer off, summer peak, winter off, winter peak)
    "Broome": ((3.00, 10.00), (3.00, 33.26, 3.00, 10.00)),
    "Cue": ((11.33, 37.76), (11.33, 37.76, 11.33, 37.76)),
    "Camballin/Looma": ((16.80, 55.99), (16.80, 55.99, 16.80, 55.99)),
}
TOWNS.update({f"Town {n}": ((3.00, 10.00), (3.00, 10.00, 3.00, 10.00)) for n in range(8)})


def page(towns=TOWNS, effective="1 July 2026", debs_header="Between 3pm and 9pm", bonus_towns=None):
    p = lambda text: f"<p>{text}</p>"  # noqa: E731
    debs_rows = "".join(p(t) + p(f"{a:.2f}") + p(f"{b:.2f}") for t, ((a, b), _) in towns.items())
    bonus_rows = "".join(p(t) + "".join(p(f"{v:.2f}") for v in rates)
                         for t, (_, rates) in (bonus_towns or towns).items())
    return "".join([
        f"<html><body>{p(f'All prices are correct as at {effective}, include GST.')}",
        p("A2 Residential Tariff"), p("The A2 tariff is the standard residential tariff."), p("A2 tariff rates"),
        p("Supply charge (<span>per day</span>)&#9432;"), p("&#9432;$1.1924"),
        p("Supply charge (additional dwelling) (<span>per day</span>)"), p("$0.4741"),
        p("Electricity unit charge (cents/kWh)&#9432;"), p("33.2621"),
        p("K2 Combined Use Tariff"), p("The K2 tariff is for premises used for a combination of purposes."),
        p("Supply charge (per day)"), p("$2.1041"), p("0-20 kWh/day (cents/kWh)"), p("34.7481"),
        p("20-1,650 kWh/day (cents/kWh)"), p("32.7455"), p("&gt;1,650 kWh/day (cents/kWh)"), p("36.9194"),
        p("Distributed Energy Buyback Scheme (DEBS)"), p("DEBS is the most common buyback scheme."), p("Learn more"),
        p("Before 3pm"), p("cents/kWh"), p(debs_header), p("cents/kWh"), debs_rows,
        p(f"All prices are correct as at {effective} and exclude GST."),
        p("Renewable Energy Buyback Scheme (REBS)"), p("No longer available."),
        p("Buyback Bonus"), p("Buyback Bonus is a buyback scheme for systems over 5kW."),
        p("Summer months (1 Nov to 31 March)"), p("cents/kWh"), p("Winter months (1 April to 31 October)"),
        p("cents/kWh"), p("Off-peak"), p("Peak"), p("Off-peak"), p("Peak"), bonus_rows,
        p(f"All prices are correct as at {effective} and exclude GST."), "</body></html>",
    ]).encode()


@pytest.fixture
def library(tmp_path, monkeypatch):
    plans_dir = tmp_path / "plans"
    monkeypatch.setattr(syn, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(syn, "plan_files", lambda: sorted(plans_dir.rglob("*.json")))
    return plans_dir


def plans_in(directory):
    return {json.loads(f.read_text())["plan"]: json.loads(f.read_text()) for f in sorted(directory.rglob("*.json"))}


def test_a2_per_town_for_each_buyback_scheme_and_one_k2(library):
    changes = hp.run(lambda url: page(), TODAY, log=lambda _: None)
    plans = plans_in(library)
    assert len(changes) == len(plans) == 2 * len(TOWNS) + 1
    for plan in plans.values():
        assert validate_plan(plan, today=TODAY).ok
        assert (plan["region"], plan["dnsp"], plan["retailer"]) == ("WA", "Horizon Power", "Horizon Power")
    broome = plans["A2 Residential (Broome)"]
    assert broome["supply_charge"]["rate"] == 1.1924  # labels with spans and info icons still read
    assert broome["import"]["periods"] == [{"id": "anytime", "name": "Anytime", "rate": 0.332621, "default": True}]
    assert [(p["id"], p["rate"]) for p in broome["export"]["periods"]] == [("peak", 0.1), ("off_peak", 0.03)]
    # Buyback Bonus: the same four ids for every town, whether or not its seasons differ
    for town in ("Broome", "Cue"):
        bonus = plans[f"A2 Residential Buyback Bonus ({town})"]
        assert [p["id"] for p in bonus["export"]["periods"]] == [
            "peak_nov_mar", "peak_apr_oct", "off_peak_nov_mar", "off_peak_apr_oct"]
    assert plans["A2 Residential Buyback Bonus (Broome)"]["export"]["periods"][0]["rate"] == 0.3326
    k2 = plans["K2 Combined Use"]
    assert k2["import"]["periods"][0]["tiers"] == [
        {"up_to_kwh": 20, "rate": 0.347481}, {"up_to_kwh": 1650, "rate": 0.327455}, {"rate": 0.369194}]
    assert k2["export"]["periods"][0]["rate"] == 0.0
    assert (library / "AU" / "WA" / "HorizonPower-A2ResidentialCamballinLooma-WA-HorizonPower-20260701.json").is_file()


def test_unchanged_then_new_prices(library):
    hp.run(lambda url: page(), TODAY, log=lambda _: None)
    assert hp.run(lambda url: page(), date(2026, 11, 28), log=lambda _: None) == []
    changes = hp.run(lambda url: page(effective="1 July 2027"), date(2027, 7, 2), log=lambda _: None)
    assert len(changes) == 2 * len(TOWNS) + 1 and all("new prices from 2027-07-01" in c for c in changes)
    old, new = (json.loads(f.read_text()) for f in sorted(library.rglob("HorizonPower-K2CombinedUse-*.json")))
    assert (old["effective_to"], new["effective_from"]) == ("2027-06-30", "2027-07-01")


@pytest.mark.parametrize("kwargs, message", [
    ({"debs_header": "Between 4pm and 9pm"}, "column headings changed"),
    ({"bonus_towns": {k: v for k, v in TOWNS.items() if k != "Cue"}}, "different towns"),
])
def test_anything_unexpected_stops_the_run_and_writes_nothing(library, kwargs, message):
    with pytest.raises(hp.HorizonError, match=message):
        hp.run(lambda url: page(**kwargs), TODAY, log=lambda _: None)
    assert not list(library.rglob("*.json"))
