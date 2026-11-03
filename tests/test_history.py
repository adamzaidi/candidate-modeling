"""History tracking: versions, removals, restores, as-of reconstruction, change feed, FEC snapshots."""
import datetime as dt

import duckdb
import pytest
import responses

from candidate_modeling import config, pipeline, query, store
from candidate_modeling.sources import fec
from candidate_modeling.sources.states import texas

T1, T2, T3 = dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 15), dt.datetime(2026, 11, 1)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock(T1)
    monkeypatch.setattr(store, "now", c)
    return c


def r(tid, amount, state="TX", slug="c1", source="tx_tec", memo=False):
    return {"candidate_slug": slug, "source": source, "jurisdiction": "TX", "committee_id": "F1",
            "committee_name": "Cmte", "transaction_id": tid, "date": "2026-09-01", "amount": amount,
            "contributor_name": f"Donor {tid}", "contributor_type": "individual", "city": "Austin", "state": state,
            "zip": "78701", "employer": None, "occupation": None, "is_memo": memo, "cycle": 2026}


def total(con, src="contributions_clean"):
    return con.execute(f"SELECT coalesce(sum(amount), 0) FROM {src} WHERE candidate_slug = 'c1'").fetchone()[0]


def as_of(con, ts):
    return total(con, f"contributions_as_of(TIMESTAMP '{ts}')")


def test_full_lifecycle(clock):
    pipeline.add_candidate("C One", slug="c1", state="TX")
    with store.connect() as con:
        m1 = store.merge_rows(con, "c1", "tx_tec", [r("a", 100), r("b", 200), r("c", 300)], full=True)
        assert m1 == {"staged": 3, "new": 3, "modified": 0, "unchanged": 0, "removed": 0, "restored": 0}

        clock.t = T2  # b amended 200 -> 250, c dropped, d new
        m2 = store.merge_rows(con, "c1", "tx_tec", [r("a", 100), r("b", 250), r("d", 50)], full=True)
        assert m2 == {"staged": 3, "new": 1, "modified": 1, "unchanged": 1, "removed": 1, "restored": 0}
        assert total(con) == 400  # removed c excluded from the live view

        clock.t = T3  # c comes back
        m3 = store.merge_rows(con, "c1", "tx_tec", [r("a", 100), r("b", 250), r("c", 300), r("d", 50)], full=True)
        assert m3["restored"] == 1 and m3["unchanged"] == 3 and m3["removed"] == 0

        hist = con.execute("SELECT transaction_id, amount, valid_from, valid_to, change FROM contribution_history "
                           "ORDER BY transaction_id").fetchall()
        assert hist == [("b", 200.0, T1, T2, "modified"), ("c", 300.0, T1, T2, "removed")]
        row_a = con.execute("SELECT first_seen, last_seen, valid_from, removed_at FROM contributions "
                            "WHERE transaction_id = 'a'").fetchone()
        assert row_a == (T1, T3, T1, None)

        # as-of reconstruction at each point in time
        assert as_of(con, "2026-09-30 00:00:00") == 0            # before first observation
        assert as_of(con, "2026-10-05 00:00:00") == 600          # a + b(200) + c
        assert as_of(con, "2026-10-20 00:00:00") == 400          # a + b(250) + d, c gone
        assert as_of(con, "2026-11-02 00:00:00") == 700          # everything incl. restored c
        assert con.execute("SELECT count(*) FROM load_runs").fetchone()[0] == 3


def test_partial_merge_never_removes(clock):
    with store.connect() as con:
        store.merge_rows(con, "c1", "fec", [r("1", 10, source="fec"), r("2", 20, source="fec")])
        clock.t = T2
        m = store.merge_rows(con, "c1", "fec", [r("1", 10, source="fec")], full=False)
        assert m["removed"] == 0
        assert con.execute("SELECT count(*) FROM contributions WHERE removed_at IS NULL").fetchone()[0] == 2


def test_memo_rows_excluded_from_as_of(clock):
    with store.connect() as con:
        store.merge_rows(con, "c1", "fec", [r("1", 10, source="fec"), r("2", 999, source="fec", memo=True)], full=True)
        assert as_of(con, "2026-10-02 00:00:00") == 10


