"""Wisconsin Ethics Commission campaign finance via Sunshine (campaignfinance.wi.gov), which replaced CFIS.

The public site is a Next.js app backed by tRPC. Statewide contributions come from
  GET /api/trpc/publicFrontendApi.getTransactions?input={"json": {...}}
      {dateFrom, dateTo, transactionType: [1], take <= 1000, cursor, sortBy: "date", sortDirection: "asc"}
      cursor paging via nextCursor; ~1M contributions per election window, 1000 rows per ~0.5 s
  GET /api/trpc/publicFrontendApi.getTransactionsTotalCount   same filters, used to skip unchanged years
History was migrated from CFIS back to mid-2008.

Each row carries the recipient (createdByEntity: committee type, party), the donor (from_entity), and a
transactionCategory whose flags (isInKind, isCommercialLoan, isTransactionReturn, isOtherIncome, isRedirection)
give contribution_kind exactly.

Gaps: contributions carry no office (the API's officeIds filter refers to independent-expenditure targets), and
candidate committees have no candidate person name, so office breakdowns and name-based self-funding are not
available for Wisconsin yet.
"""
import json
import re
import shutil
from datetime import date
from pathlib import Path

import duckdb
import requests

from ...config import USER_AGENT, state_dir
from ... import store
from ...labels import STATE_NAMES
from ._common import case_map, data_files, parquet_list, read_manifest, sql_str, write_manifest

STATE = "WI"
SOURCE = "wi_ethics"
ID_KEY = "committee_ids"        # profile ids.state.<STATE>.<ID_KEY>  (Sunshine entity IDs)
RESULT_ID = "committee_id"     # field in search() results holding that ID
API = "https://campaignfinance.wi.gov/api/trpc"
FIRST_YEAR = 2008
PAGE = 1000
CHUNK_ROWS = 100_000

SCOPE = {
    "name": "Wisconsin Ethics Commission (Sunshine, formerly CFIS)",
    "url": "https://campaignfinance.wi.gov/",
    "covers": "State candidates, PACs, party and legislative campaign committees, conduits, and other registrants "
              "filing with the Ethics Commission; migrated history from mid-2008.",
    "excludes": ["Federal candidates (use FEC)", "Local candidates and referenda (filed with local clerks)"],
    "history_from": "July 2008",
    "refresh": "Statewide API, one calendar year at a time; a year is re-downloaded only when its contribution "
               "count changes (~10 minutes per year).",
    "rules": [
        "Only transactionType 1 (Contribution) loads; conduit release records (type 3) are not double counted.",
        "contribution_kind comes from the category id (rows carry no category code): loans (commercial/personal), "
        "in-kind, returned contributions, other income and forgiven obligations (other_receipt), redirected "
        "(other_receipt).",
        "Donors with entityTypeOf PERSON are individuals; ANONYMOUS donors (small anonymous gifts) are unitemized.",
        "Recipients are the filing committee (createdByEntity); committee type and party come from its registration.",
        "The API uses superjson: the paging cursor's date must be sent with meta {values: {cursor.date: [Date]}}.",
    ],
    "known_gaps": [
        "No office for candidate committees (not exposed by the public API), so office breakdowns are unavailable.",
        "Candidate committees have no candidate person name, so name-based self-funding cannot be computed.",
        "No donor employer field; occupation only.",
    ],
}

