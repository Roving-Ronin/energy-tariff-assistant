"""scripts/fetch_synergy_plans.py on hand-written pages shaped like Synergy's (no network)."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_synergy_plans as syn  # noqa: E402

from engine import validate_plan  # noqa: E402

TODAY = date(2026, 11, 21)
DEBS_TEXT = """Non-residential customers
Peak – Between 3pm and 9pm: 12
Off Peak – Before 3pm or after 9pm: 3
Rates above correct as at [1 July 2026] and exclude GST
Residential customers
Peak – Between 3pm and 9pm: 10
Off Peak – Before 3pm or after 9pm: 2
Rates above correct as at [1 July 2026] and exclude GST"""


def page(rows, times=(), effective="1 July 2026", blurb=""):
    """A plan page: some description, stated times, the price table, a fees table, the date."""
    table = "".join(f"<tr><td>{label}</td><td>{value}</td></tr>" for label, value in rows)
    stated = "".join(f"<p>{t}</p>" for t in times)
    return (f"<html><body><script>var x='Peak (1am to 2am)'</script><p>{blurb}</p>{stated}"
            f"<table><tr><th>Item</th><th>Price inc. GST</th></tr>{table}</table>"
            f"<table><tr><th>Item</th><th>Price inc. GST</th></tr><tr><td>Paper bill fee</td><td>$1.50</td></tr></table>"
            f"<p>Unless otherwise stated, all prices include GST and are effective at {effective}.</p>"
            "</body></html>").encode()


PAGES = {
    "Home-Plan-A1": page([("Supply charge", "119.2419 cents per day "), ("Electricity charge", "33.2621 cents per unit ")]),
    "Midday-Saver": page(
        [("Supply charge", "132.7806 cents per day"),
         ("Everyday Super Off Peak electricity charge", "8.8520 cents per unit"),
         ("Peak electricity charge", "55.3253 cents per unit"),
         ("Everyday Off-peak electricity charge", "24.3431 cents per unit")],
        ["Super Off Peak (9am to 3pm)", "Peak (3pm to 9pm)", "Off Peak (9pm to 9am)"]),
    "Electric-Vehicle-Add-On": page(
        [("Supply charge", "132.7806 cents per day"),
         ("Standard home electricity charge", "55.3253 cents per unit"),
         ("Super Off Peak electricity charge", "8.8520 cents per unit"),
         ("Overnight electricity charge", "19.9172 cents per unit"),
         ("Everyday EV off peak electricity charge", "24.3431 cents per unit")],
        ["Overnight (11pm to 6am)", "Peak (3pm to 9pm)", "Super Off Peak (9am to 3pm)",
         "Off Peak (6am to 9am / 9pm to 11pm)"]),
    "Home-Business-Plan-K1-Resi": page(
        [("Supply charge", "210.4115 cents per day"),
         ("Electricity charge - first 20 units per day", "34.7481 cents per unit"),
         ("Electricity charge - next 20 - 1,650 units per day", "32.7455 cents per unit"),
         ("Electricity charge - more than 1,650 units per day", "36.9194 cents per unit")],
        # The real page's description disagrees with its table: only the table counts
        blurb="The first 20 units used per day are charged at the residential anytime rate (A1 tariff)."),
}


@pytest.fixture
def library(tmp_path, monkeypatch):
    plans_dir = tmp_path / "plans"
    monkeypatch.setattr(syn, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(syn, "plan_files", lambda: sorted(plans_dir.rglob("*.json")))
    monkeypatch.setattr(syn, "pdf_text", lambda data: DEBS_TEXT)
    return plans_dir


def fake_get(pages):
    def get(url):
        if url == syn.DEBS_URL:
            return b""
        if url == syn.GOV_PRICES_URL:
            return b"<p>Supply charge 119.2419 cents per day; electricity 33.2621 cents per unit</p>"
        return pages[url.rsplit("/", 1)[1]]
    return get


def plans_in(directory):
    return {json.loads(p.read_text())["plan"]: json.loads(p.read_text()) for p in sorted(directory.rglob("*.json"))}


def test_all_four_plans_from_the_tables(library):
    logged = []
    changes = syn.run(fake_get(PAGES), TODAY, log=logged.append)
    assert len(changes) == 4 and not any("warning" in line for line in logged)
    plans = plans_in(library)
    for plan in plans.values():
        assert validate_plan(plan, today=TODAY).ok
        assert (plan["region"], plan["dnsp"], plan["timezone"]) == ("WA", "Western Power", "Australia/Perth")
    a1 = plans["Home Plan (A1)"]
    assert a1["supply_charge"]["rate"] == 1.192419
    assert a1["import"]["periods"] == [{"id": "anytime", "name": "Anytime", "rate": 0.332621, "default": True}]
    # DEBS from the residential table only: 10c 3pm-9pm, 2c otherwise, no GST
    assert [(p["id"], p["rate"]) for p in a1["export"]["periods"]] == [("peak", 0.1), ("off_peak", 0.02)]
    ev = plans["EV Add On"]["import"]["periods"]
    assert [p["id"] for p in ev] == ["super_off_peak", "peak", "overnight", "off_peak"]
    assert ev[2]["windows"] == [{"days": "all", "start": "23:00", "end": "06:00"}]
    k1 = plans["Home Business Plan (K1)"]
    assert k1["import"]["periods"][0]["tiers"] == [
        {"up_to_kwh": 20, "rate": 0.347481}, {"up_to_kwh": 1650, "rate": 0.327455}, {"rate": 0.369194}]
    assert k1["export"]["periods"] == [{"id": "feed_in", "name": "Feed-in", "rate": 0.0, "default": True}]
    assert (library / "AU" / "WA" / "Synergy-HomePlanA1-WA-WesternPower-20260701.json").is_file()


def test_rerun_with_the_same_prices_changes_nothing(library):
    syn.run(fake_get(PAGES), TODAY, log=lambda _: None)
    assert syn.run(fake_get(PAGES), date(2026, 11, 28), log=lambda _: None) == []


def test_new_prices_add_a_file_and_close_the_old_one(library):
    syn.run(fake_get(PAGES), TODAY, log=lambda _: None)
    pages = dict(PAGES)
    pages["Home-Plan-A1"] = page([("Supply charge", "122.0000 cents per day"), ("Electricity charge", "34.0000 cents per unit")],
                                 effective="1 July 2027")
    changes = syn.run(fake_get(pages), date(2027, 7, 2), log=lambda _: None)
    assert changes == ["Home Plan (A1): new prices from 2027-07-01"]
    files = sorted((library / "AU" / "WA").glob("Synergy-HomePlanA1-*.json"))
    old, new = (json.loads(f.read_text()) for f in files)
    assert old["effective_to"] == "2027-06-30" and new["effective_from"] == "2027-07-01"
    assert new["import"]["periods"][0]["id"] == old["import"]["periods"][0]["id"] == "anytime"  # meters carry on


@pytest.mark.parametrize("change, message", [
    (("Midday-Saver", "Peak electricity charge", "Evening electricity charge"), "price table labels changed"),
    (("Midday-Saver", "Peak (3pm to 9pm)", "Peak (4pm to 9pm)"), "Peak times changed"),
    (("Home-Plan-A1", "33.2621 cents per unit", "33.2621 cents per kWh"), "price table labels changed"),
])
def test_anything_unexpected_stops_the_run_and_writes_nothing(library, change, message):
    name, old, new = change
    pages = dict(PAGES)
    pages[name] = pages[name].replace(old.encode(), new.encode())
    with pytest.raises(syn.SynergyError, match=message):
        syn.run(fake_get(pages), TODAY, log=lambda _: None)
    assert not list(library.rglob("*.json"))  # every plan is read before any is written


def test_debs_needs_one_rate_and_date_in_the_residential_table():
    assert syn.debs_rates(DEBS_TEXT) == {"peak": 0.1, "off_peak": 0.02, "effective": "2026-07-01"}
    with pytest.raises(syn.SynergyError, match="Residential customers"):
        syn.debs_rates("Peak – Between 3pm and 9pm: 10")
    with pytest.raises(syn.SynergyError, match="DEBS off_peak"):
        syn.debs_rates(DEBS_TEXT.replace("after 9pm: 2\nRates above correct as at [1 July 2026] and exclude GST",
                                         "after 9pm: 2\nOff Peak – Before 3pm or after 9pm: 2.5\nRates above correct as at [1 July 2026]"))


def test_a1_differing_from_the_government_page_is_a_warning(library):
    get = fake_get(PAGES)
    logged = []
    syn.run(lambda url: b"<p>A1: 30.0000 cents</p>" if url == syn.GOV_PRICES_URL else get(url), TODAY, log=logged.append)
    assert any("doesn't show A1 figure" in line for line in logged)


def test_a_refused_download_is_a_clear_error_not_a_traceback(monkeypatch, capsys):
    import urllib.error

    def refuse(url):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(syn, "http_get", refuse)
    monkeypatch.setattr(sys, "argv", ["fetch_synergy_plans.py", "--dry-run"])
    assert syn.main() == 1
    assert f"::error::{syn.DEBS_URL} answered HTTP 403 Forbidden: nothing was changed" in capsys.readouterr().out


def mhtml(url, html):
    """A page saved from a browser as a single file (MHTML)."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    message = MIMEMultipart("related")
    message["Snapshot-Content-Location"] = url
    message.attach(MIMEText(html, "html", "utf-8"))
    return message.as_bytes()


