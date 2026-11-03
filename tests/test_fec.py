import json

import pytest
import responses
from responses import matchers

from candidate_modeling import store
from candidate_modeling.sources import fec

PROFILE = {"slug": "talarico", "name": "James Talarico", "state": "TX",
           "ids": {"fec_candidate_ids": ["S6TX00479"], "fec_committee_ids": [], "state": {}}}


def row(sub_id, amount=100.0, memo=None, entity="IND", state="TX", date="2026-06-30T00:00:00"):
    return {"sub_id": sub_id, "committee_id": "C00919084", "committee": {"name": "TALARICO FOR SENATE"},
            "contribution_receipt_date": date, "contribution_receipt_amount": amount,
            "contributor_name": f"DONOR {sub_id}", "entity_type": entity, "contributor_city": "AUSTIN",
            "contributor_state": state, "contributor_zip": "787011234", "contributor_employer": "SELF",
            "contributor_occupation": "TEACHER", "memo_code": memo, "two_year_transaction_period": 2026}


def sched_a(results, last=None):
    return {"results": results, "pagination": {"last_indexes": last}}


@responses.activate
def test_search_candidates_maps_fields():
    responses.get(f"{fec.BASE}/candidates/search/", json={"results": [
        {"candidate_id": "S6TX00479", "name": "TALARICO, JAMES", "office_full": "Senate", "state": "TX",
         "district": "00", "party_full": "DEMOCRATIC PARTY", "election_years": [2026]}]})
    out = fec.search_candidates("Talarico", state="TX")
    assert out == [{"source": "fec", "candidate_id": "S6TX00479", "name": "TALARICO, JAMES", "office": "Senate",
                    "state": "TX", "district": "00", "party": "DEMOCRATIC PARTY", "election_years": [2026]}]
    assert responses.calls[0].request.params["api_key"] == "TEST_KEY"
    assert responses.calls[0].request.params["state"] == "TX"


def test_normalize():
    n = fec.normalize(row(1, memo="X", entity="ORG"), "talarico")
    assert n["transaction_id"] == "1" and n["is_memo"] is True
    assert n["contributor_type"] == "organization"
    assert n["zip"] == "78701" and n["date"] == "2026-06-30" and n["jurisdiction"] == "federal"


@responses.activate
def test_retries_on_429_then_succeeds():
    url = f"{fec.BASE}/candidates/search/"
    responses.get(url, status=429, headers={"Retry-After": "0"})
    responses.get(url, json={"results": []})
    assert fec.search_candidates("x") == []
    assert len(responses.calls) == 2


@responses.activate
def test_rate_limit_exhausted_raises_helpful_error():
    responses.get(f"{fec.BASE}/candidates/search/", status=429)
    with pytest.raises(fec.RateLimited, match="api.data.gov"):
        fec.search_candidates("x")


@responses.activate
def test_itemized_paginates_and_is_idempotent():
    url = f"{fec.BASE}/schedules/schedule_a/"
    cursor = {"last_index": "500", "last_contribution_receipt_date": "2026-06-01"}
    responses.get(url, json=sched_a([row(1), row(2, memo="X")], last=cursor),
                  match=[matchers.query_param_matcher({"committee_id": "C00919084"}, strict_match=False)])
    with store.connect() as con:
        # first page, stop after 1 page
        r1 = fec.fetch_itemized(con, "talarico", "C00919084", 2026, max_pages=1)
        assert r1 == {"scope": "C00919084:2026", "rows": 2, "complete": False}
        saved = json.loads(store.get_cursor(con, "talarico", "fec", "C00919084:2026")[0])
        assert saved == cursor

        # resume: next call must send the saved cursor
        responses.replace(responses.GET, url, json=sched_a([row(3)], last=None))
        r2 = fec.fetch_itemized(con, "talarico", "C00919084", 2026)
        assert r2["complete"] is True and r2["rows"] == 3
        assert responses.calls[-1].request.params["last_index"] == "500"

        # complete run restarts from top; upsert must not duplicate
        responses.replace(responses.GET, url, json=sched_a([row(1), row(2, memo="X"), row(3)], last=None))
        fec.fetch_itemized(con, "talarico", "C00919084", 2026)
        assert con.execute("SELECT count(*) FROM contributions").fetchone()[0] == 3
        assert con.execute("SELECT count(*) FROM contributions_clean").fetchone()[0] == 2


@responses.activate
def test_fetch_totals_skips_whole_election_rollups():
    responses.get(f"{fec.BASE}/candidate/S6TX00479/totals/", json={"results": [
        {"cycle": None, "receipts": 999.0},
        {"cycle": 2026, "receipts": 146349912.38, "individual_contributions": 120e6,
         "individual_unitemized_contributions": 75299932.74, "coverage_end_date": "2026-10-14T00:00:00"}]})
    with store.connect() as con:
        assert fec.fetch_totals(con, "talarico", "S6TX00479") == 1
        r = con.execute("SELECT cycle, receipts, coverage_end_date::VARCHAR FROM fec_totals").fetchall()
    assert r == [(2026, 146349912.38, "2026-10-14")]
    assert responses.calls[0].request.params["election_full"] == "false"


@responses.activate
def test_fetch_uses_authorized_committees_only():
    responses.get(f"{fec.BASE}/candidate/S6TX00479/totals/", json={"results": [{"cycle": 2026, "receipts": 1.0}]})
    responses.get(f"{fec.BASE}/candidate/S6TX00479/committees/", json={"results": [
        {"committee_id": "C00919084", "name": "TALARICO FOR SENATE", "designation": "P"},
        {"committee_id": "C00000001", "name": "SOME JFC", "designation": "J"}]})
    responses.get(f"{fec.BASE}/schedules/schedule_a/", json=sched_a([row(1)]))
    with store.connect() as con:
        out = fec.fetch(con, PROFILE)
    assert [i["scope"] for i in out["itemized"]] == ["C00919084:2026"]
