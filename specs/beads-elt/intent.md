# Intent: Beads ELT to S3 Tables (Raw Zone)

## Summary

Provide a reusable GitHub Actions shared workflow that, each time a
repository's beads Dolt data ref is updated, extracts the **entire** beads
database and loads it into Amazon S3 Tables (Apache Iceberg) in the **raw
zone**. This gives AI-DLC teams an analytics-ready, queryable history of
issues, dependencies, comments, and provenance across every repository that
uses beads, without each repository building its own pipeline.

## Background

- Beads (`bd`) stores issues in a Dolt database. Sync to the git remote uses
  the ref `refs/dolt/data`; `.beads/issues.jsonl` is only a passive export and
  is **not** the source of truth for this pipeline.
- The Dolt database can be cloned directly from the git remote:
  `dolt clone git+ssh://<remote>` (or `git+https://`), which yields a normal
  Dolt working copy on branch `main` that can be queried with `dolt sql`.
- Current beads schema (bd 1.3.x) contains these base tables:
  `child_counters`, `comments`, `compaction_snapshots`, `config`,
  `custom_statuses`, `custom_types`, `dependencies`, `federation_peers`,
  `interactions`, `issue_counter`, `issue_snapshots`, `issues`, `labels`,
  `metadata`, `provenance_events`, `routes`, `schema_migrations`.
  It also contains the views `blocked_issues` and `ready_issues`.
- The schema evolves with bd releases, so the pipeline must discover tables
  at run time rather than hard-code them.

## Goals

1. **Shared workflow**: a reusable workflow (`on: workflow_call`) published
   from this repository that any repository using beads can call with a few
   lines of YAML.
2. **Trigger on Dolt data updates**: runs every time `refs/dolt/data` changes
   on the calling repository's remote.
3. **Full extract**: extracts every base table in the beads database on every
   run (full snapshot, not incremental).
4. **Load to raw zone**: writes each table to an Iceberg table in an Amazon
   S3 Tables table bucket, in a raw-zone namespace.
5. **Lineage**: every loaded row can be traced back to the source repository,
   the Dolt commit hash, the git ref SHA, and the workflow run that loaded it.
6. **Keyless AWS auth**: authenticates to AWS with GitHub OIDC and an
   assumable IAM role; no long-lived AWS credentials in secrets.

## Non-Goals

- Transforming, deduplicating, or modeling data (curated/consumption zones
  are downstream concerns).
- Incremental / CDC extraction using Dolt diffs (possible future
  optimization).
- Writing anything back to the beads database or git remote.
- Provisioning the S3 table bucket, IAM role, or OIDC provider (documented as
  prerequisites; may be addressed by a separate IaC deliverable).

## Trigger Design

GitHub Actions `push` filters match branches and tags; a push to a custom ref
such as `refs/dolt/data` is not expected to start a workflow. The MVP must
verify this in a spike and pick a trigger strategy:

| Option | How | Trade-off |
| --- | --- | --- |
| A. `push` on `refs/dolt/data` | Caller workflow with `on: push` | Simplest, if GitHub actually fires it (verify first) |
| B. Scheduled poll + change detection | Caller runs on `schedule`; job compares `git ls-remote origin refs/dolt/data` SHA against the last loaded SHA recorded in the raw zone and exits early if unchanged | Always works; latency equals the poll interval |
| C. `repository_dispatch` | A beads post-push hook (or `bd` sync wrapper) sends a dispatch event after pushing the Dolt ref | Near real time; depends on every client having the hook and a token |

Recommended MVP: **B**, with `workflow_dispatch` for manual runs, and **C**
as an optional add-on for low latency. Either way, the shared workflow itself
is trigger-agnostic and skips the load when the ref SHA was already loaded,
so duplicate triggers are harmless.

## Functional Requirements

### Inputs (`workflow_call`)

| Input | Required | Default | Purpose |
| --- | --- | --- | --- |
| `aws-region` | yes | | Region of the S3 table bucket |
| `aws-role-arn` | yes | | IAM role assumed via GitHub OIDC |
| `table-bucket-arn` | yes | | Target S3 Tables table bucket |
| `namespace` | no | `raw_beads` | Iceberg namespace for raw-zone tables |
| `dolt-remote-url` | no | calling repo's remote | Git remote holding `refs/dolt/data` |
| `dolt-database` | no | value from `.beads/metadata.json` | Source database name |
| `table-prefix` | no | empty | Optional prefix for target table names |
| `force` | no | `false` | Load even if this ref SHA was already loaded |

Secrets: a token with read access to the calling repository, only if the
default `GITHUB_TOKEN` cannot read the Dolt ref (for example cross-repo).

Outputs: `dolt-commit`, `ref-sha`, `tables-loaded`, `rows-loaded`, `skipped`.

### Extract

1. Resolve the current `refs/dolt/data` SHA with `git ls-remote`.
2. If not `force` and that SHA is already recorded as loaded, exit
   successfully with `skipped=true`.
3. Install a pinned Dolt version and `dolt clone` the database from the git
   remote.
