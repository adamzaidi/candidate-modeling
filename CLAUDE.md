# CLAUDE.md — candidate-modeling

Private repo (`adamzaidi/candidate-modeling`) for modeling political candidates' finances, built up over
time. Owner: Adam. Exposed to agents as the `candidate-modeling` MCP server (user scope, `cm serve`).

## Architecture
- `pipeline.py` holds every operation; `cli.py` and `mcp_server.py` are thin front ends. New features
  go in `pipeline.py` first.
- All sources normalize into one DuckDB table, `contributions` (schema in `store.py`). Always aggregate
  over `contributions_clean` (memo rows removed).
- State connectors (`sources/states/<xx>.py`) share the interface `STATE, SOURCE, sync(), is_synced(),
  search(name), load(con, profile)`. `sync()` converts the state's bulk files to parquet once;
  `load()` pulls one candidate's rows from parquet into the DB with a DELETE + INSERT (idempotent).
- FEC itemized pulls save a keyset cursor to `fetch_state` after every page, so a re-run continues
  where it stopped. Upserts are keyed on `sub_id`, the FEC's unique row id (`transaction_id`
  repeats across amendments).
- History: loaders never DELETE. `store.merge_query` / `merge_rows` diff a staged snapshot against stored rows
  (content hash), archive replaced versions to `contribution_history`, and with `full=True` mark absent rows
  `removed_at`. State loads are full snapshots. FEC pages are partial merges, and removal happens only when
  a pass completes (pass_start is kept in the fetch_state cursor). `contributions_clean` = live and
  non-memo. `contributions_as_of(ts)` is a table macro. `store.now` is the clock (patched in tests).
- **Rules and scope live in `scope.py` (global RULES with ids) and each connector's `SCOPE` dict.** The README
  "Data scope and rules" section is generated from them (`cm docs`; a test fails if stale), and the MCP
  instructions embed the rules. Never document a data rule only in prose; add it to the registry.
- Election comparisons (`elections.py`) use transaction time: window(Y) = (election_day(Y-1), election_day(Y)].
  Statewide comparisons run connectors' `normalized_sql(None)` over the bulk parquet (no profile needed).
  `covered_years()` per connector drives coverage-first; deltas are skipped for partial windows.
- Calendar: `reference/elections.yaml` (sourced dates only; tests enforce fields, https sources, November
  rule for generals). `elections.stage_window(year, stage, state)`. Stages other than general need a state.
- Offices: each connector has `committees_sql()` (committee_id, committee_name, kind, office_raw,
  office_category, district) mapping raw labels with `_common.case_map` onto `scope.OFFICE_CATEGORIES`.
  TX links SPACs to candidates through spacs.csv (that's how "Texans for Greg Abbott" becomes governor money;
  without it governor 2022 showed $0.6M instead of $158.5M). GA offices come from PeachFile GetCandidateDetails
  (PageSize above 100 trips their WAF). Only map PA codes confirmed by filer names; leave the rest unknown.
- Profiles (`candidates/*.yaml`) are the source of truth for which IDs belong to a person. Never
  guess IDs. When find_candidate returns several plausible matches, confirm with Adam.

## Data facts learned the hard way
- TEC bulk zip: `https://prd.tecprd.ethicsefile.com/public/cf/public/TEC_CF_CSV.zip` (~1 GB, daily).
  Has quoted newlines, so DuckDB must not use `null_padding` (it breaks the parallel reader). Load
  only `contribs_##.csv`; `cont_ss` and `cont_t` would double count. Drop rows with
  `infoOnlyFlag='Y'`.
- Michigan MiTN: JSON listing at `page.request.do?page=gov.mi.boe.component.cfrexport.page.cfrexportresults`
  and files at `...cfrexportfile&id=N`. Tab-delimited; data rows have one more field than the
  header (trailing tab); contains stray quotes, so quoting is disabled. Verified 1,725,307 of
  1,725,307 rows for 2026.
- FEC `/candidate/{id}/totals/` returns whole-election rollups with `cycle=null`. Pass
  `election_full=false` and skip null cycles.
- The old michigan.gov/nictusa download URLs are dead (system moved to MiTN in 2025).
- PA: `.../campaign-finance-data/{year}.zip` on pa.gov. Not valid CSV (unescaped inner quotes, raw
  newlines, some cp1252 lines), so it goes through the `_common.to_clean_csv` tolerant parser. DuckDB's
  `strict_mode=false` silently dropped 150 rows; never use it as the fix. Up to 3 date/amount pairs
  per row (unpivot). The filer file has one row per report; latest SubmittedDate per (FILERID, EYEAR,
  CYCLE) wins. MONETARY/INKIND on the filer file do not reconcile with itemized sums. Candidates
  (FILERTYPE 1) raise through committees (type 2) with different IDs; use the committee ID.
