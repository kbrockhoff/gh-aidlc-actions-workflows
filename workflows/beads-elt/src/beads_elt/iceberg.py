"""Iceberg writes for the raw zone: type normalization, lineage, and the load log."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from datetime import datetime

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
from pyiceberg.expressions import And, EqualTo
from pyiceberg.table import Table
from pyiceberg.transforms import DayTransform

LOAD_LOG_TABLE = "beads_elt_load_log"

# S3 Tables: lowercase letters, digits and underscores; must start and end with a letter or digit.
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9_]{0,253}[a-z0-9])?$")

UTC_US = pa.timestamp("us", tz="UTC")

LINEAGE_SCHEMA = pa.schema(
    [
        ("_source_repo", pa.string()),
        ("_dolt_database", pa.string()),
        ("_dolt_commit", pa.string()),
        ("_dolt_ref_sha", pa.string()),
        ("_extracted_at", UTC_US),
        ("_load_id", pa.string()),
    ]
)

LOAD_LOG_SCHEMA = pa.schema(
    [
        ("source_repo", pa.string()),
        ("dolt_database", pa.string()),
        ("dolt_ref_sha", pa.string()),
        ("dolt_commit", pa.string()),
        ("load_id", pa.string()),
        ("status", pa.string()),
        ("started_at", UTC_US),
        ("finished_at", UTC_US),
        ("tables_loaded", pa.int32()),
        ("rows_loaded", pa.int64()),
        ("table_row_counts", pa.string()),
        ("error", pa.string()),
    ]
)


@dataclass(frozen=True)
class Lineage:
    source_repo: str
    dolt_database: str
    dolt_commit: str
    dolt_ref_sha: str
    extracted_at: datetime
    load_id: str


def validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid S3 Tables name {name!r}: use lowercase letters, digits and underscores, "
            "starting and ending with a letter or digit"
        )
    return name


def _iceberg_type(t: pa.DataType) -> pa.DataType:
    """Map Arrow types Iceberg can't store to the nearest type it can."""
    if pa.types.is_dictionary(t):
        return _iceberg_type(t.value_type)
    if pa.types.is_null(t):
        return pa.string()
    if pa.types.is_int8(t) or pa.types.is_int16(t) or pa.types.is_uint8(t) or pa.types.is_uint16(t):
        return pa.int32()
    if pa.types.is_uint32(t):
        return pa.int64()
    if pa.types.is_uint64(t):
        return pa.decimal128(20, 0)
    if pa.types.is_timestamp(t) and t.unit != "us":
        return pa.timestamp("us", tz=t.tz)
    if pa.types.is_time(t) and t != pa.time64("us"):
        return pa.time64("us")
    if pa.types.is_large_string(t):
        return pa.string()
    if pa.types.is_large_binary(t):
        return pa.binary()
    return t


def normalize(table: pa.Table) -> pa.Table:
    fields = [pa.field(f.name, _iceberg_type(f.type), nullable=True) for f in table.schema]
    return table.cast(pa.schema(fields), safe=False)


def with_lineage(table: pa.Table, lineage: Lineage) -> pa.Table:
    n = table.num_rows
    values = [
        lineage.source_repo,
        lineage.dolt_database,
        lineage.dolt_commit,
        lineage.dolt_ref_sha,
        lineage.extracted_at,
        lineage.load_id,
    ]
    for field, value in zip(LINEAGE_SCHEMA, values, strict=True):
        if field.name in table.column_names:
            raise ValueError(f"source column {field.name} collides with a lineage column")
        table = table.append_column(field, pa.array([value] * n, type=field.type))
    return table


def ensure_namespace(catalog: Catalog, namespace: str) -> None:
    # Check first so a pre-provisioned namespace needs only s3tables:GetNamespace,
    # which can be scoped to the namespace; s3tables:CreateNamespace cannot.
    if catalog.namespace_exists(validate_name(namespace)):
        return
    with contextlib.suppress(NamespaceAlreadyExistsError):
        catalog.create_namespace(namespace)


def _load_or_create(catalog: Catalog, identifier: str, schema: pa.Schema) -> tuple[Table, bool]:
    try:
        return catalog.load_table(identifier), False
    except NoSuchTableError:
        return catalog.create_table(identifier, schema=schema), True


def write_snapshot(catalog: Catalog, identifier: str, data: pa.Table) -> None:
    """Append `data`, creating the table (partitioned by repo and day) or evolving its schema."""
    table, created = _load_or_create(catalog, identifier, data.schema)
    if created:
        with table.update_spec() as spec:
            spec.add_identity("_source_repo")
            spec.add_field("_extracted_at", DayTransform(), "_extracted_day")
    else:
        # Adds new columns; removed source columns stay in the table as nulls.
        # Incompatible type changes raise here and fail the run.
        with table.update_schema() as update:
            update.union_by_name(data.schema)
    table.refresh()
    table.append(_align(data, table))


def _align(data: pa.Table, table: Table) -> pa.Table:
    """Order and fill columns to match the table schema so appends accept the data."""
    target = table.schema().as_arrow()
    columns = []
    for field in target:
        if field.name in data.column_names:
            columns.append(data.column(field.name).cast(field.type))
        else:
            columns.append(pa.nulls(data.num_rows, type=field.type))
    return pa.Table.from_arrays(columns, schema=target)


def already_loaded(catalog: Catalog, identifier: str, source_repo: str, **match: str) -> bool:
    """True if the load log has a succeeded load for `source_repo` matching every `match` column."""
    try:
        log = catalog.load_table(identifier)
    except NoSuchTableError:
        return False
    expr = And(EqualTo("source_repo", source_repo), EqualTo("status", "succeeded"))
    for column, value in match.items():
        expr = And(expr, EqualTo(column, value))
    return len(log.scan(row_filter=expr, selected_fields=("load_id",), limit=1).to_arrow()) > 0


def record_load(catalog: Catalog, identifier: str, entry: dict) -> None:
    row = pa.Table.from_pylist([entry], schema=LOAD_LOG_SCHEMA)
    log, _ = _load_or_create(catalog, identifier, LOAD_LOG_SCHEMA)
    log.append(_align(row, log))
