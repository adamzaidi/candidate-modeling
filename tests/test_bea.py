"""BEA normalization: SAINC1 + PCE parsing, partial-year price index, latest-year fallback, comparison output."""
import datetime as dt
import io
import zipfile

import pytest
import responses

from candidate_modeling import elections
from candidate_modeling.sources import bea
from candidate_modeling.sources.states import texas

SAINC = """GeoFIPS,GeoName,Region,TableName,LineCode,IndustryClassification,Description,Unit,2022,2023,2024,2025
 "00000","United States", ,SAINC1,1,"...","Personal income (millions of dollars) ","Millions of dollars",100.0,110.0,120.0,130.0
 "48000","Texas",6,SAINC1,1,"...","Personal income (millions of dollars) ","Millions of dollars",2000.0,2100.0,2200.0,2300.0
 "48000","Texas",6,SAINC1,2,"...","Population (persons) 1/","Number of persons",30000000,30500000,31000000,31500000
 "48000","Texas",6,SAINC1,3,"...","Per capita personal income (dollars) 2/","Dollars",66667,68852,70968,73016
 "48000","Texas",6,SAINC1,1,"...","Personal income (millions of dollars) ","Millions of dollars",(NA),(NA),(NA),(NA)
"Note: footnote line",,
"""
NIPA_A = '%SeriesCode,Period,Value\nDPCERG,2024,"124.000"\nDPCERG,2025,"128.000"\nA001RC,2025,"1"\n'
NIPA_Q = '%SeriesCode,Period,Value\nDPCERG,2025Q4,"128.5"\nDPCERG,2026Q1,"130.0"\nDPCERG,2026Q2,"132.0"\n'


def test_parse_sainc1_and_suppressed_values():
    rows = {(r["state"], r["year"]): r for r in bea.parse_sainc1(SAINC)}
    assert rows[("TX", 2025)]["personal_income_musd"] == 2300.0 and rows[("TX", 2025)]["population"] == 31500000
    assert rows[("US", 2022)]["personal_income_musd"] == 100.0


def test_parse_pce_partial_year():
    p = {r["year"]: r for r in bea.parse_pce(NIPA_A, NIPA_Q)}
    assert p[2025] == {"year": 2025, "pce_index": 128.0, "basis": "annual", "quarters": 4}    # annual wins over Q4
    assert p[2026] == {"year": 2026, "pce_index": 131.0, "basis": "partial (2 quarters)", "quarters": 2}


@pytest.fixture
def bea_synced():
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("SAINC1__ALL_AREAS_1929_2025.csv", SAINC)
    with responses.RequestsMock() as rs:
        rs.get(bea.REGIONAL_ZIP, body=z.getvalue(), headers={"Last-Modified": "Wed, 30 Sep 2026 12:51:42 GMT"})
        rs.get(bea.NIPA_A, body=NIPA_A, headers={"Last-Modified": "Wed, 04 Nov 2026 14:23:04 GMT"})
        rs.get(bea.NIPA_Q, body=NIPA_Q, headers={"Last-Modified": "Wed, 04 Nov 2026 14:23:04 GMT"})
        return bea.sync()


def test_context_fallbacks(bea_synced):
    assert bea_synced["income_latest_year"] == 2025 and bea_synced["regional_vintage"].startswith("Wed, 30 Sep")
    c26 = bea.context("TX", 2026)
    assert c26["income_year"] == 2025 and c26["income_year_is_latest_available"] is True
    assert c26["pce_base_year"] == 2025 and c26["deflator"] == round(128 / 131, 6) and c26["pce_basis"].startswith("partial")
    c24 = bea.context("TX", 2024)
    assert (c24["income_year"], c24["deflator"]) == (2024, round(128 / 124, 6))
    assert bea.context("ZZ", 2024) is None


def test_compare_adds_bea_block(bea_synced, tx_zip):
    with responses.RequestsMock() as rs:
        rs.head(texas.ZIP_URL, headers={"Last-Modified": "v1"})
        rs.get(texas.ZIP_URL, body=tx_zip)
        texas.sync()
    r = elections.compare_elections([2024, 2025], state="TX", today=dt.date(2027, 1, 1))
    b24 = r["years"][0]["bea"]
    assert b24["real_total"] == round(5427.0 * 128 / 124, 2) and b24["real_dollars_base_year"] == 2025
    assert b24["per_capita_total"] == round(5427.0 / 31000000, 4)
    assert b24["per_100k_personal_income"] == round(100_000 * 5427.0 / (2200 * 1e6), 4)
    assert "real_total_pct_change" in r["changes"][0] and "bea-normalization" in r["rules_applied"]


def test_no_bea_block_when_not_synced(tx_zip):
    with responses.RequestsMock() as rs:
        rs.head(texas.ZIP_URL, headers={"Last-Modified": "v1"})
        rs.get(texas.ZIP_URL, body=tx_zip)
        texas.sync()
    assert "bea" not in elections.compare_elections([2024], state="TX", today=dt.date(2027, 1, 1))["years"][0]
