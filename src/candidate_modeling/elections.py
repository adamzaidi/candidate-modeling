"""Election-over-election comparisons on transaction time (see scope rules: election-window, align-by-days).

A contribution belongs to election year Y if dated in (election_day(Y-1), election_day(Y)].
Each window is measured in days before its election day, so a 2026 race can be lined up against 2025's
(or 2022's) at the same point in the calendar.
"""
import datetime as _dt

import duckdb

from . import config, store
from .pipeline import records
from .sources import states

DEFAULT_CHECKPOINTS = (300, 180, 90, 30, 7, 0)  # windows are 364 or 371 days long
REPORTING_LAG_DAYS = 45  # post-election reports are typically due ~30 days after election day


def election_day(year: int) -> _dt.date:
    """Tuesday after the first Monday in November."""
    nov1 = _dt.date(year, 11, 1)
    first_monday = nov1 + _dt.timedelta(days=(0 - nov1.weekday()) % 7)
    return first_monday + _dt.timedelta(days=1)


def window(year: int) -> tuple[_dt.date, _dt.date]:
    """(exclusive start, inclusive end) of the election-year-Y window."""
    return election_day(year - 1), election_day(year)


def _coverage(year: int, covered: list[int]) -> dict:
    start, end = window(year)
    need = sorted({start.year, end.year})
    missing = [y for y in need if y not in covered]
    if not missing:
        return {"status": "complete"}
    if missing == [start.year] and end.year in covered:
        return {"status": "partial", "missing": f"{start + _dt.timedelta(days=1)} to {start.year}-12-31"}
    return {"status": "missing", "missing_years": missing}


def _metrics_sql(base: str, years: list[int], checkpoints, home_state_sql: str) -> str:
    parts = []
    for y in years:
        s, e = window(y)
        parts.append(f"SELECT *, {y} AS election_year, (DATE '{e}' - date) AS days_before FROM base "
                     f"WHERE date > DATE '{s}' AND date <= DATE '{e}'")
    cps = ", ".join(
        f"round(coalesce(sum(amount) FILTER (WHERE days_before >= {c}), 0), 2) AS raised_by_{c}d" for c in checkpoints)
    return f"""
        WITH base AS ({base}), w AS ({" UNION ALL ".join(parts)})
        SELECT election_year,
               count(*) AS rows,
               round(sum(amount), 2) AS total,
               round(sum(amount) FILTER (WHERE contributor_type = 'individual'), 2) AS individuals_total,
               round(sum(amount) FILTER (WHERE contributor_type = 'organization'), 2) AS organizations_total,
               round(sum(amount) FILTER (WHERE contributor_type = 'unitemized'), 2) AS unitemized_total,
               round(sum(amount) FILTER (WHERE contributor_type = 'individual' AND amount < 200), 2)
                   AS individual_under_200_total,
               count(DISTINCT (contributor_name, zip)) FILTER (WHERE contributor_type = 'individual')
                   AS approx_individual_donors,
               count(DISTINCT committee_id) AS recipients,
               round(100.0 * coalesce(sum(amount) FILTER (WHERE state = {home_state_sql}), 0)
                     / nullif(sum(amount) FILTER (WHERE state IS NOT NULL), 0), 1) AS pct_in_state_dollars,
               {cps},
               min(date) AS first_date, max(date) AS last_date
        FROM w GROUP BY election_year ORDER BY election_year"""


def _top_recipients(con, base: str, year: int, n: int = 5) -> list[dict]:
    s, e = window(year)
    return records(con.execute(f"""WITH base AS ({base})
        SELECT committee_id, any_value(committee_name) AS committee, round(sum(amount), 2) AS total
        FROM base WHERE date > DATE '{s}' AND date <= DATE '{e}'
        GROUP BY committee_id ORDER BY total DESC LIMIT {int(n)}"""))


