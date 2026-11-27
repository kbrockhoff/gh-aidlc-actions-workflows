"""Command line entry point used by the beads-elt reusable workflow."""

from __future__ import annotations

import argparse
import os
import sys

from pyiceberg.catalog import Catalog, load_catalog

from beads_elt import dolt
from beads_elt.pipeline import LoadConfig, LoadResult, run


def s3tables_catalog(table_bucket_arn: str, region: str) -> Catalog:
    return load_catalog(
        "s3tables",
        type="rest",
        uri=f"https://s3tables.{region}.amazonaws.com/iceberg",
        warehouse=table_bucket_arn,
        **{
            "rest.sigv4-enabled": "true",
            "rest.signing-name": "s3tables",
            "rest.signing-region": region,
        },
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="beads-elt", description=__doc__)
    p.add_argument("--remote-url", required=True, help="Dolt git remote, e.g. git+https://...")
    p.add_argument("--source-repo", required=True, help="owner/name of the source repository")
    p.add_argument("--dolt-database", required=True)
    p.add_argument("--load-id", required=True)
    p.add_argument("--table-bucket-arn", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--namespace", default="raw_beads")
    p.add_argument("--table-prefix", default="")
    p.add_argument("--force", action="store_true")
    return p


def _write_github_files(result: LoadResult) -> None:
    outputs = {
        "ref-sha": result.ref_sha,
        "dolt-commit": result.dolt_commit,
        "skipped": str(result.skipped).lower(),
        "tables-loaded": str(result.tables_loaded),
        "rows-loaded": str(result.rows_loaded),
    }
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in outputs.items())
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        lines = [
            "## Beads ELT",
            "",
            f"- `refs/dolt/data`: `{result.ref_sha}`",
            f"- Dolt commit: `{result.dolt_commit or 'n/a'}`",
        ]
        if result.skipped:
            lines.append(f"- Skipped: {result.skip_reason}")
        else:
            lines += ["", "| Table | Rows |", "| --- | ---: |"]
            lines += [f"| {t} | {n} |" for t, n in result.row_counts.items()]
            lines.append(f"| **Total** | **{result.rows_loaded}** |")
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = LoadConfig(
        remote_url=args.remote_url,
        source_repo=args.source_repo,
        dolt_database=args.dolt_database,
        load_id=args.load_id,
        namespace=args.namespace,
        table_prefix=args.table_prefix,
        force=args.force,
    )
    ref_sha = dolt.remote_ref_sha(args.remote_url)
    result = run(s3tables_catalog(args.table_bucket_arn, args.region), config, ref_sha)
    _write_github_files(result)
    if result.skipped:
        print(f"Skipped: {result.skip_reason}")
    else:
        print(
            f"Loaded {result.rows_loaded} rows into {result.tables_loaded} tables "
            f"from Dolt commit {result.dolt_commit}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
