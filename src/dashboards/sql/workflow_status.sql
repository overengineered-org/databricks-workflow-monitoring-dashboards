WITH configured_workflows AS (
-- Generated from active entries in workflow-monitoring.yml.
SELECT configured_order, workspace_id, configured_job_id, frequency, completion_time, timezone, day_of_week, first_deadline_date, day_of_month FROM workflow_sla_config
),
-- Join configured job IDs with Jobs API metadata and collection state.
collected_jobs AS (
  SELECT workspace_id, job_id, job_name, last_collection_attempt_at,
    last_successful_collection_at, last_error_message
  FROM workflow_job_api_state
  WHERE workspace_id IN (SELECT DISTINCT workspace_id FROM configured_workflows)
),
monitored_workflows AS (
  SELECT c.*, c.configured_job_id AS job_id,
    COALESCE(collected_job.job_name, CONCAT('Job ', c.configured_job_id)) AS workflow_name,
    CASE
      WHEN collected_job.job_id IS NULL THEN 'Jobs API collection pending'
      WHEN collected_job.last_error_message IS NOT NULL THEN 'Jobs API collection failed'
      WHEN collected_job.last_successful_collection_at < current_timestamp() - INTERVAL ${stale_threshold_minutes} MINUTES THEN 'Jobs API data stale'
    END AS configuration_problem
  FROM configured_workflows c
  LEFT JOIN collected_jobs collected_job
    ON c.workspace_id = collected_job.workspace_id
    AND c.configured_job_id = collected_job.job_id
),
workflow_runs AS (
  SELECT r.configured_order, r.workspace_id, r.job_id, r.timezone, t.run_id, MIN(t.period_start_time) AS run_start_time, MAX(t.period_end_time) AS run_end_time, MAX(t.result_state) AS result_state
  FROM monitored_workflows r INNER JOIN workflow_run_api_state t ON r.workspace_id = t.workspace_id AND r.job_id = t.job_id
  WHERE t.period_start_time >= date_sub(current_date(), ${run_window_days})
  -- The run table is unique per (workspace_id, job_id, run_id), so each group holds one run.
  -- The HAVING only filters run states, and a WHERE clause could do the same work.
  GROUP BY r.configured_order, r.workspace_id, r.job_id, r.timezone, t.run_id HAVING MAX(t.result_state) IS NOT NULL
),
latest_runs AS (
  SELECT * FROM workflow_runs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY configured_order ORDER BY run_start_time DESC, run_id DESC) = 1
),
today_counts AS (
  SELECT configured_order,
    COUNT(DISTINCT CASE WHEN result_state = 'SUCCEEDED' THEN run_id END) AS successful_runs_today,
    COUNT(DISTINCT CASE WHEN result_state IN ('FAILED', 'CANCELLED', 'SKIPPED') THEN run_id END) AS unsuccessful_runs_today
  FROM workflow_runs
  WHERE to_date(from_utc_timestamp(run_end_time, timezone)) = to_date(from_utc_timestamp(current_timestamp(), timezone))
  GROUP BY configured_order
),
workflow_local_dates AS (
  SELECT r.*, to_date(from_utc_timestamp(current_timestamp(), timezone)) AS local_today,
    ${weekday_number} AS target_day
  FROM monitored_workflows r
),
-- Fortnightly rows keep a pre-anchor boundary for the first period; only the anchor and later dates are eligible.
-- Build the nearby deadline dates for each supported schedule.
deadline_dates AS (
${deadline_dates}
),
deadline_instants AS (
  SELECT *, ${deadline_instant} AS deadline_utc
  FROM deadline_dates
),
deadline_periods AS (
  SELECT *, LAG(deadline_utc) OVER (PARTITION BY configured_order ORDER BY deadline_utc) AS previous_deadline_utc
  FROM deadline_instants
),
-- A period is met only when a successful run completed before its deadline.
deadline_results AS (
  SELECT d.configured_order, d.deadline_utc,
    ${deadline_status} AS deadline_status
  FROM deadline_periods d
  LEFT JOIN workflow_runs r ON d.configured_order = r.configured_order AND r.run_end_time > d.previous_deadline_utc AND r.run_end_time <= d.deadline_utc
  WHERE d.previous_deadline_utc IS NOT NULL AND (d.frequency <> 'fortnightly' OR d.deadline_date >= d.first_deadline_date)
  GROUP BY d.configured_order, d.deadline_utc
),
last_due AS (
  SELECT * FROM deadline_results WHERE deadline_utc <= current_timestamp()
  QUALIFY ROW_NUMBER() OVER (PARTITION BY configured_order ORDER BY deadline_utc DESC) = 1
),
next_due AS (
  SELECT * FROM deadline_results WHERE deadline_utc > current_timestamp()
  QUALIFY ROW_NUMBER() OVER (PARTITION BY configured_order ORDER BY deadline_utc) = 1
)
SELECT r.configured_order, r.workflow_name, r.job_id, r.frequency, r.timezone,
  r.configuration_problem,
  lr.result_state AS latest_run_result, lr.run_end_time AS latest_run_end_time, n.deadline_utc AS next_deadline_utc,
  CASE WHEN l.deadline_status = 'Missed' THEN 'Missed' ELSE COALESCE(n.deadline_status, l.deadline_status, 'Pending') END AS sla_status,
  CASE WHEN r.configuration_problem IS NOT NULL THEN 'Configuration problem' WHEN l.deadline_status = 'Missed' THEN 'Missed' ELSE COALESCE(n.deadline_status, l.deadline_status, 'Pending') END AS health_status,
  1 AS monitored_workflow_count, COALESCE(t.successful_runs_today, 0) AS successful_runs_today, COALESCE(t.unsuccessful_runs_today, 0) AS unsuccessful_runs_today,
  CASE WHEN l.deadline_status <> 'Missed' AND COALESCE(n.deadline_status, 'Pending') = 'Pending' THEN 1 ELSE 0 END AS sla_pending_flag,
  CASE WHEN l.deadline_status = 'Missed' THEN 1 ELSE 0 END AS sla_missed_flag,
  CASE WHEN r.configuration_problem IS NOT NULL THEN 1 ELSE 0 END AS configuration_problem_flag,
  CASE WHEN r.configuration_problem IS NOT NULL OR l.deadline_status = 'Missed' THEN 1 ELSE 0 END AS needs_attention_flag
FROM monitored_workflows r
LEFT JOIN latest_runs lr ON r.configured_order = lr.configured_order
LEFT JOIN today_counts t ON r.configured_order = t.configured_order
LEFT JOIN last_due l ON r.configured_order = l.configured_order
LEFT JOIN next_due n ON r.configured_order = n.configured_order
ORDER BY r.configured_order
