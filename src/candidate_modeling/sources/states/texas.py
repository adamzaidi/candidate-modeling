"""Texas Ethics Commission bulk campaign finance (state and local candidates, PACs).

Source page: https://www.ethics.state.tx.us/search/cf/
Layout:      https://www.ethics.state.tx.us/data/search/cf/CFS-ReadMe.txt
The full database is one ~1 GB zip (~8 GB unzipped), refreshed daily. `sync()` downloads it
and converts filers + contributions to parquet once, so later lookups for any filer are fast.

Rules applied:
- Only contribs_##.csv are loaded. cont_ss / cont_t (special-session and daily pre-election
  reports) are re-reported on the next regular report, so loading them would double count.
- Rows with infoOnlyFlag = 'Y' were superseded by an amended report and are dropped.
"""
import re
import zipfile
from pathlib import Path

import duckdb

from ...config import state_dir
from ... import store
from ._common import case_map, download, extract_member, read_manifest, remote_last_modified, sql_str, tempdir, write_manifest

STATE = "TX"
SOURCE = "tx_tec"
ID_KEY = "filer_ids"        # profile ids.state.<STATE>.<ID_KEY>
RESULT_ID = "filer_id"     # field in search() results holding that ID
ZIP_URL = "https://prd.tecprd.ethicsefile.com/public/cf/public/TEC_CF_CSV.zip"
CONTRIB_FILE = re.compile(r"^contribs_\d+\.csv$")

FILER_COLS = ["filerIdent", "filerTypeCd", "filerName", "filerPersentTypeCd", "filerNameLast", "filerNameFirst",
              "ctaSeekOfficeCd", "ctaSeekOfficeDistrict", "filerHoldOfficeCd", "filerHoldOfficeDistrict",
              "filerFilerpersStatusCd", "filerEffStartDt", "filerEffStopDt"]
SPAC_COLS = ["spacFilerIdent", "spacFilerTypeCd", "spacFilerName", "spacCommitteeStatusCd", "spacPositionCd",
             "candidateFilerIdent", "candidateFilerName", "candidateHoldOfficeCd", "candidateHoldOfficeDistrict",
             "candidateSeekOfficeCd", "candidateSeekOfficeDistrict"]
CONTRIB_COLS = ["reportInfoIdent", "infoOnlyFlag", "filerIdent", "filerTypeCd", "filerName", "contributionInfoId",
                "contributionDt", "contributionAmount", "contributorPersentTypeCd", "contributorNameOrganization",
                "contributorNameLast", "contributorNameFirst", "contributorStreetCity", "contributorStreetStateCd",
                "contributorStreetPostalCode", "contributorEmployer", "contributorOccupation"]


def _dirs():
    d = state_dir(STATE)
    parts = d / "parquet" / "contribs"
    parts.mkdir(parents=True, exist_ok=True)
    return d, parts


def _csv(path) -> str:
    return f"read_csv({sql_str(path)}, header=true, all_varchar=true, quote='\"', escape='\"', strict_mode=false)"


def build_parquet(zip_path, out_dir=None) -> dict:
    """Convert the TEC zip's filers + contribution files to parquet, one member at a time."""
    d, parts = _dirs()
    if out_dir:
        parts = out_dir / "contribs"
        parts.mkdir(parents=True, exist_ok=True)
    filers_out = parts.parent / "filers.parquet"
    con = duckdb.connect()
    n_files = 0
    with zipfile.ZipFile(zip_path) as zf, tempdir(d) as tmp:
        names = zf.namelist()
        tmp_path = Path(tmp)
        f = extract_member(zf, "filers.csv", tmp_path)
        con.execute(f"COPY (SELECT {', '.join(FILER_COLS)} FROM {_csv(f)}) TO {sql_str(filers_out)} (FORMAT parquet)")
        f.unlink()
        if "spacs.csv" in names:  # SPAC -> supported candidate links (office for candidate-run SPACs)
            f = extract_member(zf, "spacs.csv", tmp_path)
            con.execute(f"COPY (SELECT {', '.join(SPAC_COLS)} FROM {_csv(f)}) "
                        f"TO {sql_str(parts.parent / 'spacs.parquet')} (FORMAT parquet)")
            f.unlink()
        for name in sorted(n for n in names if CONTRIB_FILE.match(n)):
            f = extract_member(zf, name, tmp_path)
            out = parts / (f.stem + ".parquet")
            con.execute(f"COPY (SELECT {', '.join(CONTRIB_COLS)} FROM {_csv(f)}) TO {sql_str(out)} "
                        "(FORMAT parquet, COMPRESSION zstd)")
            f.unlink()
            n_files += 1
    con.close()
    return {"contrib_files": n_files, "filers": str(filers_out)}


