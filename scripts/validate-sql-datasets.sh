#!/usr/bin/env bash
set -euo pipefail

databricks_profile="${1:-}"
warehouse_id="${2:-}"
catalog="${3:-}"
schema="${4:-}"
default_dashboard_path="src/dashboards/workflow-monitoring.lvdash.json"
dashboard_path="${5:-$default_dashboard_path}"

if [[ -z "$databricks_profile" || -z "$warehouse_id" || -z "$catalog" || -z "$schema" ]]; then
  echo "usage: validate-sql-datasets.sh <profile> <warehouse-id> <catalog> <schema> [dashboard-path]" >&2
  exit 2
fi

# Temporary request files avoid placing long SQL strings directly in CLI arguments.
dataset_count="$(jq '.datasets | length' "$dashboard_path")"
request_path="$(mktemp)"
response_path="$(mktemp)"
trap 'rm -f "$request_path" "$response_path"' EXIT

validate_statement() {
  local statement_name="$1"
  local statement="$2"
  local statement_state
  local statement_error
  # Ask the warehouse to finish within 50 seconds. Cancel instead of leaving work running.
  jq -n \
    --arg statement "$statement" \
    --arg warehouse_id "$warehouse_id" \
    --arg catalog "$catalog" \
    --arg schema "$schema" \
    '{statement: $statement, warehouse_id: $warehouse_id, catalog: $catalog, schema: $schema, wait_timeout: "50s", on_wait_timeout: "CANCEL", row_limit: 1}' \
    >"$request_path"

  databricks api post /api/2.0/sql/statements \
    --profile "$databricks_profile" \
    --output json \
    --json "@$request_path" \
    >"$response_path"

  statement_state="$(jq -r '.status.state' "$response_path")"
  if [[ "$statement_state" != "SUCCEEDED" ]]; then
    statement_error="$(jq -r '.status.error.message // "statement did not succeed"' "$response_path")"
    echo "$statement_name: $statement_state: $statement_error" >&2
    exit 1
  fi
  echo "$statement_name: SQL valid"
}

for ((dataset_index = 0; dataset_index < dataset_count; dataset_index++)); do
  dataset_name="$(jq -r ".datasets[$dataset_index].name" "$dashboard_path")"
  dataset_statement="$(jq -r ".datasets[$dataset_index].queryLines | join(\"\")" "$dashboard_path")"
  validate_statement "$dataset_name" "$dataset_statement"

  measure_count="$(jq ".datasets[$dataset_index].columns // [] | length" "$dashboard_path")"
  if ((measure_count > 0)); then
    measure_expressions="$(
      jq -r ".datasets[$dataset_index].columns | map(.expression) | join(\", \")" \
        "$dashboard_path"
    )"
    validate_statement \
      "$dataset_name measures" \
      "SELECT $measure_expressions FROM ($dataset_statement) AS dashboard_dataset"
  fi
done

# The fixed-date regression SQL renders from the same fragments as the dashboard datasets.
sla_regression_statement="$(uv run python scripts/build_dashboard.py --regression-sql)"
validate_statement "sla_regressions" "$sla_regression_statement"
