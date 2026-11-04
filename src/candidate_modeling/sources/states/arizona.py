"""Arizona Secretary of State campaign finance via See The Money (seethemoney.az.gov).

There is no bulk download. The site's own JSON endpoints are used instead:
  POST /Reporting/GetNEWTableData/?Page=N&startYear=&endYear=...        entity lists (max 100 rows per page)
       Page 1 candidates, 2 PACs, 3 parties, 4 organizations (5 IE, 6 ballot measures, 7 donors, 8 vendors unused)
  POST /Reporting/GetNEWDetailedTableData/?Page=20&Name={page}~{id}&entityId={id}...   one committee's income
       (1000 rows per page)
Both are DataTables server-side endpoints: the order[0][column], order[0][dir], and columns[0][data] form fields are
required, or the server answers with an empty string. A query without entityId returns nothing, so the statewide
sync walks every recipient committee (~1,650 per cycle; the largest, Elect Katie Hobbs, has 177k rows in 2025-26).

Sync is resumable per committee: each committee's rows are written to their own parquet file, and a refresh only
refetches committees whose reported income changed. Data is organized by Arizona's two-year cycles (odd, even).
"""
import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import requests

from ...config import USER_AGENT, state_dir
from ... import store
from ._common import case_map, data_files, parquet_list, read_manifest, sql_str, write_manifest

STATE = "AZ"
SOURCE = "az_sos"
ID_KEY = "committee_ids"        # profile ids.state.<STATE>.<ID_KEY>
RESULT_ID = "committee_id"     # field in search() results holding that ID
BASE = "https://seethemoney.az.gov/Reporting"
LIST_PAGES = {1: "candidate", 2: "pac", 3: "party_committee", 4: "other_committee"}
_ORDER = {"order[0][column]": "0", "order[0][dir]": "asc", "columns[0][data]": "EntityLastName", "search[value]": ""}
_PAUSE = 0.25  # seconds between requests (be polite to a public site); patched in tests
_sleep = time.sleep

SCOPE = {
    "name": "Arizona Secretary of State (See The Money)",
    "url": "https://seethemoney.az.gov/",
    "covers": "Statewide and legislative candidates, PACs, party committees, and organizations filing with the "
              "Secretary of State.",
    "excludes": ["Federal candidates (use FEC)", "Local filers who file with a city or county",
                 "Independent expenditures and ballot-measure committees (not loaded yet)"],
    "history_from": "Per two-year cycle as synced (site data goes back to the 2000s)",
    "refresh": "No bulk file: sync walks every recipient committee per cycle (resumable; refresh refetches only "
               "committees whose income changed). A first full cycle takes on the order of an hour.",
    "rules": [
        "Only income transactions (Page 20) load; expenses are not loaded yet.",
        "Clean Elections public funding ('Receive CCEC Funding') is contribution_kind 'public_funds': reported, not "
        "counted as contributions. $5 qualifying contributions count as individual gifts; their hand-off to the "
        "CCEC fund is an other_receipt.",
        "'In-State Contributions $100 or Less' are lump sums with no donor: contributor_type 'unitemized'.",
        "Income detail page = (entity list page + 1) * 10: 20 candidates, 30 PACs, 40 parties, 50 organizations.",
        "Transactions are keyed by PublicTransactionId.",
    ],
    "known_gaps": ["Cycle-based: a cycle that has not been synced is not covered even if neighboring cycles are."],
}


# ---------- HTTP ----------

def _post(path: str, query: str, start: int, length: int, retries: int = 3) -> dict:
    for attempt in range(retries + 1):
        try:
            r = requests.post(f"{BASE}/{path}/?{query}", headers={"User-Agent": USER_AGENT}, timeout=180,
                              data={"draw": "1", "start": str(start), "length": str(length), **_ORDER})
            r.raise_for_status()
            if not r.text.strip() or r.text.strip() == '""':
                raise ValueError("empty response (missing DataTables order parameters?)")
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == retries:
                raise
            _sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def _q(page: int, cycle: tuple[int, int], start: int, length: int, extra: str = "") -> str:
    return (f"Page={page}&startYear={cycle[0]}&endYear={cycle[1]}&JurisdictionId=0&TablePage={start // length + 1}"
            f"&TableLength={length}&IsLessActive=false&ShowOfficeHolder=false&ChartName={page}{extra}")


