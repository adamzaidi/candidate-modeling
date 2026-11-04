"""The report pipeline: deterministic, numbers only from tables, honest axes."""
import datetime as dt
import importlib.util
import re

import pytest

from candidate_modeling import config

spec = importlib.util.spec_from_file_location("build", config.ROOT / "reports" / "revisions" / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

from tests.test_revisions import TODAY, tx_vintages  # noqa: E402,F401  (fixture)


@pytest.fixture
def built(tx_vintages, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(build, "REVISION_STATES", ["TX"])
    monkeypatch.setattr(build, "GOVERNOR_STATES", [])
    res = build.compute(today=TODAY)
    return res, build.render(res, tmp_path / "out"), tmp_path / "out"


def test_outputs_exist(built):
    _, out, d = built
    for f in ["report.md", "manifest.json", "tables/shares.csv", "tables/nowcast.csv", "tables/backtest.csv",
              "fig1_share_known_by_day.png", "fig2_nowcast_2026.png"]:
        assert (d / f).exists(), f
    assert out["manifest"]["rules_applied"]["vintage-reconstruction"]


def test_report_numbers_come_from_tables(built):
    _, out, _ = built
    md, n = out["report"], build.numbers(out["tables"])
    nc = next(r for r in out["tables"]["nowcast"] if r["state"] == "TX")
    assert build._m(nc["nowcast_final_usd"]) in md and build._m(nc["known_today_usd"]) in md
    assert build._pct(n["share_TX_2022_0"]) in md
    assert "{" not in md.replace("```bash", "") and "None" not in md          # no unfilled templates or leaks


def test_axes_start_at_zero(built):
    _, out, _ = built
    assert all(lo == 0 for lo, _ in out["figures"]["fig1"]["ylim"])
    assert out["figures"]["fig2"]["xlim"][0] == 0


def test_build_is_deterministic(built, tmp_path):
    res, out, d = built
    again = build.render(res, tmp_path / "again")
    assert again["report"] == out["report"]
    for name in ("shares", "nowcast", "backtest"):
        assert (tmp_path / "again" / "tables" / f"{name}.csv").read_text() == (d / "tables" / f"{name}.csv").read_text()


def test_excluded_states_are_explained(built):
    res, out, _ = built
    assert any(e["state"] == "GA" for e in res["excluded"]) and "Georgia is excluded" in out["report"]
