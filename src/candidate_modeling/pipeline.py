"""High-level operations shared by the CLI and the MCP server."""
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
            out["states"][s] = states.REGISTRY[s].search(name)
        else:
            out["states"][s] = [{"error": f"No connector for {s} yet (planned: {states.PLANNED})"}]
    return out


def add_candidate(name: str, state: str | None = None, party: str | None = None, slug: str | None = None,
                  fec_candidate_ids: list[str] | None = None, state_ids: dict | None = None,
                  openstates_person_id: str | None = None, notes: str | None = None) -> dict:
    """Create or merge a profile. state_ids: {"TX": {"filer_ids": [...]}, "MI": {"committee_ids": [...]}}."""
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


def finance_summary(slug: str, jurisdiction: str | None = None, cycle: int | None = None, top_n: int = 10) -> dict:
    """Totals, geography, donor size mix, top employers/occupations, monthly trend. Memo rows excluded."""
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
            "candidate": p["name"], "home_state": home, "filters": {"jurisdiction": jurisdiction, "cycle": cycle},
            "by_source": q(f"""SELECT jurisdiction, source, count(*) AS rows, round(sum(amount), 2) AS total,
                                min(date) AS first_date, max(date) AS last_date
                             FROM contributions_clean WHERE {w} GROUP BY ALL ORDER BY total DESC"""),
        }
        # home-state params come before the WHERE params
        geo = con.execute(f"""SELECT round(100.0 * sum(amount) FILTER (WHERE state = ?) / nullif(sum(amount), 0), 1),
                                     round(100.0 * count(*) FILTER (WHERE state = ?) / nullif(count(*), 0), 1)
                              FROM contributions_clean WHERE {w}""", [home, home, *params]).fetchone()
        out["geography"] = {"pct_in_state_dollars": geo[0], "pct_in_state_count": geo[1],
                            "top_states": q(f"""SELECT state, count(*) AS n, round(sum(amount), 2) AS total
                                               FROM contributions_clean WHERE {w}
                                               GROUP BY state ORDER BY total DESC LIMIT {int(top_n)}""")}
        out["donor_types"] = q(f"""SELECT contributor_type, count(*) AS n, round(sum(amount), 2) AS total
                                  FROM contributions_clean WHERE {w} GROUP BY ALL ORDER BY total DESC""")
        out["individual_size_mix"] = q(f"""SELECT {SIZE_BUCKETS} AS bucket, count(*) AS n, round(sum(amount), 2) AS total
                                          FROM contributions_clean WHERE {w} AND contributor_type = 'individual'
                                          GROUP BY bucket ORDER BY bucket""")
        out["top_employers"] = q(f"""SELECT upper(employer) AS employer, count(*) AS n, round(sum(amount), 2) AS total
                                    FROM contributions_clean WHERE {w} AND employer IS NOT NULL
                                      AND upper(employer) NOT IN ('', 'N/A', 'NONE', 'NOT EMPLOYED', 'RETIRED',
                                          'SELF-EMPLOYED', 'SELF EMPLOYED', 'SELF', 'INFORMATION REQUESTED')
                                    GROUP BY 1 ORDER BY total DESC LIMIT {int(top_n)}""")
        out["top_occupations"] = q(f"""SELECT upper(occupation) AS occupation, count(*) AS n, round(sum(amount), 2) AS total
                                      FROM contributions_clean WHERE {w} AND occupation IS NOT NULL
                                      GROUP BY 1 ORDER BY total DESC LIMIT {int(top_n)}""")
        out["by_month"] = q(f"""SELECT strftime(date_trunc('month', date), '%Y-%m') AS month, count(*) AS n,
                                      round(sum(amount), 2) AS total
                               FROM contributions_clean WHERE {w} GROUP BY 1 ORDER BY 1""")
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
