"""Michigan Bureau of Elections campaign finance via MiTN (Michigan Transparency Network).

Download page: https://mi-boe.entellitrak.com/etk-mi-boe-prod/page.request.do?page=gov.mi.boe.component.cfrexport.page.cfrexportdownload
One zip per (transaction type, year), refreshed daily; each holds tab-delimited .txt parts.
Data since 1998, including records migrated from the old MERTS system.

Format quirks handled:
- Data rows carry a trailing tab (29 fields vs a 28-field header) -> read with explicit columns.
- Fields contain stray double quotes -> quoting disabled.
- Candidate name casing is inconsistent (e.g. "JOCELYN BENSON" vs "Jocelyn Benson").
- Electronic filing is only mandatory above $5,000/yr, so very small committees may be missing.
"""
import re
import zipfile
from datetime import date
from pathlib import Path

import duckdb
import requests

from ...config import USER_AGENT, state_dir
from ... import store
from ._common import case_map, data_files, parquet_list, download, extract_member, read_manifest, sql_str, tempdir, write_manifest

STATE = "MI"
SOURCE = "mi_boe"
PARQUET = r"contributions_\d{4}\.parquet"  # exact names only (rule: never trust globs)
ID_KEY = "committee_ids"        # profile ids.state.<STATE>.<ID_KEY>
RESULT_ID = "committee_id"     # field in search() results holding that ID
BASE = "https://mi-boe.entellitrak.com/etk-mi-boe-prod/page.request.do"
LIST_PAGE = "gov.mi.boe.component.cfrexport.page.cfrexportresults"
FILE_PAGE = "gov.mi.boe.component.cfrexport.page.cfrexportfile"

COLUMNS = ["doc_seq_no", "contribution_id", "cont_detail_id", "doc_stmnt_year", "doc_type_desc", "com_legal_name",
           "common_name_acronym", "cfr_com_id", "com_type", "can_first_name", "can_last_name", "contribtype",
           "contributor_f_name", "contributor_l_name_or_org", "contributor_address", "contributor_city",
           "contributor_state", "contributor_zip", "contributor_occupation", "contributor_employer",
           "received_date", "amount", "aggregate", "extra_desc", "fundraiser", "can_political_party",
           "can_office_sought", "can_district_sought", "_trailing"]
KEEP = [c for c in COLUMNS if c not in ("contributor_address", "_trailing")]


def _dirs() -> tuple[Path, Path]:
    d = state_dir(STATE)
    p = d / "parquet"
    p.mkdir(parents=True, exist_ok=True)
    return d, p


def list_files(kind: str = "contribution") -> list[dict]:
    r = requests.get(BASE, params={"page": LIST_PAGE, "type": kind, "pageSize": 100, "pageNumber": 1,
                                   "sortBy": "year", "sortDirection": "DESC"},
                     headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"MiTN listing failed: {body}")
    return [{"year": f["year"], "id": f["download"], "updated": f["lastupdatedraw"]}
            for f in body["data"]["list"] if f["transactiontype"].lower() == kind]


def _read_txt(path: Path) -> str:
    cols = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c in COLUMNS) + "}"
    return (f"read_csv({sql_str(path)}, delim='\\t', header=false, skip=1, quote='', escape='', "
            f"columns={cols}, null_padding=true, strict_mode=false, ignore_errors=true)")


def build_year(zip_path: Path, year: int) -> Path:
    d, p = _dirs()
    out = p / f"contributions_{year}.parquet"
    con = duckdb.connect()
    with zipfile.ZipFile(zip_path) as zf, tempdir(d) as tmp:
        files = [extract_member(zf, n, Path(tmp)) for n in zf.namelist() if n.lower().endswith(".txt")]
        if not files:
            raise RuntimeError(f"{zip_path} contains no .txt files")
        union = " UNION ALL ".join(f"SELECT {', '.join(KEEP)} FROM {_read_txt(f)}" for f in files)
        con.execute(f"COPY ({union}) TO {sql_str(out)} (FORMAT parquet, COMPRESSION zstd)")
    con.close()
    return out


