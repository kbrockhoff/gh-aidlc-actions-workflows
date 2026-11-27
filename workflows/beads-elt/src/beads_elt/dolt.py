"""Thin wrappers around the git and dolt CLIs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DOLT_DATA_REF = "refs/dolt/data"


class DoltError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise DoltError(f"{' '.join(args[:3])} failed: {exc.stderr.strip()}") from exc
    return result.stdout


def remote_ref_sha(remote_url: str, ref: str = DOLT_DATA_REF) -> str:
    """Return the SHA of `ref` on a `git+https://` or `git+ssh://` Dolt remote."""
    git_url = remote_url.removeprefix("git+")
    out = _run(["git", "ls-remote", git_url, ref]).split()
    if not out:
        raise DoltError(f"{ref} not found on {git_url}; has bd pushed to this remote?")
    return out[0]


def clone(remote_url: str, dest: Path) -> Path:
    _run(["dolt", "clone", remote_url, str(dest)])
    return dest


def query(db_dir: Path, sql: str) -> list[dict]:
    out = _run(["dolt", "sql", "-r", "json", "-q", sql], cwd=db_dir)
    return json.loads(out).get("rows", []) if out.strip() else []


def head_commit(db_dir: Path) -> str:
    return query(db_dir, "SELECT HASHOF('HEAD') AS h")[0]["h"]


def base_tables(db_dir: Path) -> list[str]:
    rows = query(
        db_dir,
        "SELECT table_name AS t FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' ORDER BY table_name",
    )
    return [r["t"] for r in rows]


def export_parquet(db_dir: Path, table: str, out_path: Path) -> Path:
    _run(["dolt", "table", "export", "-f", table, str(out_path)], cwd=db_dir)
    return out_path
