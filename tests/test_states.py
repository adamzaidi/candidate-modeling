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
    assert set(states.REGISTRY) == {"TX", "MI", "PA", "GA"}
    for m in states.REGISTRY.values():
        for attr in ("STATE", "SOURCE", "sync", "search", "load", "is_synced"):
            assert hasattr(m, attr), f"{m.__name__} missing {attr}"
    try:
        states.get("WY")
    except KeyError as e:
        assert "Planned next" in str(e)


# ---------- Pennsylvania ----------

from candidate_modeling.sources.states import pennsylvania  # noqa: E402


def test_pa_parse_line_handles_unescaped_quotes():
    row = pennsylvania.parse_line('1,"20260011","ILLEGIBLE, CHESTER "CHET" &amp; MARION","",100.00,""')
    assert row == ["1", "20260011", 'ILLEGIBLE, CHESTER "CHET" &amp; MARION', "", "100.00", ""]


@responses.activate
def test_pa_sync_search_load(pa_zip):
    url = f"{pennsylvania.BASE}/2026.zip"
    responses.head(url, headers={"Last-Modified": "Fri, 30 Oct 2026 18:02:41 GMT"})
    responses.get(url, body=pa_zip)
    responses.head(f"{pennsylvania.BASE}/2027.zip", status=404)
    out = pennsylvania.sync(years=[2026, 2027])
    y = out["years"][0]
    assert y["contrib"] == {"rows": 9, "unparseable": 0} and y["filer"]["unparseable"] == 0
    assert out["years"][1]["status"] == "not published"

    hits = {h["filer_id"]: h for h in pennsylvania.search("Shapiro")}
    assert hits["20160016"]["filer_type"] == "committee" and hits["2026C0403"]["filer_type"] == "candidate"
    assert "99999999" not in hits

    with store.connect() as con:
        r = pennsylvania.load(con, _profile("PA", "filer_ids", ["20160016"]))
        rows = con.execute("SELECT contributor_name, amount, state, contributor_type, employer FROM contributions "
                           "ORDER BY contributor_name, amount").fetchall()
    names = [x[0] for x in rows]
    assert r["rows"] == 8
    assert names.count("Jane Doe") == 2                      # identical gifts both kept
    assert names.count('ILLEGIBLE, CHESTER "CHET" SMITH') == 2  # two slots unpivoted
    assert "Superseded Original" not in names and "Amended Version" in names
    assert "José Café" in names                               # cp1252 line decoded
    assert ("IBEW Local 98 COPE", 500000.0, "PA", "organization", None) in rows  # IC section -> organization
    assert any(x[0] == "Out Of Stater" and x[2] == "NY" and x[4].startswith("Flitter Milz PC") for x in rows)


@responses.activate
def test_pa_sync_skips_unchanged(pa_zip):
    url = f"{pennsylvania.BASE}/2026.zip"
    responses.head(url, headers={"Last-Modified": "x"})
    responses.get(url, body=pa_zip)
    pennsylvania.sync(years=[2026])
    assert pennsylvania.sync(years=[2026])["years"][0]["status"] == "current"


# ---------- Georgia ----------

from candidate_modeling.sources.states import georgia  # noqa: E402

GA_LIST = {"data": {"items": [{"transactionTypeCode": "TCON", "dataExtractedDate": "2026-11-03T15:59:52.94"}]},
           "succeeded": True}


def _mock_ga(body):
    responses.post(f"{georgia.API}/PublicFiledReportAndDownload/GetDataDownloadDetails", json=GA_LIST)
    responses.post(f"{georgia.API}/ExportPublicData/GetExportPublicDownloadData", body=body,
                   match=[matchers.json_params_matcher({"FilingYear": 2026, "TransactionTypeCode": "TCON", "Type": "CSV"})])
    responses.post(f"{georgia.API}/ExportPublicData/GetExportPublicDownloadData", body=body.split(b"\r\n")[0] + b"\r\n",
                   match=[matchers.json_params_matcher({"FilingYear": 2025, "TransactionTypeCode": "TCON", "Type": "CSV"})])


@responses.activate
def test_ga_sync_search_load(ga_csv):
    _mock_ga(ga_csv)
    out = georgia.sync(years=[2024, 2025, 2026])
    st = {y["year"]: y for y in out["years"]}
    assert st[2024]["status"].startswith("not covered") and st[2025]["status"] == "not published"
    assert st[2026] == {"year": 2026, "status": "updated", "rows": 9, "unparseable": 0}

    hits = georgia.search("Burt Jones")
    assert [h["filer_id"] for h in hits] == ["101091"]
    assert georgia.search("Barton")[0]["name"] == 'Barton, Madison "Matt" F.'

    with store.connect() as con:
        r = georgia.load(con, _profile("GA", "filer_ids", ["101091"]))
        rows = {x[0]: x[1:] for x in con.execute(
            "SELECT transaction_id, amount, contributor_type, contributor_name, state, zip FROM contributions").fetchall()}
    assert r["rows"] == 6                                    # 1,2,4,5,7,8 — loan (6) and other filer (3) excluded
    assert rows["101091-1"] == (1000.0, "individual", "Doe, Jane", "GA", "30342")
    assert rows["101091-2"][1:3] == ("organization", "GTBA - Georgia's Rural PAC")
    assert rows["101091-4"][:2] == (250.0, "unitemized") and rows["101091-4"][3] is None
    assert rows["101091-5"][0] == -500.0                     # ($500.00) -> negative
    assert rows["101091-7"][0] == 150.0                      # amended copy wins


@responses.activate
def test_ga_sync_skips_when_extract_unchanged(ga_csv):
    _mock_ga(ga_csv)
    georgia.sync(years=[2026])
    assert georgia.sync(years=[2026])["years"][0]["status"] == "current"


def test_geography_ignores_rows_without_state():
    from candidate_modeling import pipeline
    pipeline.add_candidate("G", slug="g", state="GA")
    base = {"candidate_slug": "g", "source": "ga_gtcfc", "jurisdiction": "GA", "committee_id": "1", "committee_name": "x",
            "date": "2026-01-01", "contributor_name": None, "city": None, "zip": None, "employer": None,
            "occupation": None, "is_memo": False, "cycle": 2026}
    with store.connect() as con:
        store.upsert_contributions(con, [
            {**base, "transaction_id": "a", "amount": 100.0, "contributor_type": "individual", "state": "GA"},
            {**base, "transaction_id": "b", "amount": 100.0, "contributor_type": "individual", "state": "FL"},
            {**base, "transaction_id": "c", "amount": 800.0, "contributor_type": "unitemized", "state": None}])
    assert pipeline.finance_summary("g")["geography"]["pct_in_state_dollars"] == 50.0
