#!/usr/bin/env bash
# Fast checks for every change. They need only uv and no Databricks workspace.
# validate.yml runs this script, so local results and the GitHub check agree.
set -euo pipefail

cd "$(dirname "$0")/.."

uv lock --check
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests -v

# The built dashboard must match its scaffold, SQL, and Vega-Lite sources.
dashboard_path="src/dashboards/workflow-monitoring.lvdash.json"
dashboard_snapshot="$(mktemp)"
trap 'rm -f "$dashboard_snapshot"' EXIT
cp "$dashboard_path" "$dashboard_snapshot"
uv run python scripts/build_dashboard.py
if ! cmp -s "$dashboard_snapshot" "$dashboard_path"; then
  echo "the built dashboard was stale; commit the rebuilt $dashboard_path" >&2
  exit 1
fi
uv run python -m json.tool "$dashboard_path" >/dev/null

# Build the character from bytes so this file does not contain it itself.
# grep exits 1 for no match and 2 for an error. Only exit 1 may pass.
banned_character="$(printf '\342\200\224')"
set +e
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # Scan only what Git would commit: tracked files and new files that are not ignored.
  git grep -I -l --untracked -e "$banned_character"
else
  # act can copy a checkout without usable Git metadata. Its copy holds no local junk.
  grep -rIl --exclude-dir=.git --exclude-dir=.venv "$banned_character" .
fi
grep_status=$?
set -e
if [[ "$grep_status" -gt 1 ]]; then
  echo "grep exited with code $grep_status" >&2
  exit 1
fi
if [[ "$grep_status" -eq 0 ]]; then
  echo "banned em dash found in the files above" >&2
  exit 1
fi

echo "all fast checks passed"
