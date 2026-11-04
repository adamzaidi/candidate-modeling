"""Wisconsin connector against a mocked Sunshine tRPC API."""
import datetime as dt
import json
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from candidate_modeling import elections, pipeline, store
from candidate_modeling.sources.states import wisconsin as wi


def ent(eid, name, kind="PERSON", etype="Individual", state="WI", committee=None):  # real API: PERSON
    e = {"id": eid, "name": name, "entityType": {"id": 1, "name": etype, "entityTypeOf": kind},
         "entityContactProfiles": [{"city": "Madison", "state": state, "zip": "53703-1111", "isPrimary": True}]}
    if committee:
        e["committee"] = committee
    return e


MARKLEIN = ent(16358, "Taxpayers for Marklein", "COMMITTEE", "Registrant",
               committee={"assignedCommitteeId": "0104815", "committeeType": {"name": "State Candidate"},
                          "party": {"name": "Republican"}})
CONDUIT = ent(500, "Republicans of WI Conduit", "COMMITTEE", "Registrant",
              committee={"assignedCommitteeId": "0800001", "committeeType": {"name": "Conduit"}})


CAT = {"MOC": (9, "Monetary"), "IK": (5, "In-Kind"), "PL": (12, "Personal Loan"), "RDC": (25, "Returned Contribution"),
       "OI": (10, "Other Income")}


def t(i, amount, code="MOC", date="2026-03-01", donor=None, rec=MARKLEIN):
    return {"id": i, "date": f"{date}T00:00:00.000Z", "amount": amount, "createdByEntity": rec,
            "createdByEntityId": rec["id"], "from_entity": donor or ent(9000 + i, f"Donor {i}"),
            "transactionCategory": {"id": CAT[code][0], "label": CAT[code][1]},  # real rows: no "code" field
            "fromOccupationTitle": "Teacher"}


ROWS = {2026: [t(i, 10.0) for i in range(1, 1501)]                              # spans two 1000-row pages
        + [t(2001, 500.0, "IK"), t(2002, 9000.0, "PL"), t(2003, -25.0, "RDC"), t(2004, 40.0, "OI"),
           t(2005, 75.0, donor=ent(8000, "Out Of Stater", state="Illinois")), t(2006, 60.0, rec=CONDUIT),
           t(2007, 5.0, donor=ent(8001, "Anonymous", kind="ANONYMOUS", etype="Anonymous"))],
        2025: [t(3001, 20.0, date="2025-06-01")]}
CALLS = {"tx": 0}


def _cb(req):
    q = parse_qs(urlparse(req.url).query)
    proc = urlparse(req.url).path.rsplit("/", 1)[-1]
    payload = json.loads(q["input"][0])
    inp = payload["json"]
    if inp.get("cursor") and payload.get("meta", {}).get("values", {}).get("cursor.date") != ["Date"]:
        return 400, {}, json.dumps({"error": {"json": {"message": "Expected date, received string"}}})  # real server
    year = int(inp["dateFrom"][:4])
    assert inp["transactionType"] == [1]
    rows = ROWS.get(year, [])
    if proc == "publicFrontendApi.getTransactionsTotalCount":
        return 200, {}, json.dumps({"result": {"data": {"json": len(rows)}}})
    CALLS["tx"] += 1
    start = int(inp["cursor"]["id"]) if inp.get("cursor") else 0
    page = rows[start:start + inp["take"]]
    nxt = {"id": start + inp["take"], "date": "2026-01-01T00:00:00.000Z"} if start + inp["take"] < len(rows) else None
    return 200, {}, json.dumps({"result": {"data": {"json": {"results": page, "nextCursor": nxt}}}})


@pytest.fixture
def wi_api():
    CALLS["tx"] = 0
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rs:
        rs.add_callback(responses.GET, f"{wi.API}/publicFrontendApi.getTransactionsTotalCount", callback=_cb)
        rs.add_callback(responses.GET, f"{wi.API}/publicFrontendApi.getTransactions", callback=_cb)
        yield rs


def test_sync_pages_by_cursor_and_skips_unchanged(wi_api):
    out = wi.sync(years=[2025, 2026, 2007])
    st = {y["year"]: y for y in out["years"]}
    assert st[2026] == {"year": 2026, "status": "updated", "rows": 1507, "expected": 1507}
    assert st[2007]["status"].startswith("not covered")
    assert wi.synced_years() == [2025, 2026]
    n = CALLS["tx"]
    assert wi.sync(years=[2026])["years"][0]["status"] == "current" and CALLS["tx"] == n


def test_interrupted_year_resumes_from_checkpoint(wi_api, monkeypatch):
    monkeypatch.setattr(wi, "CHUNK_ROWS", 1000)
    real = wi._call
    calls = {"n": 0}

    def flaky(proc, inp, retries=3):
        if proc.endswith("getTransactions"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ConnectionError("wifi dropped")
        return real(proc, inp, retries=0)
    monkeypatch.setattr(wi, "_call", flaky)
    with pytest.raises(ConnectionError):
        wi.sync(years=[2026])
    assert wi.synced_years() == []                                  # partial year is never readable
    out = wi.sync(years=[2026])["years"][0]
    assert out["rows"] == 1507 and calls["n"] == 3                  # resumed at page 2, not page 1


def test_kinds_state_codes_and_totals(wi_api):
    wi.sync(years=[2026])
    pipeline.add_candidate("Howard Marklein", slug="marklein", state="WI", state_ids={"WI": ["16358"]})
    pipeline.fetch_finances("marklein")
    with store.connect() as con:
        kinds = dict(con.execute("SELECT transaction_id, contribution_kind FROM contributions").fetchall())
        assert (kinds["2001"], kinds["2002"], kinds["2003"], kinds["2004"], kinds["1"]) == \
            ("in_kind", "loan", "return", "other_receipt", "monetary")
        assert con.execute("SELECT state FROM contributions WHERE transaction_id = '2005'").fetchone()[0] == "IL"
        assert con.execute("SELECT contributor_type, contributor_name FROM contributions WHERE transaction_id = '2007'"
                           ).fetchone() == ("unitemized", None)
        assert con.execute("SELECT count(*) FROM contributions WHERE contributor_type = 'individual'").fetchone()[0] == 1505
        clean = con.execute("SELECT sum(amount) FROM contributions_clean").fetchone()[0]
    assert clean == 1500 * 10 + 500 - 25 + 75 + 5                   # loan and other income excluded
    s = pipeline.finance_summary("marklein")
    assert s["labels"]["loans"]["loans_total"] == 9000.0


def test_committees_and_statewide_compare(wi_api):
    wi.sync(years=[2026])
    import duckdb
    rows = {r[0]: r[1:] for r in duckdb.connect().execute(
        f"SELECT committee_id, kind, office_category, party, is_conduit FROM ({wi.committees_sql()})").fetchall()}
    assert rows["16358"] == ("candidate", "unknown", "republican", False)
    assert rows["500"] == ("other_committee", "other_committee", "not_applicable", True)
    y = elections.compare_elections([2026], state="WI", today=dt.date(2027, 1, 1))["years"][0]
    assert y["conduit_recipient_total"] == 60.0 and y["loans_total"] == 9000.0
    assert wi.search("Marklein")[0]["ethics_committee_id"] == "0104815"
