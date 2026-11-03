# CLAUDE.md — candidate-modeling

Private repo for modeling political candidates over time. Owner: Adam. First candidate: James Talarico.

## Conventions
- One YAML profile per candidate in `candidates/<slug>.yaml`. Source IDs (FEC candidate ID, Open States person ID, TEC filer ID) live there — never hardcode them in code.
- One module per data source in `src/candidate_modeling/sources/`. Each exposes `fetch(profile, out_dir)` that writes raw JSON/CSV to `data/raw/<slug>/<source>/`.
- Raw data is never edited. Cleaning produces `data/processed/`. Both are gitignored — everything must be reproducible from scripts.
- API keys in `.env` (see `.env.example`). FEC falls back to `DEMO_KEY` (heavily rate-limited).
- Stay factual and source-cited. This is analysis, not advocacy — record where every number came from.

## Status
- Scaffolded 2026-11-03. FEC + Open States fetchers are first-pass; TEC and election results not started.
- Talarico FEC ID filled (S6TX00479). Open States + TEC IDs still empty — need an Open States key for lookup.
