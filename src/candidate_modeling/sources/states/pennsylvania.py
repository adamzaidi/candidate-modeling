"""Pennsylvania Department of State campaign finance "Full Export" (state + local filers).

Download page: https://www.pa.gov/agencies/dos/resources/voting-and-elections-resources/campaign-finance-data
One zip per election year (2000-present): filer_YYYY.txt, contrib_YYYY.txt, receipt, expense, debt.
Layout: readme-cf-data.txt on the same page.

Format quirks handled (all verified on the 2026 file):
- Not valid CSV: quotes inside quoted fields are unescaped ("CHESTER "CHET" SMITH"), and a few
  fields contain raw line breaks. A tolerant parser rebuilds rows (a quote closes a field only when
  followed by a comma or end of line; short rows are joined with the next line), then writes clean
  parquet. Rows that still can't be parsed are counted and reported in sync() output, never silently dropped.
- Mostly UTF-8 with a few cp1252 lines -> decoded per line.
- Each contribution row holds up to three (date, amount) pairs -> unpivoted to one row each.
- The filer file has one row *per report*. Amended reports are included alongside originals, so only
  the latest-submitted report per (filer, year, cycle) is kept to avoid double counting.
- Candidates (FILERTYPE 1) usually raise money through a committee (FILERTYPE 2) with its own FILERID;
  search() returns both so the right committee can be added to a profile.
- Section = DSEC-150 schedule part: IA/IC from political committees (<=$250 / >$250), IB/ID from
  individuals (<=$250 / >$250), IIF/IIG in-kind. Committee sections set contributor_type; other rows
  fall back to a name heuristic (ORG_PATTERN).
- The filer file's MONETARY/INKIND columns do NOT equal the itemized sums; don't reconcile against them.
- Federal and party-office candidates don't file here.
"""
import io
import re
import zipfile
from datetime import date
from pathlib import Path

import duckdb

from ...config import state_dir
from ... import store
from ._common import (download, parse_line, read_manifest, remote_last_modified, sql_str, tempdir,  # noqa: F401
                      to_clean_csv, write_manifest)

STATE = "PA"
SOURCE = "pa_dos"
ID_KEY = "filer_ids"        # profile ids.state.<STATE>.<ID_KEY>
RESULT_ID = "filer_id"     # field in search() results holding that ID
BASE = "https://www.pa.gov/content/dam/copapwp-pagov/en/dos/resources/voting-and-elections/campaign-finance/campaign-finance-data"
FILER_COLS = ["CampaignfinanceID", "FILERID", "EYEAR", "SubmittedDate", "CYCLE", "AMMEND", "TERMINATE", "FILERTYPE",
              "FILERNAME", "OFFICE", "DISTRICT", "PARTY", "ADDRESS1", "ADDRESS2", "CITY", "STATE", "ZIPCODE",
              "COUNTY", "PHONE", "BEGINNING", "MONETARY", "INKIND"]
CONTRIB_COLS = ["CampaignFinanceID", "FilerID", "EYEAR", "SubmittedDate", "CYCLE", "Section", "CONTRIBUTOR",
                "ADDRESS1", "ADDRESS2", "CITY", "STATE", "ZIPCODE", "OCCUPATION", "ENAME", "EADDRESS1", "EADDRESS2",
                "ECITY", "ESTATE", "EZIPCODE", "CONTDATE1", "CONTAMT1", "CONTDATE2", "CONTAMT2", "CONTDATE3",
                "CONTAMT3", "CONTDESC"]
# PA has no individual/entity flag; classify by name. Documented as a heuristic in CAVEATS.
ORG_PATTERN = (r"\b(PAC|P\.A\.C|COMMITTEE|CMTE|COMM|INC|LLC|LLP|LP|CORP|CORPORATION|COMPANY|CO|UNION|LOCAL|FUND|"
               r"PARTY|ASSOCIATION|ASSN|ASSOC|COUNCIL|FEDERATION|FRIENDS OF|CITIZENS FOR|PEOPLE FOR|DEMOCRATIC|"
               r"REPUBLICAN|CAUCUS|TRUST|GROUP|PARTNERS|ENTERPRISES|INDUSTRIES|BANK|FOUNDATION|CLUB|SOCIETY)\b")
FILER_TYPES = {"1": "candidate", "2": "committee", "3": "lobbyist"}

# ---------- sync ----------

def _dirs() -> tuple[Path, Path]:
    d = state_dir(STATE)
    p = d / "parquet"
    p.mkdir(parents=True, exist_ok=True)
    return d, p


def _member(zf: zipfile.ZipFile, prefix: str) -> str:
    for n in zf.namelist():
        if Path(n).name.lower().startswith(prefix) and n.lower().endswith(".txt"):
            return n
    raise RuntimeError(f"no {prefix}*.txt in zip")


def build_year(zip_path: Path, year: int) -> dict:
    d, p = _dirs()
    con = duckdb.connect()
    stats = {}
    with zipfile.ZipFile(zip_path) as zf, tempdir(d) as tmp:
        for kind, cols in (("filer", FILER_COLS), ("contrib", CONTRIB_COLS)):
            clean = Path(tmp) / f"{kind}.csv"
            with zf.open(_member(zf, kind)) as src:
                stats[kind] = to_clean_csv(io.BufferedReader(src), clean, len(cols))
            spec = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c in cols) + "}"
            con.execute(f"COPY (SELECT *, row_number() OVER () AS line_no FROM read_csv({sql_str(clean)}, "
                        f"header=false, quote='\"', escape='\"', columns={spec})) "
                        f"TO {sql_str(p / f'{kind}_{year}.parquet')} (FORMAT parquet, COMPRESSION zstd)")
    con.close()
    return stats