def _top_donors(con, base: str, year: int, n: int = 5) -> dict:
    """Largest contributors in the window and how concentrated the money is (exposes self-funding)."""
    s, e = window(year)
    w = f"WITH base AS ({base}) SELECT * FROM base WHERE date > DATE '{s}' AND date <= DATE '{e}' AND contributor_name IS NOT NULL"
    top = records(con.execute(f"""WITH w AS ({w}) SELECT contributor_name, any_value(contributor_type) AS type,
            state, round(sum(amount), 2) AS total, count(*) AS gifts
        FROM w GROUP BY contributor_name, state ORDER BY total DESC LIMIT {int(n)}"""))
    shares = con.execute(f"""WITH w AS ({w}), d AS (SELECT sum(amount) AS t FROM w GROUP BY contributor_name, state),
            r AS (SELECT t, row_number() OVER (ORDER BY t DESC) AS k FROM d)
        SELECT round(100.0 * sum(t) FILTER (WHERE k = 1) / nullif(sum(t), 0), 1),
               round(100.0 * sum(t) FILTER (WHERE k <= 10) / nullif(sum(t), 0), 1) FROM r""").fetchone()
    return {"top_donors": top, "pct_from_top_donor": shares[0], "pct_from_top_10_donors": shares[1]}


def _deltas(rows: list[dict], checkpoints) -> list[dict]:
    out = []
    keys = ["total", "individuals_total", *[f"raised_by_{c}d" for c in checkpoints]]
    for a, b in zip(rows, rows[1:]):
        if a["coverage"]["status"] != "complete" or b["coverage"]["status"] != "complete":
            out.append({"from": a["election_year"], "to": b["election_year"],
                        "skipped": "at least one window is not fully covered (rule coverage-first)"})
            continue
        d = {"from": a["election_year"], "to": b["election_year"]}
        for k in keys:
            x, y = a.get(k), b.get(k)
            d[f"{k}_pct_change"] = round(100 * (y - x) / x, 1) if x and y is not None else None
        out.append(d)
    return out


