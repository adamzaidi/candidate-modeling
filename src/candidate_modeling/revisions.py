"""Revision analysis and nowcasting of election-window totals from filing dates (rule vintage-reconstruction).

Like BEA's advance -> second -> third estimates: the total for an election window keeps rising after election day
as late and amended reports arrive. Rows carry filed_date (when the report became public: TX received date, PA
submitted date, GA filed date), so the total "as known on date D" can be rebuilt for any past date:

    known(L) = sum(amount) over rows in the window with filed_date <= election_day + L

For mature windows (election at least SETTLE_DAYS ago) this gives the share of the final total known at each lag,
and the revision from each vintage to the final. For a window still settling, the nowcast is

    nowcast = known(now) / mean(share known at the same lag in past mature elections)

with a range from the past elections' min and max shares, and a backtest that predicts each mature election from
the others at the same lag.
"""
import datetime as _dt

import duckdb

from . import labels, store
from .elections import _inline, stage_window
from .pipeline import records
from .sources import states

DEFAULT_LAGS = (-30, -7, 0, 7, 14, 30, 60, 90, 180)
SETTLE_DAYS = 180
FILED_SOURCES = {"TX", "PA", "GA"}  # connectors that carry filed_date


def _base(m) -> str:
    sql, params = m.normalized_sql(None, None)
    return (f"SELECT b.*, coalesce(c.kind, 'other_committee') AS kind FROM ({_inline(sql, params)}) "
            f"b({', '.join(store.COLUMN_NAMES)}) LEFT JOIN ({m.committees_sql()}) c USING (committee_id) "
            f"WHERE {labels.COUNTED_SQL}")


def _profile(con, base: str, w: dict, lags, today: _dt.date, group: str | None) -> list[dict]:
    e = w["end"]
    cols = []
    for lag in lags:
        cut = e + _dt.timedelta(days=lag)
        if cut <= today:
            cols.append(f"round(sum(amount) FILTER (WHERE filed_date <= DATE '{cut}'), 2) AS known_{_lag_key(lag)}")
        else:
            cols.append(f"NULL::DOUBLE AS known_{_lag_key(lag)}")
    g_sel, g_by = (f"{group} AS grp, ", "GROUP BY grp ORDER BY grp") if group else ("", "")
    return records(con.execute(f"""
        WITH base AS ({base})
        SELECT {g_sel} round(sum(amount), 2) AS total_now,
               round(100.0 * sum(amount) FILTER (WHERE filed_date IS NOT NULL) / nullif(sum(amount), 0), 1)
                   AS pct_with_filed_date,
               round(sum(amount) FILTER (WHERE filed_date <= DATE '{today}'), 2) AS known_today,
               {", ".join(cols)}
        FROM base WHERE date > DATE '{w['start_exclusive']}' AND date <= DATE '{e}' {g_by}"""))


def _lag_key(lag: int) -> str:
    return f"m{-lag}" if lag < 0 else f"p{lag}"


def _shares(row: dict, lags) -> dict:
    t = row.get("total_now")
    return {lag: (round(row[f"known_{_lag_key(lag)}"] / t, 4) if t and row.get(f"known_{_lag_key(lag)}") is not None
                  else None) for lag in lags}


