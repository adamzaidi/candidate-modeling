"""Election-window comparisons and the scope/rules registry."""
import asyncio
import datetime as dt

import pytest
import responses

from candidate_modeling import config, elections, pipeline, scope, store
from candidate_modeling.sources import states
from candidate_modeling.sources.states import texas


# ---------- election calendar ----------

@pytest.mark.parametrize("year,day", [(2020, "2020-11-03"), (2022, "2022-11-08"), (2024, "2024-11-05"),
                                      (2025, "2025-11-04"), (2026, "2026-11-03"), (2027, "2027-11-02")])
def test_election_day(year, day):
    assert elections.election_day(year).isoformat() == day


def test_windows_are_contiguous():
    assert elections.window(2026)[0] == elections.election_day(2025)
    assert elections.window(2025)[1] == elections.election_day(2025)


@pytest.mark.parametrize("covered,status", [([2024, 2025], "complete"), ([2025], "partial"), ([2024], "missing"),
                                            ([], "missing")])
def test_coverage_status(covered, status):
    assert elections._coverage(2025, covered)["status"] == status


# ---------- statewide comparison (Texas fixture) ----------

@pytest.fixture
def tx_synced(tx_zip):
    with responses.RequestsMock() as rs:
        rs.head(texas.ZIP_URL, headers={"Last-Modified": "v1"})
        rs.get(texas.ZIP_URL, body=tx_zip)
        texas.sync()


def test_compare_elections_statewide(tx_synced):
    # fixture: 2024-01-15 gifts 100 (TX), 250 (CA), 5000 (org), 77 (other filer); 2025-03-01 gift 1000
    r = elections.compare_elections([2024, 2025], state="TX", today=dt.date(2026, 6, 1))
    y24, y25 = r["years"]
    assert (y24["total"], y24["individuals_total"], y24["organizations_total"]) == (5427.0, 427.0, 5000.0)
    assert y24["recipients"] == 2 and y24["coverage"]["status"] == "complete"
    days_out = (dt.date(2024, 11, 5) - dt.date(2024, 1, 15)).days  # 295
    assert days_out == 295
    assert y24["raised_by_300d"] == 0 and y24["raised_by_180d"] == 5427.0  # nothing before 300d out
    assert y24["pct_in_state_dollars"] == round(100 * 5177 / 5427, 1)
    assert y25["total"] == 1000.0
    assert y24["top_donors"][0]["contributor_name"] == "Acme PAC" and y24["pct_from_top_donor"] == 92.1
    assert r["changes"][0]["total_pct_change"] == round(100 * (1000 - 5427) / 5427, 1)
    assert any("different contests" in n for n in r["notes"])        # odd vs even year
    assert any("statewide-double-count" in n for n in r["notes"])


def test_partial_coverage_skips_change(tx_synced, monkeypatch):
    monkeypatch.setattr(texas, "covered_years", lambda: [2025])  # pretend Nov-Dec 2024 is missing
    r = elections.compare_elections([2025, 2026], state="TX", today=dt.date(2027, 1, 1))
    assert r["years"][0]["coverage"]["status"] == "partial"
    assert "skipped" in r["changes"][0]


def test_in_progress_and_reporting_lag(tx_synced):
    r = elections.compare_elections([2025], state="TX", today=dt.date(2025, 6, 1))
    y = r["years"][0]
    assert y["in_progress"]["days_until_election"] == (dt.date(2025, 11, 4) - dt.date(2025, 6, 1)).days
    assert y["raised_by_30d"] is None and y["raised_by_180d"] is not None   # future checkpoints blanked
    lag = elections.compare_elections([2025], state="TX", today=dt.date(2025, 11, 10))["years"][0]
    assert lag["reporting_incomplete"]["last_contribution_date"] == "2025-03-01"


def test_compare_requires_exactly_one_subject():
    with pytest.raises(ValueError):
        elections.compare_elections([2026])
    with pytest.raises(ValueError):
        elections.compare_elections([2026], state="TX", slugs=["x"])