# category ID -> contribution_kind. Transaction rows carry only the category id and label (no code), so map by id
# (ids/codes from transactionMeta.getTransactionCategories, transactionTypeId 1):
#   loans: 2 CL, 12 PL, 18 RCPL, 24 RCTPL | in-kind: 5 IK, 16 RCIK, 22 RCTIK | return: 25 RDC
#   other_receipt: 3 ER, 10 OI, 15 RCEF, 21 RCTEF, 4 IIEF, 6 IOF, 7 LF, 154 RDRCT (redirected)
CATEGORY_KIND = {
    "2": "loan", "12": "loan", "18": "loan", "24": "loan",
    "5": "in_kind", "16": "in_kind", "22": "in_kind",
    "25": "return",
    "3": "other_receipt", "10": "other_receipt", "15": "other_receipt", "21": "other_receipt",
    "4": "other_receipt", "6": "other_receipt", "7": "other_receipt", "154": "other_receipt",
}
COMMITTEE_KIND = {
    "State Candidate": "candidate", "Recall": "candidate", "PAC": "pac", "Independent Expenditure Committee": "pac",
    "Sponsoring Organization": "pac", "Referendum": "ballot_measure", "Political Party": "party_committee",
    "Legislative Campaign Committee": "party_committee", "Conduit": "other_committee",
}


def _call(proc: str, inp: dict, retries: int = 3) -> dict:
    """tRPC with superjson: Date values must be flagged in meta (the cursor's date), or the server answers 400."""
    import time

    payload = {"json": inp}
    if isinstance(inp.get("cursor"), dict) and "date" in inp["cursor"]:
        payload["meta"] = {"values": {"cursor.date": ["Date"]}}
    for attempt in range(retries + 1):
        try:
            r = requests.get(f"{API}/{proc}", params={"input": json.dumps(payload)},
                             headers={"User-Agent": USER_AGENT}, timeout=180)
            r.raise_for_status()
            return r.json()["result"]["data"]["json"]
        except (requests.RequestException, KeyError, ValueError):
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def _dirs() -> tuple[Path, Path]:
    d = state_dir(STATE)
    p = d / "parquet"
    p.mkdir(parents=True, exist_ok=True)
    return d, p


def _primary(entity: dict | None) -> dict:
    profiles = (entity or {}).get("entityContactProfiles") or []
    return next((c for c in profiles if c.get("isPrimary")), profiles[0] if profiles else {}) or {}


def flatten(t: dict) -> dict:
    rec = t.get("createdByEntity") or t.get("to_entity") or {}
    com = rec.get("committee") or {}
    don = t.get("from_entity") or {}
    addr = _primary(don)
    cat = t.get("transactionCategory") or {}
    return {
        "id": str(t["id"]), "date": (t.get("date") or "")[:10] or None, "amount": t.get("amount"),
        "category_id": str(cat.get("id") or ""), "category": cat.get("label"), "category_code": cat.get("code"),
        "recipient_id": str(rec.get("id") or t.get("createdByEntityId") or ""), "recipient_name": rec.get("name"),
        "recipient_committee_id": com.get("assignedCommitteeId"),
        "recipient_type": (com.get("committeeType") or {}).get("name"),
        "recipient_subtype": (com.get("committeeSubtype") or {}).get("name"),
        "recipient_party": (com.get("party") or {}).get("name") or com.get("partyFreeInput"),
        "donor_id": str(don.get("id") or ""), "donor_name": don.get("name"),
        "donor_kind": (don.get("entityType") or {}).get("entityTypeOf"),
        "donor_type": (don.get("entityType") or {}).get("name"),
        "donor_city": addr.get("city"), "donor_state": addr.get("state"), "donor_zip": addr.get("zip"),
        "occupation": t.get("fromOccupationTitle"),
    }


TX_COLS = {"id": "VARCHAR", "date": "DATE", "amount": "DOUBLE", "category_id": "VARCHAR", "category": "VARCHAR",
           "category_code": "VARCHAR", "recipient_id": "VARCHAR", "recipient_name": "VARCHAR",
           "recipient_committee_id": "VARCHAR", "recipient_type": "VARCHAR", "recipient_subtype": "VARCHAR",
           "recipient_party": "VARCHAR", "donor_id": "VARCHAR", "donor_name": "VARCHAR", "donor_kind": "VARCHAR",
           "donor_type": "VARCHAR", "donor_city": "VARCHAR", "donor_state": "VARCHAR", "donor_zip": "VARCHAR",
           "occupation": "VARCHAR"}