def sync(years: list[int] | None = None, force: bool = False) -> dict:
    """Download yearly exports (default: last 4 years) when upstream Last-Modified changes."""
    d, p = _dirs()
    years = years or list(range(date.today().year - 3, date.today().year + 1))
    m = read_manifest(d)
    out = []
    for y in years:
        url = f"{BASE}/{y}.zip"
        try:
            lm = remote_last_modified(url)
        except Exception:
            out.append({"year": y, "status": "not published"})
            continue
        if not force and m.get(str(y), {}).get("last_modified") == lm and (p / f"contrib_{y}.parquet").exists():
            out.append({"year": y, "status": "current"})
            continue
        zip_path = d / f"{y}.zip"
        download(url, zip_path)
        stats = build_year(zip_path, y)
        zip_path.unlink()
        m[str(y)] = {"last_modified": lm, **stats}
        out.append({"year": y, "status": "updated", **stats})
    write_manifest(d, m)
    return {"state": STATE, "years": out}


def synced_years() -> list[int]:
    _, p = _dirs()
    return sorted(int(re.search(r"(\d{4})", f.name).group(1)) for f in p.glob("contrib_*.parquet"))


def is_synced() -> bool:
    return bool(synced_years())


def _glob(kind: str) -> str:
    _, p = _dirs()
    return sql_str(p / f"{kind}_*.parquet")


# ---------- search / load ----------

def search(name: str) -> list[dict]:
    """Candidate and committee filers whose name matches. Money is usually under the committee's ID."""
    if not is_synced():
        return [{"source": SOURCE, "error": "Pennsylvania data not synced. Run sync_state('PA') first."}]
    words = [w for w in re.split(r"[\s,]+", name.strip()) if w]
    where = " AND ".join("FILERNAME ILIKE ?" for _ in words)
    rows = duckdb.connect().execute(f"""
        SELECT FILERID, arg_max(trim(FILERNAME), SubmittedDate) AS name, any_value(FILERTYPE) AS t,
               arg_max(OFFICE, SubmittedDate) AS office, arg_max(DISTRICT, SubmittedDate) AS district,
               arg_max(PARTY, SubmittedDate) AS party,
               list_sort(list_distinct(list(EYEAR::INTEGER))) AS years, count(*) AS reports
        FROM read_parquet({_glob('filer')})
        WHERE FILERTYPE IN ('1', '2') AND {where}
        GROUP BY FILERID ORDER BY reports DESC LIMIT 25""", [f"%{w}%" for w in words]).fetchall()
    return [{"source": SOURCE, "state": STATE, "filer_id": r[0], "name": r[1], "filer_type": FILER_TYPES.get(r[2], r[2]),
             "office": " ".join(x for x in (r[3], r[4]) if x and x != "-1") or None, "party": r[5] or None,
             "years": r[6], "reports": r[7]}
            for r in rows]


def load(con, profile: dict) -> dict:
    ids = (profile["ids"].get("state", {}).get(STATE) or {}).get("filer_ids") or []
    if not ids:
        return {"source": SOURCE, "rows": 0, "note": "no PA filer_ids in profile"}
    if not is_synced():
        return {"source": SOURCE, "rows": 0, "note": "Pennsylvania data not synced; run sync_state('PA')"}
    ph = ", ".join("?" for _ in ids)
    con.execute("DELETE FROM contributions WHERE candidate_slug=? AND source=?", [profile["slug"], SOURCE])
    sql = f"""
        WITH latest AS (   -- newest submission of each report period; drops superseded originals
            SELECT arg_max(CampaignfinanceID, SubmittedDate) AS report_id, any_value(FILERNAME) AS filer_name
            FROM read_parquet({_glob('filer')}) WHERE FILERID IN ({ph})
            GROUP BY FILERID, EYEAR, CYCLE
        ),
        c AS (
            SELECT c.*, l.filer_name FROM read_parquet({_glob('contrib')}) c
            JOIN latest l ON c.CampaignFinanceID = l.report_id
            WHERE c.FilerID IN ({ph})
        ),
        u AS (
            SELECT *, 1 AS slot, CONTDATE1 AS dt, CONTAMT1 AS amt FROM c
            UNION ALL SELECT *, 2, CONTDATE2, CONTAMT2 FROM c
            UNION ALL SELECT *, 3, CONTDATE3, CONTAMT3 FROM c
        )
        SELECT ? AS candidate_slug, '{SOURCE}', '{STATE}', FilerID, trim(filer_name),
               concat_ws('-', EYEAR, line_no, slot),  -- PA has no contribution ID; identical rows are real
               try_strptime(dt, '%Y%m%d')::DATE AS d, try_cast(amt AS DOUBLE),
               trim(CONTRIBUTOR),
               CASE WHEN Section IN ('IA', 'IC') THEN 'organization'          -- from political committees
                    WHEN regexp_matches(upper(CONTRIBUTOR), ?) THEN 'organization'
                    ELSE 'individual' END,
               trim(CITY), upper(trim(STATE)), left(trim(ZIPCODE), 5),
               nullif(trim(ENAME), ''), nullif(trim(OCCUPATION), ''), false,
               (coalesce(year(d), EYEAR::INTEGER) + coalesce(year(d), EYEAR::INTEGER) % 2)::INTEGER
        FROM u WHERE try_cast(amt AS DOUBLE) <> 0
    """
    n = store.upsert_from_query(con, sql, [*ids, *ids, profile["slug"], ORG_PATTERN])
    return {"source": SOURCE, "rows": n, "filer_ids": ids, "years_available": synced_years()}
