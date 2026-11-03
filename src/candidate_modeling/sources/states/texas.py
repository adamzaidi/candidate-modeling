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
from ._common import download, extract_member, read_manifest, remote_last_modified, sql_str, tempdir, write_manifest

STATE = "TX"
SOURCE = "tx_tec"
ID_KEY = "filer_ids"        # profile ids.state.<STATE>.<ID_KEY>
RESULT_ID = "filer_id"     # field in search() results holding that ID
ZIP_URL = "https://prd.tecprd.ethicsefile.com/public/cf/public/TEC_CF_CSV.zip"
CONTRIB_FILE = re.compile(r"^contribs_\d+\.csv$")

FILER_COLS = ["filerIdent", "filerTypeCd", "filerName", "filerPersentTypeCd", "filerNameLast", "filerNameFirst",
              "ctaSeekOfficeCd", "ctaSeekOfficeDistrict", "filerHoldOfficeCd", "filerHoldOfficeDistrict",
              "filerFilerpersStatusCd", "filerEffStartDt", "filerEffStopDt"]
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


def load(con, profile: dict) -> dict:
    filer_ids = (profile["ids"].get("state", {}).get(STATE) or {}).get("filer_ids") or []
    if not filer_ids:
        return {"source": SOURCE, "rows": 0, "note": "no TX filer_ids in profile"}
    if not is_synced():
        return {"source": SOURCE, "rows": 0, "note": "Texas data not synced; run sync_state('TX')"}
    _, parts = _dirs()
    placeholders = ", ".join("?" for _ in filer_ids)
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
        WHERE filerIdent IN ({placeholders}) AND coalesce(infoOnlyFlag, 'N') <> 'Y'
    """
    m = store.merge_query(con, profile["slug"], SOURCE, sql, [profile["slug"], *filer_ids])  # full snapshot: absent rows -> removed
    return {"source": SOURCE, "rows": m["staged"], "changes": m, "filer_ids": filer_ids}
