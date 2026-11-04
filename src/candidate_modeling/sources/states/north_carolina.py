"""North Carolina State Board of Elections campaign finance (cf.ncsbe.gov).

No bulk file. The Transaction Search exports CSV per search:
  GET  /CFTxnLkup/AdvancedSearch                          -> __RequestVerificationToken (session cookie)
  POST /CFTxnLkup/AdvancedSearch  Filter.StartDate/EndDate (MM/DD/YYYY), Filter.ReceiptAll/CommitteeAll/OfficeAll/
       PartyAll=true                                      -> Results page; grid attribute data-search-params (JSON)
  POST /CFTxnLkup/Export  searchParamsJson + token        -> CSV (a month of receipts exports in ~10 s)
The search params list expenditure types even for a receipts search: they are cleared before export, or
expenditures leak in. The same params filter by party code, which is how recipient party is derived (one extra
export per party per month). Committees come from /CFOrgLkup/CommitteeGeneralResult?name=% (all 13k+ committees).

The CSV has no transaction ID: IDs are a hash of the row plus its occurrence number in the export, so identical
rows (common for aggregated entries) are all kept and cross-sync history for NC is approximate.
"""
import csv
import hashlib
import html
import io
import json
import re
import shutil
import time
from datetime import date
from pathlib import Path

import duckdb
import requests

from ...config import USER_AGENT, state_dir
from ... import store
from ._common import case_map, data_files, parquet_list, read_manifest, sql_str, write_manifest

STATE = "NC"
SOURCE = "nc_sboe"
ID_KEY = "committee_ids"        # profile ids.state.<STATE>.<ID_KEY>  (SBoE IDs, e.g. STA-C3352N-C-001)
RESULT_ID = "committee_id"     # field in search() results holding that ID
BASE = "https://cf.ncsbe.gov"
PARTY_CODES = {"DEM": "democratic", "REP": "republican"}
_sleep = time.sleep

SCOPE = {
    "name": "North Carolina State Board of Elections",
    "url": "https://cf.ncsbe.gov/CFTxnLkup/",
    "covers": "State and county candidate committees, PACs, party committees, and referendum committees whose reports "
              "were entered into the SBoE database.",
    "excludes": ["Federal candidates (use FEC)", "Reports filed only as scanned images (not data-entered)"],
    "history_from": "Per calendar year as synced",
    "refresh": "Month-by-month CSV exports (~10 s each) plus one export per party per month; a year takes a few "
               "minutes. Data on the site updates overnight.",
    "rules": [
        "Only receipts load: the export's expenditure types are cleared before each request.",
        "'Aggregated Individual Contribution' rows are lump sums: contributor_type 'unitemized'.",
        "Party comes from the SBoE party filter (DEM/REP); other committees are not_applicable or unknown.",
        "Candidate committees are recognized by the '(LAST, FIRST)' suffix SBoE adds to their names.",
    ],
    "known_gaps": [
        "No transaction ID: IDs are row hashes plus occurrence number, so NC history across syncs is approximate.",
        "No office in the export, so office breakdowns are unavailable; the committee ID prefix shows state (STA), "
        "federal (FED), or county-level committees (e.g. WAK, MEC).",
    ],
}

KIND_MAP = {
    "Nonmonetary Gift": "in_kind", "Loan": "loan", "Outstanding Loan": "loan", "Forgiven Loan": "other_receipt",
    "Refund": "other_receipt", "Interest": "other_receipt", "Contrib to Reimburse": "other_receipt",
}
CSV_COLS = ["Name", "Street Line 1", "Street Line 2", "City", "State", "Zip Code", "Profession/Job Title",
            "Employer's Name/Specific Field", "Transction Type", "Committee Name", "Committee SBoE ID",
            "Committee Street 1", "Committee Street 2", "Committee City", "Committee State", "Committee Zip Code",
            "Report Name", "Date Occured", "Account Code", "Amount", "Form of Payment", "Purpose",
            "Candidate/Referendum Name", "Declaration"]
