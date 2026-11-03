"""Single source of truth for what the data covers and the rules for using it.

Everything that explains scope is generated from here:
- the README "Data scope and rules" section (`cm docs` rewrites it; a test fails if it drifts)
- the MCP server instructions (global rules, always visible to agents)
- the `data_scope` MCP tool (rules + per-source scope + live coverage of this machine's data)

Add a rule here, not in prose elsewhere. Per-source rules live in each connector's SCOPE dict.
"""
from pathlib import Path

from .config import ROOT

RULES: dict[str, list[tuple[str, str]]] = {
    "time": [
        ("two-clocks",
         "There are two time axes. Transaction time (`date`) is when a contribution was made; use it to compare "
         "elections. Observation time (`first_seen`, `valid_from`, `removed_at`, `as_of`) is when this database saw "
         "a version of a row; use it to ask what the filings said at a past moment. Observation history starts at "
         "each source's first load and never reaches back further."),
        ("election-window",
         "For election comparisons a contribution belongs to election year Y if dated after the reference election "
         "day of Y-1 and on or before that of Y. This keeps odd-year elections separate from even-year ones."),
        ("cycle-bucket",
         "`cycle` is a two-year, even-year bucket (FEC style). It folds odd-year money into the next even year, "
         "so never use it to compare an odd-year election with an even-year one."),
        ("reference-day",
         "Reference election day = the Tuesday after the first Monday in November. Every year has one, but only "
         "jurisdictions with contests that year (e.g. PA judicial and municipal, TX constitutional amendments) "
         "actually vote; primaries and runoffs are not modeled yet."),
    ],
    "money": [
        ("itemized-only",
         "Totals are itemized contributions as filed. FEC small-dollar money (under $200 per donor per cycle) is "
         "not itemized and appears only in fec_totals; Georgia lump sums are typed 'unitemized'."),
        ("clean-view",
         "Sum over contributions_clean (or contributions_as_of): they exclude memo rows (FEC sub-items) and "
         "rows that have been removed at the source."),
        ("loans-excluded", "Loans, loan payments, and interest are not contributions and are excluded."),
        ("returns-negative", "Returned contributions are negative amounts where the source reports them."),
        ("in-state-share",
         "In-state share counts only rows with a known donor state; unitemized lump sums are left out."),
        ("nominal-dollars", "Amounts are nominal dollars; nothing is inflation-adjusted."),
        ("statewide-double-count",
         "Statewide totals add up money received by every committee, so committee-to-committee transfers "
         "(PAC to candidate, party to candidate) and conduits that forward earmarked gifts (e.g. ActBlue Texas) "
         "are counted at each hop. Prefer the individuals-only total when comparing statewide money."),
        ("self-funding",
         "A candidate's money to their own campaign is often typed 'individual' (e.g. $200M from Richard Jackson to "
         "Jackson for Governor in GA, 2026). Check top_donors and pct_from_top_donor before reading individual "
         "totals as grassroots support."),
    ],
    "comparisons": [
        ("coverage-first",
         "Only compare windows that both sources cover completely. compare_elections reports coverage per year "
         "and marks partial windows; never present a partial window as a decline."),
        ("reporting-lag",
         "Filings trail the calendar. Near and just after election day the latest window is incomplete (the "
         "final reports are not filed yet), so its late checkpoints undercount; compare_elections flags this as "
         "reporting_incomplete."),
        ("align-by-days",
         "Align elections by days before election day and compare cumulative money at the same checkpoints "
         "(300, 180, 90, 30, 7, 0 days out), especially when one election is still in progress."),
        ("like-with-like",
         "Compare the same candidate, the same office, or the same jurisdiction. Odd-year and even-year ballots "
         "have different offices on them, so statewide odd-versus-even comparisons measure different contests."),
    ],
    "identity": [
        ("no-guessed-ids",
         "Never guess source IDs. When find_candidate returns several plausible matches, ask the user."),
        ("committee-ids",
         "In some states (e.g. PA, MI) money is filed under a committee whose ID differs from the candidate's "
         "filer ID; profile the committee."),
        ("names-not-linked",
         "Donor names are free text and are not linked across sources or years; the same person can appear "
         "under several spellings."),
    ],
}


def _sources() -> dict:
    from .sources import fec, states
    out = {"FEC": fec.SCOPE}
    for code, m in states.REGISTRY.items():
        out[code] = m.SCOPE
    return out


def rules_text() -> str:
    """Compact rules for MCP server instructions."""
    lines = []
    for group, items in RULES.items():
        lines.append(f"[{group}]")
        lines += [f"- {rid}: {text}" for rid, text in items]
    return "\n".join(lines)


def data_scope() -> dict:
    """Rules + per-source scope + live coverage on this machine."""
    from .sources import states
    from .sources.states import PLANNED
    live = {code: {"synced": m.is_synced(), "covered_years": m.covered_years()} for code, m in states.REGISTRY.items()}
    sources = _sources()
    for code, info in live.items():
        sources[code] = {**sources[code], **info}
    return {"rules": {g: [{"id": i, "rule": t} for i, t in items] for g, items in RULES.items()},
            "sources": sources, "planned_states": PLANNED}


# ---------- README generation ----------

BEGIN, END = "<!-- BEGIN GENERATED: data-scope (edit src/candidate_modeling/scope.py, then run `cm docs`) -->", \
             "<!-- END GENERATED: data-scope -->"


def render_markdown() -> str:
    titles = {"time": "Time", "money": "Money", "comparisons": "Comparing elections", "identity": "Identity"}
    out = ["## Data scope and rules", ""]
    for group, items in RULES.items():
        out.append(f"### {titles.get(group, group)}")
        out.append("")
        out += [f"- **{rid}**: {text}" for rid, text in items]
        out.append("")
    out.append("### Sources")
    out.append("")
    for code, s in _sources().items():
        out.append(f"#### {code}: [{s['name']}]({s['url']})")
        out.append("")
        out.append(f"- **Covers:** {s['covers']}")
        out.append(f"- **History from:** {s['history_from']}")
        out.append(f"- **Refresh:** {s['refresh']}")
        out.append(f"- **Excludes:** {'; '.join(s['excludes'])}")
        out += [f"- **Rule:** {r}" for r in s["rules"]]
        out += [f"- **Known gap:** {g}" for g in s["known_gaps"]]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_readme(path: Path | None = None) -> bool:
    """Replace the generated block in README.md. Returns True if the file changed."""
    path = path or ROOT / "README.md"
    text = path.read_text()
    block = f"{BEGIN}\n{render_markdown()}{END}"
    if BEGIN in text and END in text:
        new = text[:text.index(BEGIN)] + block + text[text.index(END) + len(END):]
    else:
        new = text.rstrip() + "\n\n" + block + "\n"
    if new != text:
        path.write_text(new)
        return True
    return False
