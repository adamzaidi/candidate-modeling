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
from ._common import download, extract_member, read_manifest, sql_str, tempdir, write_manifest

STATE = "MI"
SOURCE = "mi_boe"
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
    return sorted(int(re.search(r"(\d{4})", f.name).group(1)) for f in p.glob("contributions_*.parquet"))


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
        FROM read_parquet({sql_str(p / 'contributions_*.parquet')})
        WHERE com_type = 'Candidate' AND {where}
        GROUP BY cfr_com_id ORDER BY n DESC LIMIT 25""", params).fetchdf()
    return [{"source": SOURCE, "state": STATE, "committee_id": r.cfr_com_id, "committee": r.committee,
             "name": r.candidate, "office": " ".join(x.strip() for x in (r.office, r.district) if x and x.strip()) or None,
             "party": (r.party or "").strip() or None, "years": [int(y) for y in r.years if y], "rows": int(r.n)}
            for r in rows.itertuples()]


def load(con, profile: dict) -> dict:
    ids = (profile["ids"].get("state", {}).get(STATE) or {}).get("committee_ids") or []
    if not ids:
        return {"source": SOURCE, "rows": 0, "note": "no MI committee_ids in profile"}
    if not synced_years():
        return {"source": SOURCE, "rows": 0, "note": "Michigan data not synced; run sync_state('MI')"}
    _, p = _dirs()
    placeholders = ", ".join("?" for _ in ids)
    sql = f"""
        SELECT ? AS candidate_slug, '{SOURCE}', '{STATE}', cfr_com_id, com_legal_name,
               concat_ws('-', contribution_id, cont_detail_id),
               try_strptime(received_date, '%m/%d/%Y')::DATE AS d,
               try_cast(replace(amount, ',', '') AS DOUBLE),
               trim(concat_ws(' ', trim(contributor_f_name), trim(contributor_l_name_or_org))),
               CASE WHEN nullif(trim(contributor_f_name), '') IS NULL THEN 'organization' ELSE 'individual' END,
               trim(contributor_city), upper(trim(contributor_state)), left(trim(contributor_zip), 5),
               nullif(trim(contributor_employer), ''), nullif(trim(contributor_occupation), ''), false,
               (year(d) + year(d) % 2)::INTEGER
        FROM read_parquet({sql_str(p / 'contributions_*.parquet')})
        WHERE cfr_com_id IN ({placeholders})
    """
    m = store.merge_query(con, profile["slug"], SOURCE, sql, [profile["slug"], *ids])  # full snapshot: absent rows -> removed
    return {"source": SOURCE, "rows": m["staged"], "changes": m, "committee_ids": ids, "years_available": synced_years()}