def sync(years: list[int] | None = None, force: bool = False) -> dict:
    """Download contribution files for `years` (default: last 4 years) if they changed upstream."""
    d, _ = _dirs()
    years = years or list(range(date.today().year - 3, date.today().year + 1))
    m = read_manifest(d)
    avail = {f["year"]: f for f in list_files("contribution")}
    done = []
    for y in years:
        f = avail.get(y)
        if not f:
            done.append({"year": y, "status": "not published"})
            continue
        key = f"contribution_{y}"
        if not force and m.get(key) == f["updated"] and (d / "parquet" / f"contributions_{y}.parquet").exists():
            done.append({"year": y, "status": "current"})
            continue
        zip_path = d / f"contributions_{y}.zip"
        download(f"{BASE}?page={FILE_PAGE}&id={f['id']}", zip_path)
        build_year(zip_path, y)
        zip_path.unlink()
        m[key] = f["updated"]
        done.append({"year": y, "status": "updated"})
    write_manifest(d, m)
    return {"state": STATE, "years": done}


def synced_years() -> list[int]:
    _, p = _dirs()
    return sorted(int(f.name[14:18]) for f in data_files(p, PARQUET))


def is_synced() -> bool:
    return bool(synced_years())


def search(name: str) -> list[dict]:
    """Candidate committees whose candidate or committee name matches."""
    if not synced_years():
        return [{"source": SOURCE, "error": "Michigan data not synced. Run sync_state('MI') first."}]
    _, p = _dirs()
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    where = " AND ".join("(concat_ws(' ', can_first_name, can_last_name) ILIKE ? OR com_legal_name ILIKE ?)"
                         for _ in words)
    params = [x for w in words for x in (f"%{w}%", f"%{w}%")]
    rows = duckdb.connect().execute(f"""
        SELECT cfr_com_id, any_value(com_legal_name) AS committee,
               upper(trim(concat_ws(' ', any_value(can_first_name), any_value(can_last_name)))) AS candidate,
               any_value(can_office_sought) AS office, any_value(can_district_sought) AS district,
               any_value(can_political_party) AS party,
               list_sort(list_distinct(list(try_cast(doc_stmnt_year AS INTEGER)))) AS years, count(*) AS n
        FROM read_parquet({parquet_list(p, PARQUET)})
        WHERE com_type = 'Candidate' AND {where}
        GROUP BY cfr_com_id ORDER BY n DESC LIMIT 25""", params).fetchdf()
    return [{"source": SOURCE, "state": STATE, "committee_id": r.cfr_com_id, "committee": r.committee,
             "name": r.candidate, "office": " ".join(x.strip() for x in (r.office, r.district) if x and x.strip()) or None,
             "party": (r.party or "").strip() or None, "years": [int(y) for y in r.years if y], "rows": int(r.n)}
            for r in rows.itertuples()]


SCOPE = {
    "name": "Michigan Bureau of Elections (MiTN)",
    "url": "https://mi-boe.entellitrak.com/etk-mi-boe-prod/page.request.do?page=gov.mi.boe.component.cfrexport.page.cfrexportdownload",
    "covers": "All committees filing with the state: candidate, PAC, party, ballot question, independent expenditure.",
    "excludes": ["Federal candidates (use FEC)", "Committees below the $5,000/yr e-filing threshold may be absent"],
    "history_from": "1998 (includes data migrated from the old MERTS system)",
    "refresh": "One file per year, refreshed daily; sync defaults to the last 4 years (pass years= for more).",
    "rules": [
        "Data rows carry a trailing tab and stray quotes; parsed with explicit columns and quoting disabled.",
        "Candidate committees are com_type = 'Candidate'; money is filed under the committee ID.",
    ],
    "known_gaps": ["Amendments may not be reflected in the export; verify key figures against the filed statement."],
}


def covered_years() -> list[int]:
    return synced_years()


