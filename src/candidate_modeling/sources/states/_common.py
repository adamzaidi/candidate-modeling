"""Shared helpers for state bulk-data connectors."""
import csv
import json
import re
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


# ---------- tolerant CSV (PA, GA: unescaped quotes inside quoted fields, stray line breaks) ----------

_FIELD = re.compile(r'"((?:[^"]|"(?!,|\s*$))*)"(?=,|\s*$)|([^,]*)')


# ---------- tolerant parsing ----------

def parse_line(line: str) -> list[str] | None:
    """Split one line of quote-broken CSV. A quote ends a field only if followed by ',' or end of line."""
    out, i, n = [], 0, len(line)
    while True:
        m = _FIELD.match(line, i)
        out.append(m.group(1) if m.group(1) is not None else m.group(2))
        i = m.end()
        if i >= n:
            return out
        if line[i] != ",":
            return None
        i += 1


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def clean_rows(lines, ncols: int, max_join: int = 4):
    """Yield (row | None) for each logical record; None = unparseable after joining. Skips the header."""
    it = iter(lines)
    next(it, None)
    for raw in it:
        text = _decode(raw).rstrip("\r\n")
        if not text.strip():
            continue
        row = parse_line(text)
        joins = 0
        while (row is None or len(row) < ncols) and joins < max_join:
            nxt = next(it, None)
            if nxt is None:
                break
            text = text + " " + _decode(nxt).rstrip("\r\n")
            row = parse_line(text)
            joins += 1
        yield row if row is not None and len(row) == ncols else None


def to_clean_csv(src, dest: Path, ncols: int) -> dict:
    good = bad = 0
    with open(dest, "w", newline="", encoding="utf-8") as fo:
        w = csv.writer(fo)
        for row in clean_rows(src, ncols):
            if row is None:
                bad += 1
            else:
                w.writerow(row)
                good += 1
    return {"rows": good, "unparseable": bad}


