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

# Inline rows evaluate fixed-date statuses without creating tables or other workspace objects.
sla_regression_statement="
WITH inline_workflows (
  workflow_name, frequency, completion_time, day_of_week, first_deadline_date,
  day_of_month, current_instant_utc
) AS (
  VALUES
    ('daily_miss', 'daily', '06:00', CAST(NULL AS STRING), CAST(NULL AS DATE), CAST(NULL AS INT), TIMESTAMP '2026-09-25 05:00:00'),
    ('daily_success', 'daily', '06:00', CAST(NULL AS STRING), CAST(NULL AS DATE), CAST(NULL AS INT), TIMESTAMP '2026-09-25 05:00:00'),
    ('monthly_miss', 'monthly', '06:00', CAST(NULL AS STRING), CAST(NULL AS DATE), 26, TIMESTAMP '2026-09-25 05:00:00'),
    ('weekly_miss', 'weekly', '06:00', 'monday', CAST(NULL AS DATE), CAST(NULL AS INT), TIMESTAMP '2026-09-28 05:00:00'),
    ('fortnightly_miss', 'fortnightly', '06:00', CAST(NULL AS STRING), DATE '2026-09-14', CAST(NULL AS INT), TIMESTAMP '2026-09-28 05:00:00'),
    ('fortnightly_before_anchor', 'fortnightly', '06:00', CAST(NULL AS STRING), DATE '2026-10-12', CAST(NULL AS INT), TIMESTAMP '2026-09-25 05:00:00'),
    ('fortnightly_on_anchor', 'fortnightly', '06:00', CAST(NULL AS STRING), DATE '2026-10-12', CAST(NULL AS INT), TIMESTAMP '2026-10-12 07:00:00'),
    ('fortnightly_after_anchor', 'fortnightly', '06:00', CAST(NULL AS STRING), DATE '2026-10-12', CAST(NULL AS INT), TIMESTAMP '2026-10-27 07:00:00')
),
inline_runs (workflow_name, run_end_time, result_state) AS (
  VALUES
    ('daily_success', TIMESTAMP '2026-09-24 05:30:00', 'SUCCEEDED'),
    ('fortnightly_on_anchor', TIMESTAMP '2026-10-05 05:30:00', 'SUCCEEDED'),
    ('fortnightly_after_anchor', TIMESTAMP '2026-10-20 05:30:00', 'SUCCEEDED')
),
workflow_local_dates AS (
  SELECT *, to_date(current_instant_utc) AS local_today,
    CASE day_of_week WHEN 'sunday' THEN 1 WHEN 'monday' THEN 2 WHEN 'tuesday' THEN 3 WHEN 'wednesday' THEN 4 WHEN 'thursday' THEN 5 WHEN 'friday' THEN 6 WHEN 'saturday' THEN 7 END AS target_day
  FROM inline_workflows
),
current_deadline_dates AS (
  -- Fortnightly rows keep a pre-anchor boundary for the first period; only the anchor and later dates are eligible.
  SELECT w.*, deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence(date_sub(local_today, 2), date_add(local_today, 1))) d AS deadline_date WHERE frequency = 'daily'
  UNION ALL
  SELECT w.*, date_add(date_add(local_today, -pmod(dayofweek(local_today) - target_day, 7)), offset * 7) AS deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence(-2, 1)) o AS offset WHERE frequency = 'weekly'
  UNION ALL
  SELECT w.*, date_add(first_deadline_date, (GREATEST(CAST(FLOOR(datediff(local_today, first_deadline_date) / 14.0) AS INT), 0) + offset) * 14) AS deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence(-2, 1)) o AS offset WHERE frequency = 'fortnightly'
  UNION ALL
  SELECT w.*, date_add(to_date(add_months(date_trunc('MONTH', local_today), offset)), LEAST(day_of_month, day(last_day(add_months(local_today, offset)))) - 1) AS deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence(-2, 1)) o AS offset WHERE frequency = 'monthly'
),
history_deadline_dates AS (
  -- Fortnightly rows keep a pre-anchor boundary for the first period; only the anchor and later dates are eligible.
  SELECT w.*, deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence(date_sub(local_today, 61), date_add(local_today, 2))) d AS deadline_date WHERE frequency = 'daily'
  UNION ALL
  SELECT w.*, date_add(date_add(date_sub(local_today, 61), pmod(target_day - dayofweek(date_sub(local_today, 61)), 7)), offset * 7) AS deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence(-1, 14)) o AS offset WHERE frequency = 'weekly'
  UNION ALL
  SELECT w.*, date_add(first_deadline_date, (GREATEST(CAST(FLOOR(datediff(local_today, first_deadline_date) / 14.0) AS INT), 0) + offset) * 14) AS deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence(-5, 3)) o AS offset WHERE frequency = 'fortnightly'
  UNION ALL
  SELECT w.*, date_add(to_date(add_months(date_trunc('MONTH', local_today), offset)), LEAST(day_of_month, day(last_day(add_months(local_today, offset)))) - 1) AS deadline_date FROM workflow_local_dates w LATERAL VIEW EXPLODE(sequence(-3, 2)) o AS offset WHERE frequency = 'monthly'
),
current_deadline_periods AS (
  SELECT *,
    to_utc_timestamp(to_timestamp(concat(CAST(deadline_date AS STRING), ' ', completion_time), 'yyyy-MM-dd HH:mm'), 'UTC') AS deadline_utc,
    LAG(to_utc_timestamp(to_timestamp(concat(CAST(deadline_date AS STRING), ' ', completion_time), 'yyyy-MM-dd HH:mm'), 'UTC')) OVER (PARTITION BY workflow_name ORDER BY deadline_date) AS previous_deadline_utc
  FROM current_deadline_dates
),
history_deadline_periods AS (
  SELECT *,
    to_utc_timestamp(to_timestamp(concat(CAST(deadline_date AS STRING), ' ', completion_time), 'yyyy-MM-dd HH:mm'), 'UTC') AS deadline_utc,
    LAG(to_utc_timestamp(to_timestamp(concat(CAST(deadline_date AS STRING), ' ', completion_time), 'yyyy-MM-dd HH:mm'), 'UTC')) OVER (PARTITION BY workflow_name ORDER BY deadline_date) AS previous_deadline_utc
  FROM history_deadline_dates
),
current_deadline_results AS (
  SELECT d.workflow_name, d.deadline_utc, d.current_instant_utc,
    CASE WHEN COUNT(DISTINCT CASE WHEN r.result_state = 'SUCCEEDED' THEN r.run_end_time END) > 0 THEN 'Met' WHEN d.deadline_utc <= d.current_instant_utc THEN 'Missed' ELSE 'Pending' END AS deadline_status
  FROM current_deadline_periods d
  LEFT JOIN inline_runs r ON d.workflow_name = r.workflow_name AND r.run_end_time > d.previous_deadline_utc AND r.run_end_time <= d.deadline_utc
  WHERE d.previous_deadline_utc IS NOT NULL AND (d.frequency <> 'fortnightly' OR d.deadline_date >= d.first_deadline_date)
  GROUP BY d.workflow_name, d.deadline_utc, d.current_instant_utc
),
history_deadline_results AS (
  SELECT d.workflow_name, d.deadline_utc, d.current_instant_utc,
    CASE WHEN COUNT(DISTINCT CASE WHEN r.result_state = 'SUCCEEDED' THEN r.run_end_time END) > 0 THEN 'Met' WHEN d.deadline_utc <= d.current_instant_utc THEN 'Missed' ELSE 'Pending' END AS deadline_status
  FROM history_deadline_periods d
  LEFT JOIN inline_runs r ON d.workflow_name = r.workflow_name AND r.run_end_time > d.previous_deadline_utc AND r.run_end_time <= d.deadline_utc
  WHERE d.previous_deadline_utc IS NOT NULL AND (d.frequency <> 'fortnightly' OR d.deadline_date >= d.first_deadline_date)
  GROUP BY d.workflow_name, d.deadline_utc, d.current_instant_utc
),
current_last_due AS (
  SELECT * FROM current_deadline_results WHERE deadline_utc <= current_instant_utc
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workflow_name ORDER BY deadline_utc DESC) = 1
),
current_next_due AS (
  SELECT * FROM current_deadline_results WHERE deadline_utc > current_instant_utc
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workflow_name ORDER BY deadline_utc) = 1
),
history_last_due AS (
  SELECT * FROM history_deadline_results WHERE deadline_utc <= current_instant_utc
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workflow_name ORDER BY deadline_utc DESC) = 1
),
history_next_due AS (
  SELECT * FROM history_deadline_results WHERE deadline_utc > current_instant_utc
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workflow_name ORDER BY deadline_utc) = 1
),
regression_results AS (
  SELECT w.workflow_name, l.deadline_utc AS latest_due_at, n.deadline_utc AS next_due_at,
    h.deadline_utc AS history_latest_due_at, hn.deadline_utc AS history_next_due_at,
    l.deadline_status AS current_latest_due_status,
    h.deadline_status AS history_latest_due_status,
    CASE WHEN l.deadline_status = 'Missed' THEN 'Missed' ELSE COALESCE(n.deadline_status, l.deadline_status, 'Pending') END AS current_sla_status,
    CASE WHEN l.deadline_status <> 'Missed' AND COALESCE(n.deadline_status, 'Pending') = 'Pending' THEN 1 ELSE 0 END AS sla_pending_flag,
    CASE WHEN l.deadline_status = 'Missed' THEN 1 ELSE 0 END AS sla_missed_flag,
    CASE WHEN l.deadline_status = 'Missed' THEN 1 ELSE 0 END AS needs_attention_flag
  FROM inline_workflows w
  LEFT JOIN current_last_due l ON w.workflow_name = l.workflow_name
  LEFT JOIN current_next_due n ON w.workflow_name = n.workflow_name
  LEFT JOIN history_last_due h ON w.workflow_name = h.workflow_name
  LEFT JOIN history_next_due hn ON w.workflow_name = hn.workflow_name
)
SELECT
  assert_true(count_if(workflow_name = 'daily_miss' AND latest_due_at = TIMESTAMP '2026-09-24 06:00:00' AND current_sla_status = 'Missed' AND sla_pending_flag = 0 AND sla_missed_flag = 1 AND needs_attention_flag = 1 AND current_latest_due_status = history_latest_due_status) = 1, 'daily missed deadline regression failed'),
  assert_true(count_if(workflow_name = 'monthly_miss' AND latest_due_at = TIMESTAMP '2026-08-26 06:00:00' AND current_sla_status = 'Missed' AND sla_pending_flag = 0 AND sla_missed_flag = 1 AND needs_attention_flag = 1 AND current_latest_due_status = history_latest_due_status) = 1, 'monthly missed deadline regression failed'),
  assert_true(count_if(workflow_name = 'weekly_miss' AND latest_due_at = TIMESTAMP '2026-09-21 06:00:00' AND current_sla_status = 'Missed' AND current_latest_due_status = history_latest_due_status) = 1, 'weekly boundary regression failed'),
  assert_true(count_if(workflow_name = 'fortnightly_miss' AND latest_due_at = TIMESTAMP '2026-09-14 06:00:00' AND current_sla_status = 'Missed' AND current_latest_due_status = history_latest_due_status) = 1, 'fortnightly boundary regression failed'),
  assert_true(count_if(workflow_name = 'fortnightly_before_anchor' AND latest_due_at IS NULL AND history_latest_due_at IS NULL AND next_due_at = TIMESTAMP '2026-10-12 06:00:00' AND history_next_due_at = TIMESTAMP '2026-10-12 06:00:00' AND current_sla_status = 'Pending' AND sla_missed_flag = 0) = 1, 'fortnightly before-anchor regression failed'),
  assert_true(count_if(workflow_name = 'fortnightly_on_anchor' AND latest_due_at = TIMESTAMP '2026-10-12 06:00:00' AND history_latest_due_at = latest_due_at AND current_latest_due_status = 'Met' AND history_latest_due_status = 'Met') = 1, 'fortnightly on-anchor regression failed'),
  assert_true(count_if(workflow_name = 'fortnightly_after_anchor' AND latest_due_at = TIMESTAMP '2026-10-26 06:00:00' AND history_latest_due_at = latest_due_at AND current_latest_due_status = 'Met' AND history_latest_due_status = 'Met') = 1, 'fortnightly after-anchor regression failed'),
  assert_true(count_if(workflow_name = 'daily_success' AND latest_due_at = TIMESTAMP '2026-09-24 06:00:00' AND current_latest_due_status = 'Met' AND history_latest_due_status = 'Met' AND current_sla_status = 'Pending' AND sla_pending_flag = 1 AND sla_missed_flag = 0 AND needs_attention_flag = 0) = 1, 'successful run control failed'),
  assert_true(to_utc_timestamp(to_timestamp('2026-01-15 06:00', 'yyyy-MM-dd HH:mm'), 'Australia/Melbourne') = TIMESTAMP '2026-01-14 19:00:00', 'timezone conversion failed')
FROM regression_results
"
validate_statement "sla_regressions" "$sla_regression_statement"
