"""Election-over-election comparisons on transaction time.

Scope rules applied: election-window, stage-window, align-by-days, coverage-first, reporting-lag, office-*.

Windows (start exclusive, end inclusive) for election year Y in a state:
  general           (general[Y-1], general[Y]]                   default; works without a calendar
  primary           (general[Y-1], primary[Y]]                   money raised before the primary
  primary_runoff    (primary[Y], primary_runoff[Y]]
  general_campaign  (last of primary/primary_runoff in Y, general[Y]]
  general_runoff    (general[Y], general_runoff[Y]]
Primary and runoff dates come from reference/elections.yaml (every date sourced). General election day
falls back to the November rule when a state/year is not in the calendar.
"""
import datetime as _dt
from functools import lru_cache

import duckdb
import yaml

from . import config, labels, store
from .pipeline import records
from .sources import states

DEFAULT_CHECKPOINTS = (300, 180, 90, 30, 7, 0)
REPORTING_LAG_DAYS = 45  # post-election reports are typically due ~30 days after election day
STAGES = ("general", "primary", "primary_runoff", "general_campaign", "general_runoff")
CALENDAR_TYPES = {"primary", "primary_runoff", "general", "general_runoff", "presidential_primary", "special"}
CALENDAR_PATH = config.ROOT / "reference" / "elections.yaml"


# ---------- calendar ----------

def election_day(year: int) -> _dt.date:
    """Tuesday after the first Monday in November."""
    nov1 = _dt.date(year, 11, 1)
    first_monday = nov1 + _dt.timedelta(days=(0 - nov1.weekday()) % 7)
    return first_monday + _dt.timedelta(days=1)


@lru_cache(maxsize=1)
def calendar() -> tuple[dict, ...]:
    with open(CALENDAR_PATH) as f:
        rows = yaml.safe_load(f) or []
    out = []
    for r in rows:
        d = r["date"] if isinstance(r["date"], _dt.date) else _dt.date.fromisoformat(str(r["date"]))
        out.append({**r, "state": r["state"].upper(), "date": d, "status": r.get("status", "held")})
    return tuple(sorted(out, key=lambda r: (r["state"], r["date"])))


def calendar_summary() -> dict:
    s: dict = {}
    for r in calendar():
        s.setdefault(r["state"], {}).setdefault(r["date"].year, []).append(f"{r['type']} {r['date']}")
    return s


def _entry(state: str | None, year: int, etype: str) -> dict | None:
    if not state:
        return None
    hits = [r for r in calendar() if r["state"] == state.upper() and r["date"].year == year and r["type"] == etype]
    return hits[0] if hits else None


def _general(state: str | None, year: int) -> tuple[_dt.date, str]:
    e = _entry(state, year, "general")
    return (e["date"], e["source"]) if e else (election_day(year), "rule: Tuesday after first Monday in November")


def stage_window(year: int, stage: str = "general", state: str | None = None) -> dict | None:
    """Window for one election stage, or None if that stage has no calendar entry for the state/year."""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    if stage != "general" and not state:
        raise ValueError(f"stage={stage!r} needs a state (primary dates differ by state).")
    prev_gen, prev_src = _general(state, year - 1)
    gen, gen_src = _general(state, year)
    pri, run = _entry(state, year, "primary"), _entry(state, year, "primary_runoff")
    if stage == "general":
        start, end, src = prev_gen, gen, gen_src
    elif stage == "primary":
        if not pri:
            return None
        start, end, src = prev_gen, pri["date"], pri["source"]
    elif stage == "primary_runoff":
        if not (pri and run):
            return None
        start, end, src = pri["date"], run["date"], run["source"]
    elif stage == "general_campaign":
        last = run or pri
        if not last:
            return None
        start, end, src = last["date"], gen, gen_src
    else:  # general_runoff
        gr = _entry(state, year, "general_runoff")
        if not gr:
            return None
        start, end, src = gen, gr["date"], gr["source"]
    return {"stage": stage, "start_exclusive": start, "end": end, "days": (end - start).days, "end_source": src}


def window(year: int) -> tuple[_dt.date, _dt.date]:
    """General-election window by the November rule (kept for callers that don't need a state)."""
    w = stage_window(year)
    return w["start_exclusive"], w["end"]


def _coverage_span(start: _dt.date, end: _dt.date, covered: list[int]) -> dict:
    need = list(range((start + _dt.timedelta(days=1)).year, end.year + 1))
    missing = [y for y in need if y not in covered]
    if not missing:
        return {"status": "complete"}
    if missing[0] == need[0] and len(missing) == 1 and len(need) > 1:
        return {"status": "partial", "missing": f"{start + _dt.timedelta(days=1)} to {need[0]}-12-31"}
    return {"status": "missing", "missing_years": missing}


