# Releasing

Use this maintainer guide after the pull request is approved. Complete the required local gate in
[CONTRIBUTING.md](CONTRIBUTING.md#3-validate) before merging.

A release takes about 10 minutes with warm validation caches. Publishing needs separate approval.

## 1. Choose the version

| Change | Version |
| --- | --- |
| Incompatible public configuration | Major |
| New public capability | Minor |
| Fix or documentation only | Patch |

Before `1.0.0`, an incompatible change normally increments the minor version.

## 2. Merge the version change

1. Update `project.version` in `pyproject.toml`.
2. Run `uv lock` so `uv.lock` has the same version.
3. Merge the approved pull request with squash.
4. Use a clean `main` checkout matching `origin/main`.

## 3. Check without publishing

Replace `<version>` with the version from `pyproject.toml`:

```sh
./scripts/release.sh --check v<version>
```

This validates the exact commit and builds temporary archives. It creates no tag or release.

## 4. Publish after approval

```sh
./scripts/release.sh --publish v<version>
```

The script creates the tag and immutable GitHub Release, then verifies the published commit.

## What the release script proves

- The SemVer tag matches `pyproject.toml`.
- Local `main` exactly matches `origin/main`.
- Act, Gitleaks, Go tests, and release builds pass.
- CLI archives exist for macOS, Linux, and Windows.
- GitHub publishes a SHA-256 digest for each immutable release asset.
- The published tag points to the validated commit.

Approved mirrors must copy each archive without modification. Before mirroring, read the trusted
GitHub digests and verify the copied bytes against the matching asset digest:

```sh
gh release view v<version> --json assets \
  --jq '.assets[] | [.name, .digest] | @tsv'
```

The repository launcher always obtains its expected digest from the GitHub Release API, including
when archive downloads use a mirror. It fails closed if that digest is unavailable. This detects
altered download, mirror, and cache bytes, but not a compromised release publisher.

Users run the released CLI through the repository-local command documented in
[README step 1](README.md#1-clone-and-prepare). This repository does not use semantic-release or
GitHub-hosted workflows.
