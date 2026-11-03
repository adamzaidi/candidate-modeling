"""Georgia Government Transparency & Campaign Finance Commission, via PeachFile (the 2025+ e-filing system).

Public site: https://efile.ethics.ga.gov/  (Angular app; data comes from the JSON API below)
API:  POST https://api-peachfile.ethics.ga.gov/api/PublicFiledReportAndDownload/GetDataDownloadDetails
           {"PageSize": 50, "PageNumber": 1}  -> list of bulk files + dataExtractedDate (refreshed daily)
      POST https://api-peachfile.ethics.ga.gov/api/ExportPublicData/GetExportPublicDownloadData
           {"FilingYear": 2026, "TransactionTypeCode": "TCON", "Type": "CSV"} -> contributions CSV

Coverage: PeachFile holds filings from 2025 on. Earlier Georgia data (2017-2024) lives in the legacy
system and is NOT covered here.

Format quirks handled (verified on 2026: 593,951 rows, 0 unparseable):
- Unescaped quotes inside quoted fields ("Barton, Madison "Matt" F.") -> shared tolerant parser.
- Amounts are "$1,000.00", returns are "($2,000.00)"; ZIPs are Excel-guarded ="30342-3429".
- Loans, loan payments, and interest share the file; only Contribution / Return Contribution rows load.
- Amended reports repeat transactions; the most recently filed copy of each transaction wins.
- Unitemized contributions are lump-sum rows with no donor -> contributor_type 'unitemized'.
"""
import re
from datetime import date
from pathlib import Path

import duckdb
import requests

from ...config import USER_AGENT, state_dir
from ... import store
from ._common import case_map, read_manifest, sql_str, tempdir, to_clean_csv, write_manifest

STATE = "GA"
SOURCE = "ga_gtcfc"
ID_KEY = "filer_ids"        # profile ids.state.<STATE>.<ID_KEY>
RESULT_ID = "filer_id"     # field in search() results holding that ID
API = "https://api-peachfile.ethics.ga.gov/api"
FIRST_YEAR = 2025

COLUMNS = ["filing_entity_id", "filing_entity_name", "committee_name", "registration_type", "registration_subtype",
           "transaction_id", "transaction_type", "transaction_subtype", "financial_institution", "contributor_type",
           "contributor_last", "contributor_first", "contributor_middle", "contributor_country", "contributor_addr1",
           "contributor_addr2", "contributor_city", "contributor_state", "contributor_zip", "fiduciary_relationship",
           "fiduciary_details", "loan_resp_last", "loan_resp_first", "loan_resp_middle", "loan_resp_addr1",
           "loan_resp_addr2", "loan_resp_city", "loan_resp_state", "loan_resp_zip", "occupation", "employer",
           "regulated_entity", "regulated_entity_name", "transaction_date", "transaction_amount", "election_type",
           "election_year", "transaction_description", "amended", "timed_report_name", "timed_report_filed_date",
           "report_name", "report_filed_date"]
INDIVIDUAL_TYPES = ("Individual", "Self", "Spouse / Dependent Child", "Lobbyist")
_HEADERS = {"User-Agent": USER_AGENT, "Content-Type": "application/json", "Origin": "https://efile.ethics.ga.gov"}


def _dirs() -> tuple[Path, Path]:
    d = state_dir(STATE)
    p = d / "parquet"
    p.mkdir(parents=True, exist_ok=True)
    return d, p


def extracted_date() -> str | None:
    r = requests.post(f"{API}/PublicFiledReportAndDownload/GetDataDownloadDetails",
                      json={"PageSize": 50, "PageNumber": 1}, headers=_HEADERS, timeout=60)
    r.raise_for_status()
    for item in r.json()["data"]["items"]:
        if item.get("transactionTypeCode") == "TCON":
            return item.get("dataExtractedDate")
    return None


