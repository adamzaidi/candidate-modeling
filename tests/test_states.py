import responses
from responses import matchers

from candidate_modeling import store
from candidate_modeling.sources import states
from candidate_modeling.sources.states import michigan, texas


def _profile(state, key, ids):
    return {"slug": "talarico", "name": "James Talarico", "state": state,
            "ids": {"fec_candidate_ids": [], "state": {state: {key: ids}}}}


# ---------- Texas ----------

def _mock_tx(body, last_modified="Tue, 03 Nov 2026 11:35:03 GMT"):
    responses.head(texas.ZIP_URL, headers={"Last-Modified": last_modified})
    responses.get(texas.ZIP_URL, body=body)


@responses.activate
def test_texas_sync_search_load(tx_zip):
    _mock_tx(tx_zip)
    out = texas.sync()
    assert out["contrib_files"] == 2  # contribs_01/02 only; cont_ss ignored
    assert not (texas.state_dir("TX") / "TEC_CF_CSV.zip").exists()  # 1 GB zip cleaned up after build

    hits = texas.search("James Talarico")
    assert [h["filer_id"] for h in hits] == ["00081913"]  # PAC with same name excluded (not COH)
    assert hits[0]["office_held"] == "STATEREP 50"

    with store.connect() as con:
        r = texas.load(con, _profile("TX", "filer_ids", ["00081913"]))
        assert r["rows"] == 4  # 1,2,3,6 — superseded (4), other filer (5), cont_ss (7) excluded
        rows = con.execute("SELECT transaction_id, amount, state, zip, contributor_type, contributor_name, cycle, "
                           "occupation FROM contributions ORDER BY transaction_id::INT").fetchall()
    assert rows[0] == ("1", 100.0, "TX", "78701", "individual", "Doe, Jane", 2024, "Teacher")
    assert rows[1][7] == "Line one\nline two"  # quoted newline preserved
    assert rows[2][4:6] == ("organization", "Acme PAC")
    assert rows[3][6] == 2026  # 2025 contribution -> 2026 cycle


@responses.activate
def test_texas_sync_skips_download_when_unchanged(tx_zip):
    _mock_tx(tx_zip)
    texas.sync()
    texas.sync()
    gets = [c for c in responses.calls if c.request.method == "GET"]
    assert len(gets) == 1


@responses.activate
def test_texas_sync_redownloads_when_upstream_changes(tx_zip):
    _mock_tx(tx_zip)
    texas.sync()
    responses.replace(responses.HEAD, texas.ZIP_URL, headers={"Last-Modified": "Wed, 04 Nov 2026 11:35:03 GMT"})
    texas.sync()
    assert len([c for c in responses.calls if c.request.method == "GET"]) == 2


@responses.activate
def test_texas_reload_replaces_not_duplicates(tx_zip):
    _mock_tx(tx_zip)
    texas.sync()
    p = _profile("TX", "filer_ids", ["00081913"])
    with store.connect() as con:
        texas.load(con, p)
        texas.load(con, p)
        assert con.execute("SELECT count(*) FROM contributions").fetchone()[0] == 4


def test_texas_unsynced_is_explicit():
    assert "not synced" in texas.search("x")[0]["error"]
    with store.connect() as con:
        assert "not synced" in texas.load(con, _profile("TX", "filer_ids", ["1"]))["note"]


# ---------- Michigan ----------

LIST_JSON = {"success": True, "data": {"list": [
    {"transactiontype": "Contribution", "year": 2026, "lastupdatedraw": "Nov 3, 2026, 3:20:00 AM", "download": 24522},
    {"transactiontype": "Expenditure", "year": 2026, "lastupdatedraw": "Nov 3, 2026, 3:20:00 AM", "download": 1}],
    "totalRecords": 2}}


def _mock_mi(body, listing=LIST_JSON):
    responses.get(michigan.BASE, json=listing,
                  match=[matchers.query_param_matcher({"page": michigan.LIST_PAGE}, strict_match=False)])
    responses.get(michigan.BASE, body=body,
                  match=[matchers.query_param_matcher({"page": michigan.FILE_PAGE, "id": "24522"})])


@responses.activate
def test_michigan_sync_search_load(mi_zip):
    _mock_mi(mi_zip)
    out = michigan.sync(years=[2026, 2027])
    assert out["years"] == [{"year": 2026, "status": "updated"}, {"year": 2027, "status": "not published"}]
    assert michigan.synced_years() == [2026]

    hits = michigan.search("Jocelyn Benson")
    assert len(hits) == 1  # the Political committee row isn't a candidate committee
    assert hits[0]["committee_id"] == "0521875" and hits[0]["rows"] == 4
    assert hits[0]["office"] == "Governor Statewide"

    with store.connect() as con:
        r = michigan.load(con, _profile("MI", "committee_ids", ["0521875"]))
        assert r["rows"] == 4
        rows = con.execute("SELECT transaction_id, amount, state, contributor_type, contributor_name, date::VARCHAR "
                           "FROM contributions ORDER BY transaction_id").fetchall()
    assert rows[0] == ("26-1-0", 50.0, "MI", "individual", "Alice Korach", "2026-09-11")
    assert rows[1][1] == 1250.0  # comma in amount
    assert rows[2][3:5] == ("organization", 'Big "Union" Local 1')  # stray quotes survive


@responses.activate
def test_michigan_sync_skips_unchanged(mi_zip):
    _mock_mi(mi_zip)
    michigan.sync(years=[2026])
    assert michigan.sync(years=[2026])["years"][0]["status"] == "current"


# ---------- registry ----------

def test_registry_and_unknown_state():
    assert set(states.REGISTRY) == {"TX", "MI"}
    for m in states.REGISTRY.values():
        for attr in ("STATE", "SOURCE", "sync", "search", "load", "is_synced"):
            assert hasattr(m, attr), f"{m.__name__} missing {attr}"
    try:
        states.get("GA")
    except KeyError as e:
        assert "Planned next" in str(e)
