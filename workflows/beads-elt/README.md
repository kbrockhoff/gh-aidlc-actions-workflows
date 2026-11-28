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

Pin `@main` to a release tag such as `@v0` or a commit SHA once releases exist.

### Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `aws-region` | yes | | Region of the S3 table bucket |
| `aws-role-arn` | yes, unless the `aws-role-arn` secret is set | | IAM role assumed through GitHub OIDC |
| `table-bucket-arn` | yes | | Target S3 Tables table bucket ARN |
| `namespace` | no | `raw_beads` | Iceberg namespace for raw-zone tables |
| `dolt-remote-url` | no | `git+<server>/<caller repo>.git` | Git remote holding `refs/dolt/data` |
| `dolt-database` | no | `dolt_database` from `.beads/metadata.json`, else the repo name | Source database name (lineage only) |
| `table-prefix` | no | empty | Prefix for target table names |
| `force` | no | `false` | Load even if the ref or Dolt commit was already loaded |
| `dolt-version` | no | `2.7.5` | Dolt CLI version to install |

Both secrets are optional:

- `aws-role-arn`: the role ARN, for callers that keep it in a repository secret. A caller
  can't reference `secrets` in `with:`, so pass it under `secrets:` instead of the input.
- `dolt-remote-token`: pass it when `GITHUB_TOKEN` can't read the Dolt remote, for example
  when `dolt-remote-url` points at another private repository.

[`beads-elt-raw.yml`](../../.github/workflows/beads-elt-raw.yml) is a working caller that
loads this repository's own beads database with the role ARN from a secret.

### Outputs

`ref-sha`, `dolt-commit`, `tables-loaded`, `rows-loaded` and `skipped`. The job summary
lists row counts per table.

## AWS prerequisites

- An S3 Tables table bucket. To query it from Athena, turn on the bucket's integration with
  AWS analytics services (Glue Data Catalog and Lake Formation).
- Optionally, the raw-zone namespace created ahead of time (for example by IaC). The role
  then needs no `s3tables:CreateNamespace`, which can't be scoped to a namespace.
- The GitHub OIDC identity provider `token.actions.githubusercontent.com` in the account,
  with audience `sts.amazonaws.com`.
- An IAM role with the trust and permissions policies below.

### Trust policy

Limit `sub` to the repositories that call the workflow. The `repo` part of `sub` comes in
two formats:

- **Immutable**: `repo:<owner>@<owner-id>/<repo>@<repo-id>`. This is the default for
  repositories created or renamed after 2026-07-15, including this one
  (`repo:kbrockhoff@1822996/gh-aidlc-actions-workflows@1473676008`).
- **Legacy**: `repo:<owner>/<repo>`, used by older repositories that haven't opted in.

Look up a repository's prefix rather than guessing it:

```bash
gh api repos/my-org/my-repo/actions/oidc/customization/sub --jq .sub_claim_prefix
```

Appending `:*` to the prefix allows any branch or event in that repository. A role trusted by
both kinds of repository can list one pattern of each format.

An owner-wide wildcard such as `repo:my-org@123456/*@*:*` matches only immutable subjects, so
older repositories under the same owner (`repo:my-org/my-repo:ref:refs/heads/main`) can't
assume the role. To trust every repository of an owner, list both
`repo:my-org@123456/*@*:*` and `repo:my-org/*:*`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
        "StringLike": { "token.actions.githubusercontent.com:sub": "repo:my-org@123456/my-repo@456789:*" }
      }
    }
  ]
}
```

To also require that the token comes from this reusable workflow, customize the calling
repository's OIDC subject to include `job_workflow_ref`:

```bash
gh api -X PUT repos/my-org/my-repo/actions/oidc/customization/sub \
  -F use_default=false -f 'include_claim_keys[]=repo' -f 'include_claim_keys[]=job_workflow_ref'