def normalized_sql(ids: list[str] | None = None, slug: str | None = None) -> tuple[str, list]:
    _, p = _dirs()
    where, params = "true", [slug]
    if ids is not None:
        where = f"cfr_com_id IN ({', '.join('?' for _ in ids)})"
        params += list(ids)
    sql = f"""
        SELECT ? AS candidate_slug, '{SOURCE}', '{STATE}', cfr_com_id, com_legal_name,
               concat_ws('-', contribution_id, cont_detail_id),
               try_strptime(received_date, '%m/%d/%Y')::DATE AS d,
               try_cast(replace(amount, ',', '') AS DOUBLE),
               trim(concat_ws(' ', trim(contributor_f_name), trim(contributor_l_name_or_org))),
               CASE WHEN nullif(trim(contributor_f_name), '') IS NULL THEN 'organization' ELSE 'individual' END,
               trim(contributor_city), upper(trim(contributor_state)), left(trim(contributor_zip), 5),
               nullif(trim(contributor_employer), ''), nullif(trim(contributor_occupation), ''), false,
               (year(d) + year(d) % 2)::INTEGER,
               CASE WHEN contribtype ILIKE '%loan%' THEN 'loan'
                    WHEN contribtype ILIKE '%in-kind%' OR contribtype ILIKE '%in kind%' THEN 'in_kind'
                    WHEN try_cast(replace(amount, ',', '') AS DOUBLE) < 0 THEN 'return' ELSE 'monetary' END
        FROM read_parquet({parquet_list(p, PARQUET)})
        WHERE {where}
    """
    return sql, params


def load(con, profile: dict) -> dict:
    ids = (profile["ids"].get("state", {}).get(STATE) or {}).get(ID_KEY) or []
    if not ids:
        return {"source": SOURCE, "rows": 0, "note": "no MI committee_ids in profile"}
    if not is_synced():
        return {"source": SOURCE, "rows": 0, "note": "Michigan data not synced; run sync_state('MI')"}
    sql, params = normalized_sql(ids, profile["slug"])
    m = store.merge_query(con, profile["slug"], SOURCE, sql, params)  # full snapshot: absent rows -> removed
    return {"source": SOURCE, "rows": m["staged"], "changes": m, ID_KEY: ids, "covered_years": covered_years()}


# ---------- committees / offices (rule office-taxonomy) ----------

OFFICE_MAP = {
    "Governor": "governor", "Lt. Governor": "lieutenant_governor", "Attorney General": "attorney_general",
    "Secretary of State": "secretary_of_state", "State Senator": "state_senate",
    "Representative in State Legislature": "state_house",
    "Member of the State Board of Education": "state_board", "Regent of the University of Michigan": "state_board",
    "Governor of Wayne State University": "state_board", "Trustee of Michigan State University": "state_board",
    "Justice of Supreme Court": "supreme_court", "Judge of Court of Appeals": "appellate_court",
    "Judge of District Court": "trial_court", "Judge of Probate Court": "trial_court",
    "Judge of Circuit Court": "trial_court", "Judge of Municipal Court": "trial_court",
}
KIND_MAP = {"Candidate": "candidate", "Independent": "pac", "Political": "pac", "Independent Expenditure": "pac",
            "County Political Party": "party_committee", "District Political Party": "party_committee",
            "State Political Party": "party_committee", "Ballot Question": "ballot_measure", "Caucus": "other_committee"}


def committees_sql() -> str:
    """committee_id, committee_name, kind, office_raw, office_category, district, party, cand_last, cand_first,
    is_conduit."""
    from ... import labels as L
    _, p = _dirs()
    return f"""
        WITH f AS (
            SELECT cfr_com_id AS committee_id,
                   arg_max(com_legal_name, doc_stmnt_year) AS committee_name,
                   arg_max(com_type, doc_stmnt_year) AS ctype,
                   arg_max(nullif(trim(can_office_sought), ''), doc_stmnt_year) AS office_raw,
                   arg_max(nullif(trim(can_district_sought), ''), doc_stmnt_year) AS district,
                   arg_max(nullif(trim(can_political_party), ''), doc_stmnt_year) AS party_raw,
                   arg_max(nullif(trim(can_last_name), ''), doc_stmnt_year) AS cand_last,
                   arg_max(nullif(trim(can_first_name), ''), doc_stmnt_year) AS cand_first
            FROM read_parquet({parquet_list(p, PARQUET)}) GROUP BY cfr_com_id)
        SELECT committee_id, committee_name, k.kind, office_raw,
               CASE WHEN k.kind = 'candidate' THEN {case_map("office_raw", OFFICE_MAP)} ELSE k.kind END AS office_category,
               district, {L.committee_party_sql("party_raw", "k.kind", "committee_name")} AS party,
               CASE WHEN k.kind = 'candidate' THEN cand_last END AS cand_last,
               CASE WHEN k.kind = 'candidate' THEN cand_first END AS cand_first,
               {L.conduit_sql("committee_name")} AS is_conduit
        FROM f, LATERAL (SELECT {case_map("ctype", KIND_MAP, "'other_committee'")} AS kind) k"""
