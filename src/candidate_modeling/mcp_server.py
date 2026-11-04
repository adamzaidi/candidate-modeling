"""MCP server: lets any agent find candidates, pull their money, and model it.

Run:  cm serve        (stdio)
Typical agent flow:
  find_candidate -> add_candidate (with the IDs it chose) -> fetch_finances -> finance_summary / query
"""
from mcp.server.mcpserver import MCPServer

from . import config, elections, pipeline, query, scope
from .sources import states

INSTRUCTIONS = f"""Campaign finance data for US political candidates: federal via FEC, state via connectors for
{", ".join(sorted(states.REGISTRY))} (planned next: {", ".join(states.PLANNED)}).
Workflow: find_candidate -> add_candidate with the IDs that belong to the person (ask the user if several
plausible matches) -> fetch_finances -> finance_summary, compare_candidates, compare_elections, or run_query.
State bulk data must be synced once with sync_state (TX is a ~1 GB download).
History: contribution_changes shows amendments/removals between loads; finance_summary(as_of=...) and the SQL
macro contributions_as_of(ts) show past states. compare_elections compares election windows (e.g. 2025 vs
2026) statewide or for profiled candidates.
Call data_scope to see what is covered on this machine, and describe_schema before writing SQL.
data_scope.not_yet_available lists features that do NOT exist yet (expenditures, outside spending, industry,
incumbency, outcomes, ...): say so instead of approximating them.

RULES (always apply; cite rule ids when explaining numbers):
""" + scope.rules_text()

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
def finance_summary(slug: str, jurisdiction: str | None = None, cycle: int | None = None,
                    as_of: str | None = None) -> dict:
    """Totals, in-state share, donor-size mix, top states/employers/occupations, monthly trend, FEC totals.
    jurisdiction: 'federal' or a state code. as_of: ISO date/time to see the data as it stood then
    (observation time: only reaches back to the candidate's first load)."""
    return pipeline.finance_summary(slug, jurisdiction=jurisdiction, cycle=cycle, as_of=as_of)


@server.tool()
def contribution_changes(slug: str, since: str | None = None, until: str | None = None) -> dict:
    """What changed between loads: contributions added after the first load, amended (old vs new amount),
    or removed (amended away / deleted at the source), with dollar impact and the largest examples.
    since/until: ISO dates. Useful after each refresh, e.g. since the last load."""
    return pipeline.contribution_changes(slug, since=since, until=until)


@server.tool()
def fundraising_history(slug: str) -> list[dict]:
    """Every distinct FEC totals snapshot observed (receipts, small-dollar, spending, cash on hand) over time."""
    return pipeline.fundraising_history(slug)


@server.tool()
def compare_candidates(slugs: list[str], jurisdiction: str | None = None, cycle: int | None = None) -> list[dict]:
    """Side-by-side headline metrics for several profiled candidates."""
    return pipeline.compare_candidates(slugs, jurisdiction=jurisdiction, cycle=cycle)


@server.tool()
def data_scope() -> dict:
    """What the data covers and the rules for using it: global rules (with ids), per-source coverage,
    exclusions, known gaps, and which years are actually synced on this machine. Read before analysis."""
    return scope.data_scope()


@server.tool()
def compare_elections(years: list[int], state: str | None = None, slugs: list[str] | None = None,
                      stage: str = "general", group_by: str | None = None, office: list[str] | None = None,
                      kind: list[str] | None = None, party: list[str] | None = None,
                      checkpoints: list[int] | None = None) -> dict:
    """Compare money across election windows. state= for every committee in a synced state, or slugs= for profiled
    candidates. stage: general (default) | primary | primary_runoff | general_campaign | general_runoff (dates from
    the sourced calendar; data_scope lists it). group_by: 'office', 'kind', or 'party' (statewide only).
    office/kind/party: filter to categories, e.g. office=["governor"], party=["democratic"] (see data_scope).
    Each year also reports self_funding_total/pct_self_funded, in_kind_total, returns_total,
    conduit_recipient_total and total_excluding_conduits.
    Returns per-year totals, individuals-only, small-dollar, donors, in-state share, cumulative money at
    days-before checkpoints, top recipients/donors and concentration, coverage, and year-over-year changes
    (skipped when a window is not fully covered)."""
    kw = {"checkpoints": tuple(checkpoints)} if checkpoints else {}
    return elections.compare_elections(years, state=state, slugs=slugs, stage=stage, group_by=group_by,
                                       office=office, kind=kind, party=party, **kw)


@server.tool()
def revision_analysis(state: str, years: list[int], stage: str = "general", group_by: str | None = None) -> dict:
    """How much do early filings understate final totals? Rebuilds the election-window total as known N days
    before/after election day from report filing dates (TX, PA, GA only), gives the share of the final known at each
    lag and the revision to final for mature elections, nowcasts elections still settling (known today / historical
    share at the same lag, with a min-max range), and backtests that nowcast leave-one-out. group_by='kind' splits
    candidates / PACs / parties."""
    from . import revisions
    return revisions.revision_analysis(state, years, stage=stage, group_by=group_by)


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