def _coverage(year: int, covered: list[int]) -> dict:
    s, e = window(year)
    return _coverage_span(s, e, covered)


# ---------- SQL ----------

def _windows_sql(windows: dict[int, dict]) -> str:
    parts = [f"SELECT *, {y} AS election_year, (DATE '{w['end']}' - date) AS days_before FROM base "
             f"WHERE date > DATE '{w['start_exclusive']}' AND date <= DATE '{w['end']}'"
             for y, w in windows.items()]
    return " UNION ALL ".join(parts) if parts else "SELECT *, NULL::INTEGER AS election_year, 0 AS days_before FROM base WHERE false"


def _metrics_sql(base: str, windows: dict[int, dict], checkpoints, home_state_sql: str, group: str | None = None) -> str:
    cps = ", ".join(
        f"round(coalesce(sum(amount) FILTER (WHERE days_before >= {c}), 0), 2) AS raised_by_{c}d" for c in checkpoints)
    g_sel, g_by = (f"{group} AS grp, ", ", grp") if group else ("", "")
    g_by_l = f", {group} AS grp" if group else ""
    return f"""
        WITH base AS ({base}), w_all AS ({_windows_sql(windows)}),
             w AS (SELECT * FROM w_all WHERE {_COUNTED}),
             loans AS (SELECT election_year{g_by_l},
                              round(sum(amount), 2) AS loans_total,
                              round(coalesce(sum(amount) FILTER (WHERE self_match(contributor_name, cand_last, cand_first)), 0), 2)
                                  AS self_loans_total
                       FROM w_all WHERE contribution_kind = 'loan' GROUP BY ALL),
             pub AS (SELECT election_year{g_by_l}, round(sum(amount), 2) AS public_funds_total
                     FROM w_all WHERE contribution_kind = 'public_funds' GROUP BY ALL),
             m AS (
        SELECT election_year, {g_sel}
               count(*) AS rows,
               round(sum(amount), 2) AS total,
               round(sum(amount) FILTER (WHERE contributor_type = 'individual'), 2) AS individuals_total,
               round(sum(amount) FILTER (WHERE contributor_type = 'organization'), 2) AS organizations_total,
               round(sum(amount) FILTER (WHERE contributor_type = 'unitemized'), 2) AS unitemized_total,
               round(sum(amount) FILTER (WHERE contributor_type = 'individual' AND amount < 200), 2)
                   AS individual_under_200_total,
               round(coalesce(sum(amount) FILTER (WHERE contributor_type = 'individual'
                     AND self_match(contributor_name, cand_last, cand_first)), 0), 2) AS self_funding_total,
               round(100.0 * coalesce(sum(amount) FILTER (WHERE contributor_type = 'individual'
                     AND self_match(contributor_name, cand_last, cand_first)), 0) / nullif(sum(amount), 0), 1)
                   AS pct_self_funded,
               round(coalesce(sum(amount) FILTER (WHERE contribution_kind = 'in_kind'), 0), 2) AS in_kind_total,
               round(coalesce(sum(amount) FILTER (WHERE contribution_kind = 'return'), 0), 2) AS returns_total,
               round(coalesce(sum(amount) FILTER (WHERE is_conduit), 0), 2) AS conduit_recipient_total,
               round(sum(amount) FILTER (WHERE NOT is_conduit), 2) AS total_excluding_conduits,
               round(coalesce(sum(amount) FILTER (WHERE {labels.conduit_sql("contributor_name")}), 0), 2)
                   AS from_conduit_total,
               count(DISTINCT (contributor_name, zip)) FILTER (WHERE contributor_type = 'individual')
                   AS approx_individual_donors,
               count(DISTINCT committee_id) AS recipients,
               round(100.0 * coalesce(sum(amount) FILTER (WHERE state = {home_state_sql}), 0)
                     / nullif(sum(amount) FILTER (WHERE state IS NOT NULL), 0), 1) AS pct_in_state_dollars,
               {cps},
               min(date) AS first_date, max(date) AS last_date
        FROM w GROUP BY election_year{g_by})
        SELECT m.*, coalesce(loans.loans_total, 0) AS loans_total, coalesce(loans.self_loans_total, 0) AS self_loans_total,
               coalesce(pub.public_funds_total, 0) AS public_funds_total
        FROM m LEFT JOIN loans USING (election_year{g_by}) LEFT JOIN pub USING (election_year{g_by})
        ORDER BY election_year{g_by}"""


