# candidate-modeling

Data modeling on political candidates — campaign finance, legislative record, and electoral performance — built up over time. First subject: **James Talarico** (Texas).

## Layout

```
candidates/            one YAML profile per candidate (IDs for each data source, offices, notes)
src/candidate_modeling/
  sources/             fetchers, one module per data source
  config.py            loads candidate profiles + API keys
scripts/               CLI entry points (fetch, build)
data/raw/              untouched API/bulk downloads (gitignored)
data/processed/        cleaned tables (gitignored; reproducible from raw)
notebooks/             exploration and analysis
```

## Data sources

| Source | Covers | Status |
|---|---|---|
| [FEC API](https://api.open.fec.gov/developers/) | Federal campaign finance (US Senate run) | first pass |
| [Open States API](https://docs.openstates.org/api-v3/) | Texas House bills, sponsorships, votes | first pass |
| [Texas Ethics Commission](https://www.ethics.state.tx.us/search/cf/) | State-level campaign finance (TX House races) | planned — bulk CSV |
| Election results (TX SOS / MIT Election Lab) | Vote share by race and precinct | planned |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # add API keys
python scripts/fetch.py talarico
```

## Ideas / roadmap

- Donor composition: in-state vs out-of-state, small-dollar share, industry mix, change between state and federal races
- Legislative record: bill sponsorship topics, party-line vote rate, bipartisan co-sponsorship network
- Fundraising velocity around news events
- Compare against other candidates once more profiles exist
