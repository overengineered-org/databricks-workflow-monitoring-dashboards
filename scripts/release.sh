#!/usr/bin/env bash
set -euo pipefail

release_action="${1:-}"
release_tag="${2:-}"
cleanup_render_directory=""

show_usage() {
  echo "usage: scripts/release.sh <--check|--publish> <vMAJOR.MINOR.PATCH> | --publish-on-merge"
}

is_semver_tag() {
  [[ "$1" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
}

project_version() {
  sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml
}

require_commands() {
  local required_command
  for required_command in "$@"; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
      echo "missing required command: $required_command" >&2
      exit 1
    fi
  done
}

assert_version_matches_tag() {
  local version
  version="$(project_version)"
  if [[ "$version" != "${1#v}" ]]; then
    echo "pyproject.toml version $version does not match $1" >&2
    exit 1
  fi
}

assert_no_existing_tag() {
  if git rev-parse --verify --quiet "refs/tags/$1" >/dev/null; then
    echo "tag already exists: $1" >&2
    exit 1
  fi
}

assert_no_existing_release() {
  if gh release view "$1" >/dev/null 2>&1; then
    echo "GitHub Release already exists: $1" >&2
    exit 1
  fi
}

scan_for_secrets() {
  gitleaks git --redact --log-opts="--all"
}

publish_release() {
  local tag="$1"
  local target="$2"
  local published_commit
  gh release create "$tag" \
    --target "$target" \
    --title "$tag" \
    --generate-notes \
    --fail-on-no-commits
  git fetch origin "refs/tags/$tag:refs/tags/$tag"
  published_commit="$(git rev-list -n 1 "$tag")"
  if [[ "$published_commit" != "$target" ]]; then
    echo "published tag does not match validated commit" >&2
    exit 1
  fi
  gh release view "$tag" --json tagName,url,isDraft,isPrerelease
}

# Workflow mode: no tag argument, no act gate, no origin/main comparison.
# The push commit in GitHub Actions must be the checked out commit.
publish_on_merge() {
  local version
  local tag
  version="$(project_version)"
  tag="v$version"
  if ! is_semver_tag "$tag"; then
    echo "release tag must use vMAJOR.MINOR.PATCH" >&2
    exit 2
  fi
  if git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null; then
    echo "no new version: $tag exists"
    exit 0
  fi
  require_commands git gh gitleaks databricks
  if gh release view "$tag" >/dev/null 2>&1; then
    echo "GitHub Release exists without tag: $tag" >&2
    exit 1
  fi
  if [[ -z "${GITHUB_SHA:-}" ]]; then
    echo "GITHUB_SHA is empty" >&2
    exit 1
  fi
  local head_commit
  head_commit="$(git rev-parse HEAD)"
  if [[ "$head_commit" != "$GITHUB_SHA" ]]; then
    echo "HEAD $head_commit does not equal GITHUB_SHA $GITHUB_SHA" >&2
    exit 1
  fi
  local render_directory
  render_directory="$(mktemp -d)"
  # A function-local name is unbound when the EXIT trap runs after the function
  # returns, and set -u then fails. The global name stays bound.
  cleanup_render_directory="$render_directory"
  trap 'rm -rf "$cleanup_render_directory"' EXIT
  DATABRICKS_HOST=https://example.invalid \
  DATABRICKS_TOKEN=fake \
    databricks bundle init template \
      --config-file tests/fixtures/template-input.json \
      --output-dir "$render_directory"
  scan_for_secrets
  publish_release "$tag" "$GITHUB_SHA"
}

if [[ "$release_action" == "--help" || "$release_action" == "-h" ]]; then
  show_usage
  exit 0
fi

if [[ "$release_action" == "--publish-on-merge" ]]; then
  if [[ $# -gt 1 ]]; then
    show_usage >&2
    exit 2
  fi
  publish_on_merge
  exit 0
fi

if [[ "$release_action" != "--check" && "$release_action" != "--publish" ]]; then
  show_usage >&2
  exit 2
fi

if ! is_semver_tag "$release_tag"; then
  echo "release tag must use vMAJOR.MINOR.PATCH" >&2
  exit 2
fi

require_commands act docker gh git gitleaks
assert_version_matches_tag "$release_tag"

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "releases must run from main" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "release worktree must be clean" >&2
  exit 1
fi

git fetch origin main --tags
release_commit="$(git rev-parse HEAD)"
origin_main_commit="$(git rev-parse origin/main)"
if [[ "$release_commit" != "$origin_main_commit" ]]; then
  echo "local main must equal origin/main" >&2
  exit 1
fi

assert_no_existing_tag "$release_tag"
assert_no_existing_release "$release_tag"

gh auth status >/dev/null
scripts/run-local-validation.sh
scan_for_secrets

if [[ "$release_action" == "--check" ]]; then
  echo "release check passed: $release_tag at $release_commit"
  exit 0
fi

publish_release "$release_tag" "$release_commit"
