# candidate-modeling

Campaign-finance data for any US political candidate, pulled from official sources into one local
database. Agents can query it directly through an MCP server, and the same operations are available
as a CLI. First subject: **James Talarico** (Texas).

## Reports

- **[How much do early filings understate final campaign totals?](reports/revisions/output/report.md)**:
  revision analysis and nowcasting for Texas and Pennsylvania (2022-2026), with BEA real-dollar and
  per-resident comparisons. Built entirely by [`reports/revisions/build.py`](reports/revisions/build.py):
  every number comes from the generated tables, and `manifest.json` records data vintages and rules.

## Coverage

| Level | Source | What | Status |
|---|---|---|---|
| Federal | [FEC API](https://api.open.fec.gov/developers/) | Every federal candidate: totals + itemized receipts | done (resumable pulls) |
| Texas | [Texas Ethics Commission](https://www.ethics.state.tx.us/search/cf/) bulk CSV | All state/local filers since 2000 (~37M contributions) | done |
| Michigan | [MiTN all-transactions export](https://mi-boe.entellitrak.com/etk-mi-boe-prod/page.request.do?page=gov.mi.boe.component.cfrexport.page.cfrexportdownload) | All committees, by year since 1998 | done |
| Pennsylvania | [DOS full export](https://www.pa.gov/agencies/dos/resources/voting-and-elections-resources/campaign-finance-data) | State/local filers, by year since 2000 | done |
| Georgia | [PeachFile](https://efile.ethics.ga.gov/) bulk API | Filings from 2025 on (legacy 2017-2024 not covered) | done |
| Arizona | [See The Money](https://seethemoney.az.gov/) JSON endpoints, committee by committee | Per two-year cycle as synced (no bulk file) | done |
| Wisconsin | [Sunshine](https://campaignfinance.wi.gov/) statewide API (replaced CFIS) | Since mid-2008; no office data | done |
| North Carolina | [SBoE transaction search](https://cf.ncsbe.gov/CFTxnLkup/) CSV exports, month by month | Per calendar year as synced; no office data | done |
| Next | NV, FL, OH, CA, NY | | planned (`sources/states/__init__.py`) |
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
cm compare-elections 2025 2026 --state PA                          # every committee in the state
cm compare-elections 2022 2026 --state TX --office governor        # one office
cm compare-elections 2022 2026 --state TX --group-by office        # every office side by side
cm compare-elections 2022 2026 --state TX --stage primary          # money before the primary
cm compare-elections 2022 2026 --slugs talarico                    # profiled candidates
```

**Stages** (`--stage`): `general` (default, last general through this one), `primary`, `primary_runoff`,
`general_campaign` (after the last primary or runoff through the general), and `general_runoff`. Primary
and runoff dates come from [`reference/elections.yaml`](reference/elections.yaml). Every date there cites
a source, and tests reject entries without one. To add a year or state, add sourced lines to that file.

**Offices** (`--group-by office`, `--office`): each state's raw office labels are mapped onto one shared
set of categories (governor, state_house, supreme_court, pac, ...; full list below). Results report
`pct_unclassified_office`, so you can see how much money couldn't be placed.

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
- **vintage-reconstruction**: Every row carries filed_date (when its report became public) where the source records it: TX report received date, PA report submitted date, GA report filed date. revision_analysis rebuilds the total known on any past date from these, measures how much early totals understated the final (like BEA's advance vs later estimates), and nowcasts windows still settling, with a leave-one-out backtest. MI, WI, AZ, and NC have no filing dates, so they are excluded.
- **election-window**: For election comparisons a contribution belongs to election year Y if dated after the reference election day of Y-1 and on or before that of Y. This keeps odd-year elections separate from even-year ones.
- **cycle-bucket**: `cycle` is a two-year, even-year bucket (FEC style). It folds odd-year money into the next even year, so never use it to compare an odd-year election with an even-year one.
- **reference-day**: General election day comes from reference/elections.yaml when the state/year is listed, otherwise the Tuesday after the first Monday in November. Every year has one, but only jurisdictions with contests that year actually vote (see each calendar entry's scope: statewide or local).
- **stage-window**: Stages split the year: primary = after last year's general through the primary; primary_runoff = after the primary through the runoff; general_campaign = after the last primary/runoff through the general; general_runoff = after the general through its runoff. Primary and runoff dates come only from reference/elections.yaml, where every date has a source; a year with no entry reports no_election.
- **calendar-sourced**: Never infer an election date from a pattern. Add dates to reference/elections.yaml only with a source URL; tests reject entries without one.

### Money

- **itemized-only**: Totals are itemized contributions as filed. FEC small-dollar money (under $200 per donor per cycle) is not itemized and appears only in fec_totals; Georgia lump sums are typed 'unitemized'.
- **clean-view**: Sum over contributions_clean (or contributions_as_of): they exclude memo rows (FEC sub-items) and rows that have been removed at the source.
- **loans-excluded**: Loans, loan payments, interest, and other non-contribution receipts are not contributions. They are labeled (contribution_kind 'loan' / 'other_receipt') where the source includes them, and excluded from every total. Public campaign funding (AZ Clean Elections) is contribution_kind 'public_funds', also excluded and reported as public_funds_total. Loans are reported separately as loans_total and self_loans_total (a candidate lending to their own campaign), available for MI, GA, AZ, and FEC; TX and PA loans live in files not loaded yet (roadmap cash-and-debt).
- **contribution-kind**: Every contribution has contribution_kind: monetary, in_kind, return (negative), transfer (from another committee of the same candidate, FEC line 12), loan, or other_receipt. Totals include monetary, in_kind, return, and transfer; in_kind_total and returns_total are reported separately.
- **returns-negative**: Returned contributions are negative amounts where the source reports them.
- **in-state-share**: In-state share counts only rows with a known donor state; unitemized lump sums are left out.
- **nominal-dollars**: Stored amounts are nominal dollars. Inflation adjustment happens only in outputs that say so.
- **bea-normalization**: When BEA data is synced (sync_state('BEA')), compare_elections adds a bea block: real dollars via the PCE price index (DPCERG, rebased to the latest full year), per capita (state population), and dollars per $100k of state personal income (SAINC1). A year without published data uses the latest available year or a partial-quarter price average, and says so (income_year_is_latest_available, pce_basis). BEA revises these series; the file vintages are included in every result.
- **statewide-double-count**: Statewide totals add up money received by every committee, so committee-to-committee transfers (PAC to candidate, party to candidate) and conduits that forward earmarked gifts (e.g. ActBlue Texas) are counted at each hop. Prefer the individuals-only total when comparing statewide money.
- **self-funding**: A candidate's money to their own campaign is usually typed 'individual' (e.g. $200M from Richard Jackson to Jackson for Governor in GA, 2026). self_funding_total flags individual gifts whose donor name matches the candidate (surname as a word plus the first 4 letters of the first name). Nicknames on one side only are missed, so treat it as a floor and check top_donors and pct_from_top_donor too.
- **conduits**: Conduits (ActBlue, WinRed, and similar) forward earmarked gifts that candidates also itemize. Committees flagged is_conduit (e.g. ActBlue Texas) are reported as conduit_recipient_total; total_excluding_conduits removes them. Gifts whose donor is a conduit are reported as from_conduit_total.
- **party-labels**: Party is the recipient committee's party on file, normalized to democratic, republican, libertarian, green, other, cross_filed (PA judicial cross-filing), nonpartisan, unknown (a candidate with no party on file), or not_applicable (PACs and other non-party committees). Party committees take their party from their name. Texas files carry no party, so TX candidates are 'unknown' until a candidate-list source is added.

### Comparing elections

- **coverage-first**: Only compare windows that both sources cover completely. compare_elections reports coverage per year and marks partial windows; never present a partial window as a decline.
- **reporting-lag**: Filings trail the calendar. Near and just after election day the latest window is incomplete (the final reports are not filed yet), so its late checkpoints undercount; compare_elections flags this as reporting_incomplete.
- **align-by-days**: Align elections by days before election day and compare cumulative money at the same checkpoints (300, 180, 90, 30, 7, 0 days out), especially when one election is still in progress.
- **like-with-like**: Compare the same candidate, the same office, or the same jurisdiction. Odd-year and even-year ballots have different offices on them, so statewide odd-versus-even comparisons measure different contests.

### Offices

- **office-taxonomy**: Offices are normalized into shared categories (see OFFICE_CATEGORIES). Raw labels are always kept as office_raw; unmapped codes become 'unknown' rather than being guessed.
- **office-snapshot**: A committee's office is its latest office on file, not necessarily the office it was raising for in an earlier window. A candidate who switched races carries the new office back in time.
- **linked-committees**: Some states route a candidate's money through a separate committee. Texas specific-purpose committees that the TEC SPAC index links to exactly one candidate (SUPPORT/ASSIST) count as that candidate's committee and take their office (e.g. Texans for Greg Abbott -> governor). Committees that oppose, or back several candidates, stay 'pac'.
- **office-not-ballot**: Money raised by officeholders who are not on the ballot that year still counts under their office; office totals measure money held by that office's committees, not only active races.

### Identity

- **no-guessed-ids**: Never guess source IDs. When find_candidate returns several plausible matches, ask the user.
- **committee-ids**: In some states (e.g. PA, MI) money is filed under a committee whose ID differs from the candidate's filer ID; profile the committee.
- **names-not-linked**: Donor names are free text and are not linked across sources or years; the same person can appear under several spellings.

### Office categories

- `governor`: Governor
- `lieutenant_governor`: Lieutenant governor
- `attorney_general`: Attorney general
- `secretary_of_state`: Secretary of state
- `statewide_other`: Other statewide executive (comptroller, treasurer, auditor, agriculture, land, railroad, insurance, labor, school superintendent, public service commission)
- `state_senate`: State senate
- `state_house`: State house / assembly
- `state_board`: Elected state boards (board of education, university regents and trustees)
- `supreme_court`: Courts of last resort (state supreme court; TX Court of Criminal Appeals)
- `appellate_court`: Intermediate appellate courts (TX/GA/MI courts of appeals; PA Superior and Commonwealth)
- `trial_court`: Trial and lower courts (district, circuit, superior in GA, common pleas, probate, magisterial)
- `prosecutor`: District attorneys and solicitors general
- `county_office`: County offices (commissioners, sheriff, clerks, tax commissioner, coroner)
- `municipal_office`: City offices (mayor, council, aldermen)
- `school_board`: Local school boards
- `party_office`: Party offices (state or county party chair)
- `federal`: Federal office filed with a state agency (rare; federal money is in FEC)
- `pac`: Non-candidate committees: PACs, independent expenditure and issue committees
- `party_committee`: Party committees (state, county, district)
- `ballot_measure`: Ballot question committees
- `other_committee`: Other committees (caucuses, direct-expenditure filers, unclassified committee types)
- `unknown`: Candidate committee whose office code is missing or not yet mapped (raw code kept in office_raw)

### Roadmap (not built yet)

| Item | Why | Source | Effort |
|---|---|---|---|
| Incumbent / challenger / open-seat label | Explains much of election-over-election change (e.g. PA governor 2022 open seat vs 2026 incumbent) | FEC incumbent_challenge; TX officeholder status; seat-holder lists elsewhere | medium |
| In-district donor share for legislative races | In-state share is too coarse for a state house seat | Census ZIP-to-district crosswalk + stored district | medium |
| Donor industry classification | Which industries fund which offices | Rules over employer/occupation text, validated on a hand-labeled sample | medium-high |
| Election outcomes (won / lost / runoff) | Did money predict winners | State results files | high |
| Party for Texas candidates | TX is 'unknown' today | TX SOS candidate filings / primary ballots | medium |
| Expenditures: where campaigns spend (vendors, consultants, media, salaries) | Half of campaign finance; every source already publishes it | TX expend_##, MI expenditure files, PA expense, GA TEXP, FEC Schedule B | medium |
| Independent expenditures and outside groups (super PACs, IE committees) | Money spent about a candidate, often larger than what they raise | FEC Schedule E, MI IE committees, TX direct campaign expenditures, GA IE | medium-high |
| Cash on hand, debts, and loans over time | Financial health and burn rate, not just money raised | TX cover sheets, PA filer BEGINNING, GA reports, FEC totals (already stored) | medium |
| Donor entity resolution across sources and years | Enables donor overlap between candidates, repeat vs first-time donors, and state-to-federal donor migration (e.g. Talarico's Texas House donors vs his Senate donors) | Name + ZIP + employer matching | high |
| Link money to votes and bills | Who funds legislators who vote a given way | Open States (key already configured) | high |
| Lobbyist and government-contract cross-reference | Donors who lobby or hold state contracts | State lobby registrations, contract databases | high |
| Inflation and per-capita adjustment | Fair cross-decade and cross-state comparisons | CPI-U; Census population / registered voters | low |
| Alerts on new filings and amendments for watched candidates | History tracking already detects changes; this pushes them | load_runs + scheduled refresh | low |
| Sync data-quality monitors | Catch upstream format changes (row-count drops, new parse failures, schema drift) | sync manifests | low |
| Local races and ballot measures beyond state filers | Most offices are local | County/city disclosure systems | high |
| More state connectors | Coverage | AZ, WI, NC, NV, FL, OH, CA, NY next | per state |

Deliberately not planned: Guessing donor gender, race, or ethnicity from names (unreliable and easy to misuse). 'Dark money' labels without documentary evidence (needs investigation, not field mapping).

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

#### BEA: [Bureau of Economic Analysis (regional and national accounts)](https://apps.bea.gov/)

- **Covers:** Reference data for normalization: state personal income, population, and per capita personal income (SAINC1); PCE price index (DPCERG) for inflation adjustment.
- **History from:** 1929 (SAINC1, PCE)
- **Refresh:** Bulk files; sync re-downloads when Last-Modified changes. BEA revises past years, so the vintage is recorded with every result.
- **Excludes:** Not campaign finance data; used only to put campaign money in context
- **Rule:** Real dollars use the PCE price index (2017 = 100), rebased to the latest full year with an annual value.
- **Rule:** A year with no annual PCE value uses the average of its published quarters (flagged partial).
- **Rule:** Per-capita and share-of-income figures use that year's state values, or the latest published year if the election year is not out yet (flagged).
- **Known gap:** State personal income for the current year is published months after it ends.

#### TX: [Texas Ethics Commission (TEC)](https://www.ethics.state.tx.us/search/cf/)

- **Covers:** State and some local candidates, officeholders, and PACs that file with the TEC.
- **History from:** July 2000 (electronic filings)
- **Refresh:** Daily full-database zip; sync re-downloads only when Last-Modified changes.
- **Excludes:** Federal candidates (use FEC); Local filers who file only with a city or county
- **Rule:** Only contribs_##.csv load; cont_ss/cont_t (special-session, daily pre-election) are re-reported later.
- **Rule:** Rows with infoOnlyFlag = 'Y' were superseded by an amended report and are dropped.
- **Rule:** Candidates file under their own filer ID (filerTypeCd COH), and often also raise through a specific-purpose committee linked in the TEC SPAC index (rule linked-committees).
- **Rule:** Offices come from filers.csv (office sought, else held) and spacs.csv links.
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
- **Rule:** Offices come from the PeachFile candidate registry (GetCandidateDetails), joined on filing entity ID.
- **Known gap:** Nothing before 2025: the 2025 election window (from Nov 2024) is only partly covered.
- **Known gap:** Some prominent filers (e.g. Raffensperger, Bottoms) are absent from the bulk export; cause unknown.
- **Known gap:** Leadership committees (e.g. WBJ Leadership Committee, Georgians First) register as non-candidate committees and are classified 'pac', not under the office of the official they are tied to.

#### AZ: [Arizona Secretary of State (See The Money)](https://seethemoney.az.gov/)

- **Covers:** Statewide and legislative candidates, PACs, party committees, and organizations filing with the Secretary of State.
- **History from:** Per two-year cycle as synced (site data goes back to the 2000s)
- **Refresh:** No bulk file: sync walks every recipient committee per cycle (resumable; refresh refetches only committees whose income changed). A first full cycle takes on the order of an hour.
- **Excludes:** Federal candidates (use FEC); Local filers who file with a city or county; Independent expenditures and ballot-measure committees (not loaded yet)
- **Rule:** Only income transactions (Page 20) load; expenses are not loaded yet.
- **Rule:** Clean Elections public funding ('Receive CCEC Funding') is contribution_kind 'public_funds': reported, not counted as contributions. $5 qualifying contributions count as individual gifts; their hand-off to the CCEC fund is an other_receipt.
- **Rule:** 'In-State Contributions $100 or Less' are lump sums with no donor: contributor_type 'unitemized'.
- **Rule:** Income detail page = (entity list page + 1) * 10: 20 candidates, 30 PACs, 40 parties, 50 organizations.
- **Rule:** Transactions are keyed by PublicTransactionId.
- **Known gap:** Cycle-based: a cycle that has not been synced is not covered even if neighboring cycles are.

#### WI: [Wisconsin Ethics Commission (Sunshine, formerly CFIS)](https://campaignfinance.wi.gov/)

- **Covers:** State candidates, PACs, party and legislative campaign committees, conduits, and other registrants filing with the Ethics Commission; migrated history from mid-2008.
- **History from:** July 2008
- **Refresh:** Statewide API, one calendar year at a time; a year is re-downloaded only when its contribution count changes (~10 minutes per year).
- **Excludes:** Federal candidates (use FEC); Local candidates and referenda (filed with local clerks)
- **Rule:** Only transactionType 1 (Contribution) loads; conduit release records (type 3) are not double counted.
- **Rule:** contribution_kind comes from the category id (rows carry no category code): loans (commercial/personal), in-kind, returned contributions, other income and forgiven obligations (other_receipt), redirected (other_receipt).
- **Rule:** Donors with entityTypeOf PERSON are individuals; ANONYMOUS donors (small anonymous gifts) are unitemized.
- **Rule:** Recipients are the filing committee (createdByEntity); committee type and party come from its registration.
- **Rule:** The API uses superjson: the paging cursor's date must be sent with meta {values: {cursor.date: [Date]}}.
- **Known gap:** No office for candidate committees (not exposed by the public API), so office breakdowns are unavailable.
- **Known gap:** Candidate committees have no candidate person name, so name-based self-funding cannot be computed.
- **Known gap:** No donor employer field; occupation only.

#### NC: [North Carolina State Board of Elections](https://cf.ncsbe.gov/CFTxnLkup/)

- **Covers:** State and county candidate committees, PACs, party committees, and referendum committees whose reports were entered into the SBoE database.
- **History from:** Per calendar year as synced
- **Refresh:** Month-by-month CSV exports (~10 s each) plus one export per party per month; a year takes a few minutes. Data on the site updates overnight.
- **Excludes:** Federal candidates (use FEC); Reports filed only as scanned images (not data-entered)
- **Rule:** Only receipts load: the export's expenditure types are cleared before each request.
- **Rule:** 'Aggregated Individual Contribution' rows are lump sums: contributor_type 'unitemized'.
- **Rule:** Party comes from the SBoE party filter (DEM/REP), applied only to candidate and party committees and only when a committee appears under exactly one party (PACs appear under both, so the filter is not a registration field). NC DEC / NC REC are the state party executive committees.
- **Rule:** In-kind ('Nonmonetary Gift') is filed under expenditure code NMG, so that one code is kept in the export.
- **Rule:** Candidate committees are recognized by the '(LAST, FIRST)' suffix SBoE adds to their names.
- **Known gap:** No transaction ID: IDs are row hashes plus occurrence number, so NC history across syncs is approximate.
- **Known gap:** No office in the export, so office breakdowns are unavailable; the committee ID prefix shows state (STA), federal (FED), or county-level committees (e.g. WAK, MEC).
<!-- END GENERATED: data-scope -->

## Adding a state

1. Write `sources/states/<xx>.py` exposing `STATE`, `SOURCE`, `ID_KEY`, `RESULT_ID`, `sync()`, `is_synced()`,
   `search(name)`, and `load(con, profile)`. If the CSV is broken, use the tolerant parser in `_common.py`.
2. Register it in `sources/states/__init__.py`.
3. Add a fixture-based test in `tests/test_states.py` that covers that state's format quirks.
