"""FEC API — federal campaign finance. Docs: https://api.open.fec.gov/developers/"""
import json
from pathlib import Path

import requests

from ..config import key

BASE = "https://api.open.fec.gov/v1"


def _get(path: str, **params) -> dict:
    params["api_key"] = key("FEC_API_KEY", "DEMO_KEY")
    r = requests.get(f"{BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def search_candidate(name: str, state: str | None = None) -> list[dict]:
    res = _get("/candidates/search/", q=name, state=state, per_page=20)
    return [
        {k: c.get(k) for k in ("candidate_id", "name", "office_full", "state", "party_full", "election_years")}
        for c in res["results"]
    ]


def fetch(profile: dict, out_dir: Path) -> None:
    cid = profile["ids"].get("fec_candidate_id")
    if not cid:
        print("  fec: no fec_candidate_id in profile — run with --lookup")
        return
    totals = _get(f"/candidate/{cid}/totals/", per_page=100)
    (out_dir / "totals.json").write_text(json.dumps(totals["results"], indent=2))
    committees = _get(f"/candidate/{cid}/committees/", per_page=100)
    (out_dir / "committees.json").write_text(json.dumps(committees["results"], indent=2))
    print(f"  fec: wrote totals ({len(totals['results'])}) and committees ({len(committees['results'])})")
    # TODO: itemized receipts (schedule_a) per principal committee — paginate with last_index
