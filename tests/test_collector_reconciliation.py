"""Tests for collector checkpoints, source contracts, and run reconciliation."""

from __future__ import annotations

import re
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import tests._paths  # noqa: F401 -- puts the repository root and src/ on sys.path

# isort: split
from collect_workflow_monitoring_jobs_api import (
    RUN_STATE_WRITE_BATCH_SIZE,
    _job_name_from_job_details,
    _last_successful_collection_by_workspace_and_job,
    _merge_job_runs_in_batches,
    _normalized_run_state,
    _stored_nonterminal_run_ids_for_reconciliation,
)
from tests.fakes import StoredRunStateDataFrame

REPOSITORY_ROOT = tests._paths.REPOSITORY_ROOT

JOBS_API_COLLECTOR_PATH = REPOSITORY_ROOT / "src/collect_workflow_monitoring_jobs_api.py"


class CollectorReconciliationTests(unittest.TestCase):
    """Protect collector source contracts, batched merges, checkpoints, and run recovery."""

    def test_collector_source_is_valid_and_idempotent(self) -> None:
        collector_source = JOBS_API_COLLECTOR_PATH.read_text(encoding="utf-8")
        compile(collector_source, str(JOBS_API_COLLECTOR_PATH), "exec")

        required_collector_fragments = (
            'RUN_STATE_TABLE_NAME = "workflow_run_api_state"',
            'JOB_STATE_TABLE_NAME = "workflow_job_api_state"',
            "RUN_HISTORY_RETENTION_DAYS = 60",
            "active_only=True",
            "completed_only=True",
            "jobs.get(job_id=job_id)",
            "jobs.get_run(run_id=stored_run_id)",
            "INCREMENTAL_COLLECTION_OVERLAP_MINUTES = 15",
            "RUN_STATE_WRITE_BATCH_SIZE = 500",
            "CREATE CATALOG IF NOT EXISTS",
            "CREATE SCHEMA IF NOT EXISTS",
            'SLA_CONFIG_TABLE_NAME = "workflow_sla_config"',
            "get_workspace_id()",
            "load_workflow_monitoring_configuration(",
            "SparkSession.builder.getOrCreate()",
            '.option("replaceWhere"',
            ".where(f\"workspace_id = '{workspace_id}'\")",
            "(workspace_id, str(job_id))",
            ".whenMatchedUpdateAll()",
            ".whenNotMatchedInsertAll()",
        )
        for required_collector_fragment in required_collector_fragments:
            self.assertIn(required_collector_fragment, collector_source)

        excluded_sensitive_fields = ("creator_user_name", "notebook_output", "job_parameters")
        for excluded_sensitive_field in excluded_sensitive_fields:
            self.assertNotIn(excluded_sensitive_field, collector_source)

        removed_complexity = (
            "SHOW CATALOGS",
            "SHOW SCHEMAS",
            "refresh_interval_minutes",
            "trigger_type",
            "run_page_url",
            "collected_at",
            'getattr(job_run, "state"',
            "RUN_STATE_RETENTION_DAYS",
            "JOBS_API_HISTORY_DAYS",
            '"STALE"',
        )
        for removed_fragment in removed_complexity:
            self.assertNotIn(removed_fragment, collector_source)
        for removed_run_time_access in (
            "globals(",
            "dbutils",
            "monitored_job_ids" + "_json",
            "notebook_task",
        ):
            self.assertNotIn(removed_run_time_access, collector_source)

    def test_run_state_merges_are_memory_bounded(self) -> None:
        job_runs = (
            SimpleNamespace(
                run_id=run_id,
                start_time=1_700_000_000_000,
                end_time=1_700_000_060_000,
                status=SimpleNamespace(
                    state="TERMINATED",
                    termination_details=SimpleNamespace(code="SUCCESS"),
                ),
            )
            for run_id in range(1, RUN_STATE_WRITE_BATCH_SIZE + 2)
        )
        with patch(
            "collect_workflow_monitoring_jobs_api._merge_run_state_rows"
        ) as merge_run_state_rows:
            _merge_job_runs_in_batches(
                SimpleNamespace(),
                "`monitoring`.`jobs`.`workflow_run_api_state`",
                "111",
                42,
                job_runs,
                datetime(2026, 8, 30),
            )

        merged_batch_sizes = [
            len(merge_call.args[2]) for merge_call in merge_run_state_rows.call_args_list
        ]
        self.assertEqual(merged_batch_sizes, [RUN_STATE_WRITE_BATCH_SIZE, 1])

    def test_current_jobs_api_status_maps_terminal_outcomes(self) -> None:
        mapping_cases = (
            ("RUNNING", None, "RUNNING"),
            ("TERMINATING", "USER_CANCELED", "TERMINATING"),
            ("INTERNAL_ERROR", None, "FAILED"),
            ("TERMINATED", "SUCCESS", "SUCCEEDED"),
            ("TERMINATED", "CANCELED", "CANCELLED"),
            ("TERMINATED", "USER_CANCELED", "CANCELLED"),
            ("TERMINATED", "DISABLED", "SKIPPED"),
            ("TERMINATED", "SKIPPED", "SKIPPED"),
            ("TERMINATED", "SUCCESS_WITH_FAILURES", "FAILED"),
            ("TERMINATED", "RUN_EXECUTION_ERROR", "FAILED"),
        )
        for lifecycle_state, termination_code, expected_state in mapping_cases:
            with self.subTest(lifecycle_state=lifecycle_state, code=termination_code):
                termination_details = SimpleNamespace(code=termination_code)
                job_run = SimpleNamespace(
                    status=SimpleNamespace(
                        state=lifecycle_state,
                        termination_details=termination_details,
                    )
                )
                self.assertEqual(_normalized_run_state(job_run), expected_state)

        self.assertEqual(_normalized_run_state(SimpleNamespace(status=None)), "UNKNOWN")

    def test_job_name_comes_from_jobs_api_metadata(self) -> None:
        job_details = SimpleNamespace(settings=SimpleNamespace(name=" Daily Orders "))
        self.assertEqual(_job_name_from_job_details(42, job_details), "Daily Orders")

        with self.assertRaisesRegex(ValueError, "no name for job ID 42"):
            _job_name_from_job_details(42, SimpleNamespace(settings=None))

    def test_checkpoint_lookup_is_scoped_to_workspace_and_job(self) -> None:
        checkpoint_time = datetime(2026, 8, 24, 1, 2, 3)
        checkpoint_rows = [
            SimpleNamespace(
                workspace_id="111",
                job_id="42",
                last_successful_collection_at=checkpoint_time,
            ),
            SimpleNamespace(
                workspace_id="222",
                job_id="42",
                last_successful_collection_at=checkpoint_time,
            ),
            SimpleNamespace(
                workspace_id="111",
                job_id="99",
                last_successful_collection_at=checkpoint_time,
            ),
        ]

        class CheckpointDataFrame:
            def __init__(self, rows: list[SimpleNamespace]) -> None:
                self.rows = rows

            def select(self, *_column_names: str) -> CheckpointDataFrame:
                return self

            def where(self, predicate: str) -> CheckpointDataFrame:
                if predicate.startswith("workspace_id ="):
                    selected_workspace_id = predicate.split("'")[1]
                    self.rows = [
                        row for row in self.rows if row.workspace_id == selected_workspace_id
                    ]
                elif predicate.startswith("job_id IN"):
                    selected_job_ids = set(re.findall(r"'([0-9]+)'", predicate))
                    self.rows = [row for row in self.rows if row.job_id in selected_job_ids]
                elif predicate == "last_successful_collection_at IS NOT NULL":
                    self.rows = [
                        row for row in self.rows if row.last_successful_collection_at is not None
                    ]
                return self

            def collect(self) -> list[SimpleNamespace]:
                return self.rows

        spark_session = SimpleNamespace(
            table=lambda _table_name: CheckpointDataFrame(checkpoint_rows.copy())
        )
        checkpoints = _last_successful_collection_by_workspace_and_job(
            spark_session,
            "monitoring",
            "jobs",
            "111",
            [42],
        )
        self.assertEqual(checkpoints, {("111", "42"): checkpoint_time})

    def test_nonterminal_reconciliation_uses_only_unlisted_history_gap(self) -> None:
        run_history_start_at = datetime(2026, 6, 26)
        incremental_start_at = datetime(2026, 8, 24)

        def run_state_row(
            workspace_id: str,
            job_id: str,
            run_id: int,
            result_state: str,
            period_start_time: datetime = datetime(2026, 8, 23),
        ) -> SimpleNamespace:
            return SimpleNamespace(
                workspace_id=workspace_id,
                job_id=job_id,
                run_id=run_id,
                period_start_time=period_start_time,
                result_state=result_state,
            )

        run_state_rows = [
            run_state_row("111", "42", 900, "RUNNING"),
            run_state_row("111", "43", 901, "SUCCEEDED"),
            run_state_row("222", "42", 902, "RUNNING"),
            run_state_row("111", "99", 903, "RUNNING"),
            run_state_row("111", "42", 904, "RUNNING", datetime(2026, 6, 25)),
            run_state_row("111", "42", 905, "RUNNING", run_history_start_at),
            run_state_row("111", "42", 906, "RUNNING", incremental_start_at),
        ]

        spark_session = SimpleNamespace(
            table=lambda _table_name: StoredRunStateDataFrame(run_state_rows.copy())
        )
        nonterminal_run_ids = list(
            _stored_nonterminal_run_ids_for_reconciliation(
                spark_session,
                "monitoring",
                "jobs",
                "111",
                42,
                run_history_start_at,
                incremental_start_at,
            )
        )
        self.assertEqual(nonterminal_run_ids, [900, 905])


if __name__ == "__main__":
    unittest.main()
