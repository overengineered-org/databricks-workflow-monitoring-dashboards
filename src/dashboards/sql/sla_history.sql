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
      WHEN collected_job.last_successful_collection_at < current_timestamp() - INTERVAL 20 MINUTES THEN 'Jobs API data stale'
    END AS configuration_problem
  FROM configured_workflows c
  LEFT JOIN collected_jobs collected_job
    ON c.workspace_id = collected_job.workspace_id
    AND c.configured_job_id = collected_job.job_id
),
workflow_runs AS (
  SELECT r.configured_order, t.run_id, MIN(t.period_start_time) AS run_start_time, MAX(t.period_end_time) AS run_end_time, MAX(t.result_state) AS result_state
  FROM monitored_workflows r INNER JOIN workflow_run_api_state t ON r.workspace_id = t.workspace_id AND r.job_id = t.job_id
  WHERE t.period_start_time >= date_sub(current_date(), 100)
  GROUP BY r.configured_order, t.run_id HAVING MAX(t.result_state) IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'SKIPPED')
),
workflow_local_dates AS (
  SELECT r.*, to_date(from_utc_timestamp(current_timestamp(), timezone)) AS local_today,
    ${weekday_number} AS target_day
  FROM monitored_workflows r
),
-- Fortnightly rows keep a pre-anchor boundary for the first period; only the anchor and later dates are eligible.
-- Generate historical deadlines in each workflow's local timezone.
deadline_dates AS (
${deadline_dates}
),
deadline_instants AS (
  SELECT *, to_utc_timestamp(to_timestamp(concat(CAST(deadline_date AS STRING), ' ', completion_time), 'yyyy-MM-dd HH:mm'), timezone) AS deadline_utc
  FROM deadline_dates
),
deadline_periods AS (
  SELECT *,
    LAG(deadline_utc) OVER (PARTITION BY configured_order ORDER BY deadline_utc) AS previous_deadline_utc,
    LEAD(deadline_utc) OVER (PARTITION BY configured_order ORDER BY deadline_utc) AS next_deadline_utc
  FROM deadline_instants
),
-- Keep SLA status separate from a later recovery so missed history never changes.
on_time_deadlines AS (
  SELECT d.configured_order, d.workflow_name, d.deadline_utc, d.next_deadline_utc, d.timezone,
    MIN(CASE WHEN r.result_state = 'SUCCEEDED' THEN r.run_end_time END) AS on_time_completion_utc,
    CASE WHEN COUNT(DISTINCT CASE WHEN r.result_state = 'SUCCEEDED' THEN r.run_id END) > 0 THEN 'Met' WHEN d.deadline_utc <= current_timestamp() THEN 'Missed' ELSE 'Pending' END AS sla_status
  FROM deadline_periods d
  LEFT JOIN workflow_runs r ON d.configured_order = r.configured_order AND r.run_end_time > d.previous_deadline_utc AND r.run_end_time <= d.deadline_utc
  WHERE d.previous_deadline_utc IS NOT NULL AND (d.frequency <> 'fortnightly' OR d.deadline_date >= d.first_deadline_date)
  GROUP BY d.configured_order, d.workflow_name, d.deadline_utc, d.next_deadline_utc, d.timezone
),
late_recoveries AS (
  SELECT d.configured_order, d.deadline_utc, MIN(r.run_end_time) AS late_recovery_utc
  FROM deadline_periods d
  LEFT JOIN workflow_runs r ON d.configured_order = r.configured_order AND r.result_state = 'SUCCEEDED' AND r.run_end_time > d.deadline_utc AND r.run_end_time <= d.next_deadline_utc
  WHERE d.previous_deadline_utc IS NOT NULL AND (d.frequency <> 'fortnightly' OR d.deadline_date >= d.first_deadline_date)
  GROUP BY d.configured_order, d.deadline_utc
),
observed_deadlines AS (
  SELECT d.*, COALESCE(d.on_time_completion_utc, r.late_recovery_utc) AS observed_completion_utc,
    CASE WHEN d.sla_status = 'Missed' AND r.late_recovery_utc IS NOT NULL THEN 'Late recovery' ELSE d.sla_status END AS delivery_state
  FROM on_time_deadlines d
  LEFT JOIN late_recoveries r ON d.configured_order = r.configured_order AND d.deadline_utc = r.deadline_utc
),
deadline_results AS (
  SELECT *,
    ROUND((unix_timestamp(deadline_utc) - unix_timestamp(observed_completion_utc)) / 60.0, 1) AS completion_margin_minutes,
    COALESCE(ROUND((unix_timestamp(deadline_utc) - unix_timestamp(observed_completion_utc)) / 60.0, 1), 0.0) AS display_margin_minutes
  FROM observed_deadlines
),
run_metrics_30d AS (
  SELECT configured_order, COUNT(DISTINCT run_id) AS total_runs_30d,
    COUNT(DISTINCT CASE WHEN result_state = 'SUCCEEDED' THEN run_id END) AS successful_runs_30d,
    COUNT(DISTINCT CASE WHEN result_state IN ('FAILED', 'CANCELLED', 'SKIPPED') THEN run_id END) AS unsuccessful_runs_30d,
    AVG(unix_timestamp(run_end_time) - unix_timestamp(run_start_time)) AS average_duration_seconds_30d
  FROM workflow_runs WHERE run_end_time >= date_sub(current_date(), 30) GROUP BY configured_order
),
sla_metrics_30d AS (
  SELECT configured_order,
    COUNT(CASE WHEN sla_status IN ('Met', 'Missed') THEN 1 END) AS evaluated_deadlines_30d,
    COUNT(CASE WHEN sla_status = 'Met' THEN 1 END) AS met_deadlines_30d,
    COUNT(CASE WHEN sla_status = 'Missed' THEN 1 END) AS missed_deadlines_30d
  FROM deadline_results
  WHERE deadline_utc >= date_sub(current_date(), 30) AND deadline_utc <= current_timestamp()
  GROUP BY configured_order
)
SELECT d.workflow_name, to_date(from_utc_timestamp(d.deadline_utc, d.timezone)) AS deadline_date, d.deadline_utc, d.timezone,
  datediff(to_date(from_utc_timestamp(d.deadline_utc, d.timezone)), to_date(from_utc_timestamp(current_timestamp(), d.timezone))) AS deadline_day_offset,
  d.sla_status, d.delivery_state, d.observed_completion_utc, d.completion_margin_minutes, d.display_margin_minutes,
  CASE WHEN d.deadline_utc <= current_timestamp() THEN ROW_NUMBER() OVER (PARTITION BY d.configured_order, CASE WHEN d.deadline_utc <= current_timestamp() THEN 1 ELSE 0 END ORDER BY d.deadline_utc DESC) END AS due_deadline_recency,
  CAST(0.99 AS DECIMAL(4, 2)) AS sla_target_rate,
  TRY_DIVIDE(r.successful_runs_30d, r.total_runs_30d) AS run_success_rate_30d,
  TRY_DIVIDE(s.met_deadlines_30d, s.evaluated_deadlines_30d) AS sla_compliance_rate_30d,
  r.average_duration_seconds_30d,
  COALESCE(r.total_runs_30d, 0) AS total_runs_30d, COALESCE(r.unsuccessful_runs_30d, 0) AS unsuccessful_runs_30d,
  COALESCE(s.evaluated_deadlines_30d, 0) AS evaluated_deadlines_30d, COALESCE(s.missed_deadlines_30d, 0) AS missed_deadlines_30d,
  CASE WHEN COALESCE(s.missed_deadlines_30d, 0) > 0 AND COALESCE(r.unsuccessful_runs_30d, 0) > 0 THEN 'Critical' WHEN COALESCE(s.missed_deadlines_30d, 0) > 0 OR COALESCE(r.unsuccessful_runs_30d, 0) > 0 THEN 'Needs attention' ELSE 'Healthy' END AS attention_level
FROM deadline_results d
LEFT JOIN run_metrics_30d r ON d.configured_order = r.configured_order
LEFT JOIN sla_metrics_30d s ON d.configured_order = s.configured_order
WHERE d.deadline_utc >= date_sub(current_date(), 60)
ORDER BY d.deadline_utc
