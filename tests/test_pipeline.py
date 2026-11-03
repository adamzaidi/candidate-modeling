import asyncio
import json

import pytest
import responses

from candidate_modeling import cli, config, pipeline, query, store
from candidate_modeling.sources import fec
from candidate_modeling.sources.states import texas


def _row(i, amount, state="TX", ctype="individual", employer="AUSTIN ISD", memo=False, date="2024-01-15",
         source="tx_tec", jur="TX", slug="talarico"):
    return {"candidate_slug": slug, "source": source, "jurisdiction": jur, "committee_id": "C1",
            "committee_name": "X", "transaction_id": str(i), "date": date, "amount": amount,
            "contributor_name": f"D{i}", "contributor_type": ctype, "city": "Austin", "state": state, "zip": "78701",
            "employer": employer, "occupation": "TEACHER", "is_memo": memo, "cycle": 2024}


@pytest.fixture
def seeded():
    pipeline.add_candidate("James Talarico", slug="talarico", state="TX", fec_candidate_ids=["S6TX00479"],
                           state_ids={"TX": {"filer_ids": ["00081913"]}})
    pipeline.add_candidate("Other Person", state="MI")
    with store.connect() as con:
        store.upsert_contributions(con, [
            _row(1, 100), _row(2, 300, state="CA"), _row(3, 5000, ctype="organization", employer=None),
            _row(4, 1500, employer="RETIRED"), _row(5, 9999, memo=True),  # memo must be ignored
            _row(10, 50, state="MI", slug="other-person", source="mi_boe", jur="MI"),
        ])
        con.execute("INSERT INTO fec_totals VALUES ('talarico','S6TX00479',2026,146349912.38,0,0,"
                    "120000000,44700000,75299932.74,0,'2026-10-14')")


# ---------- profiles ----------

def test_add_candidate_merges_ids():
    pipeline.add_candidate("James Talarico", state="tx", state_ids={"TX": {"filer_ids": ["00081913"]}})
    pipeline.add_candidate("James Talarico", fec_candidate_ids=["S6TX00479"],
                           state_ids={"TX": {"filer_ids": ["00081913", "00000002"]}})
    p = config.load_profile("james-talarico")
    assert p["state"] == "TX"
    assert p["ids"]["fec_candidate_ids"] == ["S6TX00479"]
    assert p["ids"]["state"]["TX"]["filer_ids"] == ["00000002", "00081913"]


def test_legacy_profile_field_is_migrated(isolated):
    d = isolated / "candidates"
    d.mkdir(exist_ok=True)
    (d / "old.yaml").write_text("slug: old\nname: Old\nids:\n  fec_candidate_id: S1\n")
    assert config.load_profile("old")["ids"]["fec_candidate_ids"] == ["S1"]


def test_missing_profile_message():
    with pytest.raises(FileNotFoundError, match="find_candidate"):
        config.load_profile("nobody")


# ---------- analysis ----------

def test_finance_summary_numbers(seeded):
    s = pipeline.finance_summary("talarico")
    assert s["by_source"][0]["rows"] == 4 and s["by_source"][0]["total"] == 6900.0  # memo excluded
    # in-state: 100 + 5000 + 1500 of 6900
    assert s["geography"]["pct_in_state_dollars"] == round(100 * 6600 / 6900, 1)
    assert s["geography"]["pct_in_state_count"] == 75.0
    mix = {r["bucket"]: r["n"] for r in s["individual_size_mix"]}
    assert mix == {"a: under $200": 1, "b: $200-999": 1, "c: $1,000-4,999": 1}
    assert [e["employer"] for e in s["top_employers"]] == ["AUSTIN ISD"]  # RETIRED filtered
    assert s["fec_totals"][0]["pct_individual_unitemized"] == 62.7
    assert s["caveats"]
    json.dumps(s)  # must be JSON-serializable for MCP


def test_summary_filters_by_jurisdiction(seeded):
    assert pipeline.finance_summary("talarico", jurisdiction="federal")["by_source"] == []


