# Releasing

Use this maintainer guide for a version bump. Complete the required local gate in
[CONTRIBUTING.md](CONTRIBUTING.md#3-validate) before merging.

The version-bump pull request is the release. The merge to `main` publishes the tag and the
GitHub Release.

## 1. Choose the version

| Change | Version |
| --- | --- |
| Incompatible public configuration | Major |
| New public capability | Minor |
| Fix or documentation only | Patch |

Before `1.0.0`, an incompatible change normally increments the minor version.

A change to the config format, to `workflow-monitoring.yml`, or to a `databricks.yml` variable is
"Incompatible public configuration". Adopters edit those files in their clones. Give that pull
request the
`upgrade-steps` label and an "Upgrade steps" section in its body. The label sorts the entry into
the "Upgrade steps" release notes section.

## 2. Merge the version change

1. Update `project.version` in `pyproject.toml`.
2. Run `uv lock` so `uv.lock` has the same version.
3. Merge the approved pull request with squash. The merge publishes `v<version>`.

## 3. Check without publishing

On the PR branch, run the local gate from [CONTRIBUTING.md](CONTRIBUTING.md#3-validate):

```sh
scripts/run-local-validation.sh
```

This is the local pre-merge check. It creates no tag or release.

## 4. Publish if the workflow fails

If the `release.yml` workflow does not publish, use a clean `main` checkout matching
`origin/main`. Replace `<version>` with the version from `pyproject.toml`:

```sh
./scripts/release.sh --check v<version>
./scripts/release.sh --publish v<version>
```

The script creates the tag and immutable GitHub Release, then verifies the published commit.

If GitHub answers `tag_name was used by an immutable release`, a deleted release once used that tag
name. GitHub reserves it forever, so neither the workflow nor the script can publish it. Merge a new
version-bump pull request with the next patch version instead.

## 5. Test the published tag

Clone the tag and check the wrapper offline:

```sh
git clone --branch v<version> https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard.git /tmp/check
cd /tmp/check
uv run workflow-monitoring --version
```

## What the release script proves

- The tag matches `project.version` in `pyproject.toml`.
- The local gate passes on the release commit.
- Local `main` exactly matches `origin/main`, and the tree is clean.
- Gitleaks finds no secret in the history.
- The published tag points to the validated commit.

Releases are published by the `release.yml` workflow on the merge to `main`.
`scripts/release.sh --publish` is the fallback when that workflow fails.
