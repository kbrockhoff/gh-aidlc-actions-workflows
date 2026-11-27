from __future__ import annotations

from datetime import UTC, datetime

import pyarrow as pa
import pytest

from beads_elt.iceberg import Lineage, normalize, validate_name, with_lineage


def test_normalize_maps_types_iceberg_cannot_store():
    table = pa.table(
        {
            "tiny": pa.array([1], pa.int8()),
            "u16": pa.array([1], pa.uint16()),
            "u32": pa.array([1], pa.uint32()),
            "u64": pa.array([2**64 - 1], pa.uint64()),
            "ns": pa.array([0], pa.timestamp("ns")),
            "big": pa.array(["x"], pa.large_string()),
            "empty": pa.nulls(1),
            "dict": pa.array(["a"]).dictionary_encode(),
        }
    )

    schema = normalize(table).schema

    assert schema.field("tiny").type == pa.int32()
    assert schema.field("u16").type == pa.int32()
    assert schema.field("u32").type == pa.int64()
    assert schema.field("u64").type == pa.decimal128(20, 0)
    assert schema.field("ns").type == pa.timestamp("us")
    assert schema.field("big").type == pa.string()
    assert schema.field("empty").type == pa.string()
    assert schema.field("dict").type == pa.string()
    assert all(f.nullable for f in schema)


def test_with_lineage_rejects_colliding_columns():
    lineage = Lineage("a/b", "db", "c", "r", datetime.now(UTC), "l")
    with pytest.raises(ValueError, match="_load_id"):
        with_lineage(pa.table({"_load_id": ["x"]}), lineage)


@pytest.mark.parametrize("name", ["issues", "raw_beads", "a1", "x"])
def test_valid_names(name):
    assert validate_name(name) == name


@pytest.mark.parametrize("name", ["_load_log", "Issues", "raw-beads", "trailing_", ""])
def test_invalid_names(name):
    with pytest.raises(ValueError):
        validate_name(name)