def sync(force: bool = False, keep_zip: bool = False) -> dict:
    """Download the TEC database if it changed upstream, rebuild parquet, then delete the 1 GB zip."""
    d, _ = _dirs()
    m = read_manifest(d)
    zip_path = d / "TEC_CF_CSV.zip"
    remote = remote_last_modified(ZIP_URL)
    if not force and m.get("parquet_built_from") == remote and is_synced():
        return {"state": STATE, "status": "current", "last_modified": remote, **m.get("build", {})}
    # reuse a zip already on disk only if it is the current upstream version (or unknown, e.g. downloaded by hand)
    if force or not zip_path.exists() or m.get("zip_last_modified") not in (None, remote):
        download(ZIP_URL, zip_path)
    m["zip_last_modified"] = remote
    m["build"] = build_parquet(zip_path)
    m["parquet_built_from"] = remote
    if not keep_zip:
        zip_path.unlink()
    write_manifest(d, m)
    return {"state": STATE, "status": "updated", "last_modified": remote, **m["build"]}


def is_synced() -> bool:
    d, parts = _dirs()
    return (parts.parent / "filers.parquet").exists() and any(parts.glob("*.parquet"))


def _filers_path():
    _, parts = _dirs()
    return parts.parent / "filers.parquet"


def search(name: str) -> list[dict]:
    """Candidate/officeholder filers whose name matches (any word order)."""
    if not is_synced():
        return [{"source": SOURCE, "error": "Texas data not synced. Run sync_state('TX') first (~1 GB download)."}]
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    where = " AND ".join("filerName ILIKE ?" for _ in words)
    rows = duckdb.connect().execute(
        f"SELECT * FROM read_parquet({sql_str(_filers_path())}) WHERE filerTypeCd LIKE '%COH%' AND {where} "
        "ORDER BY filerEffStartDt DESC NULLS LAST LIMIT 25",
        [f"%{w}%" for w in words],
    ).fetchdf()
    return [
        {"source": SOURCE, "state": STATE, "filer_id": r.filerIdent, "name": r.filerName, "filer_type": r.filerTypeCd,
         "office_sought": f"{r.ctaSeekOfficeCd or ''} {r.ctaSeekOfficeDistrict or ''}".strip() or None,
         "office_held": f"{r.filerHoldOfficeCd or ''} {r.filerHoldOfficeDistrict or ''}".strip() or None,
         "status": r.filerFilerpersStatusCd}
        for r in rows.itertuples()
    ]


SCOPE = {
    "name": "Texas Ethics Commission (TEC)",
    "url": "https://www.ethics.state.tx.us/search/cf/",
    "covers": "State and some local candidates, officeholders, and PACs that file with the TEC.",
    "excludes": ["Federal candidates (use FEC)", "Local filers who file only with a city or county"],
    "history_from": "July 2000 (electronic filings)",
    "refresh": "Daily full-database zip; sync re-downloads only when Last-Modified changes.",
    "rules": [
        "Only contribs_##.csv load; cont_ss/cont_t (special-session, daily pre-election) are re-reported later.",
        "Rows with infoOnlyFlag = 'Y' were superseded by an amended report and are dropped.",
        "Candidates file under their own filer ID (filerTypeCd COH), and often also raise through a specific-purpose "
        "committee linked in the TEC SPAC index (rule linked-committees).",
        "Offices come from filers.csv (office sought, else held) and spacs.csv links.",
    ],
    "known_gaps": ["Paper-era filings before July 2000 are not in the database."],
}


