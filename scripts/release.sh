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

for required_command in act docker gh git gitleaks go tar zip; do
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

release_asset_directory="$(mktemp -d)"
trap 'rm -rf "$release_asset_directory"' EXIT
release_assets=()

build_cli_asset() {
  local target_os="$1"
  local target_architecture="$2"
  local archive_format="$3"
  local asset_name="workflow-monitoring-${target_os}-${target_architecture}"
  local build_directory="$release_asset_directory/$asset_name"
  local binary_name="workflow-monitoring"
  if [[ "$target_os" == "windows" ]]; then
    binary_name="workflow-monitoring.exe"
  fi

  mkdir -p "$build_directory"
  CGO_ENABLED=0 GOOS="$target_os" GOARCH="$target_architecture" \
    go build -trimpath -ldflags="-s -w -X main.buildVersion=$project_version" \
    -o "$build_directory/$binary_name" ./cmd/workflow-monitoring

  if [[ "$archive_format" == "zip" ]]; then
    local archive_path="$release_asset_directory/$asset_name.zip"
    zip -q -j "$archive_path" "$build_directory/$binary_name"
  else
    local archive_path="$release_asset_directory/$asset_name.tar.gz"
    tar -C "$build_directory" -czf "$archive_path" "$binary_name"
  fi
  release_assets+=("$archive_path")
}

build_cli_asset darwin arm64 tar.gz
build_cli_asset darwin amd64 tar.gz
build_cli_asset linux arm64 tar.gz
build_cli_asset linux amd64 tar.gz
build_cli_asset windows amd64 zip

if [[ "$release_action" == "--check" ]]; then
  echo "release check passed: $release_tag at $release_commit"
  exit 0
fi

gh release create "$release_tag" \
  --target "$release_commit" \
  --title "$release_tag" \
  --generate-notes \
  --fail-on-no-commits \
  "${release_assets[@]}"
git fetch origin "refs/tags/$release_tag:refs/tags/$release_tag"
published_commit="$(git rev-list -n 1 "$release_tag")"
if [[ "$published_commit" != "$release_commit" ]]; then
  echo "published tag does not match validated commit" >&2
  exit 1
fi
gh release view "$release_tag" --json tagName,url,isDraft,isPrerelease
