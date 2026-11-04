"""Revision analysis: vintages from filing dates, shares/revisions, nowcast, leave-one-out backtest."""
import datetime as dt
import io
import zipfile

import pytest
import responses

from candidate_modeling.revisions import revision_analysis
from candidate_modeling.sources.states import texas
from tests.conftest import TX_CONTRIB_HEADER, TX_FILER_HEADER, _csv_bytes, tx_contrib

D = dt.date
E22, E24, E26 = D(2022, 11, 8), D(2024, 11, 5), D(2026, 11, 3)


def gift(i, election, filed_offset, amount="100.00", given_offset=-40):
    return tx_contrib(i, amount=amount, contributionDt=(election + dt.timedelta(days=given_offset)).strftime("%Y%m%d"),
                      receivedDt=(election + dt.timedelta(days=filed_offset)).strftime("%Y%m%d"))


@pytest.fixture
def tx_vintages():
    rows = ([gift(1, E22, -10), gift(2, E22, -10), gift(3, E22, 20), gift(4, E22, 100)]        # 2022: 400 final
            + [gift(11, E24, -10), gift(12, E24, -10), gift(13, E24, -10), gift(14, E24, 20)]  # 2024: 400 final
            + [gift(21, E26, -10), gift(22, E26, -10)])                                        # 2026: settling
    filers = [{"filerIdent": "00081913", "filerTypeCd": "COH", "filerName": "Talarico, James",
               "ctaSeekOfficeCd": "STATEREP"}]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("filers.csv", _csv_bytes(TX_FILER_HEADER, filers))
        zf.writestr("contribs_01.csv", _csv_bytes(TX_CONTRIB_HEADER, rows))
    with responses.RequestsMock() as rs:
        rs.head(texas.ZIP_URL, headers={"Last-Modified": "v1"})
        rs.get(texas.ZIP_URL, body=buf.getvalue())
        texas.sync()


TODAY = D(2026, 11, 20)  # 17 days after the 2026 election


def test_shares_and_revisions_for_mature_years(tx_vintages):
    r = revision_analysis("TX", [2022, 2024, 2026], lags=(0, 14, 30, 180), today=TODAY)
    y = {e["election_year"]: e for e in r["years"]}
    assert y[2022]["status"] == "mature" and y[2026]["status"] == "settling"
    r22 = y[2022]["rows"][0]
    assert r22["total_now"] == 400.0 and r22["pct_with_filed_date"] == 100.0
    assert r22["share_known_by_lag"] == {0: 0.5, 14: 0.5, 30: 0.75, 180: 1.0}
    assert r22["revision_to_final_pct"][0] == 100.0          # election-day vintage understated the final by half
    assert y[2024]["rows"][0]["share_known_by_lag"][0] == 0.75
    assert y[2026]["rows"][0]["known_by_lag"][30] is None    # vintage date still in the future


def test_nowcast_uses_mean_share_at_same_lag(tx_vintages):
    r = revision_analysis("TX", [2022, 2024, 2026], lags=(0, 14, 30, 180), today=TODAY)
    n = r["nowcasts"][0]
    assert (n["election_year"], n["lag_days"], n["known_today"]) == (2026, 14, 200.0)
    assert n["mean_share_known_at_lag"] == 0.625 and n["nowcast_final"] == 320.0
    assert n["range"] == [round(200 / 0.75, 2), 400.0]


def test_leave_one_out_backtest(tx_vintages):
    r = revision_analysis("TX", [2022, 2024], lags=(0, 30), today=TODAY)
    bt = {(b["election_year"], b["lag_days"]): b for b in r["backtest"]}
    assert bt[(2022, 0)]["predicted_final"] == round(200 / 0.75, 2) and bt[(2022, 0)]["error_pct"] == -33.3
    assert bt[(2024, 0)]["predicted_final"] == 600.0 and bt[(2024, 0)]["error_pct"] == 50.0
    assert r["backtest_summary"]["even_years"][0] == {"mean_abs_error_pct": round((33.3 + 50.0) / 2, 1), "n": 2}


def test_states_without_filing_dates_are_rejected():
    with pytest.raises(ValueError, match="no filing dates"):
        revision_analysis("MI", [2024])


def test_never_extrapolates_across_election_kinds(tx_vintages):
    # 2025 (odd) mature-looking reference must not be used for 2026 (even); with only odd references -> no nowcast
    from candidate_modeling import revisions
    r = revisions.revision_analysis("TX", [2022, 2026], lags=(0, 14), today=TODAY)
    assert r["nowcasts"][0]["nowcast_final"] is not None             # 2022 is even: comparable
    r2 = revisions.revision_analysis("TX", [2026], lags=(0, 14), today=TODAY)
    assert r2["nowcasts"][0]["nowcast_final"] is None and "same kind" in r2["nowcasts"][0]["reason"]


def test_known_curve_is_daily_cumulative(tx_vintages):
    from candidate_modeling.revisions import known_curve
    c = {p["day"]: p["share_known"] for p in known_curve("TX", 2022, start_day=-15, end_day=120, today=TODAY)}
    assert c[-15] == 0.0 and c[-10] == 0.5 and c[0] == 0.5 and c[20] == 0.75 and c[100] == 1.0
    assert min(c) == -15 and max(c) == 120
    assert all(c[d] <= c[d + 1] for d in range(-15, 120))           # never decreases