def test_changes_feed_and_as_of_summary(clock):
    pipeline.add_candidate("C One", slug="c1", state="TX")
    with store.connect() as con:
        store.merge_rows(con, "c1", "tx_tec", [r("a", 100), r("b", 200), r("c", 300, state="CA")], full=True)
        clock.t = T2
        store.merge_rows(con, "c1", "tx_tec", [r("a", 100), r("b", 250), r("d", 50)], full=True)

    ch = pipeline.contribution_changes("c1", since="2026-10-10")
    tx = ch["by_source"]["tx_tec"]
    assert tx["added"] == {"rows": 1, "amount": 50.0}           # d; baseline rows a/b/c are not "added"
    assert tx["removed"] == {"rows": 1, "amount": 300.0}
    assert tx["modified"] == {"rows": 1, "net_amount_change": 50.0}
    assert ch["samples"]["modified"][0]["old_amount"] == 200.0 and ch["samples"]["modified"][0]["new_amount"] == 250.0
    assert pipeline.contribution_changes("c1", since="2026-10-20")["by_source"] == {}  # nothing after T2

    then = pipeline.finance_summary("c1", as_of="2026-10-05")
    now_ = pipeline.finance_summary("c1")
    assert then["by_source"][0]["total"] == 600.0 and now_["by_source"][0]["total"] == 400.0
    assert then["geography"]["pct_in_state_dollars"] == 50.0  # 300 of 600 from CA back then
    assert now_["geography"]["pct_in_state_dollars"] == 100.0


def test_as_of_rejects_garbage(clock):
    pipeline.add_candidate("C One", slug="c1", state="TX")
    with pytest.raises(ValueError, match="ISO"):
        pipeline.finance_summary("c1", as_of="2026-10-01'); DROP TABLE contributions; --")


def test_as_of_macro_available_to_agent_sql(clock):
    with store.connect() as con:
        store.merge_rows(con, "c1", "tx_tec", [r("a", 100)], full=True)
    out = query.run("SELECT sum(amount) FROM contributions_as_of(TIMESTAMP '2026-10-02')")
    assert out["rows"] == [[100.0]]


def test_migrates_pre_history_database(isolated):
    path = config.db_path()
    con = duckdb.connect(str(path))
    old = [c for c in store.CONTRIBUTION_COLUMNS if c[0] not in store.DERIVED]  # v0.2 schema: no labels
    cols = ", ".join(f"{c} {t}" for c, t in old)
    con.execute(f"CREATE TABLE contributions ({cols}, PRIMARY KEY (source, transaction_id, candidate_slug))")
    con.execute("CREATE VIEW contributions_clean AS SELECT * FROM contributions")
    con.execute("INSERT INTO contributions VALUES ('c1','tx_tec','TX','F1','C','a','2026-01-01',5.0,'D','individual',"
                "'Austin','TX','78701',NULL,NULL,false,2026)")
    con.close()
    with store.connect() as con:
        row = con.execute("SELECT row_hash IS NOT NULL, first_seen IS NOT NULL, removed_at, contribution_kind "
                          "FROM contributions").fetchone()
        assert row == (True, True, None, None)  # label column added; NULL counts as monetary
        assert con.execute("SELECT count(*) FROM contributions_clean").fetchone()[0] == 1


# ---------- FEC ----------

def _sched(results, last=None):
    return {"results": results, "pagination": {"last_indexes": last}}


def _fec_row(sub_id, amount=100.0):
    return {"sub_id": sub_id, "committee_id": "C1", "committee": {"name": "X"}, "contribution_receipt_date": "2026-06-30",
            "contribution_receipt_amount": amount, "contributor_name": "D", "entity_type": "IND",
            "contributor_state": "TX", "two_year_transaction_period": 2026}


@responses.activate
def test_fec_removal_only_after_complete_pass(clock):
    url = f"{fec.BASE}/schedules/schedule_a/"
    responses.get(url, json=_sched([_fec_row(1), _fec_row(2)]))
    with store.connect() as con:
        assert fec.fetch_itemized(con, "c1", "C1", 2026)["complete"] is True

        # new pass, stopped after page 1 of 2: row 2 not seen yet, must NOT be removed
        clock.t = T2
        responses.replace(responses.GET, url, json=_sched([_fec_row(1, 150.0)], last={"last_index": "9"}))
        out = fec.fetch_itemized(con, "c1", "C1", 2026, max_pages=1)
        assert out["complete"] is False and out["modified"] == 1
        assert con.execute("SELECT count(*) FROM contributions WHERE removed_at IS NOT NULL").fetchone()[0] == 0

        # resume: last page is empty -> pass completes; row 2 never appeared this pass -> removed
        clock.t = T3
        responses.replace(responses.GET, url, json=_sched([]))
        out = fec.fetch_itemized(con, "c1", "C1", 2026)
        assert out["complete"] is True and out["removed"] == 1
        assert responses.calls[-1].request.params["last_index"] == "9"
        assert con.execute("SELECT transaction_id FROM contributions WHERE removed_at IS NOT NULL").fetchall() == [("2",)]


