"""Election calendar (sourced dates, stage windows) and office classification across states."""
import datetime as dt

import duckdb
import pytest
import responses
import yaml

from candidate_modeling import elections
from candidate_modeling.scope import OFFICE_CATEGORIES
from candidate_modeling.sources import states
from candidate_modeling.sources.states import georgia, michigan, pennsylvania, texas

D = dt.date


# ---------- calendar file integrity (rule calendar-sourced) ----------

def test_calendar_entries_are_complete_and_sourced():
    raw = yaml.safe_load(open(elections.CALENDAR_PATH))
    seen = set()
    for r in raw:
        for k in ("state", "date", "type", "scope", "description", "source"):
            assert r.get(k), f"missing {k}: {r}"
        assert str(r["source"]).startswith("https://"), r
        assert r["type"] in elections.CALENDAR_TYPES, r
        assert r["scope"] in ("statewide", "local"), r
        assert r.get("status", "held") in ("held", "scheduled"), r
        key = (r["state"], r["type"], r["date"])
        assert key not in seen, f"duplicate {key}"
        seen.add(key)


def test_calendar_generals_follow_the_november_rule():
    for r in elections.calendar():
        if r["type"] == "general":
            assert r["date"] == elections.election_day(r["date"].year), r


def test_calendar_states_have_connectors():
    assert {r["state"] for r in elections.calendar()} <= set(states.REGISTRY)


def test_primaries_precede_runoffs_precede_generals():
    for st in {r["state"] for r in elections.calendar()}:
        for y in {r["date"].year for r in elections.calendar() if r["state"] == st}:
            p = elections._entry(st, y, "primary")
            ru = elections._entry(st, y, "primary_runoff")
            g = elections._entry(st, y, "general")
            if p and ru:
                assert p["date"] < ru["date"]
            if p and g:
                assert p["date"] < g["date"]


# ---------- stage windows ----------

def test_texas_2026_stages():
    w = lambda s: (lambda x: (x["start_exclusive"], x["end"]))(elections.stage_window(2026, s, "TX"))  # noqa: E731
    assert w("general") == (D(2025, 11, 4), D(2026, 11, 3))
    assert w("primary") == (D(2025, 11, 4), D(2026, 3, 3))
    assert w("primary_runoff") == (D(2026, 3, 3), D(2026, 5, 26))
    assert w("general_campaign") == (D(2026, 5, 26), D(2026, 11, 3))
    assert elections.stage_window(2026, "general_runoff", "TX") is None


def test_georgia_general_runoff_and_campaign():
    gr = elections.stage_window(2026, "general_runoff", "GA")
    assert (gr["start_exclusive"], gr["end"]) == (D(2026, 11, 3), D(2026, 12, 1))
    gc = elections.stage_window(2026, "general_campaign", "GA")
    assert gc["start_exclusive"] == D(2026, 6, 16)          # after the primary runoff


def test_pennsylvania_campaign_starts_after_primary_when_no_runoff():
    gc = elections.stage_window(2026, "general_campaign", "PA")
    assert gc["start_exclusive"] == D(2026, 5, 19)


def test_missing_stage_is_none_and_stage_needs_state():
    assert elections.stage_window(2025, "primary", "TX") is None    # Texas has no odd-year primary
    with pytest.raises(ValueError, match="needs a state"):
        elections.stage_window(2026, "primary")
    with pytest.raises(ValueError):
        elections.stage_window(2026, "bogus", "TX")


def test_general_falls_back_to_rule_outside_calendar():
    w = elections.stage_window(2030, "general", "TX")
    assert w["end"] == D(2030, 11, 5) and w["end_source"].startswith("rule")


# ---------- compare by stage and office (Texas fixture) ----------

@pytest.fixture
def tx_synced(tx_zip):
    with responses.RequestsMock() as rs:
        rs.head(texas.ZIP_URL, headers={"Last-Modified": "v1"})
        rs.get(texas.ZIP_URL, body=tx_zip)
        texas.sync()