def _download_year(year: int, dest: Path) -> bool:
    """Returns False if the year has no data (the API returns a header-only file)."""
    with requests.post(f"{API}/ExportPublicData/GetExportPublicDownloadData",
                       json={"FilingYear": year, "TransactionTypeCode": "TCON", "Type": "CSV"},
                       headers=_HEADERS, stream=True, timeout=600) as r:
        if r.status_code == 404:
            return False
        r.raise_for_status()
        with open(dest, "wb") as f:
            for block in r.iter_content(1 << 20):
                f.write(block)
    with open(dest, "rb") as f:
        f.readline()
        return bool(f.readline().strip())


def build_year(csv_path: Path, year: int) -> dict:
    _, p = _dirs()
    d, _ = _dirs()
    with tempdir(d) as tmp, open(csv_path, "rb") as src:
        clean = Path(tmp) / "clean.csv"
        stats = to_clean_csv(src, clean, len(COLUMNS))
        spec = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c in COLUMNS) + "}"
        con = duckdb.connect()
        con.execute(f"COPY (SELECT * FROM read_csv({sql_str(clean)}, header=false, quote='\"', escape='\"', "
                    f"columns={spec})) TO {sql_str(p / f'contributions_{year}.parquet')} (FORMAT parquet, COMPRESSION zstd)")
        con.close()
    return stats


def sync(years: list[int] | None = None, force: bool = False) -> dict:
    """Download contribution files for `years` (default: every PeachFile year through this one)."""
    d, p = _dirs()
    years = years or list(range(FIRST_YEAR, date.today().year + 1))
    m = read_manifest(d)
    stamp = extracted_date()
    out = []
    for y in years:
        if y < FIRST_YEAR:
            out.append({"year": y, "status": "not covered (pre-2025 legacy system)"})
            continue
        if not force and m.get(str(y), {}).get("extracted") == stamp and (p / f"contributions_{y}.parquet").exists():
            out.append({"year": y, "status": "current"})
            continue
        raw = d / f"contributions_{y}.csv"
        if not _download_year(y, raw):
            raw.unlink(missing_ok=True)
            out.append({"year": y, "status": "not published"})
            continue
        stats = build_year(raw, y)
        raw.unlink()
        m[str(y)] = {"extracted": stamp, **stats}
        out.append({"year": y, "status": "updated", **stats})
    try:
        m["candidates"] = fetch_candidates()
    except Exception as e:  # office lookup is an enrichment; don't fail the money sync over it
        m["candidates_error"] = str(e)
    write_manifest(d, m)
    return {"state": STATE, "data_extracted": stamp, "years": out, "candidates": m.get("candidates")}


def synced_years() -> list[int]:
    _, p = _dirs()
    return sorted(int(re.search(r"(\d{4})", f.name).group(1)) for f in p.glob("contributions_*.parquet"))


def is_synced() -> bool:
    return bool(synced_years())


def _glob() -> str:
    _, p = _dirs()
    return sql_str(p / "contributions_*.parquet")


def search(name: str) -> list[dict]:
    """Filing entities (candidates and committees) whose entity or committee name matches."""
    if not is_synced():
        return [{"source": SOURCE, "error": "Georgia data not synced. Run sync_state('GA') first."}]
    words = [w for w in re.split(r"[\s,]+", name.strip()) if w]
    where = " AND ".join("concat_ws(' ', filing_entity_name, committee_name) ILIKE ?" for _ in words)
    rows = duckdb.connect().execute(f"""
        SELECT filing_entity_id, any_value(filing_entity_name), any_value(committee_name),
               any_value(registration_type), count(*) AS n,
               list_sort(list_distinct(list(right(transaction_date, 4)))) AS years
        FROM read_parquet({_glob()}) WHERE {where}
        GROUP BY filing_entity_id ORDER BY n DESC LIMIT 25""", [f"%{w}%" for w in words]).fetchall()
    return [{"source": SOURCE, "state": STATE, "filer_id": r[0], "name": r[1], "committee": r[2],
             "registration_type": r[3], "rows": r[4], "years": [int(y) for y in r[5] if y and y.isdigit()]}
            for r in rows]


