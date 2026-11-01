"""Named windows and thresholds used by the dashboard SQL."""

# Must stay equal to RUN_HISTORY_RETENTION_DAYS in src/collect_workflow_monitoring_jobs_api.py.
# The collector deletes older runs, so the dashboard never reads beyond this window.
RUN_WINDOW_DAYS = 60
SLA_RUN_WINDOW_DAYS = 100
METRIC_WINDOW_DAYS = 30
STALE_THRESHOLD_MINUTES = 20
SLA_TARGET_RATE = 0.99