_COUNTED = labels.COUNTED_SQL  # rule loans-excluded


def _in_window(base: str, w: dict) -> str:
    return (f"WITH base AS ({base}) SELECT * FROM base "
            f"WHERE date > DATE '{w['start_exclusive']}' AND date <= DATE '{w['end']}' AND {_COUNTED}")


def _top_recipients(con, base: str, w: dict, n: int = 5) -> list[dict]:
    extra = ", any_value(office_category) AS office" if "office_category" in base else ""
    return records(con.execute(f"""WITH w AS ({_in_window(base, w)})
        SELECT committee_id, any_value(committee_name) AS committee{extra}, round(sum(amount), 2) AS total
        FROM w GROUP BY committee_id ORDER BY total DESC LIMIT {int(n)}"""))


def _top_donors(con, base: str, w: dict, n: int = 5) -> dict:
    """Largest contributors in the window and how concentrated the money is (rule self-funding)."""
    q = f"WITH w0 AS ({_in_window(base, w)}) SELECT * FROM w0 WHERE contributor_name IS NOT NULL"
    top = records(con.execute(f"""WITH w AS ({q}) SELECT contributor_name, any_value(contributor_type) AS type,
            state, round(sum(amount), 2) AS total, count(*) AS gifts
        FROM w GROUP BY contributor_name, state ORDER BY total DESC LIMIT {int(n)}"""))
    shares = con.execute(f"""WITH w AS ({q}), d AS (SELECT sum(amount) AS t FROM w GROUP BY contributor_name, state),
            r AS (SELECT t, row_number() OVER (ORDER BY t DESC) AS k FROM d)
        SELECT round(100.0 * sum(t) FILTER (WHERE k = 1) / nullif(sum(t), 0), 1),
               round(100.0 * sum(t) FILTER (WHERE k <= 10) / nullif(sum(t), 0), 1) FROM r""").fetchone()
    return {"top_donors": top, "pct_from_top_donor": shares[0], "pct_from_top_10_donors": shares[1]}


def _pct(x, y):
    return round(100 * (y - x) / x, 1) if x and y is not None else None


def _deltas(rows: list[dict], checkpoints) -> list[dict]:
    out = []
    keys = ["total", "individuals_total", "total_excluding_conduits", *[f"raised_by_{c}d" for c in checkpoints]]
    rows = [r for r in rows if r.get("window")]
    for a, b in zip(rows, rows[1:]):
        if a["coverage"]["status"] != "complete" or b["coverage"]["status"] != "complete":
            out.append({"from": a["election_year"], "to": b["election_year"],
                        "skipped": "at least one window is not fully covered (rule coverage-first)"})
            continue
        d = {"from": a["election_year"], "to": b["election_year"],
             **{f"{k}_pct_change": _pct(a.get(k), b.get(k)) for k in keys}}
        if a.get("bea") and b.get("bea"):
            for k in ("real_total", "real_individuals_total", "real_per_capita_total", "per_100k_personal_income"):
                d[f"{k}_pct_change"] = _pct(a["bea"].get(k), b["bea"].get(k))
        out.append(d)
    return out


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


def _in_list(values) -> str:
    vals = [values] if isinstance(values, str) else list(values)
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in vals)


# ---------- main entry ----------