SCOPE = {
    "name": "Georgia Government Transparency & Campaign Finance Commission (PeachFile)",
    "url": "https://efile.ethics.ga.gov/",
    "covers": "State candidates and non-candidate committees filing in PeachFile.",
    "excludes": ["Federal candidates (use FEC)", "Filings in the legacy system (2017-2024)"],
    "history_from": "January 2025",
    "refresh": "Daily extract via the PeachFile API; sync skips when dataExtractedDate is unchanged.",
    "rules": [
        "Only Contribution and Return Contribution rows load; loans, loan payments, and interest are excluded.",
        "Returned contributions are negative amounts.",
        "Amended reports repeat transactions; the most recently filed copy wins.",
        "Unitemized contributions are lump sums typed 'unitemized' (no donor, excluded from geography shares).",
        "Offices come from the PeachFile candidate registry (GetCandidateDetails), joined on filing entity ID.",
    ],
    "known_gaps": [
        "Nothing before 2025: the 2025 election window (from Nov 2024) is only partly covered.",
        "Some prominent filers (e.g. Raffensperger, Bottoms) are absent from the bulk export; cause unknown.",
        "Leadership committees (e.g. WBJ Leadership Committee, Georgians First) register as non-candidate committees "
        "and are classified 'pac', not under the office of the official they are tied to.",
    ],
}


def covered_years() -> list[int]:
    return [y for y in synced_years() if y >= FIRST_YEAR]


def normalized_sql(ids: list[str] | None = None, slug: str | None = None) -> tuple[str, list]:
    ind = ", ".join(f"'{t}'" for t in INDIVIDUAL_TYPES)
    flt = f"filing_entity_id IN ({', '.join('?' for _ in ids)}) AND " if ids is not None else ""
    sql = f"""
        WITH t AS (
            SELECT *,
                   CASE WHEN transaction_amount LIKE '(%' THEN -1 ELSE 1 END
                     * try_cast(regexp_replace(transaction_amount, '[$,()]', '', 'g') AS DOUBLE) AS amt,
                   try_strptime(transaction_date, '%m/%d/%Y')::DATE AS d
            FROM read_parquet({_glob()})
            WHERE {flt}transaction_type IN ('Contribution', 'Return Contribution')
            QUALIFY row_number() OVER (PARTITION BY filing_entity_id, transaction_id
                                       ORDER BY try_strptime(report_filed_date, '%m/%d/%Y') DESC NULLS LAST) = 1
        )
        SELECT ? AS candidate_slug, '{SOURCE}', '{STATE}', filing_entity_id,
               coalesce(nullif(committee_name, ''), filing_entity_name),
               concat_ws('-', filing_entity_id, transaction_id), d, amt,
               nullif(trim(concat_ws(', ', nullif(trim(contributor_last), ''), nullif(trim(contributor_first), ''))), ''),
               CASE WHEN transaction_subtype = 'Unitemized Contribution' OR contributor_type IS NULL THEN 'unitemized'
                    WHEN contributor_type IN ({ind}) THEN 'individual' ELSE 'organization' END,
               nullif(trim(contributor_city), ''), upper(nullif(trim(contributor_state), '')),
               nullif(left(regexp_replace(contributor_zip, '[="]', '', 'g'), 5), ''),
               nullif(trim(employer), ''), nullif(trim(occupation), ''), false,
               (year(d) + year(d) % 2)::INTEGER
        FROM t
    """
    return sql, [*(ids or []), slug]


def load(con, profile: dict) -> dict:
    ids = (profile["ids"].get("state", {}).get(STATE) or {}).get(ID_KEY) or []
    if not ids:
        return {"source": SOURCE, "rows": 0, "note": "no GA filer_ids in profile"}
    if not is_synced():
        return {"source": SOURCE, "rows": 0, "note": "Georgia data not synced; run sync_state('GA')"}
    sql, params = normalized_sql(ids, profile["slug"])
    m = store.merge_query(con, profile["slug"], SOURCE, sql, params)  # full snapshot: absent rows -> removed
    return {"source": SOURCE, "rows": m["staged"], "changes": m, ID_KEY: ids, "covered_years": covered_years()}


# ---------- committees / offices (rule office-taxonomy) ----------

