"""Open States API v3 — state legislative bills and votes. Docs: https://docs.openstates.org/api-v3/"""
import json
from pathlib import Path

import requests

from ..config import key

BASE = "https://v3.openstates.org"


def _get(path: str, **params) -> dict:
    api_key = key("OPENSTATES_API_KEY")
    if not api_key:
        raise RuntimeError("OPENSTATES_API_KEY not set in .env")
    r = requests.get(f"{BASE}{path}", params=params, headers={"X-API-KEY": api_key}, timeout=30)
    r.raise_for_status()
    return r.json()


def search_person(name: str, jurisdiction: str = "tx") -> list[dict]:
    res = _get("/people", name=name, jurisdiction=jurisdiction, include="other_identifiers")
    return [{"id": p["id"], "name": p["name"], "party": p.get("party")} for p in res["results"]]


def fetch(profile: dict, out_dir: Path) -> None:
    pid = profile["ids"].get("openstates_person_id")
    if not pid:
        print("  openstates: no openstates_person_id in profile — run with --lookup")
        return
    bills, page = [], 1
    while True:
        res = _get("/bills", jurisdiction="tx", sponsor=pid, page=page, per_page=20, include="sponsorships")
        bills += res["results"]
        if page >= res["pagination"]["max_page"]:
            break
        page += 1
    (out_dir / "sponsored_bills.json").write_text(json.dumps(bills, indent=2))
    print(f"  openstates: wrote {len(bills)} sponsored bills")
    # TODO: roll-call votes (include=votes) for party-line analysis