FIELD = {c: re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_") for c in CSV_COLS}  # 'Date Occured' -> date_occured


def _dirs() -> tuple[Path, Path]:
    d = state_dir(STATE)
    p = d / "parquet"
    p.mkdir(parents=True, exist_ok=True)
    return d, p


def _token(text: str) -> str:
    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', text)
    if not m:
        raise RuntimeError("NC: no anti-forgery token on page (site layout changed?)")
    return m.group(1)


def export(start: date, end: date, party: str | None = None, retries: int = 2) -> list[dict]:
    """Receipts between start and end (inclusive) as CSV dict rows; party = SBoE party code to filter by."""
    for attempt in range(retries + 1):
        try:
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            tok = _token(s.get(f"{BASE}/CFTxnLkup/AdvancedSearch", timeout=60).text)
            form = {"__RequestVerificationToken": tok, "Filter.StartDate": start.strftime("%m/%d/%Y"),
                    "Filter.EndDate": end.strftime("%m/%d/%Y"), "Filter.ReceiptAll": "true",
                    "Filter.CommitteeAll": "true", "Filter.OfficeAll": "true", "Filter.PartyAll": "true",
                    "Filter.IsOrg": "false"}
            res = s.post(f"{BASE}/CFTxnLkup/AdvancedSearch", data=form, timeout=180).text
            m = re.search(r'data-search-params="([^"]*)"', res)
            if not m:
                raise RuntimeError("NC: results page has no search params")
            params = json.loads(html.unescape(m.group(1)))
            params["ExpenditureType"] = ""  # receipts only (otherwise expenditures are exported too)
            if party:
                params["PartyType"] = f"'{party}'"
            e = s.post(f"{BASE}/CFTxnLkup/Export", timeout=900,
                       data={"__RequestVerificationToken": _token(res), "searchParamsJson": json.dumps(params)})
            e.raise_for_status()
            if "csv" not in (e.headers.get("content-type") or ""):
                raise RuntimeError("NC: export did not return CSV")
            return list(csv.DictReader(io.StringIO(e.content.decode("utf-8-sig", errors="replace"))))
        except (requests.RequestException, RuntimeError):
            if attempt == retries:
                raise
            _sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def _with_ids(rows: list[dict]) -> list[dict]:
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        rec = {FIELD[c]: (r.get(c) or "").strip() or None for c in CSV_COLS}
        h = hashlib.sha1("|".join(str(rec[FIELD[c]]) for c in CSV_COLS).encode()).hexdigest()[:16]
        seen[h] = seen.get(h, 0) + 1
        rec["txn_id"] = f"{h}-{seen[h]}"
        try:
            rec["amount"] = float((rec["amount"] or "0").replace(",", "").replace("$", ""))
        except ValueError:
            rec["amount"] = None
        out.append(rec)
    return out


def _write(rows: list[dict], dest: Path) -> None:
    cols = {**{FIELD[c]: "VARCHAR" for c in CSV_COLS}, "amount": "DOUBLE", "txn_id": "VARCHAR"}
    tmp = dest.with_suffix(".json")
    tmp.write_text(json.dumps(rows))
    spec = "{" + ", ".join(f"'{k}': '{v}'" for k, v in cols.items()) + "}"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_json({sql_str(tmp)}, columns={spec}, format='array')) "
                f"TO {sql_str(dest)} (FORMAT parquet, COMPRESSION zstd)")
    con.close()
    tmp.unlink()


