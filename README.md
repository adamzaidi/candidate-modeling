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
| Next | GA, PA, AZ, WI, NC, NV, FL, OH, CA, NY | | planned (`sources/states/__init__.py`) |
| Legislative | [Open States](https://docs.openstates.org/api-v3/) | Bills, votes | first-pass fetcher |

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env               # add FEC_API_KEY (free: api.data.gov/signup)

cm find "James Talarico" --state TX          # IDs from FEC + synced states
cm sync TX                                   # one-time ~1 GB download -> parquet (~2 min)
cm add "James Talarico" --slug talarico --state TX --fec S6TX00479 --tx-filer 00081913
cm fetch talarico                            # load into data/candidates.duckdb
cm summary talarico --jurisdiction TX        # geography, donor size mix, employers, trend
cm query "SELECT state, sum(amount) FROM contributions_clean WHERE candidate_slug='talarico' GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
```

## For AI agents (MCP)

```bash
claude mcp add candidate-modeling -s user -- /path/to/repo/.venv/bin/cm serve
```

Tools: `find_candidate`, `add_candidate`, `list_candidates`, `list_supported_states`, `sync_state`,
`fetch_finances`, `finance_summary`, `compare_candidates`, `describe_schema`, `run_query`.

Typical flow: **find → add → fetch → summary/query**. `run_query` is read-only, takes one SELECT,
and returns at most 500 rows. Once a state is synced, its entire bulk database is also queryable as
views (`tx_filers`, `tx_contribs_raw`, `mi_contribs_raw`), so an agent can answer questions about
any filer in that state without creating a profile first.

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

## Reading the numbers correctly

- **Small-dollar money isn't itemized federally.** The FEC only itemizes donors who give more than
  $200 per cycle, so small-dollar totals appear in `fec_totals`, not in `contributions`.
- **Exclude memo rows.** Sum over `contributions_clean`; FEC memo rows are sub-items of other rows.
- **FEC itemized data lags summary totals by weeks.** Filings are processed slowly, so the newest
  itemized rows can trail `coverage_end_date`.
- **Texas:** superseded (amended) rows are dropped. Special-session and daily pre-election reports
  are skipped because their contributions are re-reported on the next regular report.
- **Michigan:** amendments may not be reflected in the export. Committees under $5k/yr aren't
  required to e-file.
- Employer and occupation are self-reported free text and not yet mapped to industries.

## Adding a state

1. Write `sources/states/<xx>.py` exposing `STATE`, `SOURCE`, `sync()`, `is_synced()`, `search(name)`, and `load(con, profile)`.
2. Register it in `sources/states/__init__.py`.
3. Add a fixture-based test in `tests/test_states.py` that covers that state's format quirks.

## Roadmap

- More state connectors, in priority order
- Industry classification from employer/occupation
- Donor overlap between candidates; donor migration from state races to federal races
- Expenditures (where campaigns spend)
- Open States legislative record linked to donors
