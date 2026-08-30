# Release guide

This repository uses local GitHub Releases with SemVer tags and prebuilt configuration CLI
archives.

It does not use semantic-release or GitHub-hosted workflows. Users install a release binary and
do not need Go.

## Choose the version

| Change | Version |
| --- | --- |
| Incompatible public configuration | Major |
| New backward-compatible capability | Minor |
| Fix or documentation only | Patch |

Before `1.0.0`, incompatible changes normally increment the minor version.

## Release

1. Update `project.version` in `pyproject.toml` through a pull request.
2. Squash-merge the approved release pull request.
3. Update local `main` with `git pull --ff-only`.
4. Run the release check:

   ```sh
   ./scripts/release.sh --check v0.1.0
   ```

5. After separate publish approval, create and verify the release:

   ```sh
   ./scripts/release.sh --publish v0.1.0
   ```

Replace `v0.1.0` with the version in `pyproject.toml`.

## What the script proves

- Version is valid SemVer and matches `pyproject.toml`.
- Branch is clean `main` at the exact `origin/main` commit.
- Required CLI tools are installed and GitHub authentication works.
- Local Act validation uses the host's ARM64 or AMD64 architecture; full-history Gitleaks passes.
- Go tests pass and CLI archives build for macOS, Linux, and Windows.
- The published GitHub tag resolves to the validated commit.

`--check` builds temporary archives but never creates a tag or release. `--publish` attaches the
same target set to the GitHub Release.
