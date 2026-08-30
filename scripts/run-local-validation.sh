#!/usr/bin/env bash
set -euo pipefail

runner_image="databricks-workflow-monitoring-dashboards-act:local"
runner_container_label="org.overengineered.workflow-monitoring.local-act=true"
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for required_command in act docker; do
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
    echo "unsupported host architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

cd "$repository_root"
docker build --platform "$runner_platform" \
  --tag "$runner_image" \
  --file .act/Dockerfile .

runner_image_id="$(docker image inspect --format '{{.Id}}' "$runner_image")"
while IFS= read -r reusable_container_id; do
  if [[ -z "$reusable_container_id" ]]; then
    continue
  fi
  container_image_id="$(docker inspect --format '{{.Image}}' "$reusable_container_id")"
  if [[ "$container_image_id" != "$runner_image_id" ]]; then
    docker rm --force "$reusable_container_id" >/dev/null
  fi
done < <(docker ps --all --quiet --filter "label=$runner_container_label")

validation_exit_code=0
act --container-architecture "$runner_platform" \
  --bind \
  --reuse \
  --rm \
  --pull=false \
  --container-options "--label $runner_container_label" \
  -P "ubuntu-latest=$runner_image" \
  -W .act/workflows/validate.yml || validation_exit_code=$?

docker image prune --force --filter dangling=true
exit "$validation_exit_code"
