"""Paths, API keys, and candidate profile I/O."""
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

USER_AGENT = "candidate-modeling/0.2 (research; github.com/adamzaidi/candidate-modeling)"


def data_dir() -> Path:
    """Root for raw downloads, parquet caches, and the DuckDB file. Override with CM_DATA_DIR."""
    d = Path(os.getenv("CM_DATA_DIR") or ROOT / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def candidates_dir() -> Path:
    d = Path(os.getenv("CM_CANDIDATES_DIR") or ROOT / "candidates")
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir(state: str) -> Path:
    d = data_dir() / "raw" / state.lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "candidates.duckdb"


def key(name: str, default: str | None = None) -> str | None:
    return os.getenv(name) or default


# ---------- profiles ----------

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


EMPTY_IDS = {
    "fec_candidate_ids": [],
    "fec_committee_ids": [],
    "openstates_person_id": None,
    "state": {},  # e.g. {"TX": {"filer_ids": ["00012345"]}, "MI": {"committee_ids": ["0516789"]}}
}


def load_profile(slug: str) -> dict:
    path = candidates_dir() / f"{slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No profile for '{slug}'. Create one with find_candidate + add_candidate.")
    with open(path) as f:
        p = yaml.safe_load(f)
    ids = p.setdefault("ids", {})
    for k, v in EMPTY_IDS.items():
        ids.setdefault(k, type(v)() if isinstance(v, (list, dict)) else v)
    # back-compat with the v0.1 single-ID field
    if ids.get("fec_candidate_id"):
        if ids["fec_candidate_id"] not in ids["fec_candidate_ids"]:
            ids["fec_candidate_ids"].append(ids["fec_candidate_id"])
        del ids["fec_candidate_id"]
    return p


def save_profile(profile: dict) -> Path:
    path = candidates_dir() / f"{profile['slug']}.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(profile, f, sort_keys=False, allow_unicode=True)
    return path


def list_profiles() -> list[dict]:
    return [load_profile(p.stem) for p in sorted(candidates_dir().glob("*.yaml"))]