4. Capture the Dolt HEAD commit hash (`dolt log -n 1`).
5. Enumerate base tables from `information_schema.tables`
   (`table_type = 'BASE TABLE'`). Views (`blocked_issues`, `ready_issues`)
   are excluded by default because they are derived.
6. Export each table with Dolt (for example `dolt table export` to Parquet,
   or `dolt sql -r json/csv`), preserving column types where possible.

### Load

1. Write each table to Iceberg table `<namespace>.<table-prefix><table>` in
   the S3 table bucket, creating the namespace and table on first run.
2. Each run **appends** a full snapshot; the raw zone is an append-only,
   immutable history. Downstream zones select the latest snapshot.
3. Add lineage columns to every row:
   - `_source_repo` (owner/name)
   - `_dolt_database`
   - `_dolt_commit`
   - `_dolt_ref_sha`
   - `_extracted_at` (UTC timestamp)
   - `_load_id` (GitHub run ID + attempt)
4. Partition by `_source_repo` and day of `_extracted_at`.
5. Schema evolution: new source columns are added to the Iceberg table;
   removed columns are retained as nullable. Type changes that cannot be
   widened fail the run with a clear error.
6. Record each successful load in a control table
   (`<namespace>._load_log`) with repo, ref SHA, Dolt commit, per-table row
   counts, status, and timestamps. This table drives the change detection in
   Extract step 2.
7. Load all tables for a run before marking it successful in `_load_log`, so
   a partial failure is retried in full on the next trigger.

### Implementation Notes

- Prefer a Python step using PyIceberg with the S3 Tables Iceberg REST
  catalog endpoint (SigV4 auth) and PyArrow, which avoids needing Spark or
  EMR. Alternatives (Athena `INSERT`, Glue job, Firehose) are acceptable if
  the spike shows PyIceberg is unsuitable.
- Concurrency: use a workflow `concurrency` group per source repository so
  only one load runs at a time; Iceberg optimistic commits handle any
  remaining races.
- Pin all actions by SHA and Dolt/Python dependencies by version, matching
  repository conventions and Dependabot config.

## Non-Functional Requirements

- **Security**: OIDC-only AWS auth; least-privilege role limited to the
  target table bucket and namespace; `permissions:` in the workflow limited to
  `contents: read` and `id-token: write`. Issue content may contain sensitive
  text, so the table bucket must use encryption at rest and nothing extracted
  is written to workflow logs or uploaded as artifacts.
- **Idempotency**: re-running for the same ref SHA is a no-op unless `force`.
- **Performance**: a typical beads database (fewer than 10,000 issues)
  completes in under 5 minutes on `ubuntu-latest`.
- **Observability**: job summary lists ref SHA, Dolt commit, tables, and row
  counts; failures name the failing table and step.
- **Portability**: works for any repository using bd with the Dolt backend
  and git-remote sync, without per-repository code.

## Prerequisites (Consumer Responsibilities)

- An S3 Tables table bucket in the target account and region.
- A GitHub OIDC identity provider in the AWS account and an IAM role whose
  trust policy is scoped to the calling repositories, with permissions for
  the S3 Tables operations needed (namespace and table create, read
  metadata, write data, commit).
- A caller workflow in each repository, for example:

```yaml
name: Beads ELT
on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch:
permissions:
  contents: read
  id-token: write
jobs:
  elt:
    uses: kbrockhoff/gh-aidlc-actions-workflows/.github/workflows/beads-elt.yml@v1
    with:
      aws-region: us-east-1
      aws-role-arn: arn:aws:iam::123456789012:role/beads-elt
      table-bucket-arn: arn:aws:s3tables:us-east-1:123456789012:bucket/aidlc-raw
```

## Acceptance Criteria

1. Calling the shared workflow from a repository with beads data creates the
   raw-zone namespace and one Iceberg table per beads base table, and the
   row counts match `SELECT COUNT(*)` against the Dolt clone.
2. After a new `bd` change is synced to `refs/dolt/data`, the next trigger
   loads a new snapshot whose `_dolt_commit` matches the new Dolt HEAD.
3. A run where `refs/dolt/data` has not changed exits successfully with
   `skipped=true` and writes no data.
4. A new table or column added by a bd upgrade is loaded without workflow
   changes.
5. The tables are queryable from Athena via the S3 Tables / Glue Data Catalog
   integration.
6. No AWS access keys are stored in GitHub secrets.
7. The reusable workflow and caller example are documented in `README.md`.

## Open Questions

1. Does GitHub Actions fire `push` events for `refs/dolt/data`? (Decides
   trigger option A vs B/C.)
2. Single table bucket shared by all repositories (partitioned by
   `_source_repo`), or one namespace per repository?
3. Should the views (`blocked_issues`, `ready_issues`) also be materialized
   in the raw zone for convenience?
4. Retention policy for raw snapshots: keep all, or expire snapshots older
   than N days via S3 Tables maintenance?
5. Should `.beads/issues.jsonl` ever be used as a fallback source when Dolt
   clone fails?