def test_compare_primary_stage(tx_synced):
    # 2024-01-15 gifts fall inside TX's 2024 primary window (2023-11-07, 2024-03-05]
    r = elections.compare_elections([2024, 2025], state="TX", stage="primary", today=D(2027, 1, 1))
    y24, y25 = r["years"]
    assert y24["window"]["end"] == "2024-03-05" and y24["total"] == 5427.0
    assert y25["coverage"]["status"] == "no_election" and y25["window"] is None
    assert any("no primary" in n for n in r["notes"])
    assert y24["raised_by_300d"] is None and y24["raised_by_30d"] == 5427.0   # 300d is before a 119-day window


def test_group_by_office_and_unclassified_share(tx_synced):
    r = elections.compare_elections([2024], state="TX", group_by="office", today=D(2027, 1, 1))
    by = {g["office"]: g["years"][2024]["total"] for g in r["by_office"]}
    assert by == {"state_house": 5350.0, "unknown": 77.0}           # Talarico (STATEREP) vs officeless COH
    assert r["years"][0]["pct_unclassified_office"] == round(100 * 77 / 5427, 1)


def test_spac_link_takes_candidate_office(tx_synced):
    c = duckdb.connect()
    rows = {r[0]: r[1:] for r in c.execute(
        f"SELECT committee_id, kind, office_category, linked_candidate_id FROM ({texas.committees_sql()})").fetchall()}
    assert rows["00077777"] == ("candidate", "state_house", "00081913")   # SUPPORT link -> his office
    assert rows["00088888"] == ("pac", "pac", None)                       # OPPOSE link stays a PAC
    assert rows["00099999"] == ("pac", "pac", None)
    r = elections.compare_elections([2026], state="TX", office="state_house", today=D(2027, 1, 1))
    assert r["years"][0]["total"] == 700.0                                 # SPAC money counted under the office


def test_office_and_kind_filters(tx_synced):
    assert elections.compare_elections([2026], state="TX", kind="pac", today=D(2027, 1, 1))["years"][0]["total"] == 300.0
    r = elections.compare_elections([2026], state="TX", office=["governor"], today=D(2027, 1, 1))
    assert r["years"][0]["rows"] == 0


def test_group_by_requires_state():
    with pytest.raises(ValueError, match="need state"):
        elections.compare_elections([2026], slugs=["x"], group_by="office")


# ---------- office classification per state ----------

def _committees(m):
    return {r[0]: r[1:] for r in duckdb.connect().execute(
        f"SELECT committee_id, kind, office_category, office_raw FROM ({m.committees_sql()})").fetchall()}


@responses.activate
def test_michigan_offices(mi_zip):
    from tests.test_states import _mock_mi
    _mock_mi(mi_zip)
    michigan.sync(years=[2026])
    c = _committees(michigan)
    assert c["0521875"] == ("candidate", "governor", "Governor Statewide".split()[0])
    assert c["0600001"][:2] == ("pac", "pac")


@responses.activate
def test_pennsylvania_offices(pa_zip):
    url = f"{pennsylvania.BASE}/2026.zip"
    responses.head(url, headers={"Last-Modified": "x"})
    responses.get(url, body=pa_zip)
    pennsylvania.sync(years=[2026])
    c = _committees(pennsylvania)
    assert c["20160016"][:2] == ("candidate", "governor")       # committee with OFFICE=GOV counts as candidate
    assert c["2026C0403"][:2] == ("candidate", "governor")
    assert c["99999999"][:2] == ("pac", "pac")


@responses.activate
def test_georgia_offices_from_registry(ga_csv):
    from tests.test_states import _mock_ga
    _mock_ga(ga_csv)
    out = georgia.sync(years=[2026])
    assert out["candidates"] == 2
    c = _committees(georgia)
    assert c["101091"] == ("candidate", "governor", "Governor")
    assert c["100599"][:2] == ("candidate", "state_house")


def test_every_mapped_category_is_in_the_taxonomy():
    for m in (texas, michigan, pennsylvania, georgia):
        for cat in set(m.OFFICE_MAP.values()) | set(getattr(m, "KIND_MAP", {}).values()):
            assert cat in OFFICE_CATEGORIES or cat == "candidate", f"{m.STATE}: {cat}"
