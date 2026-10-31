"""Tests for the Jobs API collector run collection and recovery."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import tests._paths  # noqa: F401 -- puts the repository root and src/ on sys.path

# isort: split
import collect_workflow_monitoring_jobs_api as collector
from tests.fakes import ExpectedMissingRun, StoredRunStateDataFrame
from workflow_monitoring_config import (
    ConfiguredWorkflow,
    ServiceLevelAgreement,
    WorkflowMonitoringConfiguration,
)


class CollectorCollectionTests(unittest.TestCase):
    """Drive collector.main end to end over fake Jobs API and stored run rows."""

    def _collect_with_stored_run_scenario(
        self,
        first_stored_run_error: Exception,
        run_merge_error: Exception | None = None,
        run_delete_error: Exception | None = None,
        later_run_error: Exception | None = None,
    ) -> SimpleNamespace:
        collection_time = datetime.now(UTC).replace(tzinfo=None)
        stored_run_rows = {
            run_id: SimpleNamespace(
                workspace_id="111",
                job_id="42",
                run_id=run_id,
                period_start_time=collection_time - timedelta(days=age_days),
                result_state="RUNNING",
            )
            for run_id, age_days in ((700, 75), (800, 10), (900, 2))
        }
        stored_run_rows[950] = SimpleNamespace(
            workspace_id="111",
            job_id="42",
            run_id=950,
            period_start_time=collection_time - timedelta(minutes=10),
            result_state="RUNNING",
        )

        def job_run(
            run_id: int,
            age_days: int,
            lifecycle_state: str = "TERMINATED",
        ) -> SimpleNamespace:
            run_start_at = (collection_time - timedelta(days=age_days)).replace(tzinfo=UTC)
            return SimpleNamespace(
                run_id=run_id,
                start_time=int(run_start_at.timestamp() * 1000),
                end_time=(
                    int((run_start_at + timedelta(minutes=1)).timestamp() * 1000)
                    if lifecycle_state == "TERMINATED"
                    else None
                ),
                status=SimpleNamespace(
                    state=lifecycle_state,
                    termination_details=(
                        SimpleNamespace(code="SUCCESS") if lifecycle_state == "TERMINATED" else None
                    ),
                ),
            )

        retrieved_run_ids: list[int] = []

        def retrieve_stored_run(*, run_id: int) -> SimpleNamespace:
            retrieved_run_ids.append(run_id)
            if run_id == 800:
                raise first_stored_run_error
            if run_id == 900 and later_run_error is not None:
                raise later_run_error
            return job_run(run_id, 2)

        jobs_api = SimpleNamespace(
            get=Mock(return_value=SimpleNamespace(settings=SimpleNamespace(name="Daily Orders"))),
            list_runs=Mock(
                side_effect=lambda **options: (
                    [job_run(950, 0, "RUNNING")]
                    if options.get("active_only")
                    else [job_run(1000, 0)]
                )
            ),
            get_run=Mock(side_effect=retrieve_stored_run),
        )
        workspace_client = SimpleNamespace(jobs=jobs_api, get_workspace_id=lambda: 111)
        sdk_module = ModuleType("databricks.sdk")
        sdk_module.__path__ = []
        sdk_module.WorkspaceClient = lambda: workspace_client
        sdk_errors_module = ModuleType("databricks.sdk.errors")
        sdk_errors_module.ResourceDoesNotExist = ExpectedMissingRun
        databricks_module = ModuleType("databricks")
        databricks_module.__path__ = []

        spark_session = SimpleNamespace(
            table=lambda _table_name: StoredRunStateDataFrame(list(stored_run_rows.values()))
        )
        deleted_run_state_cutoffs: list[datetime] = []
        deleted_unretrievable_run_ids: list[int] = []
        job_state_updates: list[dict[str, Any]] = []

        def delete_expired_run_state(*args: Any) -> None:
            retention_start_at = args[-1]
            deleted_run_state_cutoffs.append(retention_start_at)
            for stored_run_id, stored_run_row in list(stored_run_rows.items()):
                if stored_run_row.period_start_time < retention_start_at:
                    stored_run_rows.pop(stored_run_id)

        def delete_unretrievable_run_state(
            _spark_session: Any,
            _qualified_table_name: str,
            _workspace_id: str,
            _job_id: int,
            unretrievable_run_ids: list[int],
        ) -> None:
            if run_delete_error is not None:
                raise run_delete_error
            deleted_unretrievable_run_ids.extend(unretrievable_run_ids)
            for unretrievable_run_id in unretrievable_run_ids:
                stored_run_rows.pop(unretrievable_run_id)

        def merge_run_rows(
            _spark_session: Any, _qualified_table_name: str, incoming_rows: list[dict[str, Any]]
        ) -> None:
            if run_merge_error is not None:
                raise run_merge_error
            for incoming_row in incoming_rows:
                stored_run_rows[incoming_row["run_id"]] = SimpleNamespace(**incoming_row)

        def merge_job_rows(
            _spark_session: Any, _qualified_table_name: str, incoming_rows: list[dict[str, Any]]
        ) -> None:
            job_state_updates.extend(incoming_rows)

        collector_output = io.StringIO()
        collection_error: RuntimeError | None = None
        with (
            patch.dict(
                sys.modules,
                {
                    "databricks": databricks_module,
                    "databricks.sdk": sdk_module,
                    "databricks.sdk.errors": sdk_errors_module,
                },
            ),
            patch.object(collector, "_spark_session", return_value=spark_session),
            patch.object(
                collector,
                "load_workflow_monitoring_configuration",
                return_value=WorkflowMonitoringConfiguration(
                    active_workflows=(
                        ConfiguredWorkflow(
                            job_id=42,
                            sla=ServiceLevelAgreement(
                                frequency="daily",
                                completion_time="06:00",
                                timezone="UTC",
                            ),
                        ),
                    )
                ),
            ),
            patch.object(
                sys,
                "argv",
                [
                    "collector",
                    "--config-path",
                    "workflow-monitoring.yml",
                    "--schema-path",
                    "src/workflow-monitoring.schema.json",
                    "--catalog",
                    "monitoring",
                    "--schema",
                    "jobs",
                    "--create-catalog-and-schema-if-missing",
                    "false",
                ],
            ),
            patch.object(collector, "_prepare_collector_storage"),
            patch.object(collector, "_replace_workspace_sla_config"),
            patch.object(
                collector,
                "_delete_expired_run_state",
                side_effect=delete_expired_run_state,
            ),
            patch.object(
                collector,
                "_delete_unretrievable_run_state",
                side_effect=delete_unretrievable_run_state,
            ),
            patch.object(
                collector,
                "_last_successful_collection_by_workspace_and_job",
                return_value={("111", "42"): collection_time - timedelta(minutes=5)},
            ),
            patch.object(collector, "_merge_run_state_rows", side_effect=merge_run_rows),
            patch.object(collector, "_merge_job_state_rows", side_effect=merge_job_rows),
            redirect_stdout(collector_output),
        ):
            try:
                collector.main()
            except RuntimeError as error:
                collection_error = error

        return SimpleNamespace(
            collection_time=collection_time,
            collection_error=collection_error,
            stored_run_rows=stored_run_rows,
            retrieved_run_ids=retrieved_run_ids,
            list_runs_requests=jobs_api.list_runs.call_args_list,
            deleted_run_state_cutoffs=deleted_run_state_cutoffs,
            deleted_unretrievable_run_ids=deleted_unretrievable_run_ids,
            job_state_updates=job_state_updates,
            collector_output=collector_output.getvalue(),
        )

    def test_expired_and_missing_runs_are_removed_without_blocking_collection(self) -> None:
        collection = self._collect_with_stored_run_scenario(
            ExpectedMissingRun("run expired or deleted")
        )
        self.assertIsNone(collection.collection_error)
        self.assertEqual(collection.retrieved_run_ids, [800, 900])
        self.assertNotIn(950, collection.retrieved_run_ids)
        self.assertNotIn(700, collection.stored_run_rows)
        self.assertNotIn(800, collection.stored_run_rows)
        self.assertEqual(collection.deleted_unretrievable_run_ids, [800])
        self.assertEqual(collection.stored_run_rows[900].result_state, "SUCCEEDED")
        self.assertEqual(collection.stored_run_rows[950].result_state, "RUNNING")
        self.assertEqual(collection.stored_run_rows[1000].result_state, "SUCCEEDED")
        self.assertIn("Removing unavailable stored run ID 800", collection.collector_output)
        self.assertIn("RESOURCE_DOES_NOT_EXIST", collection.collector_output)
        self.assertIn("run expired or deleted", collection.collector_output)
        self.assertGreater(
            collection.job_state_updates[-1]["last_successful_collection_at"],
            collection.collection_time - timedelta(minutes=1),
        )
        self.assertIsNone(collection.job_state_updates[-1]["last_error_message"])
        self.assertEqual(len(collection.deleted_run_state_cutoffs), 1)
        self.assertLess(
            abs(
                collection.collection_time
                - collection.deleted_run_state_cutoffs[0]
                - timedelta(days=60)
            ),
            timedelta(minutes=1),
        )
        requested_start_at = datetime.fromtimestamp(
            collection.list_runs_requests[0].kwargs["start_time_from"] / 1000, tz=UTC
        ).replace(tzinfo=None)
        self.assertEqual(len(collection.list_runs_requests), 2)
        self.assertTrue(collection.list_runs_requests[0].kwargs["completed_only"])
        self.assertNotIn("active_only", collection.list_runs_requests[0].kwargs)
        self.assertTrue(collection.list_runs_requests[1].kwargs["active_only"])
        self.assertNotIn("completed_only", collection.list_runs_requests[1].kwargs)
        self.assertLess(
            abs(collection.collection_time - requested_start_at - timedelta(minutes=20)),
            timedelta(minutes=1),
        )

    def test_unexpected_api_and_storage_errors_fail_collection(self) -> None:
        for unexpected_error, run_merge_error, run_delete_error in (
            (RuntimeError("Jobs API unavailable"), None, None),
            (ExpectedMissingRun("run expired or deleted"), OSError("Delta merge failed"), None),
            (ExpectedMissingRun("run expired or deleted"), None, OSError("Delta delete failed")),
        ):
            with self.subTest(
                error=str(unexpected_error),
                merge_error=str(run_merge_error),
                delete_error=str(run_delete_error),
            ):
                collection = self._collect_with_stored_run_scenario(
                    unexpected_error,
                    run_merge_error,
                    run_delete_error,
                )
                self.assertRegex(str(collection.collection_error), "failed for job IDs: 42")
                self.assertIsNone(collection.job_state_updates[-1]["last_successful_collection_at"])
                expected_error = run_merge_error or run_delete_error or unexpected_error
                self.assertIn(
                    str(expected_error), collection.job_state_updates[-1]["last_error_message"]
                )

    def test_confirmed_missing_run_is_removed_before_later_api_failure(self) -> None:
        collection = self._collect_with_stored_run_scenario(
            ExpectedMissingRun("run expired or deleted"),
            later_run_error=RuntimeError("later Jobs API failure"),
        )

        self.assertRegex(str(collection.collection_error), "failed for job IDs: 42")
        self.assertEqual(collection.retrieved_run_ids, [800, 900])
        self.assertNotIn(800, collection.stored_run_rows)
        self.assertEqual(collection.deleted_unretrievable_run_ids, [800])
        self.assertIn(
            "later Jobs API failure",
            collection.job_state_updates[-1]["last_error_message"],
        )


if __name__ == "__main__":
    unittest.main()
