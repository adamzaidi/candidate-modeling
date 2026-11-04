"""Local DuckDB store with history. Every source normalizes into the same `contributions` table.

Schema notes (also served to agents via describe_schema):
- One row per itemized contribution, keyed by (source, transaction_id, candidate_slug).
- `is_memo` rows are sub-itemizations of another row (FEC memo code X). Exclude them from sums.
- FEC does not itemize individual donations under $200 (aggregate per cycle). Small-dollar
  totals live in `fec_totals.individual_unitemized_contributions`, not in `contributions`.

History model (observation time: what the filings said when *we* looked, not filing dates):
- contributions: the current version of every row ever seen, plus tracking columns:
    first_seen  first load that contained it
    last_seen   most recent load that contained it
    valid_from  when the current version (current values) was first observed
    removed_at  set when a full load no longer contains it (amended away or deleted); NULL = live
- contribution_history: prior versions, each valid on [valid_from, valid_to).
    change = 'modified' (values changed) or 'removed' (row was absent for that period, then came back)
- contributions_as_of(ts): table macro returning the rows that were live at timestamp ts.
- load_runs: one row per merge with counts (new / modified / unchanged / removed / restored).
- fec_totals_history: every distinct FEC totals snapshot, with fetched_at.
"""
import datetime as _dt
import json
from contextlib import contextmanager

import duckdb

from .config import db_path
from .labels import COUNTED_SQL, MACROS

CONTRIBUTION_COLUMNS = [
    ("candidate_slug", "VARCHAR"),
    ("source", "VARCHAR"),          # fec | tx_tec | mi_boe | pa_dos | ga_gtcfc
    ("jurisdiction", "VARCHAR"),    # federal | TX | MI | ...
    ("committee_id", "VARCHAR"),
    ("committee_name", "VARCHAR"),
    ("transaction_id", "VARCHAR"),
    ("date", "DATE"),
    ("amount", "DOUBLE"),
    ("contributor_name", "VARCHAR"),
    ("contributor_type", "VARCHAR"),  # individual | organization | unitemized
    ("city", "VARCHAR"),
    ("state", "VARCHAR"),
    ("zip", "VARCHAR"),
    ("employer", "VARCHAR"),
    ("occupation", "VARCHAR"),
    ("is_memo", "BOOLEAN"),
    ("cycle", "INTEGER"),
    ("contribution_kind", "VARCHAR"),  # monetary | in_kind | return | transfer | loan | public_funds | other_receipt
    ("filed_date", "DATE"),            # when the row became public (report filed/received); NULL if unknown
]
COLUMN_NAMES = [c for c, _ in CONTRIBUTION_COLUMNS]
KEY = ["source", "transaction_id", "candidate_slug"]
# Derived labels are refreshed on every load but excluded from the change hash, so adding or improving a
# label never makes rows look amended in the history.
DERIVED = ["contribution_kind", "filed_date"]
CONTENT = [c for c in COLUMN_NAMES if c not in KEY]
HASHED = [c for c in CONTENT if c not in DERIVED]
TRACKING = [("row_hash", "VARCHAR"), ("first_seen", "TIMESTAMP"), ("last_seen", "TIMESTAMP"),
            ("valid_from", "TIMESTAMP"), ("removed_at", "TIMESTAMP")]
FEC_TOTALS_COLS = ["candidate_slug", "candidate_id", "cycle", "receipts", "disbursements", "cash_on_hand_end_period",
                   "individual_contributions", "individual_itemized_contributions",
                   "individual_unitemized_contributions", "other_political_committee_contributions",
                   "coverage_end_date"]


