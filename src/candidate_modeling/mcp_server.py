"""MCP server: lets any agent find candidates, pull their money, and model it.

Run:  cm serve        (stdio)
Typical agent flow:
  find_candidate -> add_candidate (with the IDs it chose) -> fetch_finances -> finance_summary / query
"""
from mcp.server.mcpserver import MCPServer

from . import config, pipeline, query
from .sources import states

INSTRUCTIONS = """Campaign finance data for US political candidates (federal via FEC; state via
per-state connectors: TX, MI so far). Workflow: find_candidate -> add_candidate with the IDs that belong
to the person (confirm with the user if several plausible matches) -> fetch_finances -> finance_summary,
compare_candidates, or query (SQL). Call describe_schema before writing SQL. State bulk data must be synced
once with sync_state (TX is a ~1 GB download). Always report caveats: FEC small-dollar donations are not
itemized; numbers are itemized contributions as filed."""

server = MCPServer("candidate-modeling", instructions=INSTRUCTIONS)


@server.tool()
def find_candidate(name: str, state: str | None = None) -> dict:
    """Search FEC and synced state databases for a person. Returns candidate/filer/committee IDs per source.
    state: two-letter code to narrow results (recommended)."""
    return pipeline.find_candidate(name, state)


@server.tool()
def add_candidate(name: str, state: str | None = None, party: str | None = None,
                  fec_candidate_ids: list[str] | None = None, state_ids: dict[str, list[str]] | None = None,
                  openstates_person_id: str | None = None, slug: str | None = None) -> dict:
    """Create or extend a candidate profile with IDs chosen from find_candidate. Merges, never drops IDs.
    state_ids maps state code -> list of the `id` values find_candidate returned, e.g. {"TX": ["00081913"]}.
    In states where candidates raise money through a separate committee (e.g. PA), use the committee's id."""
    return pipeline.add_candidate(name, state=state, party=party, slug=slug, fec_candidate_ids=fec_candidate_ids,
                                  state_ids=state_ids, openstates_person_id=openstates_person_id)


@server.tool()
def list_candidates() -> list[dict]:
    """Profiles already set up, with their source IDs."""
    return [{"slug": p["slug"], "name": p["name"], "state": p.get("state"), "ids": p["ids"]}
            for p in config.list_profiles()]


@server.tool()
def list_supported_states() -> dict:
    """Which state connectors exist, whether their data is synced, and what's planned."""
    out = {}
    for s, m in states.REGISTRY.items():
        out[s] = {"source": m.SOURCE, "synced": m.is_synced()}
        if hasattr(m, "synced_years"):
            out[s]["years"] = m.synced_years()
    return {"supported": out, "planned": states.PLANNED, "federal": "FEC (all federal candidates)"}


@server.tool()
def sync_state(state: str, years: list[int] | None = None, force: bool = False) -> dict:
    """Download/refresh a state's bulk data. Skips if unchanged upstream. TX: ~1 GB, ~2 min.
    MI: per year (years defaults to the last 4)."""
    kw = {"force": force}
    if years and state.upper() == "MI":
        kw["years"] = years
    return pipeline.sync_state(state, **kw)


@server.tool()
def fetch_finances(slug: str, sources: list[str] | None = None, cycles: list[int] | None = None,
                   max_pages: int | None = None) -> dict:
    """Load a profiled candidate's contributions into the local DB. sources: e.g. ["fec", "TX"].
    FEC itemized pulls are resumable — if rate-limited, call again later to continue."""
    return pipeline.fetch_finances(slug, sources=sources, cycles=cycles, max_pages=max_pages)


@server.tool()
def finance_summary(slug: str, jurisdiction: str | None = None, cycle: int | None = None) -> dict:
    """Totals, in-state share, donor-size mix, top states/employers/occupations, monthly trend, FEC totals.
    jurisdiction: 'federal' or a state code."""
    return pipeline.finance_summary(slug, jurisdiction=jurisdiction, cycle=cycle)


@server.tool()
def compare_candidates(slugs: list[str], jurisdiction: str | None = None, cycle: int | None = None) -> list[dict]:
    """Side-by-side headline metrics for several profiled candidates."""
    return pipeline.compare_candidates(slugs, jurisdiction=jurisdiction, cycle=cycle)


@server.tool()
def describe_schema() -> dict:
    """Tables, columns, and caveats available to `query`. Call before writing SQL."""
    return query.describe_schema()


@server.tool()
def run_query(sql: str) -> dict:
    """Run one read-only DuckDB SELECT. Tables: contributions, contributions_clean, fec_totals, plus full state
    bulk views (tx_filers, tx_contribs_raw, mi_contribs_raw) when synced. Max 500 rows returned."""
    return query.run(sql)


def main():
    server.run()


if __name__ == "__main__":
    main()
