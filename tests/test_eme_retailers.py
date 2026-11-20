"""scripts/fetch_eme_retailers.py with canned API responses (no network)."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_eme_retailers as eme  # noqa: E402

REGISTER = {
    "data": [
        {"brandName": "CovaU", "productBaseUri": "https://cdr.energymadeeasy.gov.au/covau"},
        {"brandName": "AGL", "productBaseUri": "https://cdr.energymadeeasy.gov.au/agl/"},
        {"brandName": "Own Host", "productBaseUri": "https://public.cdr.example.com.au"},
        {"brandName": "RAA Energy", "productBaseUri": "https://cdr.energymadeeasy.gov.au/energy-locals"},
        {"brandName": "Energy Locals Retail", "productBaseUri": "https://cdr.energymadeeasy.gov.au/energy-locals"},
        {"brandName": "Amber Electric", "productBaseUri": "https://cdr.energymadeeasy.gov.au/amber"},
        {"brandName": "Next Business Energy", "productBaseUri": "https://cdr.energymadeeasy.gov.au/next-business"},
        {"brandName": "Empty", "productBaseUri": "https://cdr.energymadeeasy.gov.au/empty"},
    ],
    "meta": {"totalPages": 1},
}


_ids = iter(range(10**6))


def plan(distributors, customer="RESIDENTIAL", brand_name=None, brand=None, fuel="ELECTRICITY", plan_id=None):
    return {"planId": plan_id or f"p{next(_ids)}", "customerType": customer, "brandName": brand_name,
            "brand": brand, "fuelType": fuel, "geography": {"distributors": distributors}}


PAGES = {
    "covau": [[plan(["Essential Energy"], brand_name="CovaU"), plan(["Ausgrid", "Endeavour"])],
              [plan(["Essential"], customer="BUSINESS")]],
    "agl": [[plan(["Energex"]), plan(["Mystery Networks"]), plan(["United Energy Distribution"]),
             plan(["Jemena"], fuel="GAS"), plan(["Ausgrid"], fuel="DUAL")]],
    # Trailing space in the brand, and a plan repeated across pages
    "amber": [[plan(["Ausgrid"], brand="amber", brand_name="Amber Electric", plan_id="A1")],
              [plan(["Ausgrid"], brand="amber ", brand_name="Amber Electric", plan_id="A1"),
               plan(["Energex"], brand="amber ", brand_name="Amber Electric")]],
    "next-business": [[plan(["Ausgrid"], brand="indigo", brand_name="Indigo Power")]],
    "empty": [[]],
    # Several brands on one path
    "energy-locals": [[plan(["SA Power Networks"], brand="raa", brand_name="RAA Energy"),
                       plan(["Ausgrid"], brand="energy-locals", brand_name="Energy Locals"),
                       plan(["Ausgrid"], brand="indigo", brand_name="Indigo Power")]],
}


def fake_fetch(calls):
    def fetch(url, headers):
        calls.append((url, headers))
        assert headers == {"x-v": "1"}
        if url.startswith(eme.REGISTER_URL):
            return REGISTER
        path = url.split("cdr.energymadeeasy.gov.au/")[1].split("/")[0]
        page = int(url.rsplit("page=", 1)[1])
        pages = PAGES[path]
        assert "fuelType=ALL" in url and "effective=ALL" in url and "page-size=1000" in url
        return {"data": {"plans": pages[page - 1]}, "meta": {"totalPages": len(pages)}}
    return fetch


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Ausgrid", ("Ausgrid", "NSW")),
        ("Essential Energy", ("Essential Energy", "NSW")),
        ("SA Power Networks", ("SA Power Networks", "SA")),
        ("AusNet Services", ("AusNet Services", "VIC")),
        ("ActewAGL Distribution", ("Evoenergy", "ACT")),
        ("Somewhere Else", None),
    ],
)
def test_map_distributor(name, expected):
    assert eme.map_distributor(name) == expected


def test_build():
    calls = []
    data = eme.build(fake_fetch(calls), pause=0, log=lambda *_: None)
    assert data["brand_list"] == "ACCC CDR Register"
    by_name = {r["name"]: r for r in data["retailers"]}
    # The brand on its own host is skipped; energy-locals is split into its brands
    assert sorted(by_name) == ["AGL", "Amber Electric", "CovaU", "Energy Locals", "Indigo Power", "RAA Energy"]
    assert by_name["RAA Energy"]["cdr_paths"] == ["energy-locals"]
    # Same brand with a trailing space, and a repeated plan: one entry, counted once
    amber = by_name["Amber Electric"]
    assert amber["plans"] == 2 and amber["cdr_brands"] == ["amber"]
    # One retailer under two paths: merged
    indigo = by_name["Indigo Power"]
    assert indigo["cdr_paths"] == ["energy-locals", "next-business"]
    assert indigo["distributors"]["Ausgrid"]["residential"] == 2
    assert data["paths_without_electricity_plans"] == ["empty"]
    # Diagnostics: what the empty path and the Register returned
    empty = data["diagnostics"]["empty_paths"]["empty"]
    assert empty["plans_on_page_1"] == 0 and empty["top_keys"] == ["data", "meta"]
    assert empty["data_keys"] == ["plans"] and empty["meta"] == {"totalPages": 1}
    register = data["diagnostics"]["register"]
    assert register["items"] == len(REGISTER["data"])
    assert register["product_base_uri_hosts"] == {"cdr.energymadeeasy.gov.au": 7, "public.cdr.example.com.au": 1}
    assert register["sample"][0]["brandName"] == "CovaU"
    assert by_name["RAA Energy"]["states"] == ["SA"]
    covau = by_name["CovaU"]
    assert covau["plans"] == 3
    assert covau["states"] == ["NSW"]
    assert covau["distributors"]["Essential Energy"] == {"state": "NSW", "residential": 1, "business": 1}
    assert set(covau["distributors"]) == {"Ausgrid", "Endeavour Energy", "Essential Energy"}
    agl = by_name["AGL"]
    # Gas-only plan dropped; dual-fuel plan kept
    assert agl["states"] == ["NSW", "QLD", "VIC"]
    assert agl["plans"] == 4
    assert agl["distributors"]["Mystery Networks"]["state"] is None
    assert data["unmapped_distributors"] == ["Mystery Networks"]
    assert data["distributors"]["Essential Energy"] == {"state": "NSW", "retailers": ["CovaU"]}
    # Both CovaU pages were read
    assert sum("covau/cds-au" in url for url, _ in calls) == 2
    # The shared path was read once
    assert sum("energy-locals/cds-au" in url for url, _ in calls) == 1


def test_register_down_uses_fallback(monkeypatch):
    def fetch(url, headers):
        if url.startswith(eme.REGISTER_URL):
            raise OSError("down")
        raise OSError("no plans either")

    data = eme.build(fetch, pause=0, log=lambda *_: None)
    assert data["brand_list"] == "AER retailer base URI list"
    assert data["diagnostics"]["register"] == {"error": "down"}
    assert data["retailers"] == []
    assert len(data["failed_brands"]) == len(eme.unique_paths(eme.fallback_brands()))


def test_fallback_list_matches_the_aer_pdf():
    brands = eme.fallback_brands()
    assert len(brands) == 83
    assert all(b["uri"].startswith(eme.EME_HOST + "/") for b in brands)
    shared = {b["brand"]: b["uri"].rsplit("/", 1)[-1] for b in brands if b["uri"].rsplit("/", 1)[-1] != b["brand"]}
    assert shared == {
        "arcline": "energy-locals", "cooperative": "energy-locals", "raa": "energy-locals",
        "sunswitch": "future-x", "io-energy": "radian", "myob": "ovo-energy", "ovo-energy-ctm": "ovo-energy",
    }
    assert len(eme.unique_paths(brands)) == 76


def test_brands_not_in_list():
    listed = [{"name": "ARCLINE by RACV", "brand": "arcline"}, {"name": "Sumo", "brand": "sumo-power"}]
    assert eme.brands_not_in_list(["Arcline by RACV", "SUMO", "New Retail Co", "sumo-power"], listed) == ["New Retail Co"]


def test_build_reports_new_register_brands():
    data = eme.build(fake_fetch([]), pause=0, log=lambda *_: None)
    # The fake Register's names that aren't in scripts/eme_brands.json
    assert "Own Host" in data["register_brands_not_in_list"]
    assert "AGL" not in data["register_brands_not_in_list"]
    assert "names" not in data["diagnostics"]["register"]
