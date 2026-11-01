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
monitored_runs AS (
  SELECT r.configured_order, r.workflow_name, r.workspace_id, r.job_id, t.run_id, MIN(t.period_start_time) AS run_start_time, MAX(t.period_end_time) AS run_end_time, MAX(t.result_state) AS result_state
  FROM monitored_workflows r INNER JOIN workflow_run_api_state t ON r.workspace_id = t.workspace_id AND r.job_id = t.job_id
  WHERE t.period_start_time >= date_sub(current_date(), ${run_window_days})
  GROUP BY r.configured_order, r.workflow_name, r.workspace_id, r.job_id, t.run_id HAVING MAX(t.result_state) IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'SKIPPED')
)
SELECT r.workflow_name, r.job_id, r.run_id, to_date(r.run_end_time) AS run_date, r.run_start_time, r.run_end_time,
  unix_timestamp(r.run_end_time) - unix_timestamp(r.run_start_time) AS duration_seconds, r.result_state,
  CASE WHEN r.result_state = 'SUCCEEDED' THEN 'Success' ELSE 'Not successful' END AS run_outcome
FROM monitored_runs r
ORDER BY r.run_end_time DESC