def cycle_for_year(year: int) -> tuple[int, int]:
    """Arizona cycles run odd year -> even year (2025-2026)."""
    return (year - 1, year) if year % 2 == 0 else (year, year + 1)


def _tag(cycle) -> str:
    return f"{cycle[0]}_{cycle[1]}"


def _dirs() -> tuple[Path, Path]:
    d = state_dir(STATE)
    p = d / "parquet"
    p.mkdir(parents=True, exist_ok=True)
    return d, p


# ---------- entities ----------

def list_entities(cycle: tuple[int, int]) -> list[dict]:
    """Every recipient committee for the cycle, merged across the rows the site returns per entity."""
    out: dict[int, dict] = {}
    for page, kind in LIST_PAGES.items():
        start, total = 0, None
        while total is None or start < total:
            d = _post("GetNEWTableData", _q(page, cycle, start, 100), start, 100)
            total = d["recordsTotal"]
            for r in d["data"]:
                e = out.setdefault(r["EntityID"], {"entity_id": str(r["EntityID"]), "list_page": page, "kind": kind,
                                                   "income": 0.0})
                e["income"] += r.get("Income") or 0
                name = re.sub(r"\s*<br>.*$", "", r.get("CommitteeName") or "").strip()
                last = re.sub(r"\s*<br>.*$", "", r.get("EntityLastName") or "").strip()
                e["committee_name"] = e.get("committee_name") or name or last
                e["filer_name"] = e.get("filer_name") or last
                for k_src, k_dst in (("OfficeName", "office"), ("PartyName", "party"), ("EntityTypeName", "entity_type")):
                    if r.get(k_src):
                        e[k_dst] = r[k_src]
            start += 100
            if not d["data"]:
                break
            _sleep(_PAUSE)
    return list(out.values())


# ---------- transactions ----------

def _ms_date(v) -> str | None:
    m = re.search(r"-?\d+", str(v or ""))
    return datetime.fromtimestamp(int(m.group()) / 1000, tz=timezone.utc).date().isoformat() if m else None


TX_FIELDS = ["PublicTransactionId", "CommitteeId", "CommitteeName", "TransactionDate", "Amount", "TransactionType",
             "TransactionGroupName", "TransactionEntityTypeId", "TransactionFirstName", "TransactionLastName",
             "TransactionCity", "TransactionState", "TransactionZipCode", "TransactionEmployer", "TransactionOccupation",
             "CandidateFirstName", "CandidateLastName", "CandidateOfficeId", "Memo"]


def fetch_entity(entity: dict, cycle: tuple[int, int]) -> list[dict]:
    eid, page = entity["entity_id"], entity["list_page"]
    income_page = (page + 1) * 10  # income detail views: 20 candidates, 30 PACs, 40 parties, 50 organizations
    rows, start, total = [], 0, None
    while total is None or start < total:
        d = _post("GetNEWDetailedTableData", _q(income_page, cycle, start, 1000, f"&Name={page}~{eid}&entityId={eid}"),
                  start, 1000)
        total = d["recordsTotal"]
        for t in d["data"]:
            row = {k: t.get(k) for k in TX_FIELDS}
            row["TransactionDate"] = _ms_date(row["TransactionDate"])
            rows.append(row)
        start += 1000
        if not d["data"]:
            break
        _sleep(_PAUSE)
    return rows


def _write_json_parquet(rows: list[dict], dest: Path, columns: dict[str, str]) -> None:
    tmp = dest.with_suffix(".json")
    tmp.write_text(json.dumps(rows))
    spec = "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_json({sql_str(tmp)}, columns={spec}, format='array')) "
                f"TO {sql_str(dest)} (FORMAT parquet)")
    con.close()
    tmp.unlink()


TX_TYPES = {k: "VARCHAR" for k in TX_FIELDS} | {"Amount": "DOUBLE"}
ENTITY_TYPES = {"entity_id": "VARCHAR", "list_page": "INTEGER", "kind": "VARCHAR", "income": "DOUBLE",
                "committee_name": "VARCHAR", "filer_name": "VARCHAR", "office": "VARCHAR", "party": "VARCHAR",
                "entity_type": "VARCHAR"}


