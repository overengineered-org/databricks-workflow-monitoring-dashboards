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

A change to the config format or to a user-owned template file is "Incompatible public
configuration". The user-owned template files are `.gitignore`, `databricks.yml.tmpl`,
`workflow-monitoring.yml.tmpl`, and `resources/*.yml.tmpl`. Give that pull request the
`upgrade-steps` label and an "Upgrade steps" section in its body. The label sorts the entry into
the "Upgrade steps" release notes section.

## 2. Merge the version change

1. Update `project.version` in `pyproject.toml`.
2. Run `uv lock` so `uv.lock` has the same version.
3. Run `uv run python scripts/build_template.py` so `VERSION` and the template copy match.
4. Update the README `--tag` and the schema URL tag to `v<version>`.
5. Merge the approved pull request with squash. The merge publishes `v<version>`.
6. Use a clean `main` checkout matching `origin/main`.

## 3. Check without publishing

Replace `<version>` with the version from `pyproject.toml`:

```sh
./scripts/release.sh --check v<version>
```

This is the local pre-merge check. It validates the exact commit. It creates no tag or release.

## 4. Publish if the workflow fails

If the `release.yml` workflow does not publish, run the fallback on the clean `main` checkout:

```sh
./scripts/release.sh --publish v<version>
```

The script creates the tag and immutable GitHub Release, then verifies the published commit.

## 5. Test the published tag

The pinned CLI can ask for credentials during `bundle init`. Fake values keep the render offline.
The same values are used in `tests/test_bundle_template.py`:

```sh
DATABRICKS_HOST=https://example.invalid DATABRICKS_TOKEN=fake \
databricks bundle init https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards \
  --tag v<version> --template-dir template --output-dir /tmp/check
```

This proves that the remote template renders from the pushed tag. The local check in section 3
proves that only the local checkout renders.

## What the release script proves

- The tag matches `project.version` in `pyproject.toml`.
- The local gate passes, so `VERSION`, the rendered template copy, and the docs tags match that
  version.
- Local `main` exactly matches `origin/main`, and the tree is clean.
- Gitleaks finds no secret in the history.
- The published tag points to the validated commit.

Releases are published by the `release.yml` workflow on the merge to `main`.
`scripts/release.sh --publish` is the fallback when that workflow fails.
