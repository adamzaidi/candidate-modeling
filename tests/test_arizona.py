"""Arizona connector against a mocked See The Money API (DataTables endpoints, per-committee paging)."""
import datetime as dt
import json
import re
from urllib.parse import parse_qs, urlparse

import duckdb
import pytest
import responses

from candidate_modeling import elections, pipeline, store
from candidate_modeling.sources.states import arizona as az

MS = lambda d: f"/Date({int(dt.datetime(*d, tzinfo=dt.timezone.utc).timestamp() * 1000)})/"  # noqa: E731

ENTITIES = {
    1: [  # candidates: the site returns several rows per entity (one with party, one with office)
        {"EntityID": 201800057, "EntityLastName": "Hobbs, Katie <br>(201800057)", "CommitteeName": "Elect Katie Hobbs",
         "OfficeName": "", "PartyName": "Democratic", "Income": 100.0, "EntityTypeName": "Candidate"},
        {"EntityID": 201800057, "EntityLastName": "Hobbs, Katie <br>(201800057)", "CommitteeName": "Elect Katie Hobbs",
         "OfficeName": "Governor", "PartyName": "", "Income": 1000.0, "EntityTypeName": "Candidate"},
        {"EntityID": 102162, "EntityLastName": "Accalia, Jayme <br>(102162)",
         "CommitteeName": "Committee to Elect Jayme Accalia for State Senate LD15<br>(Clean Elections)",
         "OfficeName": "State Senator - District No. 15", "PartyName": "Democratic", "Income": 50.0},
        {"EntityID": 999, "EntityLastName": "Nobody, No", "CommitteeName": "No Money", "OfficeName": "Governor",
         "PartyName": "Republican", "Income": 0.0},
    ],
    2: [{"EntityID": 200602825, "EntityLastName": "ACTBLUE ARIZONA", "CommitteeName": None, "Income": 70.0}],
    3: [], 4: [],
}


def tx(pid, cid, amount, ttype, last="DOE, JANE", etype=1, date=(2026, 2, 1), state="AZ"):
    return {"PublicTransactionId": pid, "CommitteeId": cid, "CommitteeName": "x", "TransactionDate": MS(date),
            "Amount": amount, "TransactionType": ttype, "TransactionGroupName": "Individuals",
            "TransactionEntityTypeId": etype, "TransactionLastName": last, "TransactionCity": "PHOENIX",
            "TransactionState": state, "TransactionZipCode": "85001-1234", "TransactionEmployer": "ASU",
            "TransactionOccupation": "Teacher", "CandidateFirstName": None, "CandidateLastName": None}


TXS = {  # keyed by (income_page, entity id)
    (20, "201800057"): [tx(i, 201800057, 100.0, "Contribution from Individuals") for i in range(1, 1201)]  # 2 pages
                       + [tx(5000, 201800057, -20.0, "Refund Contribution from Individual"),
                          tx(5001, 201800057, 300.0, "In-State Contributions $100 or Less", last="AGGREGATE"),
                          tx(5002, 201800057, 250.0, "Contribution from Candidate or Family", last="HOBBS, KATIE")],
    (20, "102162"): [tx(6000, 102162, 5.0, "CCEC $5 Qualifying Contribution"),
                     tx(6001, 102162, 100000.0, "Receive CCEC Funding", etype=3, last="CITIZENS CLEAN ELECTIONS"),
                     tx(6002, 102162, -5.0, "Submit $5 Qualifying Contributions to SOS", etype=3, last="SOS"),
                     tx(6003, 102162, 1.0, "Receive Interest/Dividends", etype=3, last="BANK")],
    (30, "200602825"): [tx(7000, 200602825, 70.0, "Contribution from Individuals")],
}
CALLS = {"list": 0, "detail": 0}


def _form(req):
    return {k: v[0] for k, v in parse_qs(req.body).items()}


def _list_cb(req):
    q = {k: v[0] for k, v in parse_qs(urlparse(req.url).query).items()}
    f = _form(req)
    if "order[0][column]" not in f:
        return 200, {}, '""'                       # real site: missing DataTables params -> empty string
    CALLS["list"] += 1
    rows = ENTITIES[int(q["Page"])]
    s, n = int(f["start"]), int(f["length"])
    assert n <= 100
    return 200, {}, json.dumps({"draw": "1", "recordsTotal": len(rows), "data": rows[s:s + n]})


def _detail_cb(req):
    q = {k: v[0] for k, v in parse_qs(urlparse(req.url).query).items()}
    f = _form(req)
    CALLS["detail"] += 1
    rows = TXS.get((int(q["Page"]), q["entityId"]), [])   # wrong page number -> 0 rows, like the real site
    s, n = int(f["start"]), int(f["length"])
    return 200, {}, json.dumps({"draw": "1", "recordsTotal": len(rows), "data": rows[s:s + n]})


