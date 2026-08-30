# Release guide

Run the check command only from clean, synchronized `main`. A release takes about 10 minutes
after local validation caches are warm.

This repository uses SemVer tags and GitHub Releases. Each release includes prebuilt
configuration CLI archives, so users do not need Go.

## Choose the version

| Change | Version |
| --- | --- |
| Incompatible public configuration | Major |
| New public capability | Minor |
| Fix or documentation only | Patch |

Before `1.0.0`, an incompatible change normally increments the minor version.

## Release in five steps

1. Update `project.version` in `pyproject.toml` through a pull request.
2. Squash-merge the approved pull request.
3. Update local `main` with `git pull --ff-only`.
4. Check the exact version without publishing:

   ```sh
   ./scripts/release.sh --check v0.1.0
   ```

5. After separate publish approval, create and verify the release:

   ```sh
   ./scripts/release.sh --publish v0.1.0
   ```

Replace `v0.1.0` with the version in `pyproject.toml`.

## What the script proves

- The version is valid SemVer and matches `pyproject.toml`.
- The branch is clean `main` at the exact `origin/main` commit.
- Required tools and GitHub authentication work.
- Local Act, full-history Gitleaks, and Go tests pass.
- CLI archives build for macOS, Linux, and Windows.

`--check` builds temporary archives but creates no tag or release. `--publish` also verifies
that the published tag resolves to the validated commit. This project does not use
semantic-release or GitHub-hosted workflows.

Next: compare `pyproject.toml` with the intended tag, then run step 4.
