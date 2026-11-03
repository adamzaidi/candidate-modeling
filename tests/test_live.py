"""Smoke tests against the real APIs. Run with: pytest -m live"""
import pytest
from dotenv import dotenv_values

from candidate_modeling.config import ROOT
from candidate_modeling.sources import fec
from candidate_modeling.sources.states import michigan

pytestmark = pytest.mark.live


def test_fec_finds_talarico(monkeypatch):
    real = dotenv_values(ROOT / ".env").get("FEC_API_KEY") or "DEMO_KEY"
    monkeypatch.setenv("FEC_API_KEY", real)
    ids = [c["candidate_id"] for c in fec.search_candidates("Talarico", state="TX")]
    assert "S6TX00479" in ids


def test_michigan_listing_has_current_year():
    years = [f["year"] for f in michigan.list_files()]
    assert max(years) >= 2026
