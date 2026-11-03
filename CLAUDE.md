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
- Never use DuckDB `strict_mode=false` / `ignore_errors` as a fix for broken files. Count rows
  against an independent parse first.

## Conventions
- Tests are offline (fixtures in `tests/conftest.py`, HTTP mocked with `responses`). `pytest -m live`
  runs the real-API smoke tests. Every new state needs a fixture test covering its format quirks.
- `data/` is gitignored and reproducible. `CM_DATA_DIR` / `CM_CANDIDATES_DIR` override paths (tests use them).
- Analysis must be factual and source-cited, not advocacy. Surface the caveats (`pipeline.CAVEATS`) with numbers.
- Secrets: `.env` locally; GitHub secrets `FEC_API_KEY` and `OPENSTATES_API_KEY` for Actions.

## Status (2026-11-03)
- Done: FEC, TX, MI, PA, GA, MCP server, CLI, 51 offline tests + 2 live, CI + weekly refresh.
- Talarico: TX loaded (62,097 rows, ~$4.0M, 2017 to June 2026). FEC pull is only partial (2 pages,
  DEMO_KEY). Run `cm fetch talarico --sources fec` once the data.gov key is in `.env`. His committee
  totals: $146.3M receipts, $75.3M unitemized (through 2026-10-14).
- States done: TX, MI, PA, GA. Next: AZ, WI, NC, NV, FL, OH, CA, NY.