OFFICE_MAP = {
    "Governor": "governor", "Lieutenant Governor": "lieutenant_governor", "Attorney General": "attorney_general",
    "Secretary of State": "secretary_of_state",
    "State School Superintendent": "statewide_other", "Commissioner of Insurance": "statewide_other",
    "Commissioner of Labor": "statewide_other", "Commissioner of Agriculture": "statewide_other",
    "Public Service Commissioner": "statewide_other",
    "State Senator": "state_senate", "State Representative": "state_house",
    "Justice of the Supreme Court": "supreme_court", "Court of Appeals Judge": "appellate_court",
    "Superior Court Judge": "trial_court", "State Court Judge": "trial_court", "Probate Court Judge": "trial_court",
    "Chief Magistrate Judge": "trial_court",
    "District Attorney": "prosecutor", "Solicitor General": "prosecutor",
    "County Commissioner": "county_office", "County Commission - Chair": "county_office", "Sheriff": "county_office",
    "Clerk of Superior Court": "county_office", "Tax Commissioner": "county_office", "Coroner": "county_office",
    "City Councilperson": "municipal_office", "Mayor": "municipal_office", "Mayor Pro Tempore": "municipal_office",
    "Alderman": "municipal_office", "Water Board": "municipal_office",
    "School Board Member": "school_board",
}
CANDIDATES_FILE = "candidates.parquet"


def fetch_candidates(page_size: int = 100) -> int:
    """PeachFile candidate registry (office, district) -> parquet. Page size >100 trips the site's WAF."""
    import json
    import time

    _, p = _dirs()
    items, page = [], 1
    while True:
        r = requests.post(f"{API}/PublicFilerDetails/GetCandidateDetails",
                          json={"PageSize": page_size, "PageNumber": page}, headers=_HEADERS, timeout=60)
        r.raise_for_status()
        d = r.json()["data"]
        items += d["items"]
        if not d["items"] or len(items) >= d["totalItems"]:
            break
        page += 1
        time.sleep(0.3)
    keep = ["filerEntityId", "filerName", "office", "districtType", "districtName", "electionCycleId",
            "electionCycleName", "partyAffiliation"]
    rows = [{k: i.get(k) for k in keep} for i in items]
    d, _ = _dirs()
    tmp = d / "candidates.json"
    tmp.write_text(json.dumps(rows))
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_json({sql_str(tmp)}, columns={{'filerEntityId': 'VARCHAR', "
                "'filerName': 'VARCHAR', 'office': 'VARCHAR', 'districtType': 'VARCHAR', 'districtName': 'VARCHAR', "
                "'electionCycleId': 'BIGINT', 'electionCycleName': 'VARCHAR', 'partyAffiliation': 'VARCHAR'})) "
                f"TO {sql_str(p / CANDIDATES_FILE)} (FORMAT parquet)")
    con.close()
    tmp.unlink()
    return len(rows)


def committees_sql() -> str:
    _, p = _dirs()
    cand = p / CANDIDATES_FILE
    office_join = (f"""LEFT JOIN (SELECT filerEntityId, arg_max(office, electionCycleId) AS office,
                          arg_max(districtName, electionCycleId) AS district
                   FROM read_parquet({sql_str(cand)}) GROUP BY filerEntityId) o ON o.filerEntityId = f.committee_id"""
                   if cand.exists() else "LEFT JOIN (SELECT NULL AS filerEntityId, NULL AS office, NULL AS district) o ON false")
    return f"""
        WITH f AS (
            SELECT filing_entity_id AS committee_id,
                   any_value(coalesce(nullif(committee_name, ''), filing_entity_name)) AS committee_name,
                   any_value(registration_type) AS rtype
            FROM read_parquet({_glob()}) GROUP BY filing_entity_id)
        SELECT f.committee_id, f.committee_name,
               CASE WHEN rtype LIKE 'Candidate%' THEN 'candidate' WHEN rtype = 'Non-Candidate Committee' THEN 'pac'
                    ELSE 'other_committee' END AS kind,
               o.office AS office_raw,
               CASE WHEN rtype LIKE 'Candidate%' THEN {case_map("o.office", OFFICE_MAP)}
                    WHEN rtype = 'Non-Candidate Committee' THEN 'pac' ELSE 'other_committee' END AS office_category,
               o.district
        FROM f {office_join}"""