def test_saved_pages_are_read_like_live_ones(library, tmp_path):
    saved = tmp_path / "saved"
    saved.mkdir()
    for name, html in PAGES.items():
        (saved / f"{name}.mhtml").write_bytes(mhtml(f"{syn.BASE}/{name}", html.decode()))
    (saved / "DEBS_Pricing_schedule.pdf").write_bytes(b"%PDF")  # pdf_text is faked by the fixture
    pages = syn.saved_pages([saved])
    logged = []
    changes = syn.run(syn.from_saved(pages), TODAY, log=logged.append)
    assert len(changes) == 4
    # The WA Government page wasn't saved: the cross-check is only a warning
    assert any("couldn't cross-check A1" in line for line in logged)
    a1 = plans_in(library)["Home Plan (A1)"]
    assert a1["source"]["url"] == f"{syn.BASE}/Home-Plan-A1"


def test_a_missing_saved_page_stops_the_run(library, tmp_path):
    saved = tmp_path / "saved"
    saved.mkdir()
    (saved / "a1.mhtml").write_bytes(mhtml(f"{syn.BASE}/Home-Plan-A1", PAGES["Home-Plan-A1"].decode()))
    (saved / "debs.pdf").write_bytes(b"%PDF")
    with pytest.raises(syn.SynergyError, match="no saved copy of .*Midday-Saver"):
        syn.run(syn.from_saved(syn.saved_pages([saved])), TODAY, log=lambda _: None)
    assert not list(library.rglob("*.json"))
