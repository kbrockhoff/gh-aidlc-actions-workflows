# beads-elt

Reusable workflow [`.github/workflows/beads-elt.yml`](../../.github/workflows/beads-elt.yml)
extracts the entire [beads](https://github.com/gastownhall/beads) Dolt database from a
repository's `refs/dolt/data` ref and appends it as a full snapshot to Amazon S3 Tables
(Apache Iceberg) in the raw zone. The design is in
[`specs/beads-elt/intent.md`](../../specs/beads-elt/intent.md).

## How it works

1. Reads the `refs/dolt/data` SHA with `git ls-remote`. If the `beads_elt_load_log` table
   already has a successful load of that SHA for this repository, it exits with
   `skipped=true`.
2. Runs `dolt clone` against the git remote and reads the Dolt HEAD commit. If that commit
   was already loaded (under any ref SHA), it also skips.
3. Lists every base table from `information_schema` (views are left out) and exports each
   one to Parquet with `dolt table export`.
4. Appends each table to `<namespace>.<table-prefix><table>` through the S3 Tables Iceberg
   REST catalog, creating the namespace and tables on first use. New source columns are
   added to the Iceberg table; dropped ones stay and are null in later snapshots.
5. Records the load in `<namespace>.<table-prefix>beads_elt_load_log`, with status
   `succeeded` or `failed`.

Every row carries these lineage columns: `_source_repo`, `_dolt_database`, `_dolt_commit`,
`_dolt_ref_sha`, `_extracted_at` (UTC) and `_load_id` (`<run_id>-<run_attempt>`). Tables are
partitioned by `_source_repo` and the day of `_extracted_at`.

A run that fails partway through can leave rows for the tables it finished. Those rows are
not marked as succeeded in the load log, and the next run loads every table again. To read
the latest complete snapshot, join on a succeeded `load_id`:

```sql
SELECT i.*
FROM raw_beads.issues i
JOIN (
  SELECT source_repo, max_by(load_id, finished_at) AS load_id
  FROM raw_beads.beads_elt_load_log
  WHERE status = 'succeeded'
  GROUP BY source_repo
) latest
  ON i._source_repo = latest.source_repo AND i._load_id = latest.load_id;
```

## Usage

A Dolt push can't trigger `on: push` (see the spec's Trigger Design), so call the workflow
on a schedule. Runs where nothing changed exit after one `git ls-remote` call.

```yaml
name: Beads ELT
on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch:
    inputs:
      force:
        description: Reload even if unchanged
        type: boolean
        default: false
permissions:
  contents: read
  id-token: write
jobs:
  elt:
    uses: kbrockhoff/gh-aidlc-actions-workflows/.github/workflows/beads-elt.yml@main
    with:
      aws-region: us-east-1
      aws-role-arn: arn:aws:iam::123456789012:role/beads-elt
      table-bucket-arn: arn:aws:s3tables:us-east-1:123456789012:bucket/aidlc-raw
      force: ${{ inputs.force || false }}
```

Pin `@main` to a release tag or commit SHA once releases exist.

### Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `aws-region` | yes | | Region of the S3 table bucket |
| `aws-role-arn` | yes | | IAM role assumed through GitHub OIDC |
| `table-bucket-arn` | yes | | Target S3 Tables table bucket ARN |
| `namespace` | no | `raw_beads` | Iceberg namespace for raw-zone tables |
| `dolt-remote-url` | no | `git+<server>/<caller repo>.git` | Git remote holding `refs/dolt/data` |
| `dolt-database` | no | `dolt_database` from `.beads/metadata.json`, else the repo name | Source database name (lineage only) |
| `table-prefix` | no | empty | Prefix for target table names |
| `force` | no | `false` | Load even if the ref or Dolt commit was already loaded |
| `dolt-version` | no | `2.7.5` | Dolt CLI version to install |

Secret `dolt-remote-token` is optional. Pass it when `GITHUB_TOKEN` can't read the Dolt
remote, for example when `dolt-remote-url` points at another private repository.

### Outputs

`ref-sha`, `dolt-commit`, `tables-loaded`, `rows-loaded` and `skipped`. The job summary
lists row counts per table.

## AWS prerequisites

- An S3 Tables table bucket. To query it from Athena, turn on the bucket's integration with
  AWS analytics services (Glue Data Catalog and Lake Formation).
- A GitHub OIDC identity provider (`token.actions.githubusercontent.com`) in the account.
- An IAM role whose trust policy limits `sub` to the calling repositories, for example
  `repo:my-org/my-repo:*`, and that allows the S3 Tables operations the loader uses:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3tables:GetTableBucket",
        "s3tables:ListNamespaces",
        "s3tables:GetNamespace",
        "s3tables:CreateNamespace",
        "s3tables:ListTables",
        "s3tables:GetTable",
        "s3tables:CreateTable",
        "s3tables:GetTableMetadataLocation",
        "s3tables:UpdateTableMetadataLocation",
        "s3tables:GetTableData",
        "s3tables:PutTableData"
      ],
      "Resource": [
        "arn:aws:s3tables:us-east-1:123456789012:bucket/aidlc-raw",
        "arn:aws:s3tables:us-east-1:123456789012:bucket/aidlc-raw/table/*"
      ]
    }
  ]
}
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and the `dolt` CLI. The tests run the whole
pipeline against a local Dolt `file://` remote and a SQLite-backed Iceberg catalog, so they
don't need AWS.

```bash
cd workflows/beads-elt
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
actionlint ../../.github/workflows/beads-elt*.yml
```
