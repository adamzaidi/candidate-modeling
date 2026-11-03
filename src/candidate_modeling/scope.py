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
         "General election day comes from reference/elections.yaml when the state/year is listed, otherwise the "
         "Tuesday after the first Monday in November. Every year has one, but only jurisdictions with contests "
         "that year actually vote (see each calendar entry's scope: statewide or local)."),
        ("stage-window",
         "Stages split the year: primary = after last year's general through the primary; primary_runoff = after "
         "the primary through the runoff; general_campaign = after the last primary/runoff through the general; "
         "general_runoff = after the general through its runoff. Primary and runoff dates come only from "
         "reference/elections.yaml, where every date has a source; a year with no entry reports no_election."),
        ("calendar-sourced",
         "Never infer an election date from a pattern. Add dates to reference/elections.yaml only with a source "
         "URL; tests reject entries without one."),
    ],
    "money": [
        ("itemized-only",
         "Totals are itemized contributions as filed. FEC small-dollar money (under $200 per donor per cycle) is "
         "not itemized and appears only in fec_totals; Georgia lump sums are typed 'unitemized'."),
        ("clean-view",
         "Sum over contributions_clean (or contributions_as_of): they exclude memo rows (FEC sub-items) and "
         "rows that have been removed at the source."),
        ("loans-excluded",
         "Loans, loan payments, interest, and other non-contribution receipts are not contributions. They are labeled "
         "(contribution_kind 'loan' / 'other_receipt') where the source includes them, and excluded from every total. "
         "Loans are reported separately as loans_total and self_loans_total (a candidate lending to their own campaign), "
         "available for MI, GA, and FEC; TX and PA loans live in files not loaded yet (roadmap cash-and-debt)."),
        ("contribution-kind",
         "Every contribution has contribution_kind: monetary, in_kind, return (negative), transfer (from another "
         "committee of the same candidate, FEC line 12), loan, or other_receipt. Totals include monetary, in_kind, "
         "return, and transfer; in_kind_total and returns_total are reported separately."),
        ("returns-negative", "Returned contributions are negative amounts where the source reports them."),
        ("in-state-share",
         "In-state share counts only rows with a known donor state; unitemized lump sums are left out."),
        ("nominal-dollars", "Amounts are nominal dollars; nothing is inflation-adjusted."),
        ("statewide-double-count",
         "Statewide totals add up money received by every committee, so committee-to-committee transfers "
         "(PAC to candidate, party to candidate) and conduits that forward earmarked gifts (e.g. ActBlue Texas) "
         "are counted at each hop. Prefer the individuals-only total when comparing statewide money."),
        ("self-funding",
         "A candidate's money to their own campaign is usually typed 'individual' (e.g. $200M from Richard Jackson to "
         "Jackson for Governor in GA, 2026). self_funding_total flags individual gifts whose donor name matches the "
         "candidate (surname as a word plus the first 4 letters of the first name). Nicknames on one side only are "
         "missed, so treat it as a floor and check top_donors and pct_from_top_donor too."),
        ("conduits",
         "Conduits (ActBlue, WinRed, and similar) forward earmarked gifts that candidates also itemize. Committees "
         "flagged is_conduit (e.g. ActBlue Texas) are reported as conduit_recipient_total; total_excluding_conduits "
         "removes them. Gifts whose donor is a conduit are reported as from_conduit_total."),
        ("party-labels",
         "Party is the recipient committee's party on file, normalized to democratic, republican, libertarian, green, "
         "other, cross_filed (PA judicial cross-filing), nonpartisan, unknown (a candidate with no party on file), or "
         "not_applicable (PACs and other non-party committees). Party committees take their party from their name. "
         "Texas files carry no party, so TX candidates are 'unknown' until a candidate-list source is added."),
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
    "offices": [
        ("office-taxonomy",
         "Offices are normalized into shared categories (see OFFICE_CATEGORIES). Raw labels are always kept as "
         "office_raw; unmapped codes become 'unknown' rather than being guessed."),
        ("office-snapshot",
         "A committee's office is its latest office on file, not necessarily the office it was raising for in an "
         "earlier window. A candidate who switched races carries the new office back in time."),
        ("linked-committees",
         "Some states route a candidate's money through a separate committee. Texas specific-purpose committees "
         "that the TEC SPAC index links to exactly one candidate (SUPPORT/ASSIST) count as that candidate's "
         "committee and take their office (e.g. Texans for Greg Abbott -> governor). Committees that oppose, or "
         "back several candidates, stay 'pac'."),
        ("office-not-ballot",
         "Money raised by officeholders who are not on the ballot that year still counts under their office; "
         "office totals measure money held by that office's committees, not only active races."),
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


# Normalized office categories. Every connector's committees_sql() maps raw office labels onto these;
# raw values are always kept in office_raw. Non-candidate committees get their committee kind instead.
OFFICE_CATEGORIES = {
    "governor": "Governor",
    "lieutenant_governor": "Lieutenant governor",
    "attorney_general": "Attorney general",
    "secretary_of_state": "Secretary of state",
    "statewide_other": "Other statewide executive (comptroller, treasurer, auditor, agriculture, land, railroad, "
                       "insurance, labor, school superintendent, public service commission)",
    "state_senate": "State senate",
    "state_house": "State house / assembly",
    "state_board": "Elected state boards (board of education, university regents and trustees)",
    "supreme_court": "Courts of last resort (state supreme court; TX Court of Criminal Appeals)",
    "appellate_court": "Intermediate appellate courts (TX/GA/MI courts of appeals; PA Superior and Commonwealth)",
    "trial_court": "Trial and lower courts (district, circuit, superior in GA, common pleas, probate, magisterial)",
    "prosecutor": "District attorneys and solicitors general",
    "county_office": "County offices (commissioners, sheriff, clerks, tax commissioner, coroner)",
    "municipal_office": "City offices (mayor, council, aldermen)",
    "school_board": "Local school boards",
    "party_office": "Party offices (state or county party chair)",
    "federal": "Federal office filed with a state agency (rare; federal money is in FEC)",
    "pac": "Non-candidate committees: PACs, independent expenditure and issue committees",
    "party_committee": "Party committees (state, county, district)",
    "ballot_measure": "Ballot question committees",
    "other_committee": "Other committees (caucuses, direct-expenditure filers, unclassified committee types)",
    "unknown": "Candidate committee whose office code is missing or not yet mapped (raw code kept in office_raw)",
}


# Not built yet. Rendered into the README and returned by data_scope so agents never claim these exist.
ROADMAP = [
    # labels deferred from the label round
    ("incumbency", "Incumbent / challenger / open-seat label",
     "Explains much of election-over-election change (e.g. PA governor 2022 open seat vs 2026 incumbent)",
     "FEC incumbent_challenge; TX officeholder status; seat-holder lists elsewhere", "medium"),
    ("in-district-share", "In-district donor share for legislative races",
     "In-state share is too coarse for a state house seat", "Census ZIP-to-district crosswalk + stored district",
     "medium"),
    ("industry", "Donor industry classification",
     "Which industries fund which offices", "Rules over employer/occupation text, validated on a hand-labeled sample",
     "medium-high"),
    ("outcomes", "Election outcomes (won / lost / runoff)", "Did money predict winners",
     "State results files", "high"),
    ("texas-party", "Party for Texas candidates", "TX is 'unknown' today", "TX SOS candidate filings / primary ballots",
     "medium"),
    # additional
    ("expenditures", "Expenditures: where campaigns spend (vendors, consultants, media, salaries)",
     "Half of campaign finance; every source already publishes it",
     "TX expend_##, MI expenditure files, PA expense, GA TEXP, FEC Schedule B", "medium"),
    ("outside-spending", "Independent expenditures and outside groups (super PACs, IE committees)",
     "Money spent about a candidate, often larger than what they raise",
     "FEC Schedule E, MI IE committees, TX direct campaign expenditures, GA IE", "medium-high"),
    ("cash-and-debt", "Cash on hand, debts, and loans over time",
     "Financial health and burn rate, not just money raised",
     "TX cover sheets, PA filer BEGINNING, GA reports, FEC totals (already stored)", "medium"),
    ("donor-resolution", "Donor entity resolution across sources and years",
     "Enables donor overlap between candidates, repeat vs first-time donors, and state-to-federal donor migration "
     "(e.g. Talarico's Texas House donors vs his Senate donors)", "Name + ZIP + employer matching", "high"),
    ("legislation-link", "Link money to votes and bills",
     "Who funds legislators who vote a given way", "Open States (key already configured)", "high"),
    ("lobbying-contracts", "Lobbyist and government-contract cross-reference",
     "Donors who lobby or hold state contracts", "State lobby registrations, contract databases", "high"),
    ("normalization", "Inflation and per-capita adjustment",
     "Fair cross-decade and cross-state comparisons", "CPI-U; Census population / registered voters", "low"),
    ("alerts", "Alerts on new filings and amendments for watched candidates",
     "History tracking already detects changes; this pushes them", "load_runs + scheduled refresh", "low"),
    ("data-quality", "Sync data-quality monitors",
     "Catch upstream format changes (row-count drops, new parse failures, schema drift)", "sync manifests", "low"),
    ("local-and-measures", "Local races and ballot measures beyond state filers",
     "Most offices are local", "County/city disclosure systems", "high"),
    ("more-states", "More state connectors", "Coverage", "AZ, WI, NC, NV, FL, OH, CA, NY next", "per state"),
]
NOT_PLANNED = [
    "Guessing donor gender, race, or ethnicity from names (unreliable and easy to misuse).",
    "'Dark money' labels without documentary evidence (needs investigation, not field mapping).",
]


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
    from . import elections
    return {"rules": {g: [{"id": i, "rule": t} for i, t in items] for g, items in RULES.items()},
            "office_categories": OFFICE_CATEGORIES, "election_calendar": elections.calendar_summary(),
            "not_yet_available": [{"id": i, "item": t, "why": w} for i, t, w, _, _ in ROADMAP],
            "not_planned": NOT_PLANNED,
            "sources": sources, "planned_states": PLANNED}


# ---------- README generation ----------

BEGIN, END = "<!-- BEGIN GENERATED: data-scope (edit src/candidate_modeling/scope.py, then run `cm docs`) -->", \
             "<!-- END GENERATED: data-scope -->"


def render_markdown() -> str:
    titles = {"time": "Time", "money": "Money", "comparisons": "Comparing elections", "offices": "Offices",
              "identity": "Identity"}
    out = ["## Data scope and rules", ""]
    for group, items in RULES.items():
        out.append(f"### {titles.get(group, group)}")
        out.append("")
        out += [f"- **{rid}**: {text}" for rid, text in items]
        out.append("")
    out.append("### Office categories")
    out.append("")
    out += [f"- `{k}`: {v}" for k, v in OFFICE_CATEGORIES.items()]
    out.append("")
    out.append("### Roadmap (not built yet)")
    out.append("")
    out.append("| Item | Why | Source | Effort |")
    out.append("|---|---|---|---|")
    out += [f"| {t} | {w} | {src} | {e} |" for _, t, w, src, e in ROADMAP]
    out.append("")
    out.append("Deliberately not planned: " + " ".join(NOT_PLANNED))
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
