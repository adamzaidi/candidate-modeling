"""Bureau of Economic Analysis reference data for normalizing campaign money (no API key needed).

- State personal income, population, per capita personal income: Regional accounts table SAINC1
  (https://apps.bea.gov/regional/zip/SAINC.zip), annual, 1929 to the latest published year.
- PCE price index (series DPCERG, Fisher index, 2017 = 100): NIPA bulk files
  (https://apps.bea.gov/national/Release/TXT/NipaDataA.txt and NipaDataQ.txt). A year without an annual value yet
  uses the average of its published quarters and is flagged as partial.

BEA revises these series. Each sync records the files' Last-Modified dates as the data vintage, and results
report which vintage, income year, and price basis they used (rule bea-normalization).
"""
import csv
import io
import json
import zipfile
from pathlib import Path

import duckdb
import requests

from ..config import USER_AGENT, state_dir
from ..labels import STATE_NAMES, sql_str

SOURCE = "bea"
REGIONAL_ZIP = "https://apps.bea.gov/regional/zip/SAINC.zip"
NIPA_A = "https://apps.bea.gov/national/Release/TXT/NipaDataA.txt"
NIPA_Q = "https://apps.bea.gov/national/Release/TXT/NipaDataQ.txt"
PCE_SERIES = "DPCERG"

SCOPE = {
    "name": "Bureau of Economic Analysis (regional and national accounts)",
    "url": "https://apps.bea.gov/",
    "covers": "Reference data for normalization: state personal income, population, and per capita personal income "
              "(SAINC1); PCE price index (DPCERG) for inflation adjustment.",
    "excludes": ["Not campaign finance data; used only to put campaign money in context"],
    "history_from": "1929 (SAINC1, PCE)",
    "refresh": "Bulk files; sync re-downloads when Last-Modified changes. BEA revises past years, so the vintage "
               "is recorded with every result.",
    "rules": [
        "Real dollars use the PCE price index (2017 = 100), rebased to the latest full year with an annual value.",
        "A year with no annual PCE value uses the average of its published quarters (flagged partial).",
        "Per-capita and share-of-income figures use that year's state values, or the latest published year if the "
        "election year is not out yet (flagged).",
    ],
    "known_gaps": ["State personal income for the current year is published months after it ends."],
}


def _dir() -> Path:
    d = state_dir("BEA")
    (d / "parquet").mkdir(parents=True, exist_ok=True)
    return d


def _get(url: str) -> tuple[bytes, str | None]:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=300)
    r.raise_for_status()
    return r.content, r.headers.get("Last-Modified")


def _num(v: str) -> float | None:
    v = (v or "").replace(",", "").strip().strip('"')
    try:
        return float(v)
    except ValueError:
        return None  # BEA uses (D), (NA), (L) for suppressed / not available


def parse_sainc1(text: str) -> list[dict]:
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    years = [(i, int(h)) for i, h in enumerate(header) if h.strip().isdigit()]
    out: dict[tuple, dict] = {}
    for r in rows[1:]:
        if len(r) < len(header):
            continue  # footnote lines at the end of the file
        fips, name, line = r[0].strip().strip('"'), r[1].strip().replace(" *", ""), r[4].strip()
        code = STATE_NAMES.get(name.upper()) or ("US" if fips == "00000" else None)
        if not code or line not in ("1", "2", "3"):
            continue
        field = {"1": "personal_income_musd", "2": "population", "3": "per_capita_income"}[line]
        for i, y in years:
            rec = out.setdefault((code, y), {"state": code, "fips": fips, "year": y})
            v = _num(r[i])
            if v is not None or field not in rec:  # a suppressed (NA)/(D) cell never overwrites a real value
                rec[field] = v
    return list(out.values())


def parse_pce(annual: str, quarterly: str) -> list[dict]:
    vals: dict[int, dict] = {}
    for line in annual.splitlines():
        p = next(csv.reader([line]))
        if p and p[0] == PCE_SERIES and p[1].isdigit():
            vals[int(p[1])] = {"year": int(p[1]), "pce_index": _num(p[2]), "basis": "annual", "quarters": 4}
    q: dict[int, list[float]] = {}
    for line in quarterly.splitlines():
        p = next(csv.reader([line]))
        if p and p[0] == PCE_SERIES and "Q" in p[1]:
            y = int(p[1][:4])
            if (v := _num(p[2])) is not None:
                q.setdefault(y, []).append(v)
    for y, vs in q.items():
        if y not in vals:
            vals[y] = {"year": y, "pce_index": round(sum(vs) / len(vs), 3), "basis": f"partial ({len(vs)} quarters)",
                       "quarters": len(vs)}
    return sorted(vals.values(), key=lambda r: r["year"])


