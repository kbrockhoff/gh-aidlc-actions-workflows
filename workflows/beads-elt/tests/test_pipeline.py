from __future__ import annotations

import json

import pytest
from conftest import requires_dolt

from beads_elt import dolt
from beads_elt.pipeline import LoadConfig, run

pytestmark = requires_dolt


def _config(remote, **overrides) -> LoadConfig:
    values = dict(
        remote_url=remote.remote_url,
        source_repo="acme/widgets",
        dolt_database="widgets",
        load_id="run-1",
    )
    return LoadConfig(**{**values, **overrides})


def _log(catalog):
    return catalog.load_table("raw_beads.beads_elt_load_log").scan().to_arrow().to_pylist()


def test_first_load_creates_tables_with_lineage(catalog, dolt_remote):
    result = run(catalog, _config(dolt_remote), resolve_ref=lambda: "sha-1")

    assert not result.skipped
    assert result.row_counts == {"issues": 2, "labels": 1}
    assert set(catalog.list_tables("raw_beads")) == {
        ("raw_beads", "issues"),
        ("raw_beads", "labels"),
        ("raw_beads", "beads_elt_load_log"),
    }  # the view is not loaded

    issues = catalog.load_table("raw_beads.issues")
    rows = issues.scan().to_arrow().to_pylist()
    assert {r["id"] for r in rows} == {"bd-1", "bd-2"}
    assert {r["_dolt_commit"] for r in rows} == {result.dolt_commit}
    assert {r["_dolt_ref_sha"] for r in rows} == {"sha-1"}
    assert {r["_source_repo"] for r in rows} == {"acme/widgets"}
    assert [f.name for f in issues.spec().fields] == ["_source_repo", "_extracted_day"]

    (entry,) = _log(catalog)
    assert entry["status"] == "succeeded"
    assert json.loads(entry["table_row_counts"]) == {"issues": 2, "labels": 1}


def test_same_ref_is_skipped(catalog, dolt_remote):
    run(catalog, _config(dolt_remote), resolve_ref=lambda: "sha-1")
    result = run(catalog, _config(dolt_remote, load_id="run-2"), resolve_ref=lambda: "sha-1")

    assert result.skipped
    assert "sha-1" in result.skip_reason
    assert len(catalog.load_table("raw_beads.issues").scan().to_arrow()) == 2


def test_new_ref_with_same_dolt_commit_is_skipped(catalog, dolt_remote):
    first = run(catalog, _config(dolt_remote), resolve_ref=lambda: "sha-1")
    result = run(catalog, _config(dolt_remote, load_id="run-2"), resolve_ref=lambda: "sha-2")

    assert result.skipped
    assert first.dolt_commit in result.skip_reason


def test_force_appends_another_snapshot(catalog, dolt_remote):
    run(catalog, _config(dolt_remote), resolve_ref=lambda: "sha-1")
    result = run(
        catalog, _config(dolt_remote, load_id="run-2", force=True), resolve_ref=lambda: "sha-1"
    )

    assert not result.skipped
    assert len(catalog.load_table("raw_beads.issues").scan().to_arrow()) == 4
    assert sorted(e["load_id"] for e in _log(catalog)) == ["run-1", "run-2"]


def test_schema_changes_are_evolved(catalog, dolt_remote):
    run(catalog, _config(dolt_remote), resolve_ref=lambda: "sha-1")
    dolt_remote.sql(
        "ALTER TABLE issues ADD COLUMN owner VARCHAR(255);"
        "ALTER TABLE issues DROP COLUMN ephemeral;"
        "UPDATE issues SET owner = 'kb' WHERE id = 'bd-1';"
        "CREATE TABLE comments (id CHAR(36) PRIMARY KEY, text LONGTEXT);"
    )
    dolt_remote.commit_and_push("upgrade schema")

    result = run(catalog, _config(dolt_remote, load_id="run-2"), resolve_ref=lambda: "sha-2")

    assert result.row_counts == {"comments": 0, "issues": 2, "labels": 1}
    rows = catalog.load_table("raw_beads.issues").scan().to_arrow().to_pylist()
    latest = [r for r in rows if r["_load_id"] == "run-2"]
    assert {r["owner"] for r in latest} == {"kb", None}
    assert {r["ephemeral"] for r in latest} == {None}
    assert ("raw_beads", "comments") in catalog.list_tables("raw_beads")


def test_table_prefix_applies_to_all_tables(catalog, dolt_remote):
    run(catalog, _config(dolt_remote, table_prefix="widgets_"), resolve_ref=lambda: "sha-1")

    assert set(catalog.list_tables("raw_beads")) == {
        ("raw_beads", "widgets_issues"),
        ("raw_beads", "widgets_labels"),
        ("raw_beads", "widgets_beads_elt_load_log"),
    }


def test_invalid_namespace_fails_before_cloning(catalog, dolt_remote):
    with pytest.raises(ValueError, match="invalid S3 Tables name"):
        run(catalog, _config(dolt_remote, namespace="_raw"), resolve_ref=lambda: "sha-1")


def test_existing_namespace_is_not_created_again(catalog, dolt_remote, monkeypatch):
    catalog.create_namespace("raw_beads")

    def fail(*_args, **_kwargs):
        raise AssertionError("create_namespace called for an existing namespace")

    monkeypatch.setattr(catalog, "create_namespace", fail)
    result = run(catalog, _config(dolt_remote), resolve_ref=lambda: "sha-1")

    assert not result.skipped


def test_ref_moving_during_clone_records_the_cloned_ref(catalog, dolt_remote, monkeypatch):
    refs = iter(["sha-1", "sha-2", "sha-2"])
    clones = []
    real_clone = dolt.clone
    monkeypatch.setattr(
        dolt, "clone", lambda url, dest: clones.append(dest) or real_clone(url, dest)
    )

    result = run(catalog, _config(dolt_remote), resolve_ref=lambda: next(refs))

    assert result.ref_sha == "sha-2"
    assert len(clones) == 2
    rows = catalog.load_table("raw_beads.issues").scan().to_arrow().to_pylist()
    assert {r["_dolt_ref_sha"] for r in rows} == {"sha-2"}
    assert [e["dolt_ref_sha"] for e in _log(catalog)] == ["sha-2"]


def test_ref_that_never_settles_fails(catalog, dolt_remote):
    refs = (f"sha-{n}" for n in range(10))

    with pytest.raises(dolt.DoltError, match="changed during each"):
        run(catalog, _config(dolt_remote), resolve_ref=lambda: next(refs))


def test_unpartitioned_table_is_repaired(catalog, dolt_remote):
    # Simulates a run that created the table but failed before adding the partition spec.
    run(catalog, _config(dolt_remote), resolve_ref=lambda: "sha-1")
    issues = catalog.load_table("raw_beads.issues")
    with issues.update_spec() as spec:
        spec.remove_field("_source_repo")
        spec.remove_field("_extracted_day")
    assert catalog.load_table("raw_beads.issues").spec().is_unpartitioned()

    run(catalog, _config(dolt_remote, load_id="run-2", force=True), resolve_ref=lambda: "sha-1")

    fields = catalog.load_table("raw_beads.issues").spec().fields
    assert {f.name for f in fields} == {"_source_repo", "_extracted_day"}
