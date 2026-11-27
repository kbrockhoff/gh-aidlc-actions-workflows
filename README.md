# Brockhoff Cloud GitHub Actions and Shared Workflows for AI-DLC

Provides GitHub Actions and shared workflows which support AI-DLC done the
Brockhoff way.

## Shared workflows

Each shared workflow has a spec in `specs/<name>/` and its code in `workflows/<name>/`.

| Workflow | Description |
| --- | --- |
| [beads-elt](workflows/beads-elt/README.md) | Loads the full beads Dolt database into S3 Tables Iceberg tables in the raw zone |

## Releases

Releases are automated with [release-please](https://github.com/googleapis/release-please)
from [Conventional Commits](https://www.conventionalcommits.org/) on `main`. The repository
has one version for all shared workflows:

- Each push to `main` updates a release PR with the next version and changelog.
- Merging the release PR tags `vX.Y.Z`, publishes a GitHub release, and moves the floating
  `vX` tag to it.
- Pin callers to `@vX` for compatible updates, or to `@vX.Y.Z` or a commit SHA for exact
  versions.

While the version is below 1.0.0, `feat` commits bump the minor version and breaking changes
don't move to 1.0.0 on their own. Use a `Release-As: 1.0.0` footer for that.

The workflow uses the `RELEASE_PLEASE_TOKEN` secret, a token with `contents: write` and
`pull-requests: write` on this repository. Pushes and PRs made with `GITHUB_TOKEN` don't
trigger other workflows, so CI wouldn't run on the release PR without it.