def covered_years() -> list[int]:
    """Calendar years fully present once synced (the TEC zip is the whole database)."""
    from datetime import date
    return list(range(2001, date.today().year + 1)) if is_synced() else []


def normalized_sql(ids: list[str] | None = None, slug: str | None = None) -> tuple[str, list]:
    """SELECT yielding store.COLUMN_NAMES for the given filers (None = every filer in Texas)."""
    _, parts = _dirs()
    where, params = "coalesce(infoOnlyFlag, 'N') <> 'Y'", [slug]
    if ids is not None:
        where += f" AND filerIdent IN ({', '.join('?' for _ in ids)})"
        params += list(ids)
    sql = f"""
        SELECT ? AS candidate_slug, '{SOURCE}', '{STATE}', filerIdent, filerName, contributionInfoId,
               try_strptime(contributionDt, '%Y%m%d')::DATE AS d,
               try_cast(contributionAmount AS DOUBLE),
               CASE WHEN contributorPersentTypeCd = 'INDIVIDUAL'
                    THEN trim(concat_ws(', ', contributorNameLast, contributorNameFirst))
                    ELSE contributorNameOrganization END,
               CASE WHEN contributorPersentTypeCd = 'INDIVIDUAL' THEN 'individual' ELSE 'organization' END,
               contributorStreetCity, upper(contributorStreetStateCd), left(contributorStreetPostalCode, 5),
               contributorEmployer, contributorOccupation, false,
               (year(d) + year(d) % 2)::INTEGER
        FROM read_parquet({sql_str(parts / '*.parquet')})
        WHERE {where}
    """
    return sql, params


def load(con, profile: dict) -> dict:
    ids = (profile["ids"].get("state", {}).get(STATE) or {}).get(ID_KEY) or []
    if not ids:
        return {"source": SOURCE, "rows": 0, "note": "no TX filer_ids in profile"}
    if not is_synced():
        return {"source": SOURCE, "rows": 0, "note": "Texas data not synced; run sync_state('TX')"}
    sql, params = normalized_sql(ids, profile["slug"])
    m = store.merge_query(con, profile["slug"], SOURCE, sql, params)  # full snapshot: absent rows -> removed
    return {"source": SOURCE, "rows": m["staged"], "changes": m, ID_KEY: ids, "covered_years": covered_years()}


# ---------- committees / offices (rule office-taxonomy) ----------

OFFICE_MAP = {
    "GOVERNOR": "governor", "LTGOVERNOR": "lieutenant_governor", "ATTYGEN": "attorney_general", "SOS": "secretary_of_state",
    "COMPTROLLER": "statewide_other", "LANDCOMM": "statewide_other", "AGRICULTUR": "statewide_other",
    "RRCOMM": "statewide_other", "RRCOMM_UNEXPIRED": "statewide_other",
    "STATESEN": "state_senate", "STATEREP": "state_house", "STATEEDU": "state_board",
    "JUSTICE_SC": "supreme_court", "CHIEFJUSTICE_SC": "supreme_court",
    "JUDGE_COCA": "supreme_court", "PRESIDINGJUDGE_COCA": "supreme_court",   # Court of Criminal Appeals: last resort
    "JUSTICE_COA": "appellate_court", "CHIEFJUSTICE_COA": "appellate_court",
    "JUDGEDIST": "trial_court", "JUDGEDIST_MULTI": "trial_court", "JUDGEDIST_FAMILY": "trial_court",
    "JUDGE_BUS": "trial_court", "CRIMINAL_JUDGEDIST": "trial_court", "CRIMINAL_JUDGEDIST_DAL": "trial_court",
    "CRIMINAL_JUDGEDIST_JEF": "trial_court", "CRIMINAL_JUDGEDIST_TAR": "trial_court", "JUDGESTATCO": "trial_court",
    "JUDGE_SENIOR": "trial_court",
    "DISTATTY": "prosecutor", "DISTATTY_MULTI": "prosecutor", "CRIMINAL_DISTATTY": "prosecutor",
    "PARTYCHAIRCO": "party_office", "STATE_CHAIR": "party_office",
}
KIND_MAP = {"COH": "candidate", "JCOH": "candidate", "SCC": "candidate", "SPK": "candidate",
            "GPAC": "pac", "SPAC": "pac", "MPAC": "pac", "ASIFSPAC": "pac", "JSPC": "pac", "SCPC": "pac",
            "CEC": "party_committee", "MCEC": "party_committee", "PTYCORP": "party_committee"}


