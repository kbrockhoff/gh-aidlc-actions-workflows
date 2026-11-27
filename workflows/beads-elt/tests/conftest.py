from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pyiceberg.catalog.sql import SqlCatalog

requires_dolt = pytest.mark.skipif(shutil.which("dolt") is None, reason="dolt CLI not installed")


class DoltRemote:
    """A local Dolt database pushed to a file:// remote, standing in for refs/dolt/data."""

    def __init__(self, root: Path):
        self.work = root / "work"
        self.remote_url = f"file://{root / 'remote'}"
        self.work.mkdir()
        self._dolt("init")
        self._dolt("remote", "add", "origin", self.remote_url)

    def _dolt(self, *args: str) -> None:
        subprocess.run(["dolt", *args], cwd=self.work, check=True, capture_output=True)

    def sql(self, statement: str) -> None:
        self._dolt("sql", "-q", statement)

    def commit_and_push(self, message: str) -> None:
        self._dolt("add", "-A")
        self._dolt("commit", "-m", message)
        self._dolt("push", "origin", "main")


@pytest.fixture
def dolt_remote(tmp_path, monkeypatch) -> DoltRemote:
    # Keep dolt's global config out of the developer's home directory.
    monkeypatch.setenv("DOLT_ROOT_PATH", str(tmp_path / "dolt-home"))
    for key, value in (("user.name", "beads-elt-test"), ("user.email", "test@example.com")):
        subprocess.run(
            ["dolt", "config", "--global", "--add", key, value], check=True, capture_output=True
        )
    remote = DoltRemote(tmp_path)
    remote.sql(
        "CREATE TABLE issues (id VARCHAR(255) PRIMARY KEY, title VARCHAR(500), priority INT,"
        " ephemeral TINYINT(1), metadata JSON, created_at DATETIME);"
        "CREATE TABLE labels (issue_id VARCHAR(255), label VARCHAR(255),"
        " PRIMARY KEY (issue_id, label));"
        "CREATE VIEW open_issues AS SELECT * FROM issues;"
        "INSERT INTO issues VALUES"
        " ('bd-1', 'First', 1, 0, '{\"a\": 1}', '2026-11-27 10:00:00'),"
        " ('bd-2', 'Second', 2, 1, NULL, '2026-11-27 11:00:00');"
        "INSERT INTO labels VALUES ('bd-1', 'mvp');"
    )
    remote.commit_and_push("seed")
    return remote


@pytest.fixture
def catalog(tmp_path) -> SqlCatalog:
    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    return SqlCatalog(
        "test", uri=f"sqlite:///{tmp_path / 'catalog.db'}", warehouse=f"file://{warehouse}"
    )