def test_unsynced_state_is_explicit():
    with pytest.raises(ValueError, match="not synced"):
        elections.compare_elections([2026], state="TX")


# ---------- candidate comparison ----------

def test_compare_elections_for_profiled_candidate():
    pipeline.add_candidate("C One", slug="c1", state="TX", fec_candidate_ids=["S1"],
                           state_ids={"TX": ["00081913"]})
    base = {"candidate_slug": "c1", "jurisdiction": "TX", "committee_id": "F1", "committee_name": "C",
            "contributor_type": "individual", "city": None, "zip": "78701", "employer": None, "occupation": None,
            "is_memo": False, "cycle": 2026}
    with store.connect() as con:
        store.upsert_contributions(con, [
            {**base, "source": "tx_tec", "transaction_id": "1", "date": "2022-06-01", "amount": 100.0,
             "contributor_name": "A", "state": "TX"},
            {**base, "source": "tx_tec", "transaction_id": "2", "date": "2026-06-01", "amount": 300.0,
             "contributor_name": "B", "state": "CA"}])
    r = elections.compare_elections([2022, 2026], slugs=["c1"], today=dt.date(2027, 1, 1))
    y22, y26 = r["years"]
    assert (y22["total"], y26["total"]) == (100.0, 300.0)
    assert y22["pct_in_state_dollars"] == 100.0 and y26["pct_in_state_dollars"] == 0.0
    # TX not synced in this test and FEC never fetched -> coverage reports it rather than hiding it
    assert y26["coverage"]["sources"]["c1:fec"]["status"] == "missing"
    assert y26["coverage"]["sources"]["c1:TX"]["status"] == "missing"
    assert "skipped" in r["changes"][0]


# ---------- scope / rules ----------

def test_rule_ids_unique_and_nonempty():
    ids = [rid for items in scope.RULES.values() for rid, text in items]
    assert len(ids) == len(set(ids)) and all(t for items in scope.RULES.values() for _, t in items)


def test_every_source_declares_scope():
    from candidate_modeling.sources import fec
    for m in [fec, *states.REGISTRY.values()]:
        for k in ("name", "url", "covers", "excludes", "history_from", "refresh", "rules", "known_gaps"):
            assert k in m.SCOPE, f"{m.__name__} SCOPE missing {k}"
    for m in states.REGISTRY.values():
        assert callable(m.covered_years) and callable(m.normalized_sql)


def test_rules_referenced_in_code_exist():
    ids = {rid for items in scope.RULES.values() for rid, _ in items}
    r = elections.compare_elections.__defaults__  # noqa: F841 (just ensure import)
    import inspect
    src = inspect.getsource(elections)
    for rid in ["election-window", "align-by-days", "coverage-first", "reporting-lag", "self-funding",
                "statewide-double-count", "like-with-like"]:
        assert rid in ids and rid in src + scope.rules_text()


def test_readme_matches_registry():
    text = (config.ROOT / "README.md").read_text()
    assert scope.BEGIN in text and scope.END in text
    block = text[text.index(scope.BEGIN) + len(scope.BEGIN):text.index(scope.END)].strip()
    assert block == scope.render_markdown().strip(), "README scope section is stale: run `cm docs`"


def test_data_scope_reports_live_coverage(tx_synced):
    d = scope.data_scope()
    assert d["sources"]["TX"]["synced"] is True and 2025 in d["sources"]["TX"]["covered_years"]
    assert d["sources"]["GA"]["synced"] is False and d["sources"]["GA"]["covered_years"] == []
    assert "FEC" in d["sources"] and d["rules"]["comparisons"]


def test_mcp_instructions_carry_rules():
    from candidate_modeling.mcp_server import INSTRUCTIONS, server
    for rid, text in [x for items in scope.RULES.values() for x in items]:
        assert f"- {rid}: {text}" in INSTRUCTIONS
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"data_scope", "compare_elections"} <= names