def _write_chunk(rows: list[dict], dest: Path) -> None:
    tmp = dest.with_suffix(".json")
    tmp.write_text(json.dumps(rows))
    spec = "{" + ", ".join(f"'{k}': '{v}'" for k, v in TX_COLS.items()) + "}"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_json({sql_str(tmp)}, columns={spec}, format='array')) "
                f"TO {sql_str(dest)} (FORMAT parquet, COMPRESSION zstd)")
    con.close()
    tmp.unlink()


def _year_filter(year: int) -> dict:
    return {"dateFrom": f"{year}-01-01", "dateTo": f"{year}-12-31", "transactionType": [1]}


def sync(years: list[int] | None = None, force: bool = False) -> dict:
    """Download each calendar year's contributions (default: last 4 years). Resumable within a year."""
    d, p = _dirs()
    years = years or list(range(date.today().year - 3, date.today().year + 1))
    m = read_manifest(d)
    out = []
    for y in years:
        if y < FIRST_YEAR:
            out.append({"year": y, "status": f"not covered (data starts {FIRST_YEAR})"})
            continue
        ym = m.setdefault(str(y), {})
        count = _call("publicFrontendApi.getTransactionsTotalCount", _year_filter(y))
        final, work = p / f"tx_{y}", p / f"tx_{y}_partial"
        if not force and ym.get("complete") and ym.get("count") == count and final.exists():
            out.append({"year": y, "status": "current", "rows": count})
            continue
        if force or ym.get("count") != count:  # upstream changed: restart this year from scratch
            shutil.rmtree(work, ignore_errors=True)
            ym.update({"cursor": None, "chunks": 0, "rows": 0, "complete": False})
        work.mkdir(exist_ok=True)
        ym["count"] = count
        buf, cursor = [], ym.get("cursor")
        while True:
            inp = {**_year_filter(y), "take": PAGE, "sortBy": "date", "sortDirection": "asc"}
            if cursor:
                inp["cursor"] = cursor
            res = _call("publicFrontendApi.getTransactions", inp)
            buf += [flatten(t) for t in res["results"]]
            cursor = res.get("nextCursor")
            done = len(res["results"]) < PAGE or not cursor
            if len(buf) >= CHUNK_ROWS or (done and buf):
                _write_chunk(buf, work / f"chunk_{ym['chunks']:04d}.parquet")
                ym["chunks"] += 1
                ym["rows"] += len(buf)
                ym["cursor"] = cursor  # checkpoint only after the chunk is on disk
                buf = []
                write_manifest(d, m)
            if done:
                break
        shutil.rmtree(final, ignore_errors=True)
        work.rename(final)
        ym.update({"complete": True, "cursor": None})
        write_manifest(d, m)
        out.append({"year": y, "status": "updated", "rows": ym["rows"], "expected": count})
    write_manifest(d, m)
    return {"state": STATE, "years": out}


def _tx_files() -> list[Path]:
    _, p = _dirs()
    files = []
    for ydir in data_files(p, r"tx_\d{4}"):
        files += data_files(ydir, r"chunk_\d{4}\.parquet")
    return files


def synced_years() -> list[int]:
    _, p = _dirs()
    return sorted(int(f.name[3:7]) for f in data_files(p, r"tx_\d{4}"))


def covered_years() -> list[int]:
    return synced_years()


def is_synced() -> bool:
    return bool(_tx_files())


def _tx_list() -> str:
    files = _tx_files()
    if not files:
        raise FileNotFoundError("Wisconsin not synced; run sync_state('WI')")
    return "[" + ", ".join(sql_str(f) for f in files) + "]"


def _state_code(expr: str) -> str:
    return f"coalesce({case_map(expr, STATE_NAMES, 'NULL')}, CASE WHEN length(trim({expr})) = 2 THEN upper(trim({expr})) END)"


