"""FEC API: federal candidates, totals, and itemized receipts (Schedule A).

Docs: https://api.open.fec.gov/developers/
Rate limits: DEMO_KEY is tiny (~30 req/hr); a free api.data.gov key gets 1,000 req/hr.
Itemized pulls are resumable: the keyset cursor is saved in `fetch_state` after every page.
"""
import time

import requests

from ..config import USER_AGENT, key
from .. import store

BASE = "https://api.open.fec.gov/v1"
SOURCE = "fec"
_sleep = time.sleep  # patched in tests


class RateLimited(RuntimeError):
    pass


def _get(path: str, retries: int = 4, **params) -> dict:
    params = {k: v for k, v in params.items() if v is not None}
    params["api_key"] = key("FEC_API_KEY", "DEMO_KEY")
    for attempt in range(retries + 1):
        r = requests.get(f"{BASE}{path}", params=params, headers={"User-Agent": USER_AGENT}, timeout=60)
        if r.status_code == 429:
            if attempt == retries:
                raise RateLimited("FEC rate limit hit. Get a free key at https://api.data.gov/signup/ "
                                  "and set FEC_API_KEY; re-run to resume where it stopped.")
            _sleep(float(r.headers.get("Retry-After", 2 ** attempt * 5)))
            continue
        if r.status_code >= 500 and attempt < retries:
            _sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


# ---------- discovery ----------

def search_candidates(name: str, state: str | None = None, office: str | None = None) -> list[dict]:
    """office: H, S, or P."""
    res = _get("/candidates/search/", q=name, state=state, office=office, per_page=20)
    return [
        {
            "source": SOURCE,
            "candidate_id": c["candidate_id"],
            "name": c["name"],
            "office": c.get("office_full"),
            "state": c.get("state"),
            "district": c.get("district"),
            "party": c.get("party_full"),
            "election_years": c.get("election_years"),
        }
        for c in res["results"]
    ]


def committees(candidate_id: str) -> list[dict]:
    res = _get(f"/candidate/{candidate_id}/committees/", per_page=100)
    return [
        {"committee_id": c["committee_id"], "name": c["name"], "designation": c.get("designation"),
         "cycles": c.get("cycles")}
        for c in res["results"]
    ]


def authorized_committee_ids(candidate_id: str) -> list[str]:
    """Principal (P) and authorized (A) committees — the candidate's own money."""
    return [c["committee_id"] for c in committees(candidate_id) if c["designation"] in ("P", "A")]


# ---------- totals ----------

def fetch_totals(con, slug: str, candidate_id: str) -> int:
    res = _get(f"/candidate/{candidate_id}/totals/", per_page=100, election_full="false")
    rows = [t for t in res["results"] if t.get("cycle")]  # skip whole-election rollups (cycle is null)
    for t in rows:
        con.execute(
            "INSERT OR REPLACE INTO fec_totals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [slug, candidate_id, t.get("cycle"), t.get("receipts"), t.get("disbursements"),
             t.get("last_cash_on_hand_end_period"), t.get("individual_contributions"),
             t.get("individual_itemized_contributions"), t.get("individual_unitemized_contributions"),
             t.get("other_political_committee_contributions"), (t.get("coverage_end_date") or "")[:10] or None],
        )
    return len(rows)


# ---------- itemized receipts ----------

def normalize(r: dict, slug: str) -> dict:
    return {
        "candidate_slug": slug,
        "source": SOURCE,
        "jurisdiction": "federal",
        "committee_id": r.get("committee_id"),
        "committee_name": (r.get("committee") or {}).get("name"),
        "transaction_id": str(r["sub_id"]),  # sub_id is unique per row; transaction_id repeats across amendments
        "date": (r.get("contribution_receipt_date") or "")[:10] or None,
        "amount": r.get("contribution_receipt_amount"),
        "contributor_name": r.get("contributor_name"),
        "contributor_type": "individual" if r.get("entity_type") == "IND" else "organization",
        "city": r.get("contributor_city"),
        "state": r.get("contributor_state"),
        "zip": (r.get("contributor_zip") or "")[:5] or None,
        "employer": r.get("contributor_employer"),
        "occupation": r.get("contributor_occupation"),
        "is_memo": r.get("memo_code") == "X",
        "cycle": r.get("two_year_transaction_period"),
    }


def fetch_itemized(con, slug: str, committee_id: str, cycle: int, max_pages: int | None = None) -> dict:
    """Page through Schedule A for one committee+cycle, saving after every page. Resumable."""
    scope = f"{committee_id}:{cycle}"
    state = store.get_cursor(con, slug, SOURCE, scope)
    import json

    cursor = json.loads(state[0]) if state and state[0] else {}
    loaded = state[2] if state else 0
    if state and state[1]:
        cursor, loaded = {}, 0  # previous run finished; refresh from the top (upserts dedupe)

    pages = 0
    while True:
        res = _get("/schedules/schedule_a/", committee_id=committee_id, two_year_transaction_period=cycle,
                   per_page=100, sort="-contribution_receipt_date", **cursor)
        rows = [normalize(r, slug) for r in res["results"]]
        loaded += store.upsert_contributions(con, rows)
        last = (res.get("pagination") or {}).get("last_indexes")
        done = not rows or not last
        cursor = last or {}
        store.set_cursor(con, slug, SOURCE, scope, cursor, done, loaded)
        pages += 1
        if done:
            return {"scope": scope, "rows": loaded, "complete": True}
        if max_pages and pages >= max_pages:
            return {"scope": scope, "rows": loaded, "complete": False}


def fetch(con, profile: dict, cycles: list[int] | None = None, max_pages: int | None = None) -> dict:
    slug, ids = profile["slug"], profile["ids"]
    out = {"totals": 0, "itemized": []}
    for cid in ids["fec_candidate_ids"]:
        out["totals"] += fetch_totals(con, slug, cid)
        committee_ids = ids["fec_committee_ids"] or authorized_committee_ids(cid)
        cyc = cycles or sorted({r[0] for r in con.execute(
            "SELECT cycle FROM fec_totals WHERE candidate_id=?", [cid]).fetchall()})
        for com in committee_ids:
            for cy in cyc:
                out["itemized"].append(fetch_itemized(con, slug, com, cy, max_pages=max_pages))
    return out
