#!/usr/bin/env bash
set -euo pipefail

databricks_profile="${1:-}"
warehouse_id="${2:-}"
dashboard_path="${3:-src/dashboards/workflow-monitoring.lvdash.json}"

if [[ -z "$databricks_profile" || -z "$warehouse_id" ]]; then
  echo "usage: validate-sql-datasets.sh <profile> <warehouse-id> [dashboard-path]" >&2
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
    '{statement: $statement, warehouse_id: $warehouse_id, wait_timeout: "50s", on_wait_timeout: "CANCEL", row_limit: 1}' \
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

# These fixed dates test schedule math only. They do not create tables or rows.
sla_calculation_statement="
SELECT
  assert_true(array_contains(sequence(DATE '2026-02-10', DATE '2026-02-12'), DATE '2026-02-11'), 'daily schedule failed'),
  assert_true(date_add(DATE '2026-02-11', -pmod(dayofweek(DATE '2026-02-11') - 2, 7)) = DATE '2026-02-09', 'weekly schedule failed'),
  assert_true(date_add(date_add(DATE '2026-02-11', -pmod(dayofweek(DATE '2026-02-11') - 2, 7)), -7) = DATE '2026-02-02', 'weekly predecessor failed'),
  assert_true(date_add(DATE '2026-01-12', CAST(FLOOR(datediff(DATE '2026-02-11', DATE '2026-01-12') / 14.0) AS INT) * 14) = DATE '2026-02-09', 'fortnightly schedule failed'),
  assert_true(date_add(to_date(date_trunc('MONTH', DATE '2026-02-11')), LEAST(31, day(last_day(DATE '2026-02-11'))) - 1) = DATE '2026-02-28', 'monthly cap failed'),
  assert_true(to_utc_timestamp(to_timestamp('2026-01-15 06:00', 'yyyy-MM-dd HH:mm'), 'Australia/Melbourne') = TIMESTAMP '2026-01-14 19:00:00', 'timezone conversion failed')
"
validate_statement "sla_calculations" "$sla_calculation_statement"