def search(name: str) -> list[dict]:
    if not is_synced():
        return [{"source": SOURCE, "error": "Wisconsin data not synced. Run sync_state('WI') first."}]
    words = [w for w in re.split(r"[\s,]+", name.strip()) if w]
    where = " AND ".join("recipient_name ILIKE ?" for _ in words)
    rows = duckdb.connect().execute(f"""
        SELECT recipient_id, any_value(recipient_name), any_value(recipient_committee_id), any_value(recipient_type),
               any_value(recipient_party), count(*) AS n, round(sum(amount), 2)
        FROM read_parquet({_tx_list()}) WHERE {where} GROUP BY recipient_id ORDER BY n DESC LIMIT 25""",
        [f"%{w}%" for w in words]).fetchall()
    return [{"source": SOURCE, "state": STATE, "committee_id": r[0], "name": r[1], "ethics_committee_id": r[2],
             "committee_type": r[3], "party": r[4], "rows": r[5], "total": r[6]} for r in rows]


def normalized_sql(ids: list[str] | None = None, slug: str | None = None) -> tuple[str, list]:
    where, params = "true", [slug]
    if ids is not None:
        where = f"recipient_id IN ({', '.join('?' for _ in ids)})"
        params += [str(i) for i in ids]
    kind = case_map("category_id", CATEGORY_KIND, "CASE WHEN amount < 0 THEN 'return' ELSE 'monetary' END")
    sql = f"""
        SELECT ? AS candidate_slug, '{SOURCE}', '{STATE}', recipient_id, recipient_name, id,
               date AS d, amount,
               CASE WHEN donor_kind IN ('ANONYMOUS') OR donor_type = 'Unitemized' THEN NULL ELSE nullif(trim(donor_name), '') END,
               CASE WHEN donor_kind IN ('ANONYMOUS') OR donor_type = 'Unitemized' THEN 'unitemized'
                    WHEN donor_kind IN ('PERSON', 'INDIVIDUAL') THEN 'individual' ELSE 'organization' END,
               nullif(trim(donor_city), ''), {_state_code("donor_state")}, nullif(left(trim(donor_zip), 5), ''),
               NULL, nullif(trim(occupation), ''), false,
               (year(d) + year(d) % 2)::INTEGER, {kind},
               NULL::DATE  -- filed_date: when this row became public
        FROM read_parquet({_tx_list()})
        WHERE {where}
        QUALIFY row_number() OVER (PARTITION BY id ORDER BY date) = 1
    """
    return sql, params


def load(con, profile: dict) -> dict:
    ids = (profile["ids"].get("state", {}).get(STATE) or {}).get(ID_KEY) or []
    if not ids:
        return {"source": SOURCE, "rows": 0, "note": "no WI committee_ids in profile"}
    if not is_synced():
        return {"source": SOURCE, "rows": 0, "note": "Wisconsin data not synced; run sync_state('WI')"}
    sql, params = normalized_sql(ids, profile["slug"])
    m = store.merge_query(con, profile["slug"], SOURCE, sql, params)
    return {"source": SOURCE, "rows": m["staged"], "changes": m, ID_KEY: ids, "covered_years": covered_years()}


def committees_sql() -> str:
    from ... import labels as L
    return f"""
        WITH e AS (
            SELECT recipient_id AS committee_id, arg_max(recipient_name, date) AS committee_name,
                   arg_max(recipient_type, date) AS ctype, arg_max(recipient_party, date) AS party_raw
            FROM read_parquet({_tx_list()}) GROUP BY recipient_id),
        k AS (SELECT *, {case_map("ctype", COMMITTEE_KIND, "'other_committee'")} AS kind FROM e)
        SELECT committee_id, committee_name, kind, NULL AS office_raw,
               CASE WHEN kind = 'candidate' THEN 'unknown' ELSE kind END AS office_category,  -- no office in WI API
               NULL AS district, {L.committee_party_sql("party_raw", "kind", "committee_name")} AS party,
               NULL AS cand_last, NULL AS cand_first,
               (ctype = 'Conduit' OR {L.conduit_sql("committee_name")}) AS is_conduit
        FROM k"""