def compare_elections(years: list[int], state: str | None = None, slugs: list[str] | None = None,
                      checkpoints=DEFAULT_CHECKPOINTS, top_n: int = 5, today: _dt.date | None = None) -> dict:
    """Compare money across election windows.

    state:  every committee in that state's bulk data (statewide), e.g. compare_elections([2025, 2026], state="PA")
    slugs:  profiled candidates from the local DB (all their sources), e.g. compare_elections([2022, 2026], slugs=[...])
    """
    if not years or bool(state) == bool(slugs):
        raise ValueError("Pass years and exactly one of state= or slugs=.")
    years = sorted(set(int(y) for y in years))
    today = today or _dt.date.today()
    notes = []
    if len({y % 2 for y in years}) > 1:
        notes.append("Odd- and even-year windows hold different contests (rule like-with-like); differences "
                     "reflect which offices were on the ballot, not just fundraising strength.")

    if state:
        m = states.get(state)
        if not m.is_synced():
            raise ValueError(f"{state.upper()} is not synced; run sync_state('{state.upper()}').")
        sql, params = m.normalized_sql(None, None)
        base = f"SELECT * FROM ({_inline(sql, params)}) s({', '.join(store.COLUMN_NAMES)})"
        covered = m.covered_years()
        cov = {y: _coverage(y, covered) for y in years}
        home = f"'{m.STATE}'"
        notes.append("Statewide totals count committee-to-committee transfers at each hop (rule "
                     "statewide-double-count); individuals_total is the cleaner comparison.")
        con = duckdb.connect()
        rows = records(con.execute(_metrics_sql(base, years, checkpoints, home)))
        tops = {y: _top_recipients(con, base, y, top_n) for y in years}
        donors = {y: _top_donors(con, base, y, top_n) for y in years}
        con.close()
        subject = {"state": m.STATE, "source": m.SOURCE}
    else:
        profiles = [config.load_profile(s) for s in slugs]
        homes = {p["slug"]: (p.get("state") or "").upper() for p in profiles}
        base = ("SELECT c.*, h.home FROM contributions_clean c JOIN (SELECT * FROM (VALUES "
                + ", ".join(f"('{s}', '{h}')" for s, h in homes.items()) + ") v(slug, home)) h ON h.slug = c.candidate_slug "
                f"WHERE c.candidate_slug IN ({', '.join(repr(s) for s in slugs)})")
        cov = {y: _slug_coverage(profiles, y) for y in years}
        with store.connect(read_only=True) as con:
            rows = records(con.execute(_metrics_sql(base, years, checkpoints, "home")))
            tops = {y: _top_recipients(con, base, y, top_n) for y in years}
            donors = {y: _top_donors(con, base, y, top_n) for y in years}
        subject = {"candidates": slugs}

    by_year = {r["election_year"]: r for r in rows}
    out_rows = []
    for y in years:
        r = by_year.get(y, {"election_year": y, "rows": 0, "total": None})
        e = election_day(y)
        r = {**r, "election_day": e.isoformat(), "window_start_exclusive": window(y)[0].isoformat(),
             "coverage": cov[y], "top_recipients": tops.get(y, []), **donors.get(y, {})}
        if e > today:
            left = (e - today).days
            r["in_progress"] = {"days_until_election": left,
                                "checkpoints_not_reached": [c for c in checkpoints if c < left]}
            for c in checkpoints:
                if c < left:
                    r[f"raised_by_{c}d"] = None
        if e <= today <= e + _dt.timedelta(days=REPORTING_LAG_DAYS) and r.get("last_date"):
            gap = (e - _dt.date.fromisoformat(r["last_date"])).days
            r["reporting_incomplete"] = {
                "last_contribution_date": r["last_date"], "days_without_data_before_election": gap,
                "note": "Final pre-election and post-election reports are not all filed yet (rule reporting-lag); "
                        "late checkpoints (7d, 0d) will rise as they arrive."}
            notes.append(f"{y}: data ends {r['last_date']}, {gap} days before election day (reporting lag).")
        out_rows.append(r)
    return {"subject": subject, "years": out_rows, "changes": _deltas(out_rows, checkpoints),
            "checkpoints_days_before": list(checkpoints), "notes": notes,
            "rules_applied": ["election-window", "align-by-days", "coverage-first", "reporting-lag", "self-funding",
                              "clean-view", "itemized-only"]}


def _slug_coverage(profiles: list[dict], year: int) -> dict:
    """Per-source coverage for profiled candidates: state sources by synced years; FEC by fetched cycles."""
    detail = {}
    for p in profiles:
        for st in p["ids"].get("state", {}):
            m = states.get(st)
            detail[f"{p['slug']}:{st}"] = _coverage(year, m.covered_years())
        if p["ids"].get("fec_candidate_ids"):
            cyc = year + year % 2
            with store.connect(read_only=True) as con:
                r = con.execute("SELECT bool_and(complete), count(*) FROM fetch_state WHERE candidate_slug = ? "
                                "AND source = 'fec' AND scope LIKE ?", [p["slug"], f"%:{cyc}"]).fetchone()
            detail[f"{p['slug']}:fec"] = ({"status": "complete"} if r[1] and r[0] else
                                          {"status": "partial" if r[1] else "missing",
                                           "note": f"FEC cycle {cyc} itemized pull {'incomplete' if r[1] else 'not fetched'}"})
    worst = "complete"
    for v in detail.values():
        if v["status"] == "missing":
            worst = "missing"
        elif v["status"] == "partial" and worst == "complete":
            worst = "partial"
    return {"status": worst, "sources": detail}


def _inline(sql: str, params: list) -> str:
    """Inline connector params so the SELECT can be embedded in a CTE (params are IDs/slugs/patterns we control)."""
    out, i = [], 0
    for ch in sql:
        if ch == "?":
            v = params[i]
            i += 1
            out.append("NULL" if v is None else "'" + str(v).replace("'", "''") + "'")
        else:
            out.append(ch)
    if i != len(params):
        raise ValueError(f"placeholder count mismatch: {i} in SQL, {len(params)} params")
    return "".join(out)