def _write(rows: list[dict], dest: Path, cols: dict[str, str]) -> None:
    tmp = dest.with_suffix(".json")
    tmp.write_text(json.dumps(rows))
    spec = "{" + ", ".join(f"'{k}': '{v}'" for k, v in cols.items()) + "}"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_json({sql_str(tmp)}, columns={spec}, format='array')) "
                f"TO {sql_str(dest)} (FORMAT parquet)")
    con.close()
    tmp.unlink()


def sync(force: bool = False) -> dict:
    d = _dir()
    mf = d / "manifest.json"
    m = json.loads(mf.read_text()) if mf.exists() else {}
    zbytes, z_lm = _get(REGIONAL_ZIP)
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        name = next(n for n in zf.namelist() if n.startswith("SAINC1__ALL_AREAS") and n.endswith(".csv"))
        income = parse_sainc1(zf.read(name).decode("utf-8", errors="replace"))
    a_bytes, a_lm = _get(NIPA_A)
    q_bytes, q_lm = _get(NIPA_Q)
    pce = parse_pce(a_bytes.decode("utf-8", errors="replace"), q_bytes.decode("utf-8", errors="replace"))
    _write(income, d / "parquet" / "state_income.parquet",
           {"state": "VARCHAR", "fips": "VARCHAR", "year": "INTEGER", "personal_income_musd": "DOUBLE",
            "population": "DOUBLE", "per_capita_income": "DOUBLE"})
    _write(pce, d / "parquet" / "pce.parquet",
           {"year": "INTEGER", "pce_index": "DOUBLE", "basis": "VARCHAR", "quarters": "INTEGER"})
    m.update({"regional_vintage": z_lm, "nipa_annual_vintage": a_lm, "nipa_quarterly_vintage": q_lm,
              "income_rows": len(income), "pce_years": [pce[0]["year"], pce[-1]["year"]] if pce else None,
              "income_latest_year": max((r["year"] for r in income if r.get("personal_income_musd")), default=None)})
    mf.write_text(json.dumps(m, indent=2))
    return {"source": SOURCE, **m}


def is_synced() -> bool:
    d = _dir() / "parquet"
    return (d / "state_income.parquet").exists() and (d / "pce.parquet").exists()


def vintage() -> dict:
    mf = _dir() / "manifest.json"
    return json.loads(mf.read_text()) if mf.exists() else {}


def income_path() -> Path:
    return _dir() / "parquet" / "state_income.parquet"


def pce_path() -> Path:
    return _dir() / "parquet" / "pce.parquet"


def context(state: str, year: int) -> dict | None:
    """Income/population for the state (latest year <= `year`) and PCE deflator to the base year."""
    if not is_synced():
        return None
    con = duckdb.connect()
    inc = con.execute(f"""SELECT year, personal_income_musd, population, per_capita_income
        FROM read_parquet({sql_str(income_path())}) WHERE state = ? AND year <= ? AND personal_income_musd IS NOT NULL
        ORDER BY year DESC LIMIT 1""", [state.upper(), year]).fetchone()
    base = con.execute(f"""SELECT year, pce_index FROM read_parquet({sql_str(pce_path())})
        WHERE basis = 'annual' ORDER BY year DESC LIMIT 1""").fetchone()
    py = con.execute(f"SELECT pce_index, basis FROM read_parquet({sql_str(pce_path())}) WHERE year = ?",
                     [year]).fetchone()
    con.close()
    if not inc or not base or not py:
        return None
    return {"income_year": inc[0], "income_year_is_latest_available": inc[0] < year,
            "personal_income_musd": inc[1], "population": inc[2], "per_capita_income": inc[3],
            "pce_base_year": base[0], "deflator": round(base[1] / py[0], 6), "pce_basis": py[1]}