def _hash_expr(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return "md5(concat_ws('|', " + ", ".join(f"coalesce(CAST({p}{c} AS VARCHAR), '␀')" for c in HASHED) + "))"


def _cols(prefix: str = "") -> str:
    return ", ".join(f"{prefix}{c}" for c in COLUMN_NAMES)


_DEFS = ", ".join(f"{c} {t}" for c, t in CONTRIBUTION_COLUMNS)
DDL = f"""
CREATE TABLE IF NOT EXISTS contributions ({_DEFS}, PRIMARY KEY (source, transaction_id, candidate_slug));
CREATE TABLE IF NOT EXISTS contribution_history ({_DEFS}, row_hash VARCHAR, valid_from TIMESTAMP,
    valid_to TIMESTAMP, change VARCHAR);
CREATE TABLE IF NOT EXISTS load_runs (run_at TIMESTAMP, candidate_slug VARCHAR, source VARCHAR, scope VARCHAR,
    full_snapshot BOOLEAN, staged BIGINT, new BIGINT, modified BIGINT, unchanged BIGINT, removed BIGINT,
    restored BIGINT);
CREATE TABLE IF NOT EXISTS fec_totals (
    candidate_slug VARCHAR, candidate_id VARCHAR, cycle INTEGER,
    receipts DOUBLE, disbursements DOUBLE, cash_on_hand_end_period DOUBLE,
    individual_contributions DOUBLE, individual_itemized_contributions DOUBLE,
    individual_unitemized_contributions DOUBLE, other_political_committee_contributions DOUBLE,
    coverage_end_date DATE,
    PRIMARY KEY (candidate_slug, candidate_id, cycle)
);
CREATE TABLE IF NOT EXISTS fec_totals_history AS SELECT *, NULL::TIMESTAMP AS fetched_at FROM fec_totals WHERE false;
CREATE TABLE IF NOT EXISTS store_meta (key VARCHAR PRIMARY KEY, value VARCHAR);
CREATE TABLE IF NOT EXISTS fetch_state (
    candidate_slug VARCHAR, source VARCHAR, scope VARCHAR, cursor JSON, complete BOOLEAN,
    rows_loaded BIGINT, updated_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (candidate_slug, source, scope)
);
"""

_COUNTS = "NOT coalesce(is_memo, false) AND " + COUNTED_SQL
VIEWS = f"""
CREATE OR REPLACE VIEW contributions_clean AS
    SELECT {_cols()} FROM contributions WHERE {_COUNTS} AND removed_at IS NULL;
CREATE OR REPLACE MACRO contributions_as_of(ts) AS TABLE
    SELECT {_cols()} FROM contributions
        WHERE valid_from <= ts AND (removed_at IS NULL OR removed_at > ts) AND {_COUNTS}
    UNION ALL
    SELECT {_cols()} FROM contribution_history
        WHERE valid_from <= ts AND valid_to > ts AND {_COUNTS};
""" + MACROS


# Bump when the hashed column set changes. Stored hashes are then recomputed and any history versions that
# differ only in no-longer-hashed columns are folded back (see _rehash).
HASH_VERSION = "2:" + ",".join(HASHED)


def now() -> _dt.datetime:
    """Observation clock (UTC, naive). Patched in tests."""
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def _migrate(con) -> None:
    con.execute(DDL)
    con.execute("DROP VIEW IF EXISTS contributions_clean")  # views block ALTER
    existing = {r[0] for r in con.execute("DESCRIBE contributions").fetchall()}
    for c, t in [*[(d, dict(CONTRIBUTION_COLUMNS)[d]) for d in DERIVED], *TRACKING]:
        if c not in existing:
            con.execute(f"ALTER TABLE contributions ADD COLUMN {c} {t}")
    hist = [r[0] for r in con.execute("DESCRIBE contribution_history").fetchall()]
    for d in DERIVED:
        if d not in hist:  # history rows must keep the base-column order of contributions: rebuild if needed
            con.execute(f"CREATE TABLE contribution_history_new ({_DEFS}, row_hash VARCHAR, valid_from TIMESTAMP, "
                        "valid_to TIMESTAMP, change VARCHAR)")
            keep = [c for c in COLUMN_NAMES if c in hist]
            con.execute(f"INSERT INTO contribution_history_new ({', '.join(keep)}, row_hash, valid_from, valid_to, change) "
                        f"SELECT {', '.join(keep)}, row_hash, valid_from, valid_to, change FROM contribution_history")
            con.execute("DROP TABLE contribution_history")
            con.execute("ALTER TABLE contribution_history_new RENAME TO contribution_history")
            break
    # rows that predate history tracking: treat them as first observed now
    if con.execute("SELECT count(*) FROM contributions WHERE row_hash IS NULL").fetchone()[0]:
        ts = now()
        con.execute(f"UPDATE contributions SET row_hash = {_hash_expr()}, first_seen = coalesce(first_seen, ?), "
                    "last_seen = coalesce(last_seen, ?), valid_from = coalesce(valid_from, ?) WHERE row_hash IS NULL",
                    [ts, ts, ts])
    con.execute(VIEWS)
    _rehash(con)


def _rehash(con) -> None:
    """Recompute row hashes when HASH_VERSION changes, and drop history versions that were only created because
    a derived (unhashed) column changed: same hashed values as the version that replaced them."""
    cur = con.execute("SELECT value FROM store_meta WHERE key = 'hash_version'").fetchone()
    if cur and cur[0] == HASH_VERSION:
        return
    con.execute(f"UPDATE contributions SET row_hash = {_hash_expr()}")
    con.execute(f"UPDATE contribution_history SET row_hash = {_hash_expr()}")
    same = " AND ".join(f"h.{c} IS NOT DISTINCT FROM c.{c}" for c in HASHED)
    keys = " AND ".join(f"h.{k} = c.{k}" for k in KEY)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE spurious AS
        SELECT h.source, h.transaction_id, h.candidate_slug, h.valid_from, h.valid_to FROM contribution_history h
        JOIN contributions c ON {keys}
        WHERE h.change = 'modified' AND h.valid_to = c.valid_from AND {same}""")
    con.execute("""UPDATE contributions AS c SET valid_from = s.valid_from FROM spurious s
        WHERE c.source = s.source AND c.transaction_id = s.transaction_id AND c.candidate_slug = s.candidate_slug""")
    con.execute("""DELETE FROM contribution_history h USING spurious s
        WHERE h.source = s.source AND h.transaction_id = s.transaction_id AND h.candidate_slug = s.candidate_slug
          AND h.valid_to = s.valid_to AND h.change = 'modified'""")
    con.execute("INSERT OR REPLACE INTO store_meta VALUES ('hash_version', ?)", [HASH_VERSION])


@contextmanager
def connect(read_only: bool = False):
    path = db_path()
    if read_only and not path.exists():
        read_only = False  # first run: create it
    con = duckdb.connect(str(path), read_only=read_only)
    try:
        if not read_only:
            _migrate(con)
        yield con
    finally:
        con.close()


# ---------- merging ----------

_STAGED_DEFS = ", ".join(f"{c} {t}" for c, t in CONTRIBUTION_COLUMNS)


def _finish_stage(con) -> None:
    con.execute(f"""CREATE OR REPLACE TEMP TABLE staged AS
        SELECT *, {_hash_expr()} AS row_hash FROM staged_raw
        QUALIFY row_number() OVER (PARTITION BY {", ".join(KEY)} ORDER BY {_hash_expr()}) = 1""")


def _merge(con, slug: str, source: str, full: bool, scope: str = "", scope_sql: str = "",
           scope_params: list | None = None, ts: _dt.datetime | None = None) -> dict:
    """Diff `staged` against stored rows for (slug, source). full=True also marks unseen rows removed."""
    ts = ts or now()
    key_eq = " AND ".join(f"c.{k} = s.{k}" for k in KEY)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE diff AS
        SELECT s.source, s.transaction_id, s.candidate_slug,
               CASE WHEN c.transaction_id IS NULL THEN 'new'
                    WHEN c.removed_at IS NOT NULL THEN 'restored'
                    WHEN c.row_hash = s.row_hash THEN 'unchanged'
                    ELSE 'modified' END AS kind
        FROM staged s LEFT JOIN contributions c ON {key_eq}""")
    counts = dict(con.execute("SELECT kind, count(*) FROM diff GROUP BY kind").fetchall())
    in_diff = lambda kinds: f"""EXISTS (SELECT 1 FROM diff d WHERE d.kind IN ({kinds})
                                AND {" AND ".join(f"d.{k} = c.{k}" for k in KEY)})"""

    # archive the versions being replaced
    con.execute(f"""INSERT INTO contribution_history
        SELECT {_cols('c.')}, c.row_hash, c.valid_from, ?, 'modified' FROM contributions c WHERE {in_diff("'modified'")}""",
                [ts])
    con.execute(f"""INSERT INTO contribution_history
        SELECT {_cols('c.')}, c.row_hash, c.valid_from, c.removed_at, 'removed' FROM contributions c
        WHERE {in_diff("'restored'")}""")
    sets = ", ".join(f"{c} = s.{c}" for c in CONTENT)
    con.execute(f"""UPDATE contributions AS c SET {sets}, row_hash = s.row_hash, valid_from = ?, last_seen = ?,
                    removed_at = NULL
        FROM staged s WHERE {key_eq} AND {in_diff("'modified', 'restored'")}""", [ts, ts])
    derived_sets = ", ".join(f"{d} = s.{d}" for d in DERIVED)
    con.execute(f"""UPDATE contributions AS c SET last_seen = ?, {derived_sets} FROM staged s
        WHERE {key_eq} AND {in_diff(chr(39) + 'unchanged' + chr(39))}""", [ts])
    con.execute(f"""INSERT INTO contributions ({_cols()}, row_hash, first_seen, last_seen, valid_from, removed_at)
        SELECT {_cols('s.')}, s.row_hash, ?, ?, ?, NULL FROM staged s
        JOIN diff d ON {" AND ".join(f"d.{k} = s.{k}" for k in KEY)} WHERE d.kind = 'new'""", [ts, ts, ts])

    removed = 0
    if full:
        removed = mark_removed(con, slug, source, ts, scope_sql=scope_sql, scope_params=scope_params,
                               unseen_since=ts)
    staged_n = con.execute("SELECT count(*) FROM staged").fetchone()[0]
    out = {"staged": staged_n, "new": counts.get("new", 0), "modified": counts.get("modified", 0),
           "unchanged": counts.get("unchanged", 0), "removed": removed, "restored": counts.get("restored", 0)}
    con.execute("INSERT INTO load_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [ts, slug, source, scope, full, *out.values()])
    return out


def mark_removed(con, slug: str, source: str, ts: _dt.datetime, unseen_since: _dt.datetime,
                 scope_sql: str = "", scope_params: list | None = None) -> int:
    """Mark live rows for (slug, source[, scope]) whose last_seen is before `unseen_since` as removed at ts."""
    where = "candidate_slug = ? AND source = ? AND removed_at IS NULL AND last_seen < ?"
    params = [slug, source, unseen_since]
    if scope_sql:
        where += f" AND ({scope_sql})"
        params += scope_params or []
    n = con.execute(f"SELECT count(*) FROM contributions WHERE {where}", params).fetchone()[0]
    if n:
        con.execute(f"UPDATE contributions SET removed_at = ? WHERE {where}", [ts, *params])
    return n


def merge_query(con, slug: str, source: str, select_sql: str, params: list | None = None, full: bool = True,
                scope: str = "", ts: _dt.datetime | None = None) -> dict:
    """Bulk path for state data. SELECT must yield COLUMN_NAMES in order; full=True means it is the complete
    set of this candidate's rows from this source, so anything missing is marked removed."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE staged_raw ({_STAGED_DEFS})")
    con.execute(f"INSERT INTO staged_raw {select_sql}", params or [])
    _finish_stage(con)
    return _merge(con, slug, source, full, scope=scope, ts=ts)


def merge_rows(con, slug: str, source: str, rows: list[dict], full: bool = False, scope: str = "",
               ts: _dt.datetime | None = None) -> dict:
    con.execute(f"CREATE OR REPLACE TEMP TABLE staged_raw ({_STAGED_DEFS})")
    if rows:
        con.executemany(f"INSERT INTO staged_raw VALUES ({', '.join('?' for _ in COLUMN_NAMES)})",
                        [tuple(r.get(c) for c in COLUMN_NAMES) for r in rows])
    _finish_stage(con)
    return _merge(con, slug, source, full, scope=scope, ts=ts)


def upsert_contributions(con, rows: list[dict]) -> int:
    """Partial (non-removing) merge; rows may span several candidates/sources. Returns rows staged."""
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["candidate_slug"], r["source"]), []).append(r)
    return sum(merge_rows(con, s, src, g)["staged"] for (s, src), g in groups.items())


# ---------- FEC totals ----------

def record_fec_totals(con, row: list, ts: _dt.datetime | None = None) -> bool:
    """Upsert current totals and append to history if anything changed. Returns True if new snapshot."""
    con.execute(f"INSERT OR REPLACE INTO fec_totals VALUES ({', '.join('?' for _ in FEC_TOTALS_COLS)})", row)
    cols = ", ".join(FEC_TOTALS_COLS)
    last = con.execute(f"""SELECT {cols} FROM fec_totals_history WHERE candidate_slug=? AND candidate_id=? AND cycle=?
                           ORDER BY fetched_at DESC LIMIT 1""", row[:3]).fetchone()
    cur = con.execute(f"SELECT {cols} FROM fec_totals WHERE candidate_slug=? AND candidate_id=? AND cycle=?",
                      row[:3]).fetchone()
    if last == cur:
        return False
    con.execute(f"INSERT INTO fec_totals_history SELECT *, ? FROM fec_totals "
                "WHERE candidate_slug=? AND candidate_id=? AND cycle=?", [ts or now(), *row[:3]])
    return True


# ---------- fetch cursors ----------

def get_cursor(con, slug, source, scope):
    return con.execute(
        "SELECT cursor, complete, rows_loaded FROM fetch_state WHERE candidate_slug=? AND source=? AND scope=?",
        [slug, source, scope],
    ).fetchone()


def set_cursor(con, slug, source, scope, cursor, complete, rows_loaded):
    con.execute(
        "INSERT OR REPLACE INTO fetch_state VALUES (?, ?, ?, ?, ?, ?, current_timestamp)",
        [slug, source, scope, json.dumps(cursor, default=str), complete, rows_loaded],
    )
