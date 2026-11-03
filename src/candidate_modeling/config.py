import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def load_profile(slug: str) -> dict:
    with open(ROOT / "candidates" / f"{slug}.yaml") as f:
        return yaml.safe_load(f)


def raw_dir(slug: str, source: str) -> Path:
    d = ROOT / "data" / "raw" / slug / source
    d.mkdir(parents=True, exist_ok=True)
    return d


def key(name: str, default: str | None = None) -> str | None:
    return os.getenv(name) or default