def sync(years: list[int] | None = None, force: bool = False, max_entities: int | None = None) -> dict:
    """Sync the cycles covering `years` (default: the current cycle). Resumable; safe to interrupt and rerun."""
    d, p = _dirs()
    cycles = sorted({cycle_for_year(y) for y in (years or [date.today().year])})
    m = read_manifest(d)
    out = []
    for cy in cycles:
        tag = _tag(cy)
        cm = m.setdefault(tag, {"entities": {}, "complete": False})
        entities = list_entities(cy)
        _write_json_parquet(entities, p / f"entities_{tag}.parquet", ENTITY_TYPES)
        txdir = p / f"tx_{tag}"
        txdir.mkdir(exist_ok=True)
        fetched = skipped = 0
        todo = [e for e in entities if (e["income"] or 0) != 0]
        for i, e in enumerate(todo):
            if max_entities is not None and fetched >= max_entities:
                break
            f = txdir / f"{e['entity_id']}.parquet"
            if not force and f.exists() and cm["entities"].get(e["entity_id"]) == round(e["income"], 2):
                skipped += 1
                continue
            rows = fetch_entity(e, cy)
            if rows:
                _write_json_parquet(rows, f, TX_TYPES)
            elif f.exists():
                f.unlink()
            cm["entities"][e["entity_id"]] = round(e["income"], 2)
            fetched += 1
            if fetched % 25 == 0:
                write_manifest(d, m)  # checkpoint: an interrupted sync resumes from here
        cm["complete"] = max_entities is None or fetched < max_entities
        cm["synced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_manifest(d, m)
        out.append({"cycle": tag, "entities": len(entities), "with_income": len(todo), "fetched": fetched,
                    "skipped_unchanged": skipped, "complete": cm["complete"]})
    return {"state": STATE, "cycles": out}


def _tx_files() -> list[Path]:
    _, p = _dirs()
    files = []
    for cdir in data_files(p, r"tx_\d{4}_\d{4}"):
        files += data_files(cdir, r"\d+\.parquet")
    return files


def covered_years() -> list[int]:
    d, _ = _dirs()
    m = read_manifest(d)
    yrs = set()
    for tag, cm in m.items():
        if isinstance(cm, dict) and cm.get("complete"):
            a, b = (int(x) for x in tag.split("_"))
            yrs |= {a, b}
    return sorted(yrs)


def is_synced() -> bool:
    return bool(_tx_files())


def _tx_list() -> str:
    files = _tx_files()
    if not files:
        raise FileNotFoundError("Arizona not synced; run sync_state('AZ')")
    return "[" + ", ".join(sql_str(f) for f in files) + "]"


def _entities_list() -> str:
    _, p = _dirs()
    return parquet_list(p, r"entities_\d{4}_\d{4}\.parquet")


# ---------- search / normalize ----------

def search(name: str) -> list[dict]:
    if not is_synced():
        return [{"source": SOURCE, "error": "Arizona data not synced. Run sync_state('AZ') first."}]
    words = [w for w in re.split(r"[\s,]+", name.strip()) if w]
    where = " AND ".join("concat_ws(' ', committee_name, filer_name) ILIKE ?" for _ in words)
    rows = duckdb.connect().execute(f"""
        SELECT entity_id, any_value(committee_name), any_value(filer_name), any_value(kind), any_value(office),
               any_value(party), round(sum(income), 2) AS income
        FROM read_parquet({_entities_list()}) WHERE {where}
        GROUP BY entity_id ORDER BY income DESC LIMIT 25""", [f"%{w}%" for w in words]).fetchall()
    return [{"source": SOURCE, "state": STATE, "committee_id": r[0], "committee": r[1], "name": r[2], "kind": r[3],
             "office": r[4], "party": r[5], "income": r[6]} for r in rows]


# TransactionType -> contribution_kind, from a survey of real 2025-26 transactions (Hobbs, Mayes, Neely, Hill,
# ActBlue Arizona, Navajo County Democratic Committee). Checked in order; unmatched income is monetary.
KIND_RULES = [
    ("Receive CCEC Funding%", "public_funds"),            # Clean Elections public money
    ("%clean elections%fund%", "public_funds"),
    ("Submit $5 Qualifying Contributions%", "other_receipt"),  # $5 qualifying gifts passed on to the CCEC fund
    ("%loan%", "loan"),
    ("%in-kind%", "in_kind"),
    ("%in kind%", "in_kind"),
    ("%refund%", "return"),
    ("%interest%", "other_receipt"),
    ("%transfer%", "transfer"),
]


def _kind_sql() -> str:
    whens = " ".join(f"WHEN TransactionType ILIKE {sql_str(p)} THEN {sql_str(k)}" for p, k in KIND_RULES)
    return f"CASE {whens} WHEN Amount < 0 THEN 'return' ELSE 'monetary' END"


def normalized_sql(ids: list[str] | None = None, slug: str | None = None) -> tuple[str, list]:
    where, params = "true", [slug]
    if ids is not None:
        where = f"CommitteeId IN ({', '.join('?' for _ in ids)})"
        params += [str(i) for i in ids]
    sql = f"""
        SELECT ? AS candidate_slug, '{SOURCE}', '{STATE}', CommitteeId, CommitteeName, PublicTransactionId,
               TransactionDate::DATE AS d, Amount,
               CASE WHEN TransactionType ILIKE '%$100 or Less%' THEN NULL
                    ELSE nullif(trim(coalesce(TransactionLastName, '')), '') END,
               CASE WHEN TransactionType ILIKE '%$100 or Less%' THEN 'unitemized'
                    WHEN TransactionEntityTypeId = '1' THEN 'individual' ELSE 'organization' END,
               nullif(trim(TransactionCity), ''), upper(nullif(trim(TransactionState), '')),
               nullif(left(trim(TransactionZipCode), 5), ''),
               nullif(trim(TransactionEmployer), ''), nullif(trim(TransactionOccupation), ''), false,
               (year(d) + year(d) % 2)::INTEGER, {_kind_sql()}
        FROM read_parquet({_tx_list()}, union_by_name=true)
        WHERE {where}
        QUALIFY row_number() OVER (PARTITION BY PublicTransactionId ORDER BY TransactionDate) = 1
    """
    return sql, params


def load(con, profile: dict) -> dict:
    ids = (profile["ids"].get("state", {}).get(STATE) or {}).get(ID_KEY) or []
    if not ids:
        return {"source": SOURCE, "rows": 0, "note": "no AZ committee_ids in profile"}
    if not is_synced():
        return {"source": SOURCE, "rows": 0, "note": "Arizona data not synced; run sync_state('AZ')"}
    sql, params = normalized_sql(ids, profile["slug"])
    m = store.merge_query(con, profile["slug"], SOURCE, sql, params)
    return {"source": SOURCE, "rows": m["staged"], "changes": m, ID_KEY: ids, "covered_years": covered_years()}


# ---------- committees / offices ----------

OFFICE_PATTERNS = [  # (ILIKE pattern, category) checked in order
    ("Governor", "governor"), ("Secretary of State", "secretary_of_state"), ("Attorney General", "attorney_general"),
    ("State Treasurer", "statewide_other"), ("Superintendent of Public Instruction", "statewide_other"),
    ("Corporation Commissioner%", "statewide_other"), ("State Mine Inspector", "statewide_other"),
    ("State Senator%", "state_senate"), ("State Representative%", "state_house"),
    ("%Supreme Court%", "supreme_court"), ("%Court of Appeals%", "appellate_court"),
    ("%Superior Court%", "trial_court"), ("%Justice of the Peace%", "trial_court"),
    ("%County%", "county_office"), ("%Mayor%", "municipal_office"), ("%Council%", "municipal_office"),
]


def committees_sql() -> str:
    from ... import labels as L
    office_case = "CASE " + " ".join(f"WHEN office ILIKE {sql_str(p)} THEN {sql_str(c)}" for p, c in OFFICE_PATTERNS) \
        + " ELSE 'unknown' END"
    return f"""
        WITH e AS (
            SELECT entity_id AS committee_id, arg_max(committee_name, income) AS committee_name,
                   any_value(kind) AS kind, arg_max(office, coalesce(office, '') <> '') AS office,
                   arg_max(party, coalesce(party, '') <> '') AS party_raw, arg_max(filer_name, income) AS filer_name
            FROM read_parquet({_entities_list()}) GROUP BY entity_id)
        SELECT committee_id, committee_name, kind, office AS office_raw,
               CASE WHEN kind = 'candidate' THEN {office_case} ELSE kind END AS office_category,
               regexp_extract(coalesce(office, ''), '(\\d+)\\s*$', 1) AS district,
               {L.committee_party_sql("party_raw", "kind", "committee_name")} AS party,
               CASE WHEN kind = 'candidate' THEN {L.name_last_sql("filer_name")} END AS cand_last,
               CASE WHEN kind = 'candidate' THEN {L.name_first_sql("filer_name")} END AS cand_first,
               {L.conduit_sql("committee_name")} AS is_conduit
        FROM e"""
