#!/usr/bin/env bash
set -euo pipefail

release_action="${1:-}"
release_tag="${2:-}"

show_usage() {
  echo "usage: scripts/release.sh <--check|--publish> <vMAJOR.MINOR.PATCH>"
}

if [[ "$release_action" == "--help" || "$release_action" == "-h" ]]; then
  show_usage
  exit 0
fi

if [[ "$release_action" != "--check" && "$release_action" != "--publish" ]]; then
  show_usage >&2
  exit 2
fi

if [[ ! "$release_tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "release tag must use vMAJOR.MINOR.PATCH" >&2
  exit 2
fi

for required_command in act docker gh git gitleaks; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "missing required command: $required_command" >&2
    exit 1
  fi
done

case "$(uname -m)" in
  arm64 | aarch64)
    runner_platform="linux/arm64"
    ;;
  x86_64 | amd64)
    runner_platform="linux/amd64"
    ;;
  *)
    echo "unsupported release host architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

project_version="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml)"
if [[ "$project_version" != "${release_tag#v}" ]]; then
  echo "pyproject.toml version $project_version does not match $release_tag" >&2
  exit 1
fi

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

if git rev-parse --verify --quiet "refs/tags/$release_tag" >/dev/null; then
  echo "tag already exists: $release_tag" >&2
  exit 1
fi

if gh release view "$release_tag" >/dev/null 2>&1; then
  echo "GitHub Release already exists: $release_tag" >&2
  exit 1
fi

gh auth status >/dev/null
docker build --platform "$runner_platform" \
  -t databricks-workflow-monitoring-dashboards-act:local \
  -f .act/Dockerfile .
act --container-architecture "$runner_platform" --pull=false \
  -P ubuntu-latest=databricks-workflow-monitoring-dashboards-act:local \
  -W .act/workflows/validate.yml
gitleaks git --redact --log-opts="--all"

if [[ "$release_action" == "--check" ]]; then
  echo "release check passed: $release_tag at $release_commit"
  exit 0
fi

gh release create "$release_tag" \
  --target "$release_commit" \
  --title "$release_tag" \
  --generate-notes \
  --fail-on-no-commits
git fetch origin "refs/tags/$release_tag:refs/tags/$release_tag"
published_commit="$(git rev-list -n 1 "$release_tag")"
if [[ "$published_commit" != "$release_commit" ]]; then
  echo "published tag does not match validated commit" >&2
  exit 1
fi
gh release view "$release_tag" --json tagName,url,isDraft,isPrerelease