@pytest.fixture
def az_api(monkeypatch):
    monkeypatch.setattr(az, "_sleep", lambda s: None)
    CALLS.update(list=0, detail=0)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rs:
        rs.add_callback(responses.POST, re.compile(re.escape(az.BASE) + r"/GetNEWTableData/.*"), callback=_list_cb)
        rs.add_callback(responses.POST, re.compile(re.escape(az.BASE) + r"/GetNEWDetailedTableData/.*"),
                        callback=_detail_cb)
        yield rs


def test_cycles():
    assert az.cycle_for_year(2026) == (2025, 2026) and az.cycle_for_year(2025) == (2025, 2026)
    assert az.cycle_for_year(2022) == (2021, 2022)


def test_sync_pages_entities_and_uses_right_income_page(az_api):
    out = az.sync(years=[2026])["cycles"][0]
    assert out["entities"] == 4 and out["with_income"] == 3 and out["fetched"] == 3 and out["complete"]
    assert az.covered_years() == [2025, 2026]
    c = duckdb.connect()
    n = c.execute(f"SELECT count(*), count(DISTINCT PublicTransactionId) FROM read_parquet({az._tx_list()})").fetchone()
    assert n == (1203 + 4 + 1, 1208)                  # Hobbs paged past 1000; ActBlue AZ found on page 30


def test_resync_skips_unchanged_and_refetches_changed(az_api):
    az.sync(years=[2026])
    before = CALLS["detail"]
    assert az.sync(years=[2026])["cycles"][0]["skipped_unchanged"] == 3
    assert CALLS["detail"] == before                  # nothing refetched
    ENTITIES[2][0]["Income"] = 71.0
    try:
        out = az.sync(years=[2026])["cycles"][0]
        assert (out["fetched"], out["skipped_unchanged"]) == (1, 2)
    finally:
        ENTITIES[2][0]["Income"] = 70.0


def test_partial_sync_is_not_complete(az_api):
    out = az.sync(years=[2026], max_entities=1)["cycles"][0]
    assert out["complete"] is False and az.covered_years() == []


def test_empty_string_response_is_an_error(monkeypatch):
    monkeypatch.setattr(az, "_sleep", lambda s: None)
    with responses.RequestsMock() as rs:
        rs.add(responses.POST, re.compile(re.escape(az.BASE) + r"/.*"), body='""')
        with pytest.raises(ValueError, match="empty response"):
            az._post("GetNEWTableData", "Page=1", 0, 10)


def test_normalized_kinds_and_types(az_api):
    az.sync(years=[2026])
    pipeline.add_candidate("Jayme Accalia", slug="accalia", state="AZ", state_ids={"AZ": ["102162"]})
    pipeline.add_candidate("Katie Hobbs", slug="hobbs", state="AZ", party="Democratic", state_ids={"AZ": ["201800057"]})
    pipeline.fetch_finances("accalia")
    pipeline.fetch_finances("hobbs")
    with store.connect() as con:
        k = dict(con.execute("SELECT transaction_id, contribution_kind FROM contributions WHERE candidate_slug='accalia'").fetchall())
        assert k == {"6000": "monetary", "6001": "public_funds", "6002": "other_receipt", "6003": "other_receipt"}
        assert con.execute("SELECT sum(amount) FROM contributions_clean WHERE candidate_slug='accalia'").fetchone()[0] == 5.0
        h = con.execute("""SELECT transaction_id, contributor_type, contributor_name, contribution_kind, date::VARCHAR, zip
                           FROM contributions WHERE candidate_slug='hobbs' AND transaction_id IN ('1','5000','5001')
                           ORDER BY transaction_id""").fetchall()
    assert h[0] == ("1", "individual", "DOE, JANE", "monetary", "2026-02-01", "85001")
    assert h[1][3] == "return"
    assert h[2][1:3] == ("unitemized", None)
    s = pipeline.finance_summary("hobbs")
    assert s["labels"]["self_funding"]["total"] == 250.0 and s["labels"]["party"] == "democratic"


def test_committees_offices_party_conduit(az_api):
    az.sync(years=[2026])
    rows = {r[0]: r[1:] for r in duckdb.connect().execute(
        f"SELECT committee_id, kind, office_category, district, party, cand_last, is_conduit FROM ({az.committees_sql()})").fetchall()}
    assert rows["201800057"] == ("candidate", "governor", "", "democratic", "Hobbs", False)
    assert rows["102162"][:4] == ("candidate", "state_senate", "15", "democratic")
    assert rows["200602825"] == ("pac", "pac", "", "not_applicable", None, True)
    hits = az.search("Katie Hobbs")
    assert hits[0]["committee_id"] == "201800057"


def test_statewide_compare_reports_public_funds_and_conduits(az_api):
    az.sync(years=[2026])
    y = elections.compare_elections([2026], state="AZ", today=dt.date(2027, 1, 1))["years"][0]
    assert y["public_funds_total"] == 100000.0                     # reported...
    assert y["conduit_recipient_total"] == 70.0
    assert y["total"] == 1200 * 100 - 20.0 + 300.0 + 250.0 + 5.0 + 70.0   # ...but public funds never counted
    assert y["coverage"]["status"] == "complete"
