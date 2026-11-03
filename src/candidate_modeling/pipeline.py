"""High-level operations shared by the CLI and the MCP server."""
import datetime as _dt

from . import config, store
from .sources import fec, states


# ---------- discovery ----------

def find_candidate(name: str, state: str | None = None) -> dict:
    """Search every source for a person. Returns matches grouped by source, for a human or agent to pick IDs."""
    out: dict = {"query": name, "state": state, "fec": [], "states": {}}
    try:
        out["fec"] = fec.search_candidates(name, state=state)
        if not out["fec"] and " " in name:
            out["fec"] = fec.search_candidates(name.split()[-1], state=state)
    except Exception as e:  # network / rate limit shouldn't hide state results
        out["fec_error"] = str(e)
    targets = [state.upper()] if state else list(states.REGISTRY)
    for s in targets:
        if s in states.REGISTRY:
            m = states.REGISTRY[s]
            out["states"][s] = [{**r, "id": r.get(m.RESULT_ID)} if "error" not in r else r for r in m.search(name)]
        else:
            out["states"][s] = [{"error": f"No connector for {s} yet (planned: {states.PLANNED})"}]
    return out


def add_candidate(name: str, state: str | None = None, party: str | None = None, slug: str | None = None,
                  fec_candidate_ids: list[str] | None = None, state_ids: dict | None = None,
                  openstates_person_id: str | None = None, notes: str | None = None) -> dict:
    """Create or merge a profile. state_ids: {"TX": ["00081913"], "PA": ["20160016"]} (the `id` values from
    find_candidate), or the explicit form {"TX": {"filer_ids": [...]}}."""
    slug = slug or config.slugify(name)
    try:
        p = config.load_profile(slug)
    except FileNotFoundError:
        p = {"slug": slug, "name": name, "party": party, "state": state, "ids": {}, "notes": notes or ""}
        p["ids"] = {k: (type(v)() if isinstance(v, (list, dict)) else v) for k, v in config.EMPTY_IDS.items()}
    if party:
        p["party"] = party
    if state:
        p["state"] = state.upper()
    ids = p["ids"]
    for cid in fec_candidate_ids or []:
        if cid not in ids["fec_candidate_ids"]:
            ids["fec_candidate_ids"].append(cid)
    for st, d in (state_ids or {}).items():
        if isinstance(d, (list, tuple)):
            d = {states.get(st).ID_KEY: list(d)}
        cur = ids["state"].setdefault(st.upper(), {})
        for k, vals in d.items():
            cur[k] = sorted(set(cur.get(k, [])) | set(vals))
    if openstates_person_id:
        ids["openstates_person_id"] = openstates_person_id
    if notes:
        p["notes"] = notes
    path = config.save_profile(p)
    return {"profile": p, "path": str(path)}


# ---------- data ----------

def sync_state(state: str, **kw) -> dict:
    return states.get(state).sync(**kw)


def fetch_finances(slug: str, sources: list[str] | None = None, cycles: list[int] | None = None,
                   max_pages: int | None = None) -> dict:
    """Load everything we have IDs for into the local DB. sources: subset of ['fec', 'TX', 'MI', ...]."""
    p = config.load_profile(slug)
    want = {s if s == "fec" else s.upper() for s in (sources or [])}
    out = {"slug": slug}
    with store.connect() as con:
        if (not want or "fec" in want) and p["ids"]["fec_candidate_ids"]:
            try:
                out["fec"] = fec.fetch(con, p, cycles=cycles, max_pages=max_pages)
            except fec.RateLimited as e:
                out["fec"] = {"error": str(e), "resumable": True}
        for st in p["ids"].get("state", {}):
            if want and st not in want:
                continue
            out[st] = states.get(st).load(con, p)
    return out


# ---------- analysis ----------

def _parse_ts(value: str) -> _dt.datetime:
    try:
        ts = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError(f"Not an ISO date/time: {value!r} (e.g. 2026-10-01 or 2026-10-01T12:00)")
    if ts.tzinfo:
        ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return ts