def test_compare_candidates(seeded):
    rows = pipeline.compare_candidates(["talarico", "other-person"])
    assert rows[0]["itemized_total"] == 6900.0 and rows[1]["itemized_total"] == 50.0
    assert rows[1]["pct_in_state_dollars"] == 100.0


@responses.activate
def test_find_candidate_survives_fec_failure(tx_zip):
    responses.get(f"{fec.BASE}/candidates/search/", status=500)
    responses.head(texas.ZIP_URL, headers={"Last-Modified": "x"})
    responses.get(texas.ZIP_URL, body=tx_zip)
    texas.sync()
    out = pipeline.find_candidate("Talarico", state="TX")
    assert "fec_error" in out
    assert out["states"]["TX"][0]["filer_id"] == "00081913"


def test_find_candidate_unsupported_state():
    with responses.RequestsMock() as rs:
        rs.get(f"{fec.BASE}/candidates/search/", json={"results": []})
        out = pipeline.find_candidate("Someone", state="GA")
    assert "No connector for GA" in out["states"]["GA"][0]["error"]


# ---------- query safety ----------

def test_query_select_works(seeded):
    r = query.run("SELECT count(*) AS n FROM contributions_clean WHERE candidate_slug = 'talarico'")
    assert r == {"columns": ["n"], "rows": [[4]], "truncated": False}


@pytest.mark.parametrize("sql", [
    "DELETE FROM contributions",
    "DROP TABLE contributions",
    "COPY contributions TO '/tmp/x.csv'",
    "SELECT 1; SELECT 2",
    "ATTACH 'other.db'",
])
def test_query_rejects_writes(seeded, sql):
    with pytest.raises(ValueError):
        query.run(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_text('/etc/hosts')",
    "SELECT * FROM read_csv('https://example.com/x.csv')",
    "SELECT * FROM read_text('{root}/.env.example')",
])
def test_query_cannot_read_outside_data_dir(seeded, sql):
    from candidate_modeling.config import ROOT
    with pytest.raises(Exception, match="(?i)permission|disabled|not allowed"):
        query.run(sql.format(root=ROOT))


@pytest.mark.parametrize("slug", ["../escape", "a/b", "/etc/x", "UPPER", ".hidden"])
def test_bad_slugs_rejected(slug):
    with pytest.raises(ValueError, match="Invalid slug"):
        pipeline.add_candidate("Someone", slug=slug)
    with pytest.raises(ValueError, match="Invalid slug"):
        config.load_profile(slug)


def test_empty_slug_means_auto_but_cannot_be_loaded():
    assert pipeline.add_candidate("Some One", slug="")["profile"]["slug"] == "some-one"
    with pytest.raises(ValueError, match="Invalid slug"):
        config.load_profile("")


def test_query_truncates(seeded):
    r = query.run("SELECT * FROM range(1000)", max_rows=10)
    assert len(r["rows"]) == 10 and r["truncated"] is True


@responses.activate
def test_state_bulk_views_queryable(seeded, tx_zip):
    responses.head(texas.ZIP_URL, headers={"Last-Modified": "x"})
    responses.get(texas.ZIP_URL, body=tx_zip)
    texas.sync()
    r = query.run("SELECT count(*) FROM tx_contribs_raw")
    assert r["rows"] == [[5]]  # superseded row hidden; other filers visible
    assert "tx_filers" in query.describe_schema()["tables"]


# ---------- MCP + CLI ----------

def test_mcp_tools_registered_and_callable(seeded):
    from candidate_modeling.mcp_server import server

    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {"find_candidate", "add_candidate", "fetch_finances", "finance_summary", "run_query",
            "describe_schema", "sync_state", "compare_candidates", "list_candidates"} <= tools
    result = asyncio.run(server.call_tool("list_candidates", {}))
    assert "talarico" in json.dumps(result, default=str)


def test_cli_summary(seeded, capsys):
    cli.main(["summary", "talarico", "--jurisdiction", "TX"])
    out = json.loads(capsys.readouterr().out)
    assert out["by_source"][0]["total"] == 6900.0
