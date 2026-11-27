# Brockhoff Cloud GitHub Actions and Shared Workflows for AI-DLC

Provides GitHub Actions and shared workflows which support AI-DLC done the
Brockhoff way.

## Shared workflows

Each shared workflow has a spec in `specs/<name>/` and its code in `workflows/<name>/`.

| Workflow | Description |
| --- | --- |
| [beads-elt](workflows/beads-elt/README.md) | Loads the full beads Dolt database into S3 Tables Iceberg tables in the raw zone |