def compare_elections(years: list[int], state: str | None = None, slugs: list[str] | None = None,
                      stage: str = "general", group_by: str | None = None, office=None, kind=None, party=None,
                      checkpoints=DEFAULT_CHECKPOINTS, top_n: int = 5, today: _dt.date | None = None) -> dict:
    """Compare money across election windows.

    state=   every committee in that state's bulk data, e.g. compare_elections([2022, 2026], state="TX")
    slugs=   profiled candidates from the local DB (all their sources)
    stage=   general | primary | primary_runoff | general_campaign | general_runoff (non-general needs a state
             or candidates who share one home state)
    group_by=office | kind | party   (statewide only) breakdown by office, committee type, or recipient party
    office=, kind=, party=   (statewide only) filter to categories, e.g. office="governor", party="democratic"
    """
    if not years or bool(state) == bool(slugs):
        raise ValueError("Pass years and exactly one of state= or slugs=.")
    if slugs and (group_by or office or kind or party):
        raise ValueError("group_by/office/kind/party need state= (labels come from the state's committee registry).")
    if group_by not in (None, "office", "kind", "party"):
        raise ValueError("group_by must be 'office', 'kind', or 'party'.")
    years = sorted(set(int(y) for y in years))
    today = today or _dt.date.today()
    notes: list[str] = []
    if stage == "general" and len({y % 2 for y in years}) > 1:
        notes.append("Odd- and even-year windows hold different contests (rule like-with-like); differences "
                     "reflect which offices were on the ballot, not just fundraising strength.")

    if state:
        m = states.get(state)
        if not m.is_synced():
            raise ValueError(f"{state.upper()} is not synced; run sync_state('{state.upper()}').")
        cal_state, covered, home = m.STATE, m.covered_years(), f"'{m.STATE}'"
        sql, params = m.normalized_sql(None, None)
        base = (f"SELECT b.*, coalesce(c.kind, 'other_committee') AS kind, "
                f"coalesce(c.office_category, 'unknown') AS office_category, c.office_raw, "
                f"coalesce(c.party, 'unknown') AS party, c.cand_last, c.cand_first, coalesce(c.is_conduit, false) AS is_conduit "
                f"FROM ({_inline(sql, params)}) b({', '.join(store.COLUMN_NAMES)}) "
                f"LEFT JOIN ({m.committees_sql()}) c USING (committee_id)")
        filters = []
        if office:
            filters.append(f"office_category IN ({_in_list(office)})")
        if kind:
            filters.append(f"kind IN ({_in_list(kind)})")
        if party:
            filters.append(f"party IN ({_in_list(party)})")
        if filters:
            base = f"SELECT * FROM ({base}) f WHERE {' AND '.join(filters)}"
        notes.append("Statewide totals count committee-to-committee transfers at each hop (rule "
                     "statewide-double-count); individuals_total and total_excluding_conduits are cleaner comparisons.")
        if office or group_by == "office":
            notes.append("Office = each committee's latest office on file (rule office-snapshot).")
        subject = {"state": m.STATE, "source": m.SOURCE, "office": office, "kind": kind, "party": party}
        if party or group_by == "party":
            notes.append("Party = the recipient committee's party on file (rule party-labels)"
                         + ("; Texas files carry no party, so everything is 'unknown'." if m.STATE == "TX" else "."))
        cov_fn = lambda w: _coverage_span(w["start_exclusive"], w["end"], covered)  # noqa: E731
        con, close = duckdb.connect(), True
        labels.install_macros(con)
    else:
        profiles = [config.load_profile(s) for s in slugs]
        homes = {p["slug"]: (p.get("state") or "").upper() for p in profiles}
        meta = {p["slug"]: (*labels.first_last(p.get("name")), labels.normalize_party(p.get("party"))) for p in profiles}
        if stage != "general" and len(set(homes.values())) != 1:
            raise ValueError("Non-general stages need candidates who share one home state.")
        cal_state = next(iter(homes.values())) or None
        q = lambda v: "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"  # noqa: E731
        base = ("SELECT c.*, h.home, h.cand_last, h.cand_first, h.party, false AS is_conduit FROM "
                "(SELECT * FROM contributions WHERE removed_at IS NULL AND NOT coalesce(is_memo, false)) c "
                "JOIN (SELECT * FROM (VALUES "
                + ", ".join(f"({q(s)}, {q(h)}, {q(meta[s][1])}, {q(meta[s][0])}, {q(meta[s][2])})" for s, h in homes.items())
                + ") v(slug, home, cand_last, cand_first, party)) h ON h.slug = c.candidate_slug "
                f"WHERE c.candidate_slug IN ({', '.join(q(s) for s in slugs)})")
        home = "home"
        subject = {"candidates": slugs}
        cov_fn = lambda w: _slug_coverage(profiles, w)  # noqa: E731
        ctx = store.connect(read_only=True)
        con, close = ctx.__enter__(), False

    try:
        windows = {y: w for y in years if (w := stage_window(y, stage, cal_state if stage != "general" else cal_state))}
        missing_stage = [y for y in years if y not in windows]
        for y in missing_stage:
            notes.append(f"{y}: no {stage} in the calendar for {cal_state} (reference/elections.yaml).")
        rows = records(con.execute(_metrics_sql(base, windows, checkpoints, home)))
        by_year = {r["election_year"]: r for r in rows}
        out_rows = []
        for y in years:
            w = windows.get(y)
            if not w:
                out_rows.append({"election_year": y, "window": None, "coverage": {"status": "no_election"}})
                continue
            r = by_year.get(y, {"election_year": y, "rows": 0, "total": None})
            r = {**r, "window": {k: (v.isoformat() if isinstance(v, _dt.date) else v) for k, v in w.items()},
                 "election_day": w["end"].isoformat(), "coverage": cov_fn(w),
                 "top_recipients": _top_recipients(con, base, w, top_n), **_top_donors(con, base, w, top_n)}
            for c in checkpoints:
                if c >= w["days"]:
                    r[f"raised_by_{c}d"] = None  # checkpoint falls before this window starts
            e = w["end"]
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
                            "late checkpoints will rise as they arrive."}
                notes.append(f"{y}: data ends {r['last_date']}, {gap} days before the {stage} (reporting lag).")
            _add_bea(r, cal_state if state else (next(iter(homes.values())) if len(set(homes.values())) == 1 else None), y)
            out_rows.append(r)

        result = {"subject": subject, "stage": stage, "years": out_rows, "changes": _deltas(out_rows, checkpoints),
                  "checkpoints_days_before": list(checkpoints), "notes": notes,
                  "rules_applied": ["election-window", "stage-window", "align-by-days", "coverage-first",
                                    "reporting-lag", "self-funding", "contribution-kind", "conduits", "clean-view",
                                    "bea-normalization",
                                    "itemized-only"]}

        if group_by and windows:
            col = {"office": "office_category", "kind": "kind", "party": "party"}[group_by]
            g = records(con.execute(_metrics_sql(base, windows, checkpoints, home, group=col)))
            cov_ok = {r["election_year"]: r["coverage"]["status"] == "complete" for r in out_rows if r.get("window")}
            groups: dict = {}
            for r in g:
                groups.setdefault(r["grp"], {})[r["election_year"]] = {
                    k: r[k] for k in ("total", "individuals_total", "self_funding_total", "total_excluding_conduits",
                                      "loans_total",
                                      "rows", "recipients", "pct_in_state_dollars",
                                      *[f"raised_by_{c}d" for c in checkpoints])}
            ys = sorted(windows)
            breakdown = []
            for grp, per in groups.items():
                item = {group_by: grp, "years": per}
                ch = []
                for a, b in zip(ys, ys[1:]):
                    if cov_ok.get(a) and cov_ok.get(b) and a in per and b in per:
                        ch.append({"from": a, "to": b, "total_pct_change": _pct(per[a]["total"], per[b]["total"]),
                                   "individuals_total_pct_change": _pct(per[a]["individuals_total"],
                                                                        per[b]["individuals_total"])})
                item["changes"] = ch
                breakdown.append(item)
            last = ys[-1]
            breakdown.sort(key=lambda i: -(i["years"].get(last, {}).get("total") or 0))
            result["by_" + group_by] = breakdown
            if group_by == "office":
                for r in out_rows:
                    y = r["election_year"]
                    unk = (groups.get("unknown", {}).get(y) or {}).get("total") or 0
                    if r.get("total"):
                        r["pct_unclassified_office"] = round(100 * unk / r["total"], 1)
        return result
    finally:
        if close:
            con.close()
        else:
            ctx.__exit__(None, None, None)


