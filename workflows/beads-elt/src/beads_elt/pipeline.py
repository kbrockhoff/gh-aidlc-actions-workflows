"""Full-snapshot extract of a beads Dolt database into raw-zone Iceberg tables."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
from pyiceberg.catalog import Catalog

from beads_elt import dolt
from beads_elt.iceberg import (
    LOAD_LOG_TABLE,
    Lineage,
    already_loaded,
    ensure_namespace,
    normalize,
    record_load,
    validate_name,
    with_lineage,
    write_snapshot,
)


@dataclass
class LoadConfig:
    remote_url: str
    source_repo: str
    dolt_database: str
    load_id: str
    namespace: str = "raw_beads"
    table_prefix: str = ""
    force: bool = False


@dataclass
class LoadResult:
    ref_sha: str
    dolt_commit: str = ""
    skipped: bool = False
    skip_reason: str = ""
    row_counts: dict[str, int] = field(default_factory=dict)

    @property
    def tables_loaded(self) -> int:
        return len(self.row_counts)

    @property
    def rows_loaded(self) -> int:
        return sum(self.row_counts.values())


def run(catalog: Catalog, config: LoadConfig, ref_sha: str) -> LoadResult:
    """Load every base table at `ref_sha` unless that ref or its Dolt commit was already loaded."""
    ensure_namespace(catalog, config.namespace)
    log_id = f"{config.namespace}.{validate_name(config.table_prefix + LOAD_LOG_TABLE)}"
    result = LoadResult(ref_sha=ref_sha)

    if not config.force and already_loaded(
        catalog, log_id, config.source_repo, dolt_ref_sha=ref_sha
    ):
        result.skipped, result.skip_reason = True, f"ref {ref_sha} already loaded"
        return result

    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="beads-elt-") as tmp:
        db_dir = dolt.clone(config.remote_url, Path(tmp) / "db")
        result.dolt_commit = dolt.head_commit(db_dir)

        if not config.force and already_loaded(
            catalog, log_id, config.source_repo, dolt_commit=result.dolt_commit
        ):
            result.skipped = True
            result.skip_reason = f"Dolt commit {result.dolt_commit} already loaded"
            return result

        lineage = Lineage(
            source_repo=config.source_repo,
            dolt_database=config.dolt_database,
            dolt_commit=result.dolt_commit,
            dolt_ref_sha=ref_sha,
            extracted_at=started_at,
            load_id=config.load_id,
        )
        entry = {
            "source_repo": config.source_repo,
            "dolt_database": config.dolt_database,
            "dolt_ref_sha": ref_sha,
            "dolt_commit": result.dolt_commit,
            "load_id": config.load_id,
            "started_at": started_at,
        }
        current = ""
        try:
            for table in dolt.base_tables(db_dir):
                current = table
                target = f"{config.namespace}.{validate_name(config.table_prefix + table)}"
                parquet = dolt.export_parquet(db_dir, table, Path(tmp) / f"{table}.parquet")
                data = with_lineage(normalize(pq.read_table(parquet)), lineage)
                write_snapshot(catalog, target, data)
                result.row_counts[table] = data.num_rows
        except Exception as exc:
            record_load(
                catalog,
                log_id,
                {**_summary(entry, result), "status": "failed", "error": f"{current}: {exc}"},
            )
            raise RuntimeError(f"loading table {current!r} failed: {exc}") from exc

    record_load(catalog, log_id, {**_summary(entry, result), "status": "succeeded"})
    return result


def _summary(entry: dict, result: LoadResult) -> dict:
    return {
        **entry,
        "finished_at": datetime.now(UTC),
        "tables_loaded": result.tables_loaded,
        "rows_loaded": result.rows_loaded,
        "table_row_counts": json.dumps(result.row_counts, sort_keys=True),
    }
