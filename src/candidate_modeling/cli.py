"""`cm` command line. Same operations as the MCP server.

  cm find "James Talarico" --state TX
  cm add "James Talarico" --state TX --fec S6TX00479 --state-id TX=00081913
  cm sync TX            | cm sync MI --years 2024 2025 2026
  cm fetch talarico [--sources fec TX] [--max-pages 50]
  cm summary talarico [--jurisdiction TX] [--as-of 2026-10-01]
  cm changes talarico [--since 2026-11-01]   # amendments / removals / new filings between loads
  cm history talarico                        # FEC totals snapshots over time
  cm compare-elections 2025 2026 --state PA  # election windows side by side (or --slugs a b)
  cm compare-elections 2022 2026 --state TX --stage primary --group-by office
  cm compare-elections 2022 2026 --state TX --office governor
  cm scope                                   # rules + what is covered on this machine
  cm docs                                    # regenerate README data-scope section
  cm compare talarico someone-else
  cm query "SELECT ..."
  cm refresh            # sync every state in use, re-fetch every profile (used by CI)
  cm serve              # MCP server over stdio
"""
import argparse
import json
import sys

from . import config, pipeline, query


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cm", description="Candidate campaign-finance modeling")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("find"); s.add_argument("name"); s.add_argument("--state")
    s = sub.add_parser("add")
    s.add_argument("name"); s.add_argument("--state"); s.add_argument("--party"); s.add_argument("--slug")
    s.add_argument("--fec", nargs="*", default=[]); s.add_argument("--openstates")
    s.add_argument("--state-id", action="append", default=[], metavar="ST=ID",
                   help="repeatable, e.g. --state-id TX=00081913 --state-id PA=20160016")
    s = sub.add_parser("sync"); s.add_argument("state"); s.add_argument("--years", nargs="*", type=int)
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("fetch"); s.add_argument("slug"); s.add_argument("--sources", nargs="*")
    s.add_argument("--cycles", nargs="*", type=int); s.add_argument("--max-pages", type=int)
    s = sub.add_parser("summary"); s.add_argument("slug"); s.add_argument("--jurisdiction"); s.add_argument("--cycle", type=int)
    s.add_argument("--as-of", help="ISO date/time: the data as it stood then")
    s = sub.add_parser("changes"); s.add_argument("slug"); s.add_argument("--since"); s.add_argument("--until")
    s = sub.add_parser("history"); s.add_argument("slug")
    s = sub.add_parser("compare-elections"); s.add_argument("years", nargs="+", type=int)
    g = s.add_mutually_exclusive_group(required=True); g.add_argument("--state"); g.add_argument("--slugs", nargs="+")
    s.add_argument("--stage", default="general",
                   choices=["general", "primary", "primary_runoff", "general_campaign", "general_runoff"])
    s.add_argument("--group-by", choices=["office", "kind", "party"]); s.add_argument("--office", nargs="+")
    s.add_argument("--kind", nargs="+"); s.add_argument("--party", nargs="+")
    sub.add_parser("scope")
    sub.add_parser("docs", help="regenerate the README data-scope section from scope.py")
    s = sub.add_parser("compare"); s.add_argument("slugs", nargs="+"); s.add_argument("--jurisdiction")
    s = sub.add_parser("query"); s.add_argument("sql")
    sub.add_parser("schema")
    s = sub.add_parser("refresh"); s.add_argument("--max-pages", type=int)
    sub.add_parser("serve")

    a = ap.parse_args(argv)
    if a.cmd == "find":
        _print(pipeline.find_candidate(a.name, a.state))
    elif a.cmd == "add":
        st: dict[str, list[str]] = {}
        for pair in a.state_id:
            code, _, ident = pair.partition("=")
            st.setdefault(code.upper(), []).append(ident)
        _print(pipeline.add_candidate(a.name, state=a.state, party=a.party, slug=a.slug, fec_candidate_ids=a.fec,
                                      state_ids=st, openstates_person_id=a.openstates))
    elif a.cmd == "sync":
        kw = {"force": a.force}
        if a.years:
            kw["years"] = a.years
        _print(pipeline.sync_state(a.state, **kw))
    elif a.cmd == "fetch":
        _print(pipeline.fetch_finances(a.slug, sources=a.sources, cycles=a.cycles, max_pages=a.max_pages))
    elif a.cmd == "summary":
        _print(pipeline.finance_summary(a.slug, jurisdiction=a.jurisdiction, cycle=a.cycle, as_of=a.as_of))
    elif a.cmd == "changes":
        _print(pipeline.contribution_changes(a.slug, since=a.since, until=a.until))
    elif a.cmd == "history":
        _print(pipeline.fundraising_history(a.slug))
    elif a.cmd == "compare-elections":
        from .elections import compare_elections
        _print(compare_elections(a.years, state=a.state, slugs=a.slugs, stage=a.stage, group_by=a.group_by,
                                 office=a.office, kind=a.kind, party=a.party))
    elif a.cmd == "scope":
        from .scope import data_scope
        _print(data_scope())
    elif a.cmd == "docs":
        from .scope import write_readme
        print("README.md updated" if write_readme() else "README.md already current")
    elif a.cmd == "compare":
        _print(pipeline.compare_candidates(a.slugs, jurisdiction=a.jurisdiction))
    elif a.cmd == "query":
        _print(query.run(a.sql))
    elif a.cmd == "schema":
        _print(query.describe_schema())
    elif a.cmd == "refresh":
        profiles = config.list_profiles()
        used = sorted({s for p in profiles for s in p["ids"].get("state", {})})
        for st in used:
            _print(pipeline.sync_state(st))
        for p in profiles:
            _print(pipeline.fetch_finances(p["slug"], max_pages=a.max_pages))
    elif a.cmd == "serve":
        from .mcp_server import main as serve
        serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