def fetch_committees() -> int:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    page = s.get(f"{BASE}/CFOrgLkup/CommitteeGeneralResult", timeout=300,
                 params={"name": "%", "useOrgName": "True", "useCandName": "True", "useInHouseName": "True",
                         "useAcronym": "True"}).text
    m = re.search(r'"Data"\s*:\s*(\[\{.*?\}\])', page, re.S)
    if not m:
        raise RuntimeError("NC: committee list not found on page")
    data = [{"sboe_id": d.get("SBoEID"), "org_name": d.get("OrgName"), "cand_name": d.get("CandName"),
             "status": d.get("StatusDesc")} for d in json.loads(m.group(1)) if d.get("SBoEID")]
    _, p = _dirs()
    tmp = p / "committees.json"
    tmp.write_text(json.dumps(data))
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_json({sql_str(tmp)}, format='array', columns={{'sboe_id': 'VARCHAR', "
                f"'org_name': 'VARCHAR', 'cand_name': 'VARCHAR', 'status': 'VARCHAR'}})) "
                f"TO {sql_str(p / 'committees.parquet')} (FORMAT parquet)")
    con.close()
    tmp.unlink()
    return len(data)


def _months(year: int) -> list[tuple[date, date]]:
    last = date.today().month if year == date.today().year else 12
    out = []
    for mth in range(1, last + 1):
        nxt = date(year + (mth == 12), mth % 12 + 1, 1)
        out.append((date(year, mth, 1), date.fromordinal(nxt.toordinal() - 1)))
    return out


def sync(years: list[int] | None = None, force: bool = False) -> dict:
    """Re-download each requested calendar year month by month (default: last 2 years), plus party maps."""
    d, p = _dirs()
    years = years or [date.today().year - 1, date.today().year]
    m = read_manifest(d)
    out = []
    try:
        m["committees"] = fetch_committees()
    except Exception as e:  # enrichment; money still syncs
        m["committees_error"] = str(e)
    for y in years:
        work, final = p / f"tx_{y}_partial", p / f"tx_{y}"
        ym = m.setdefault(str(y), {})
        if not force and ym.get("complete") and final.exists():  # finished past year: nothing changes upstream often
            out.append({"year": y, "status": "current", "rows": ym.get("rows")})
            continue
        if force or not ym.get("months_done"):  # otherwise resume an interrupted run month by month
            shutil.rmtree(work, ignore_errors=True)
            ym.update({"months_done": [], "rows": 0, "complete": False})
        work.mkdir(exist_ok=True)
        party: dict[str, str] = ym.get("party", {})
        for start, end in _months(y):
            key = start.strftime("%m")
            if key in ym["months_done"]:
                continue
            rows = _with_ids(export(start, end))
            if rows:
                _write(rows, work / f"month_{key}.parquet")
            for code, label in PARTY_CODES.items():
                for r in export(start, end, party=code):
                    if r.get("Committee SBoE ID"):
                        party[r["Committee SBoE ID"].strip()] = label
            ym["months_done"].append(key)
            ym["rows"] += len(rows)
            ym["party"] = party
            write_manifest(d, m)  # checkpoint per month
        shutil.rmtree(final, ignore_errors=True)
        work.rename(final)
        (final / "party.json").write_text(json.dumps(party))
        ym["complete"] = date(y, 12, 31) < date.today()  # current year stays incomplete and re-downloads next sync
        ym["months_done"] = []
        write_manifest(d, m)
        out.append({"year": y, "rows": ym["rows"], "party_committees": len(party), "complete": ym["complete"]})
    return {"state": STATE, "committees": m.get("committees"), "years": out}


def _tx_files() -> list[Path]:
    _, p = _dirs()
    files = []
    for ydir in data_files(p, r"tx_\d{4}"):
        files += data_files(ydir, r"month_\d{2}\.parquet")
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
        raise FileNotFoundError("North Carolina not synced; run sync_state('NC')")
    return "[" + ", ".join(sql_str(f) for f in files) + "]"


def _party_rows() -> list[tuple[str, str]]:
    _, p = _dirs()
    rows = {}
    for ydir in data_files(p, r"tx_\d{4}"):
        f = ydir / "party.json"
        if f.exists():
            rows.update(json.loads(f.read_text()))
    return list(rows.items())


def search(name: str) -> list[dict]:
    if not is_synced():
        return [{"source": SOURCE, "error": "North Carolina data not synced. Run sync_state('NC') first."}]
    words = [w for w in re.split(r"[\s,]+", name.strip()) if w]
    where = " AND ".join("committee_name ILIKE ?" for _ in words)
    rows = duckdb.connect().execute(f"""
        SELECT committee_sboe_id, any_value(committee_name), count(*) AS n, round(sum(amount), 2)
        FROM read_parquet({_tx_list()}) WHERE {where} GROUP BY 1 ORDER BY n DESC LIMIT 25""",
        [f"%{w}%" for w in words]).fetchall()
    return [{"source": SOURCE, "state": STATE, "committee_id": r[0], "name": r[1], "rows": r[2], "total": r[3]}
            for r in rows]


def normalized_sql(ids: list[str] | None = None, slug: str | None = None) -> tuple[str, list]:
    where, params = "true", [slug]
    if ids is not None:
        where = f"committee_sboe_id IN ({', '.join('?' for _ in ids)})"
        params += list(ids)
    agg = "name ILIKE 'Aggregated%'"
    sql = f"""
        SELECT ? AS candidate_slug, '{SOURCE}', '{STATE}', committee_sboe_id, committee_name, txn_id,
               try_strptime(date_occured, '%m/%d/%Y')::DATE AS d, amount,
               CASE WHEN {agg} THEN NULL ELSE name END,
               CASE WHEN {agg} THEN 'unitemized' WHEN transction_type = 'Individual' THEN 'individual'
                    ELSE 'organization' END,
               city, upper(state), left(zip_code, 5), employer_s_name_specific_field, profession_job_title, false,
               (year(d) + year(d) % 2)::INTEGER,
               {case_map("transction_type", KIND_MAP, "CASE WHEN amount < 0 THEN 'return' ELSE 'monetary' END")}
        FROM read_parquet({_tx_list()})
        WHERE {where}
        QUALIFY row_number() OVER (PARTITION BY txn_id ORDER BY date_occured) = 1  -- same row in two year folders
    """
    return sql, params


def load(con, profile: dict) -> dict:
    ids = (profile["ids"].get("state", {}).get(STATE) or {}).get(ID_KEY) or []
    if not ids:
        return {"source": SOURCE, "rows": 0, "note": "no NC committee_ids in profile"}
    if not is_synced():
        return {"source": SOURCE, "rows": 0, "note": "North Carolina data not synced; run sync_state('NC')"}
    sql, params = normalized_sql(ids, profile["slug"])
    m = store.merge_query(con, profile["slug"], SOURCE, sql, params)
    return {"source": SOURCE, "rows": m["staged"], "changes": m, ID_KEY: ids, "covered_years": covered_years()}


CANDIDATE_SUFFIX = r"\(([^(),]+),\s*([^(),]+)(?:\s*\([^()]*\))?\)\s*$"   # "(LAST, FIRST M (COUNTY))"
PARTY_NAME = r"\b(DEMOCRATIC|REPUBLICAN|LIBERTARIAN|GREEN)\b.*\b(PARTY|EXECUTIVE COMMITTEE|EXEC COMM)\b"


def committees_sql() -> str:
    from ... import labels as L
    _, p = _dirs()
    party_vals = _party_rows()
    party_cte = ("SELECT * FROM (VALUES " + ", ".join(f"({sql_str(k)}, {sql_str(v)})" for k, v in party_vals)
                 + ") v(sboe_id, party_label)") if party_vals else \
        "SELECT NULL::VARCHAR AS sboe_id, NULL::VARCHAR AS party_label WHERE false"
    reg = p / "committees.parquet"
    reg_cte = (f"SELECT sboe_id, any_value(org_name) AS org_name FROM read_parquet({sql_str(reg)}) GROUP BY 1"
               if reg.exists() else "SELECT NULL::VARCHAR AS sboe_id, NULL::VARCHAR AS org_name WHERE false")
    return f"""
        WITH t AS (SELECT committee_sboe_id AS committee_id, any_value(committee_name) AS tx_name
                   FROM read_parquet({_tx_list()}) GROUP BY 1),
        r AS ({reg_cte}), pt AS ({party_cte}),
        j AS (SELECT t.committee_id, coalesce(r.org_name, t.tx_name) AS committee_name, pt.party_label
              FROM t LEFT JOIN r ON r.sboe_id = t.committee_id LEFT JOIN pt ON pt.sboe_id = t.committee_id),
        k AS (SELECT *, CASE WHEN regexp_matches(upper(committee_name), {sql_str(CANDIDATE_SUFFIX)}) THEN 'candidate'
                             WHEN regexp_matches(upper(committee_name), {sql_str(PARTY_NAME)}) THEN 'party_committee'
                             ELSE 'pac' END AS kind FROM j)
        SELECT committee_id, committee_name, kind, NULL AS office_raw,
               CASE WHEN kind = 'candidate' THEN 'unknown' ELSE kind END AS office_category,  -- no office in export
               split_part(committee_id, '-', 1) AS district,
               CASE WHEN party_label IS NOT NULL THEN party_label
                    ELSE {L.committee_party_sql("NULL", "kind", "committee_name")} END AS party,
               CASE WHEN kind = 'candidate' THEN trim(regexp_extract(committee_name, {sql_str(CANDIDATE_SUFFIX)}, 1)) END
                   AS cand_last,
               CASE WHEN kind = 'candidate'
                    THEN split_part(trim(regexp_extract(committee_name, {sql_str(CANDIDATE_SUFFIX)}, 2)), ' ', 1) END
                   AS cand_first,
               {L.conduit_sql("committee_name")} AS is_conduit
        FROM k"""