- GA: PeachFile JSON API (`api-peachfile.ethics.ga.gov/api`) found by reading the Angular bundle.
  POST `ExportPublicData/GetExportPublicDownloadData` {FilingYear, TransactionTypeCode: "TCON",
  Type: "CSV"}. Years before 2025 return a header only. Same broken-quote problem as PA. Loans share
  the file (exclude them). Amended copies repeat transaction IDs (latest report_filed_date wins).
  Raffensperger and Bottoms are absent from the export: a known gap, not yet explained.
- Scripted edits: `str.replace` silently no-ops when the target is missing (store.py contains a literal ␀).
  Always `assert old in s` first. A missed edit once made 62,097 rows look amended.
- Never glob data files: iCloud creates `name 2.parquet` conflict copies. Use `_common.parquet_list`/`data_files`.
- Labels (labels.py): party, self_match macro (surname word + first 4 letters), contribution_kind (DERIVED, not
  hashed; bump store.HASH_VERSION if the hashed set changes), conduits. Loans are labeled, never counted.
- AZ (sources/states/arizona.py): no bulk file. Lists via GetNEWTableData (DataTables order params REQUIRED or
  it returns ""; max 100/page; pages 1 candidates, 2 PACs, 3 parties, 4 orgs). Income via GetNEWDetailedTableData
  with Page = (list page + 1) * 10 (the wrong page silently returns 0 rows). Per-committee parquet + manifest
  income checkpoint, so sync is resumable and refreshes are incremental. Kinds: 'Receive CCEC Funding' ->
  public_funds (excluded and reported); '$100 or Less' -> unitemized; '$5 Qualifying' handoff -> other_receipt.
- WI (sources/states/wisconsin.py): Sunshine tRPC, GET /api/trpc/<proc>?input={"json": {...}}.
  getTransactions is statewide with cursor paging (take <= 1000); transactionType must be an ARRAY ([1]).
  Category codes give contribution_kind exactly. The officeIds filter targets IE subjects, NOT a committee's office,
  so WI has no office data; candidate committees carry no person name (no self-match). Years download into
  tx_YYYY_partial and are renamed only when complete, so a partial year is never read.
- NC (NOT BUILT, research done): cf.ncsbe.gov/CFTxnLkup. GET AdvancedSearch -> __RequestVerificationToken;
  POST AdvancedSearch with Filter.StartDate/EndDate (MM/DD/YYYY), Filter.ReceiptAll/CommitteeAll/OfficeAll/PartyAll=true,
  Filter.IsOrg=false -> Results page; its grid has data-search-params (JSON). POST /CFTxnLkup/Export with
  searchParamsJson + token -> CSV (transinq_results.csv; one day ~4k rows). CSV has no transaction ID and no
  committee type/party/office; the JSON grid (GET Results?handler=PagedData&searchParamsJson=...&pageSize=500)
  has CommTypeCode/PartyCode/CandOfficeCode but takes ~18s per 500 rows, so use it only for a committee lookup.
  "Aggregated Individual Contribution" rows = unitemized.
- Never use DuckDB `strict_mode=false` / `ignore_errors` as a fix for broken files. Count rows
  against an independent parse first.

## Conventions
- Tests are offline (fixtures in `tests/conftest.py`, HTTP mocked with `responses`). `pytest -m live`
  runs the real-API smoke tests. Every new state needs a fixture test covering its format quirks.
- `data/` is gitignored and reproducible. `CM_DATA_DIR` / `CM_CANDIDATES_DIR` override paths (tests use them).
- Analysis must be factual and source-cited, not advocacy. Surface the caveats (`pipeline.CAVEATS`) with numbers.
- Secrets: `.env` locally; GitHub secrets `FEC_API_KEY` and `OPENSTATES_API_KEY` for Actions.

## Status (2026-11-03)
- Done: FEC, TX, MI, PA, GA, history tracking, election comparisons (stages + offices), sourced calendar, scope registry, MCP server, CLI, 102 offline tests + 2 live, CI + weekly refresh.
- Talarico: TX loaded (62,097 rows, ~$4.0M, 2017 to June 2026). FEC pull is only partial (2 pages,
  DEMO_KEY). Run `cm fetch talarico --sources fec` once the data.gov key is in `.env`. His committee
  totals: $146.3M receipts, $75.3M unitemized (through 2026-10-14).
- States done: TX, MI, PA, GA, AZ, WI. Next: NC, NV, FL, OH, CA, NY.
