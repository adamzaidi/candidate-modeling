"""North Carolina connector against a mocked cf.ncsbe.gov (token, search params, CSV export, committee list)."""
import csv
import datetime as dt
import html
import io
import json
from urllib.parse import parse_qs

import duckdb
import pytest
import responses

from candidate_modeling import elections, pipeline, store
from candidate_modeling.sources.states import north_carolina as nc

STEIN = "STA-AAAAAA-C-001"
PAC = "STA-BBBBBB-C-001"
DEMS = "STA-CCCCCC-C-001"
COMMS = {STEIN: "FRIENDS OF JOSH STEIN (STEIN, JOSHUA H)", PAC: "NC REALTORS PAC",
         DEMS: "NORTH CAROLINA DEMOCRATIC PARTY - FEDERAL"}


def row(name, typ, com, amount, d="09/15/2026", state="NC", job="Teacher"):
    return {"Name": name, "Street Line 1": "1 Main St", "City": "Raleigh", "State": state, "Zip Code": "27601-0001",
            "Profession/Job Title": job, "Employer's Name/Specific Field": "Wake County", "Transction Type": typ,
            "Committee Name": COMMS[com], "Committee SBoE ID": com, "Report Name": "2026 Third Quarter",
            "Date Occured": d, "Amount": amount}


RECEIPTS = [row("JANE DOE", "Individual", STEIN, "100.00"), row("JANE DOE", "Individual", STEIN, "100.00"),  # identical
            row("Aggregated Individual Contribution", "Individual", STEIN, "250.00"),
            row("JOSHUA STEIN", "Individual", STEIN, "500.00"),
            row("REALTORS PAC", "Non-Party Comm", STEIN, "5,000.00", job=""),
            row("ACME", "Nonmonetary Gift", PAC, "300.00"), row("BANK", "Interest", DEMS, "4.00"),
            row("LENDER", "Loan", STEIN, "10000.00"), row("OUT OF STATE", "Individual", DEMS, "50.00", state="VA")]
EXPENSE = row("VENDOR", "Operating Exp", STEIN, "999.00")
PARTY = {"'DEM'": {STEIN, DEMS, PAC}, "'REP'": {PAC}}   # real filter: PACs show up under both parties


def _csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=nc.CSV_COLS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in nc.CSV_COLS})
    return ("﻿" + buf.getvalue()).encode()


def _export(req):
    f = {k: v[0] for k, v in parse_qs(req.body).items()}
    assert f["__RequestVerificationToken"] == "TOK2"
    p = json.loads(f["searchParamsJson"])
    rows = [r for r in RECEIPTS if r["Date Occured"][:2] == p["DateFrom"][:2] and r["Date Occured"][-4:] == p["DateFrom"][-4:]]
    if "OPER" in p.get("ExpenditureType", ""):
        rows = rows + [EXPENSE]                     # real site: expenditures leak in unless cleared
    if "NMG" not in p.get("ExpenditureType", ""):
        rows = [r for r in rows if r["Transction Type"] != "Nonmonetary Gift"]   # in-kind lives under code NMG
    if p["PartyType"] in PARTY:
        rows = [r for r in rows if r["Committee SBoE ID"] in PARTY[p["PartyType"]]]
    return 200, {"content-type": "text/csv"}, _csv(rows)


def _results(req):
    f = {k: v[0] for k, v in parse_qs(req.body).items()}
    params = {"ReceiptType": "'IND '", "ExpenditureType": "'NMG ','OPER'", "PartyType": "'DEM','REP','UNA'",
              "DateFrom": f["Filter.StartDate"], "DateTo": f["Filter.EndDate"]}
    body = (f'<div id="grid" data-search-params="{html.escape(json.dumps(params))}"></div>'
            '<form><input name="__RequestVerificationToken" type="hidden" value="TOK2" /></form>')
    return 200, {}, body