def committees_sql() -> str:
    """One row per filer: committee_id, committee_name, kind, office_raw, office_category, district, linked_candidate_id.

    Texas candidates often raise through a specific-purpose committee (e.g. "Texans for Greg Abbott") that has no
    office of its own. The TEC SPAC index links SPACs to the candidates they SUPPORT/ASSIST/OPPOSE; a SPAC that
    supports or assists exactly one candidate is treated as that candidate's committee (rule linked-committees).
    """
    _, parts = _dirs()
    f = sql_str(parts.parent / "filers.parquet")
    spacs = parts.parent / "spacs.parquet"
    links = (f"""SELECT spacFilerIdent AS committee_id, count(DISTINCT candidateFilerIdent) AS n,
                       any_value(candidateFilerIdent) AS cand,
                       any_value(coalesce(nullif(candidateSeekOfficeCd, ''), nullif(candidateHoldOfficeCd, ''))) AS cand_office,
                       any_value(coalesce(nullif(candidateSeekOfficeDistrict, ''), nullif(candidateHoldOfficeDistrict, ''))) AS cand_district
                FROM read_parquet({sql_str(spacs)}) WHERE spacPositionCd IN ('SUPPORT', 'ASSIST') GROUP BY 1"""
             if spacs.exists() else
             "SELECT NULL::VARCHAR AS committee_id, 0 AS n, NULL AS cand, NULL AS cand_office, NULL AS cand_district WHERE false")
    return f"""
        WITH f AS (
            SELECT filerIdent AS committee_id,
                   arg_max(filerName, coalesce(filerEffStartDt, '')) AS committee_name,
                   arg_max(filerTypeCd, coalesce(filerEffStartDt, '')) AS ftype,
                   arg_max(coalesce(nullif(ctaSeekOfficeCd, ''), nullif(filerHoldOfficeCd, '')),
                           coalesce(filerEffStartDt, '')) AS office_raw,
                   arg_max(coalesce(nullif(ctaSeekOfficeDistrict, ''), nullif(filerHoldOfficeDistrict, '')),
                           coalesce(filerEffStartDt, '')) AS district
            FROM read_parquet({f}) GROUP BY filerIdent),
        l AS ({links}),
        j AS (
            SELECT f.*, {case_map("f.ftype", KIND_MAP, "'other_committee'")} AS base_kind,
                   l.n, l.cand, coalesce(cf.office_raw, l.cand_office) AS cand_office,
                   coalesce(cf.district, l.cand_district) AS cand_district
            FROM f LEFT JOIN l USING (committee_id) LEFT JOIN f cf ON cf.committee_id = l.cand),
        k AS (
            SELECT committee_id, committee_name,
                   CASE WHEN base_kind = 'pac' AND n = 1 AND cand_office IS NOT NULL THEN 'candidate' ELSE base_kind END AS kind,
                   CASE WHEN base_kind = 'pac' AND n = 1 AND cand_office IS NOT NULL THEN cand_office ELSE office_raw END AS office_raw,
                   CASE WHEN base_kind = 'pac' AND n = 1 AND cand_office IS NOT NULL THEN cand_district ELSE district END AS district,
                   CASE WHEN base_kind = 'pac' AND n = 1 THEN cand END AS linked_candidate_id
            FROM j)
        SELECT committee_id, committee_name, kind, office_raw,
               CASE WHEN kind = 'candidate' THEN {case_map("office_raw", OFFICE_MAP)} ELSE kind END AS office_category,
               district, linked_candidate_id
        FROM k"""