REAL_KEYS = ("total", "individuals_total", "total_excluding_conduits")


def _add_bea(r: dict, state: str | None, year: int) -> None:
    """Real dollars (PCE price index), per capita, and share of state personal income (rule bea-normalization)."""
    from .sources import bea
    if not state or not r.get("total"):
        return
    ctx = bea.context(state, year)
    if not ctx:
        return
    real = {f"real_{k}": round(r[k] * ctx["deflator"], 2) for k in REAL_KEYS if r.get(k) is not None}
    r["bea"] = {
        **real,
        f"real_dollars_base_year": ctx["pce_base_year"],
        "per_capita_total": round(r["total"] / ctx["population"], 4) if ctx["population"] else None,
        "real_per_capita_total": round(r["total"] * ctx["deflator"] / ctx["population"], 4) if ctx["population"] else None,
        "per_capita_individuals": (round(r["individuals_total"] / ctx["population"], 4)
                                   if ctx["population"] and r.get("individuals_total") else None),
        "per_100k_personal_income": round(100_000 * r["total"] / (ctx["personal_income_musd"] * 1e6), 4)
                                    if ctx["personal_income_musd"] else None,
        "income_year": ctx["income_year"], "income_year_is_latest_available": ctx["income_year_is_latest_available"],
        "pce_basis": ctx["pce_basis"], "vintage": {k: v for k, v in bea.vintage().items() if k.endswith("vintage")},
    }


def _slug_coverage(profiles: list[dict], w: dict) -> dict:
    """Per-source coverage for profiled candidates: state sources by synced years; FEC by fetched cycles."""
    detail = {}
    for p in profiles:
        for st in p["ids"].get("state", {}):
            m = states.get(st)
            detail[f"{p['slug']}:{st}"] = _coverage_span(w["start_exclusive"], w["end"], m.covered_years())
        if p["ids"].get("fec_candidate_ids"):
            y = w["end"].year
            cyc = y + y % 2
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
