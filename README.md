# candidate-modeling

Campaign-finance data for any US political candidate, pulled from official sources into one local
database. Agents can query it directly through an MCP server, and the same operations are available
as a CLI. First subject: **James Talarico** (Texas).

## Coverage

| Level | Source | What | Status |
|---|---|---|---|
| Federal | [FEC API](https://api.open.fec.gov/developers/) | Every federal candidate: totals + itemized receipts | done (resumable pulls) |
| Texas | [Texas Ethics Commission](https://www.ethics.state.tx.us/search/cf/) bulk CSV | All state/local filers since 2000 (~37M contributions) | done |
| Michigan | [MiTN all-transactions export](https://mi-boe.entellitrak.com/etk-mi-boe-prod/page.request.do?page=gov.mi.boe.component.cfrexport.page.cfrexportdownload) | All committees, by year since 1998 | done |
| Pennsylvania | [DOS full export](https://www.pa.gov/agencies/dos/resources/voting-and-elections-resources/campaign-finance-data) | State/local filers, by year since 2000 | done |
| Georgia | [PeachFile](https://efile.ethics.ga.gov/) bulk API | Filings from 2025 on (legacy 2017-2024 not covered) | done |
| Next | AZ, WI, NC, NV, FL, OH, CA, NY | | planned (`sources/states/__init__.py`) |
| Legislative | [Open States](https://docs.openstates.org/api-v3/) | Bills, votes | first-pass fetcher |

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env               # add FEC_API_KEY (free: api.data.gov/signup)

cm find "James Talarico" --state TX          # IDs from FEC + synced states
cm sync TX                                   # one-time ~1 GB download -> parquet (~2 min)
cm add "James Talarico" --slug talarico --state TX --fec S6TX00479 --state-id TX=00081913
cm fetch talarico                            # load into data/candidates.duckdb
cm summary talarico --jurisdiction TX        # geography, donor size mix, employers, trend
cm query "SELECT state, sum(amount) FROM contributions_clean WHERE candidate_slug='talarico' GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
```

## For AI agents (MCP)

```bash
claude mcp add candidate-modeling -s user -- /path/to/repo/.venv/bin/cm serve
```

Tools: `find_candidate`, `add_candidate`, `list_candidates`, `list_supported_states`, `sync_state`,
`fetch_finances`, `finance_summary` (with `as_of`), `compare_candidates`, `contribution_changes`,
`fundraising_history`, `describe_schema`, `run_query`.

Typical flow: **find → add → fetch → summary/query**. `run_query` is read-only, takes one SELECT,
and returns at most 500 rows. Once a state is synced, its entire bulk database is also queryable as
views (`tx_filers`, `tx_contribs_raw`, `mi_contribs_raw`), so an agent can answer questions about
any filer in that state without creating a profile first.

## Comparing elections

```bash
cm compare-elections 2025 2026 --state PA          # every committee in the state
cm compare-elections 2022 2026 --slugs talarico    # profiled candidates
```

Each year is an election window: contributions after the previous November election day, through this
one. For each window you get totals (all, individuals only, small-dollar, unitemized), approximate
donors, in-state share, money raised by 300/180/90/30/7/0 days before the election, top recipients, top
donors and concentration, and coverage status. Year-over-year changes are only computed when both windows
are fully covered. See the rules below: election-window, align-by-days, coverage-first, reporting-lag,
self-funding, statewide-double-count.

## History

Every load is diffed against what's already stored. Nothing is overwritten or deleted:

- **Amendments:** when a filing changes a contribution, the old version moves to `contribution_history`
  with the dates it was valid.
- **Removals:** rows that disappear from the source get `removed_at` set and stay queryable. FEC rows are
  only marked removed after a *complete* pass, so a rate-limited partial pull can't fake deletions.
- **Point in time:** `cm summary talarico --as-of 2026-10-01`, or in SQL
  `SELECT ... FROM contributions_as_of(TIMESTAMP '2026-10-01')`.
- **Change feed:** `cm changes talarico --since 2026-11-01` shows what was added, amended, or removed,
  with the dollar impact.
- **Fundraising over time:** `cm history talarico` lists every distinct FEC totals snapshot.
- `load_runs` logs every load with new / modified / unchanged / removed / restored counts.

Times are **observation time**, meaning when this database saw the data, not when it was filed. History
starts at each candidate's first load. The storage cost is small: only changed rows are copied, and
each state's bulk data is never duplicated.

## Layout

```
candidates/<slug>.yaml          profile: name, home state, source IDs (human-editable, versioned)
src/candidate_modeling/
  sources/fec.py                federal: search, totals, itemized Schedule A (resumable)
  sources/states/<state>.py     one connector per state: sync(), search(), load()
  store.py                      DuckDB schema; normalized `contributions` table
  pipeline.py                   find / add / fetch / summary / compare
  query.py                      read-only SQL + state bulk views
  mcp_server.py, cli.py         two front ends over pipeline
tests/                          offline tests (fixtures + mocked HTTP); `pytest -m live` hits real APIs
.github/workflows/              tests on push; weekly refresh using repo secrets
```

<!-- BEGIN GENERATED: data-scope (edit src/candidate_modeling/scope.py, then run `cm docs`) -->
## Data scope and rules

### Time

- **two-clocks**: There are two time axes. Transaction time (`date`) is when a contribution was made; use it to compare elections. Observation time (`first_seen`, `valid_from`, `removed_at`, `as_of`) is when this database saw a version of a row; use it to ask what the filings said at a past moment. Observation history starts at each source's first load and never reaches back further.
- **election-window**: For election comparisons a contribution belongs to election year Y if dated after the reference election day of Y-1 and on or before that of Y. This keeps odd-year elections separate from even-year ones.
- **cycle-bucket**: `cycle` is a two-year, even-year bucket (FEC style). It folds odd-year money into the next even year, so never use it to compare an odd-year election with an even-year one.
- **reference-day**: Reference election day = the Tuesday after the first Monday in November. Every year has one, but only jurisdictions with contests that year (e.g. PA judicial and municipal, TX constitutional amendments) actually vote; primaries and runoffs are not modeled yet.

### Money

- **itemized-only**: Totals are itemized contributions as filed. FEC small-dollar money (under $200 per donor per cycle) is not itemized and appears only in fec_totals; Georgia lump sums are typed 'unitemized'.
- **clean-view**: Sum over contributions_clean (or contributions_as_of): they exclude memo rows (FEC sub-items) and rows that have been removed at the source.
- **loans-excluded**: Loans, loan payments, and interest are not contributions and are excluded.
- **returns-negative**: Returned contributions are negative amounts where the source reports them.
- **in-state-share**: In-state share counts only rows with a known donor state; unitemized lump sums are left out.
- **nominal-dollars**: Amounts are nominal dollars; nothing is inflation-adjusted.
- **statewide-double-count**: Statewide totals add up money received by every committee, so committee-to-committee transfers (PAC to candidate, party to candidate) and conduits that forward earmarked gifts (e.g. ActBlue Texas) are counted at each hop. Prefer the individuals-only total when comparing statewide money.
- **self-funding**: A candidate's money to their own campaign is often typed 'individual' (e.g. $200M from Richard Jackson to Jackson for Governor in GA, 2026). Check top_donors and pct_from_top_donor before reading individual totals as grassroots support.

### Comparing elections

- **coverage-first**: Only compare windows that both sources cover completely. compare_elections reports coverage per year and marks partial windows; never present a partial window as a decline.
- **reporting-lag**: Filings trail the calendar. Near and just after election day the latest window is incomplete (the final reports are not filed yet), so its late checkpoints undercount; compare_elections flags this as reporting_incomplete.
- **align-by-days**: Align elections by days before election day and compare cumulative money at the same checkpoints (300, 180, 90, 30, 7, 0 days out), especially when one election is still in progress.
- **like-with-like**: Compare the same candidate, the same office, or the same jurisdiction. Odd-year and even-year ballots have different offices on them, so statewide odd-versus-even comparisons measure different contests.

### Identity

- **no-guessed-ids**: Never guess source IDs. When find_candidate returns several plausible matches, ask the user.
- **committee-ids**: In some states (e.g. PA, MI) money is filed under a committee whose ID differs from the candidate's filer ID; profile the committee.
- **names-not-linked**: Donor names are free text and are not linked across sources or years; the same person can appear under several spellings.

### Sources

#### FEC: [Federal Election Commission (FEC) API](https://api.open.fec.gov/developers/)

- **Covers:** Federal candidates (President, Senate, House): per-cycle totals and itemized receipts of their authorized committees (designation P/A).
- **History from:** 1970s (per cycle, as far as the FEC API has data)
- **Refresh:** Pulled per candidate; itemized pulls are resumable and rate-limited (1,000 req/hr with a key).
- **Excludes:** Joint fundraising, party, and outside committees (unless added as committee IDs); Individual donations under $200 per cycle (not itemized; appear only in totals)
- **Rule:** Rows are keyed by sub_id (unique); memo rows (memo_code X) are sub-items and excluded from sums.
- **Rule:** Whole-election totals rollups (cycle = null) are skipped; totals are per two-year cycle.
- **Rule:** Rows are marked removed only after a complete pass over a committee-cycle.
- **Known gap:** Itemized data lags summary totals by weeks; newest itemized dates trail coverage_end_date.
- **Known gap:** Only cycles/committees that have been fetched are present; check fetch_state.complete.

#### TX: [Texas Ethics Commission (TEC)](https://www.ethics.state.tx.us/search/cf/)

- **Covers:** State and some local candidates, officeholders, and PACs that file with the TEC.
- **History from:** July 2000 (electronic filings)
- **Refresh:** Daily full-database zip; sync re-downloads only when Last-Modified changes.
- **Excludes:** Federal candidates (use FEC); Local filers who file only with a city or county
- **Rule:** Only contribs_##.csv load; cont_ss/cont_t (special-session, daily pre-election) are re-reported later.
- **Rule:** Rows with infoOnlyFlag = 'Y' were superseded by an amended report and are dropped.
- **Rule:** Candidates file under their own filer ID (filerTypeCd COH).
- **Known gap:** Paper-era filings before July 2000 are not in the database.

#### MI: [Michigan Bureau of Elections (MiTN)](https://mi-boe.entellitrak.com/etk-mi-boe-prod/page.request.do?page=gov.mi.boe.component.cfrexport.page.cfrexportdownload)

- **Covers:** All committees filing with the state: candidate, PAC, party, ballot question, independent expenditure.
- **History from:** 1998 (includes data migrated from the old MERTS system)
- **Refresh:** One file per year, refreshed daily; sync defaults to the last 4 years (pass years= for more).
- **Excludes:** Federal candidates (use FEC); Committees below the $5,000/yr e-filing threshold may be absent
- **Rule:** Data rows carry a trailing tab and stray quotes; parsed with explicit columns and quoting disabled.
- **Rule:** Candidate committees are com_type = 'Candidate'; money is filed under the committee ID.
- **Known gap:** Amendments may not be reflected in the export; verify key figures against the filed statement.

#### PA: [Pennsylvania Department of State](https://www.pa.gov/agencies/dos/resources/voting-and-elections-resources/campaign-finance-data)

- **Covers:** State and local candidates and committees filing with the Department of State.
- **History from:** 2000
- **Refresh:** One zip per election year; sync defaults to the last 4 years (pass years= for more).
- **Excludes:** Federal and party-office candidates; County-level filers who file only with their county
- **Rule:** Rebuilt with a tolerant parser (unescaped inner quotes, raw line breaks, cp1252 lines); unparseable rows are counted, never silently dropped.
- **Rule:** Up to three (date, amount) pairs per row are unpivoted into separate contributions.
- **Rule:** Only the latest-submitted report per (filer, year, reporting cycle) counts; amended originals are dropped.
- **Rule:** Candidates usually raise money through a committee with a different FILERID; profile the committee.
- **Rule:** Sections IA/IC are from political committees; IB/ID from individuals; IIF/IIG in-kind.
- **Known gap:** No unique contribution ID: transaction IDs are row positions, so cross-sync history for PA is approximate.
- **Known gap:** Donor type outside committee sections is a name-pattern guess.
- **Known gap:** The filer file's MONETARY/INKIND columns do not reconcile with itemized sums.

#### GA: [Georgia Government Transparency & Campaign Finance Commission (PeachFile)](https://efile.ethics.ga.gov/)

- **Covers:** State candidates and non-candidate committees filing in PeachFile.
- **History from:** January 2025
- **Refresh:** Daily extract via the PeachFile API; sync skips when dataExtractedDate is unchanged.
- **Excludes:** Federal candidates (use FEC); Filings in the legacy system (2017-2024)
- **Rule:** Only Contribution and Return Contribution rows load; loans, loan payments, and interest are excluded.
- **Rule:** Returned contributions are negative amounts.
- **Rule:** Amended reports repeat transactions; the most recently filed copy wins.
- **Rule:** Unitemized contributions are lump sums typed 'unitemized' (no donor, excluded from geography shares).
- **Known gap:** Nothing before 2025: the 2025 election window (from Nov 2024) is only partly covered.
- **Known gap:** Some prominent filers (e.g. Raffensperger, Bottoms) are absent from the bulk export; cause unknown.
<!-- END GENERATED: data-scope -->

## Adding a state

1. Write `sources/states/<xx>.py` exposing `STATE`, `SOURCE`, `ID_KEY`, `RESULT_ID`, `sync()`, `is_synced()`,
   `search(name)`, and `load(con, profile)`. If the CSV is broken, use the tolerant parser in `_common.py`.
2. Register it in `sources/states/__init__.py`.
3. Add a fixture-based test in `tests/test_states.py` that covers that state's format quirks.

## Roadmap

- More state connectors, in priority order
- Industry classification from employer/occupation
- Donor overlap between candidates; donor migration from state races to federal races
- Expenditures (where campaigns spend)
- Open States legislative record linked to donors
