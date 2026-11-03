"""Shared helpers for state bulk-data connectors."""
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import requests

from ...config import USER_AGENT


def download(url: str, dest: Path, user_agent: str = USER_AGENT, chunk: int = 1 << 20) -> Path:
    """Stream a file to disk atomically (writes .part then renames)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, headers={"User-Agent": user_agent}, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for block in r.iter_content(chunk):
                f.write(block)
    tmp.replace(dest)
    return dest


def remote_last_modified(url: str, user_agent: str = USER_AGENT) -> str | None:
    r = requests.head(url, headers={"User-Agent": user_agent}, allow_redirects=True, timeout=60)
    r.raise_for_status()
    return r.headers.get("Last-Modified")


def read_manifest(d: Path) -> dict:
    p = d / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def write_manifest(d: Path, m: dict) -> None:
    (d / "manifest.json").write_text(json.dumps(m, indent=2))


def extract_member(zf: zipfile.ZipFile, name: str, tmpdir: Path) -> Path:
    out = tmpdir / Path(name).name
    with zf.open(name) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


def tempdir(base: Path) -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(dir=base)


def sql_str(s: str | Path) -> str:
    return "'" + str(s).replace("'", "''") + "'"