@responses.activate
def test_fec_totals_history_dedupes(clock):
    url = f"{fec.BASE}/candidate/S1/totals/"
    responses.get(url, json={"results": [{"cycle": 2026, "receipts": 100.0}]})
    pipeline.add_candidate("C One", slug="c1")
    with store.connect() as con:
        assert fec.fetch_totals(con, "c1", "S1")["new_snapshots"] == 1
        clock.t = T2
        assert fec.fetch_totals(con, "c1", "S1")["new_snapshots"] == 0  # unchanged
        clock.t = T3
        responses.replace(responses.GET, url, json={"results": [{"cycle": 2026, "receipts": 250.0}]})
        assert fec.fetch_totals(con, "c1", "S1")["new_snapshots"] == 1
    hist = pipeline.fundraising_history("c1")
    assert [(h["fetched_at"][:10], h["receipts"]) for h in hist] == [("2026-10-01", 100.0), ("2026-11-01", 250.0)]


# ---------- state integration ----------

@responses.activate
def test_texas_resync_records_amendment_and_removal(clock, tx_zip):
    import io
    import zipfile

    from tests.conftest import TX_CONTRIB_HEADER, _csv_bytes, tx_contrib

    responses.head(texas.ZIP_URL, headers={"Last-Modified": "v1"})
    responses.get(texas.ZIP_URL, body=tx_zip)
    texas.sync()
    p = {"slug": "talarico", "name": "J", "state": "TX", "ids": {"state": {"TX": {"filer_ids": ["00081913"]}}}}
    with store.connect() as con:
        assert texas.load(con, p)["changes"]["new"] == 4

    # upstream amends contribution 1 (100 -> 175) and drops contribution 2
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(tx_zip)) as old, zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("filers.csv", old.read("filers.csv"))
        zf.writestr("contribs_01.csv", _csv_bytes(TX_CONTRIB_HEADER, [
            tx_contrib(1, amount="175.00"), tx_contrib(3, amount="5000.00", kind="ENTITY", contributorEmployer="")]))
        zf.writestr("contribs_02.csv", old.read("contribs_02.csv"))
    clock.t = T2
    responses.replace(responses.HEAD, texas.ZIP_URL, headers={"Last-Modified": "v2"})
    responses.replace(responses.GET, texas.ZIP_URL, body=buf.getvalue())
    texas.sync()
    with store.connect() as con:
        ch = texas.load(con, p)["changes"]
    assert (ch["modified"], ch["removed"], ch["unchanged"], ch["new"]) == (1, 1, 2, 0)


def test_derived_label_change_is_not_a_modification(clock):
    with store.connect() as con:
        a = r("a", 100)
        store.merge_rows(con, "c1", "tx_tec", [{**a, "contribution_kind": None}], full=True)
        clock.t = T2
        m = store.merge_rows(con, "c1", "tx_tec", [{**a, "contribution_kind": "in_kind"}], full=True)
        assert (m["modified"], m["unchanged"]) == (0, 1)
        assert con.execute("SELECT contribution_kind, valid_from FROM contributions").fetchone() == ("in_kind", T1)
        assert con.execute("SELECT count(*) FROM contribution_history").fetchone()[0] == 0


def test_rehash_folds_back_spurious_versions(clock):
    with store.connect() as con:
        store.merge_rows(con, "c1", "tx_tec", [r("a", 100)], full=True)
        # simulate the pre-fix bug: a 'modified' version whose hashed values equal the current row
        con.execute("INSERT INTO contribution_history SELECT " + ", ".join(store.COLUMN_NAMES)
                    + ", row_hash, valid_from, ?, 'modified' FROM contributions", [T2])
        con.execute("UPDATE contributions SET valid_from = ?", [T2])
        con.execute("DELETE FROM store_meta")
    with store.connect() as con:  # reconnect runs the migration
        assert con.execute("SELECT count(*) FROM contribution_history").fetchone()[0] == 0
        assert con.execute("SELECT valid_from FROM contributions").fetchone()[0] == T1