def revision_analysis(state: str, years: list[int], stage: str = "general", lags=DEFAULT_LAGS,
                      group_by: str | None = None, today: _dt.date | None = None) -> dict:
    """Vintage shares and revisions for mature windows; nowcast + backtest for windows still settling.
    group_by='kind' splits candidates / PACs / parties."""
    st = state.upper()
    if st not in FILED_SOURCES:
        raise ValueError(f"{st} has no filing dates in its data; revision analysis supports {sorted(FILED_SOURCES)}.")
    m = states.get(st)
    if not m.is_synced():
        raise ValueError(f"{st} is not synced; run sync_state('{st}').")
    today = today or _dt.date.today()
    lags = sorted(set(lags))
    base = _base(m)
    con = duckdb.connect()
    labels.install_macros(con)
    out_years, mature, settling = [], {}, {}
    try:
        for y in sorted(set(years)):
            w = stage_window(y, stage, st)
            if not w:
                out_years.append({"election_year": y, "status": "no_election"})
                continue
            days_since = (today - w["end"]).days
            rows = _profile(con, base, w, lags, today, "kind" if group_by == "kind" else None)
            status = "mature" if days_since >= SETTLE_DAYS else ("settling" if days_since >= 0 else "upcoming")
            entry = {"election_year": y, "election_day": w["end"].isoformat(), "days_since_election": days_since,
                     "status": status, "rows": []}
            for r in rows:
                sh = _shares(r, lags)
                rec = {**({"kind": r["grp"]} if group_by == "kind" else {}), "total_now": r["total_now"],
                       "known_today": r["known_today"], "pct_with_filed_date": r["pct_with_filed_date"],
                       "known_by_lag": {lag: r[f"known_{_lag_key(lag)}"] for lag in lags}}
                if status == "mature":
                    rec["share_known_by_lag"] = sh
                    rec["revision_to_final_pct"] = {
                        lag: (round(100 * (r["total_now"] - r[f"known_{_lag_key(lag)}"]) / r[f"known_{_lag_key(lag)}"], 1)
                              if r.get(f"known_{_lag_key(lag)}") else None) for lag in lags}
                entry["rows"].append(rec)
                key = r.get("grp") if group_by == "kind" else "all"
                (mature if status == "mature" else settling).setdefault(key, {})[y] = (rec, sh, days_since)
            out_years.append(entry)
    finally:
        con.close()

    nowcasts, backtests = [], []
    for key, targets in settling.items():
        for y, (rec, _, days_since) in targets.items():
            # like-with-like: only past elections of the same kind (even vs odd year) are comparable references
            hist = {hy: h for hy, h in mature.get(key, {}).items() if hy % 2 == y % 2}
            lag = max((l for l in lags if l <= days_since), default=None)
            past = [h[1].get(lag) for h in hist.values() if lag is not None and h[1].get(lag)]
            if not past or not rec["known_today"]:
                nowcasts.append({"election_year": y, **({"kind": key} if group_by else {}), "nowcast_final": None,
                                 "reason": "no mature election of the same kind (even/odd year) with a share at this "
                                           "lag; refusing to extrapolate from a different kind of election"})
                continue
            mean = sum(past) / len(past)
            nowcasts.append({"election_year": y, **({"kind": key} if group_by else {}), "lag_days": lag,
                             "known_today": rec["known_today"], "reference_years": sorted(hist),
                             "mean_share_known_at_lag": round(mean, 4),
                             "nowcast_final": round(rec["known_today"] / mean, 2),
                             "range": [round(rec["known_today"] / max(past), 2), round(rec["known_today"] / min(past), 2)],
                             "implied_still_to_come_pct": round(100 * (1 / mean - 1), 1)})
    for key, hist in mature.items():  # leave-one-out within the same kind (even/odd) of election
        for y, (rec, sh, _) in hist.items():
            others = {oy: o for oy, o in hist.items() if oy != y and oy % 2 == y % 2}
            for lag in lags:
                past = [o[1].get(lag) for o in others.values() if o[1].get(lag)]
                known = rec["known_by_lag"].get(lag)
                if past and known and rec["total_now"]:
                    pred = known / (sum(past) / len(past))
                    backtests.append({"election_year": y, **({"kind": key} if group_by else {}), "lag_days": lag,
                                      "predicted_final": round(pred, 2), "actual_final": rec["total_now"],
                                      "error_pct": round(100 * (pred - rec["total_now"]) / rec["total_now"], 1)})
    summary: dict = {}
    for kind_of_year, parity in (("even_years", 0), ("odd_years", 1)):  # never blend the two
        for lag in lags:
            errs = [abs(b["error_pct"]) for b in backtests if b["lag_days"] == lag and b["election_year"] % 2 == parity]
            if errs:
                summary.setdefault(kind_of_year, {})[lag] = {"mean_abs_error_pct": round(sum(errs) / len(errs), 1),
                                                             "n": len(errs)}
    return {"state": st, "stage": stage, "lags_days_after_election": lags, "settle_days": SETTLE_DAYS,
            "years": out_years, "nowcasts": nowcasts, "backtest": backtests, "backtest_summary": summary,
            "notes": [
                "Vintages are rebuilt from report filing dates (rule vintage-reconstruction); rows without a filing "
                "date are excluded from vintages but included in total_now (see pct_with_filed_date).",
                f"'Final' = total as known today; a window counts as mature {SETTLE_DAYS} days after the election.",
                "Amended reports carry their amendment date, so amendments show up as late revisions.",
                "Nowcasts and backtests only use past elections of the same kind (even vs odd year): reporting "
                "patterns differ sharply between them (rule like-with-like).",
                "With one or two reference elections, the range is a min-max of those shares, not a statistical "
                "confidence interval.",
            ],
            "rules_applied": ["vintage-reconstruction", "election-window", "stage-window", "loans-excluded"]}