def _source_table(as_of: str | None) -> str:
    """contributions_clean, or the as-of macro for a validated timestamp (safe to inline)."""
    if not as_of:
        return "contributions_clean"
    return f"contributions_as_of(TIMESTAMP '{_parse_ts(as_of).isoformat(sep=' ')}')"

def _plain(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def records(cur) -> list[dict]:
    """Cursor -> JSON-safe list of dicts (dates as ISO strings)."""
    cols = [d[0] for d in cur.description]
    return [{c: _plain(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


SIZE_BUCKETS = """CASE WHEN amount < 200 THEN 'a: under $200' WHEN amount < 1000 THEN 'b: $200-999'
                  WHEN amount < 5000 THEN 'c: $1,000-4,999' ELSE 'd: $5,000+' END"""


def finance_summary(slug: str, jurisdiction: str | None = None, cycle: int | None = None, top_n: int = 10,
                    as_of: str | None = None) -> dict:
    """Totals, geography, donor size mix, top employers/occupations, monthly trend. Memo rows excluded.
    as_of: ISO date/time; summarize the data as it stood at that moment (observation time)."""
    src = _source_table(as_of)
    p = config.load_profile(slug)
    home = (p.get("state") or "").upper()
    where, params = ["candidate_slug = ?"], [slug]
    if jurisdiction:
        where.append("jurisdiction = ?")
        params.append(jurisdiction if jurisdiction == "federal" else jurisdiction.upper())
    if cycle:
        where.append("cycle = ?")
        params.append(cycle)
    w = " AND ".join(where)

    with store.connect(read_only=True) as con:
        def q(sql, extra=()):
            return records(con.execute(sql, [*params, *extra]))

        out = {
            "candidate": p["name"], "home_state": home, "filters": {"jurisdiction": jurisdiction, "cycle": cycle, "as_of": as_of},
            "by_source": q(f"""SELECT jurisdiction, source, count(*) AS rows, round(sum(amount), 2) AS total,
                                min(date) AS first_date, max(date) AS last_date
                             FROM {src} WHERE {w} GROUP BY ALL ORDER BY total DESC"""),
        }
        # home-state params come before the WHERE params
        # only rows with a known donor state count (unitemized lump sums have none)
        geo = con.execute(f"""SELECT round(100.0 * sum(amount) FILTER (WHERE state = ?)
                                           / nullif(sum(amount) FILTER (WHERE state IS NOT NULL), 0), 1),
                                     round(100.0 * count(*) FILTER (WHERE state = ?)
                                           / nullif(count(*) FILTER (WHERE state IS NOT NULL), 0), 1)
                              FROM {src} WHERE {w}""", [home, home, *params]).fetchone()
        out["geography"] = {"pct_in_state_dollars": geo[0], "pct_in_state_count": geo[1],
                            "top_states": q(f"""SELECT state, count(*) AS n, round(sum(amount), 2) AS total
                                               FROM {src} WHERE {w}
                                               GROUP BY state ORDER BY total DESC LIMIT {int(top_n)}""")}
        out["donor_types"] = q(f"""SELECT contributor_type, count(*) AS n, round(sum(amount), 2) AS total
                                  FROM {src} WHERE {w} GROUP BY ALL ORDER BY total DESC""")
        out["individual_size_mix"] = q(f"""SELECT {SIZE_BUCKETS} AS bucket, count(*) AS n, round(sum(amount), 2) AS total
                                          FROM {src} WHERE {w} AND contributor_type = 'individual'
                                          GROUP BY bucket ORDER BY bucket""")
        out["top_employers"] = q(f"""SELECT upper(employer) AS employer, count(*) AS n, round(sum(amount), 2) AS total
                                    FROM {src} WHERE {w} AND employer IS NOT NULL
                                      AND upper(employer) NOT IN ('', 'N/A', 'NONE', 'NOT EMPLOYED', 'RETIRED',
                                          'SELF-EMPLOYED', 'SELF EMPLOYED', 'SELF', 'INFORMATION REQUESTED')
                                    GROUP BY 1 ORDER BY total DESC LIMIT {int(top_n)}""")
        out["top_occupations"] = q(f"""SELECT upper(occupation) AS occupation, count(*) AS n, round(sum(amount), 2) AS total
                                      FROM {src} WHERE {w} AND occupation IS NOT NULL
                                      GROUP BY 1 ORDER BY total DESC LIMIT {int(top_n)}""")
        out["by_month"] = q(f"""SELECT strftime(date_trunc('month', date), '%Y-%m') AS month, count(*) AS n,
                                      round(sum(amount), 2) AS total
                               FROM {src} WHERE {w} GROUP BY 1 ORDER BY 1""")
        if not jurisdiction or jurisdiction == "federal":
            tw = "candidate_slug = ?" + (" AND cycle = ?" if cycle else "")
            out["fec_totals"] = records(con.execute(
                f"""SELECT cycle, receipts, individual_contributions, individual_unitemized_contributions,
                           round(100.0 * individual_unitemized_contributions / nullif(individual_contributions, 0), 1)
                               AS pct_individual_unitemized, other_political_committee_contributions,
                           disbursements, cash_on_hand_end_period, coverage_end_date
                    FROM fec_totals WHERE {tw} ORDER BY cycle""", [slug, *([cycle] if cycle else [])]))
    out["caveats"] = CAVEATS
    return out


CAVEATS = [
    "FEC itemizes individuals only above $200/cycle; small-dollar money appears in fec_totals, not contributions.",
    "FEC memo rows (sub-itemizations) are excluded via contributions_clean to avoid double counting.",
    "State totals are itemized contributions only; loans and in-kind may be categorized differently per state.",
    "Michigan amendments may not be reflected; verify important figures against the filed statement.",
    "Employer/occupation are self-reported free text; not yet normalized to industries.",
    "History is observation time: as_of and change feeds reflect what the filings said when this database "
    "loaded them, starting from each candidate's first load, not the original filing dates.",
    "In-state share counts only contributions with a known donor state (unitemized lump sums excluded).",
    "Georgia coverage starts in 2025 (PeachFile system); Pennsylvania donor types use a name heuristic "
    "outside committee sections.",
]


def compare_candidates(slugs: list[str], jurisdiction: str | None = None, cycle: int | None = None) -> list[dict]:
    rows = []
    for s in slugs:
        sm = finance_summary(s, jurisdiction=jurisdiction, cycle=cycle, top_n=3)
        tot = sum(r["total"] or 0 for r in sm["by_source"])
        small = next((r for r in sm["individual_size_mix"] if r["bucket"].startswith("a")), {})
        ind_total = sum(r["total"] or 0 for r in sm["individual_size_mix"]) or None
        rows.append({
            "candidate": sm["candidate"], "itemized_total": round(tot, 2),
            "pct_in_state_dollars": sm["geography"]["pct_in_state_dollars"],
            "pct_itemized_individual_under_200": round(100 * (small.get("total") or 0) / ind_total, 1) if ind_total else None,
            "top_states": [r["state"] for r in sm["geography"]["top_states"]],
            "top_employers": [r["employer"] for r in sm["top_employers"]],
        })
    return rows


# ---------- history ----------

def contribution_changes(slug: str, since: str | None = None, until: str | None = None, sample: int = 10) -> dict:
    """What changed in a candidate's data between two observation times: rows added after the first load,
    rows amended (old vs new values), rows removed. Includes dollar impact and samples."""
    lo = _parse_ts(since) if since else _dt.datetime(1970, 1, 1)
    hi = _parse_ts(until) if until else _dt.datetime(9999, 1, 1)
    cols = "source, transaction_id, committee_name, date, contributor_name, state"
    with store.connect(read_only=True) as con:
        runs = records(con.execute("""SELECT source, min(run_at) AS first_load, max(run_at) AS last_load,
                count(*) AS loads FROM load_runs WHERE candidate_slug = ? GROUP BY source ORDER BY source""", [slug]))
        baseline = {r["source"]: r["first_load"] for r in runs}
        # 'added' = first seen after that source's baseline load (otherwise everything looks new on day one)
        added = con.execute(f"""
            SELECT c.source, count(*), round(sum(amount), 2) FROM contributions c
            JOIN (SELECT source, min(run_at) AS b FROM load_runs WHERE candidate_slug = ? GROUP BY source) r
              ON r.source = c.source
            WHERE candidate_slug = ? AND first_seen > r.b AND first_seen BETWEEN ? AND ?
              AND NOT coalesce(is_memo, false) GROUP BY 1""", [slug, slug, lo, hi]).fetchall()
        removed = con.execute("""SELECT source, count(*), round(sum(amount), 2) FROM contributions
            WHERE candidate_slug = ? AND removed_at BETWEEN ? AND ? AND NOT coalesce(is_memo, false) GROUP BY 1""",
                              [slug, lo, hi]).fetchall()
        # modified: old version from history joined to whichever version replaced it
        modified_sql = f"""
            WITH versions AS (
                SELECT source, transaction_id, candidate_slug, amount, valid_from FROM contributions
                UNION ALL SELECT source, transaction_id, candidate_slug, amount, valid_from FROM contribution_history)
            SELECT h.{cols.replace(', ', ', h.')}, h.amount AS old_amount, v.amount AS new_amount, h.valid_to AS changed_at
            FROM contribution_history h JOIN versions v
              ON v.source = h.source AND v.transaction_id = h.transaction_id AND v.candidate_slug = h.candidate_slug
             AND v.valid_from = h.valid_to
            WHERE h.candidate_slug = ? AND h.change = 'modified' AND h.valid_to BETWEEN ? AND ?
              AND NOT coalesce(h.is_memo, false)"""
        mod_rows = records(con.execute(f"{modified_sql} ORDER BY abs(v.amount - h.amount) DESC NULLS LAST",
                                       [slug, lo, hi]))
        samples = {
            "added": records(con.execute(f"""SELECT {cols}, amount, first_seen FROM contributions c
                WHERE candidate_slug = ? AND first_seen BETWEEN ? AND ? AND NOT coalesce(is_memo, false)
                  AND first_seen > (SELECT min(run_at) FROM load_runs r WHERE r.candidate_slug = c.candidate_slug
                                    AND r.source = c.source)
                ORDER BY amount DESC LIMIT {int(sample)}""", [slug, lo, hi])),
            "removed": records(con.execute(f"""SELECT {cols}, amount, removed_at FROM contributions
                WHERE candidate_slug = ? AND removed_at BETWEEN ? AND ? AND NOT coalesce(is_memo, false)
                ORDER BY amount DESC LIMIT {int(sample)}""", [slug, lo, hi])),
            "modified": mod_rows[:sample],
        }
    by_source: dict = {}
    for src, n, amt in added:
        by_source.setdefault(src, {})["added"] = {"rows": n, "amount": amt}
    for src, n, amt in removed:
        by_source.setdefault(src, {})["removed"] = {"rows": n, "amount": amt}
    for r in mod_rows:
        m = by_source.setdefault(r["source"], {}).setdefault("modified", {"rows": 0, "net_amount_change": 0.0})
        m["rows"] += 1
        m["net_amount_change"] = round(m["net_amount_change"] + (r["new_amount"] or 0) - (r["old_amount"] or 0), 2)
    return {"candidate_slug": slug, "window": {"since": since, "until": until}, "loads": runs,
            "baseline_load_per_source": baseline, "by_source": by_source, "samples": samples,
            "note": "Rows present in each source's first load are the baseline and are not counted as added."}


def fundraising_history(slug: str) -> list[dict]:
    """Every distinct FEC totals snapshot observed for the candidate (raising, spending, cash over time)."""
    with store.connect(read_only=True) as con:
        return records(con.execute("""SELECT fetched_at, candidate_id, cycle, coverage_end_date, receipts,
                individual_unitemized_contributions, disbursements, cash_on_hand_end_period
            FROM fec_totals_history WHERE candidate_slug = ? ORDER BY cycle, fetched_at""", [slug]))