@pytest.fixture
def nc_api(monkeypatch):
    monkeypatch.setattr(nc, "_sleep", lambda s: None)
    monkeypatch.setattr(nc, "_months", lambda y: [(dt.date(y, 9, 1), dt.date(y, 9, 30))])
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rs:
        rs.get(f"{nc.BASE}/CFTxnLkup/AdvancedSearch",
               body='<input name="__RequestVerificationToken" type="hidden" value="TOK1" />')
        rs.add_callback(responses.POST, f"{nc.BASE}/CFTxnLkup/AdvancedSearch", callback=_results)
        rs.add_callback(responses.POST, f"{nc.BASE}/CFTxnLkup/Export", callback=_export)
        data = [{"SBoEID": k, "OrgName": v, "CandName": None, "StatusDesc": "ACTIVE"} for k, v in COMMS.items()]
        rs.get(f"{nc.BASE}/CFOrgLkup/CommitteeGeneralResult", body='grid({"data":{"Data":' + json.dumps(data) + ',"Total":3}})')
        yield rs


def test_sync_receipts_only_with_party_and_ids(nc_api):
    out = nc.sync(years=[2026])
    assert out["committees"] == 3 and out["years"][0]["rows"] == len(RECEIPTS)    # expense row never stored
    c = duckdb.connect()
    ids = [r[0] for r in c.execute(f"SELECT txn_id FROM read_parquet({nc._tx_list()})").fetchall()]
    assert len(ids) == len(set(ids)) == len(RECEIPTS)                              # identical rows both kept
    assert dict(nc._party_rows()) == {STEIN: "democratic", DEMS: "democratic"}   # PAC ambiguous -> no label


def test_normalized_labels(nc_api):
    nc.sync(years=[2026])
    pipeline.add_candidate("Josh Stein", slug="stein", state="NC", state_ids={"NC": [STEIN]})
    pipeline.fetch_finances("stein")
    with store.connect() as con:
        r = {(x[0], x[1]): x[2:] for x in con.execute(
            "SELECT contributor_name, amount, contributor_type, contribution_kind FROM contributions").fetchall()}
        clean = con.execute("SELECT sum(amount) FROM contributions_clean").fetchone()[0]
    assert r[(None, 250.0)] == ("unitemized", "monetary")
    assert r[("REALTORS PAC", 5000.0)] == ("organization", "monetary")       # "5,000.00" parsed
    assert r[("LENDER", 10000.0)][1] == "loan"
    assert clean == 100 + 100 + 250 + 500 + 5000                              # loan excluded
    assert pipeline.finance_summary("stein")["labels"]["self_funding"]["total"] == 500.0


def test_committees_kind_party_and_candidate_names(nc_api):
    nc.sync(years=[2026])
    rows = {x[0]: x[1:] for x in duckdb.connect().execute(
        f"SELECT committee_id, kind, party, cand_last, cand_first, district FROM ({nc.committees_sql()})").fetchall()}
    assert rows[STEIN] == ("candidate", "democratic", "STEIN", "JOSHUA", "STA")
    assert rows[PAC][:2] == ("pac", "not_applicable")
    assert rows[DEMS][:2] == ("party_committee", "democratic")


def test_duplicate_rows_across_year_folders_counted_once(nc_api, monkeypatch):
    nc.sync(years=[2026])
    import shutil
    _, p = nc._dirs()
    shutil.copytree(p / "tx_2026", p / "tx_2025")          # simulate overlap between year folders
    y = elections.compare_elections([2026], state="NC", today=dt.date(2027, 1, 1))["years"][0]
    assert y["loans_total"] == 10000.0


def test_statewide_compare_and_resync_skips_past_years(nc_api):
    nc.sync(years=[2025])
    assert nc.sync(years=[2025])["years"][0]["status"] == "current"            # finished past year not refetched
    nc.sync(years=[2026])
    y = elections.compare_elections([2026], state="NC", today=dt.date(2027, 1, 1))["years"][0]
    assert y["loans_total"] == 10000.0 and y["in_kind_total"] == 300.0 and y["unitemized_total"] == 250.0