```

Then match the new `sub` format, for example
`repo:my-org@123456/my-repo@456789:job_workflow_ref:kbrockhoff/gh-aidlc-actions-workflows/.github/workflows/beads-elt.yml@*`.
Repositories with immutable subjects keep the IDs in the `repo` part even when the claim is
customized.
This changes the `sub` of every OIDC token the repository issues, so update any other roles
that trust it.

### Permissions policy

Each action below is one the loader triggers through the
[S3 Tables Iceberg REST endpoint](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-open-source.html).
Replace the region, account, bucket name and namespace.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadCatalogConfig",
      "Effect": "Allow",
      "Action": "s3tables:GetTableBucket",
      "Resource": "arn:aws:s3tables:us-east-1:123456789012:bucket/aidlc-raw"
    },
    {
      "Sid": "UseRawNamespace",
      "Effect": "Allow",
      "Action": ["s3tables:GetNamespace", "s3tables:CreateTable"],
      "Resource": "arn:aws:s3tables:us-east-1:123456789012:bucket/aidlc-raw",
      "Condition": { "StringEquals": { "s3tables:namespace": "raw_beads" } }
    },
    {
      "Sid": "ReadWriteRawTables",
      "Effect": "Allow",
      "Action": [
        "s3tables:GetTableMetadataLocation",
        "s3tables:UpdateTableMetadataLocation",
        "s3tables:GetTableData",
        "s3tables:PutTableData"
      ],
      "Resource": "arn:aws:s3tables:us-east-1:123456789012:bucket/aidlc-raw/table/*",
      "Condition": { "StringEquals": { "s3tables:namespace": "raw_beads" } }
    }
  ]
}
```

| Action | Why the loader needs it |
| --- | --- |
| `s3tables:GetTableBucket` | `GET /v1/config` when the catalog connects |
| `s3tables:GetNamespace` | `HEAD` namespace: checks that the namespace exists |
| `s3tables:CreateTable` | Creates each table, and the load log, on first load |
| `s3tables:GetTableMetadataLocation` | Loads a table before appending or scanning the load log |
| `s3tables:UpdateTableMetadataLocation` | Commits appends, schema changes and the partition spec |
| `s3tables:GetTableData` | Reads metadata and manifest files and scans the load log |
| `s3tables:PutTableData` | Writes Parquet data, manifest and metadata files |

These are left out on purpose: `ListNamespaces`, `ListTables` and `GetTable` (the loader
never lists or checks for tables), and every delete, rename, policy and maintenance action.
The loader doesn't use vended credentials, so no plain `s3:` actions are needed.

If the namespace isn't created ahead of time, add this statement. `CreateNamespace` has no
`s3tables:namespace` condition key, so it can't be limited to one namespace.

```json
{
  "Sid": "CreateRawNamespace",
  "Effect": "Allow",
  "Action": "s3tables:CreateNamespace",
  "Resource": "arn:aws:s3tables:us-east-1:123456789012:bucket/aidlc-raw"
}
```

If the bucket or its tables use SSE-KMS with a customer managed key, the role also needs
`kms:GenerateDataKey` and `kms:Decrypt` on that key. The key policy must grant the same two
actions to `maintenance.s3tables.amazonaws.com`, or table creation fails. See
[permission requirements for S3 Tables SSE-KMS encryption](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-kms-permissions.html).

### Example infrastructure

Both examples create the IAM role with the trust and permissions policies above, and can
optionally create the GitHub OIDC provider, the table bucket and the namespace. They treat
deletion differently: the CloudFormation template sets `DeletionPolicy: Retain` on the
bucket and namespace, so deleting the stack keeps the data. The Terraform example doesn't,
so `terraform destroy` tries to delete a bucket and namespace it created. Set
`create_table_bucket = false` to manage the bucket outside the example. Their outputs map one-to-one to the workflow's `aws-role-arn`,
`table-bucket-arn`, `aws-region` and `namespace` inputs.

- **Terraform**: [`examples/terraform`](examples/terraform). Copy
  `terraform.tfvars.example` to `terraform.tfvars`, set `github_repositories` to each
  repository's `sub` prefix without `repo:`, then run
  `terraform init && terraform apply`.
- **CloudFormation**: [`examples/cloudformation/beads-elt-role.yaml`](examples/cloudformation/beads-elt-role.yaml).
  `AllowedSubjects` takes full `sub` patterns:

  ```bash
  aws cloudformation deploy \
    --template-file examples/cloudformation/beads-elt-role.yaml \
    --stack-name beads-elt \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
    --parameter-overrides 'AllowedSubjects=repo:my-org@123456/my-repo@456789:*'
  ```

Set `create_oidc_provider` / `CreateOidcProvider` to false if the account already has the
GitHub provider, since an account can have only one per URL.

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
(cd examples/terraform && terraform fmt -check && terraform init -backend=false && terraform validate)
uvx cfn-lint examples/cloudformation/beads-elt-role.yaml
```
