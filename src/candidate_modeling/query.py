"""Read-only SQL for agents. One SELECT per call; results capped.

Besides the stored tables, every synced state's full bulk data is exposed as views, so an agent can
answer questions about *any* filer in that state without first creating a profile.
"""
import duckdb

from . import store
from .config import data_dir
from .pipeline import CAVEATS
from .sources.states import michigan, texas
from .sources.states._common import sql_str

MAX_ROWS = 500
ALLOWED = {duckdb.StatementType.SELECT, duckdb.StatementType.EXPLAIN}


def _state_views(con) -> list[str]:
    made = []
    if texas.is_synced():
        _, parts = texas._dirs()
        con.execute(f"CREATE TEMP VIEW tx_filers AS SELECT * FROM read_parquet({sql_str(parts.parent / 'filers.parquet')})")
        con.execute(f"CREATE TEMP VIEW tx_contribs_raw AS SELECT * FROM read_parquet({sql_str(parts / '*.parquet')}) "
                    "WHERE coalesce(infoOnlyFlag, 'N') <> 'Y'")
        made += ["tx_filers", "tx_contribs_raw"]
    if michigan.synced_years():
        _, p = michigan._dirs()
        con.execute(f"CREATE TEMP VIEW mi_contribs_raw AS SELECT * FROM read_parquet({sql_str(p / 'contributions_*.parquet')})")
        made.append("mi_contribs_raw")
    return made


def _sandbox(con) -> None:
    """Agent SQL may only read files under data/: no other local files, no network, no extensions.
    The statement-type check alone is not enough: SELECT read_text('~/.ssh/...') is a SELECT."""
    con.execute(f"SET allowed_directories=[{sql_str(str(data_dir().resolve()) + '/')}]")
    con.execute("SET enable_external_access=false")
    con.execute("SET autoload_known_extensions=false")
    con.execute("SET lock_configuration=true")


def run(sql: str, max_rows: int = MAX_ROWS) -> dict:
    stmts = duckdb.extract_statements(sql)
    if len(stmts) != 1:
        raise ValueError("Exactly one SQL statement per call.")
    if stmts[0].type not in ALLOWED:
        raise ValueError(f"Read-only: only SELECT/EXPLAIN allowed, got {stmts[0].type.name}.")
    with store.connect(read_only=True) as con:
        _state_views(con)
        _sandbox(con)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    return {"columns": cols, "rows": [list(map(_jsonable, r)) for r in rows[:max_rows]], "truncated": truncated}


def _jsonable(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def describe_schema() -> dict:
    with store.connect() as con:
        views = _state_views(con)
        tables = {}
        for t in ["contributions", "contributions_clean", "fec_totals", "fetch_state", *views]:
            tables[t] = [f"{r[0]} {r[1]}" for r in con.execute(f"DESCRIBE {t}").fetchall()]
    return {
        "tables": tables,
        "notes": {
            "contributions": "Normalized itemized contributions for profiled candidates, all sources. "
                             "Use contributions_clean (memo rows removed) for any sum.",
            "fec_totals": "FEC per-cycle summary totals; the only place small-dollar (<$200, unitemized) money appears.",
            "tx_filers / tx_contribs_raw": "Entire Texas Ethics Commission database (superseded rows removed). "
                                           "contributionAmount and contributionDt are VARCHAR (yyyyMMdd).",
            "mi_contribs_raw": "Entire Michigan MiTN contribution export for synced years. All VARCHAR; "
                               "received_date is MM/DD/YYYY; amount may contain commas.",
        },
        "caveats": CAVEATS,
    }
