"""Local DuckDB store. Every source normalizes into the same `contributions` table.

Schema notes (also served to agents via describe_schema):
- One row per itemized contribution, keyed by (source, transaction_id).
- `is_memo` rows are sub-itemizations of another row (FEC memo code X). Exclude them from sums.
- FEC does not itemize individual donations under $200 (aggregate per cycle). Small-dollar
  totals live in `fec_totals.individual_unitemized_contributions`, not in `contributions`.
"""
from contextlib import contextmanager

import duckdb

from .config import db_path

CONTRIBUTION_COLUMNS = [
    ("candidate_slug", "VARCHAR"),
    ("source", "VARCHAR"),          # fec | tx_tec | mi_boe
    ("jurisdiction", "VARCHAR"),    # federal | TX | MI | ...
    ("committee_id", "VARCHAR"),
    ("committee_name", "VARCHAR"),
    ("transaction_id", "VARCHAR"),
    ("date", "DATE"),
    ("amount", "DOUBLE"),
    ("contributor_name", "VARCHAR"),
    ("contributor_type", "VARCHAR"),  # individual | organization
    ("city", "VARCHAR"),
    ("state", "VARCHAR"),
    ("zip", "VARCHAR"),
    ("employer", "VARCHAR"),
    ("occupation", "VARCHAR"),
    ("is_memo", "BOOLEAN"),
    ("cycle", "INTEGER"),
]
COLUMN_NAMES = [c for c, _ in CONTRIBUTION_COLUMNS]

DDL = f"""
CREATE TABLE IF NOT EXISTS contributions (
    {", ".join(f"{c} {t}" for c, t in CONTRIBUTION_COLUMNS)},
    PRIMARY KEY (source, transaction_id, candidate_slug)
);
CREATE TABLE IF NOT EXISTS fec_totals (
    candidate_slug VARCHAR, candidate_id VARCHAR, cycle INTEGER,
    receipts DOUBLE, disbursements DOUBLE, cash_on_hand_end_period DOUBLE,
    individual_contributions DOUBLE, individual_itemized_contributions DOUBLE,
    individual_unitemized_contributions DOUBLE, other_political_committee_contributions DOUBLE,
    coverage_end_date DATE,
    PRIMARY KEY (candidate_slug, candidate_id, cycle)
);
CREATE TABLE IF NOT EXISTS fetch_state (
    candidate_slug VARCHAR, source VARCHAR, scope VARCHAR, cursor JSON, complete BOOLEAN,
    rows_loaded BIGINT, updated_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (candidate_slug, source, scope)
);
CREATE OR REPLACE VIEW contributions_clean AS
    SELECT * FROM contributions WHERE NOT coalesce(is_memo, false);
"""


@contextmanager
def connect(read_only: bool = False):
    path = db_path()
    if read_only and not path.exists():
        read_only = False  # first run: create it
    con = duckdb.connect(str(path), read_only=read_only)
    try:
        if not read_only:
            con.execute(DDL)
        yield con
    finally:
        con.close()


def upsert_contributions(con, rows: list[dict]) -> int:
    """Insert or replace normalized rows. Idempotent on (source, transaction_id, candidate_slug)."""
    if not rows:
        return 0
    data = [tuple(r.get(c) for c in COLUMN_NAMES) for r in rows]
    placeholders = ", ".join("?" for _ in COLUMN_NAMES)
    con.executemany(f"INSERT OR REPLACE INTO contributions VALUES ({placeholders})", data)
    return len(rows)


def upsert_from_query(con, select_sql: str, params: list | None = None) -> int:
    """Bulk path for state data: SELECT must yield COLUMN_NAMES in order."""
    before = con.execute("SELECT count(*) FROM contributions").fetchone()[0]
    con.execute(f"INSERT OR REPLACE INTO contributions ({', '.join(COLUMN_NAMES)}) {select_sql}", params or [])
    after = con.execute("SELECT count(*) FROM contributions").fetchone()[0]
    return after - before


def get_cursor(con, slug, source, scope):
    r = con.execute(
        "SELECT cursor, complete, rows_loaded FROM fetch_state WHERE candidate_slug=? AND source=? AND scope=?",
        [slug, source, scope],
    ).fetchone()
    return r


def set_cursor(con, slug, source, scope, cursor, complete, rows_loaded):
    import json

    con.execute(
        "INSERT OR REPLACE INTO fetch_state VALUES (?, ?, ?, ?, ?, ?, current_timestamp)",
        [slug, source, scope, json.dumps(cursor), complete, rows_loaded],
    )
