"""Tests for the Jobs API collector storage, config write, and main entry points."""

from __future__ import annotations

import re
import sys
import tomllib
import unittest
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import yaml

import tests._paths  # noqa: F401 -- puts the repository root and src/ on sys.path

# isort: split
import collect_workflow_monitoring_jobs_api as collector
from collect_workflow_monitoring_jobs_api import (
    RUN_STATE_WRITE_BATCH_SIZE,
    _delete_expired_run_state,
    _delete_unretrievable_run_state,
    _prepare_collector_storage,
    _replace_workspace_sla_config,
    _utc_now_naive,
)
from workflow_monitoring_config import (
    ConfiguredWorkflow,
    ServiceLevelAgreement,
    WorkflowMonitoringConfiguration,
    validated_workspace_id,
    workflow_sla_config_rows,
)

REPOSITORY_ROOT = tests._paths.REPOSITORY_ROOT

COLLECTOR_JOB_PATH = REPOSITORY_ROOT / "resources/workflow-monitoring.collector.job.yml"
JOBS_API_COLLECTOR_PATH = REPOSITORY_ROOT / "src/collect_workflow_monitoring_jobs_api.py"


class JobsApiCollectorTests(unittest.TestCase):
    """Protect the collector's durable storage and polling guarantees."""

    def test_collector_is_a_plain_python_file(self) -> None:
        # spark_python_task rejects notebooks, so the collector must not start with a notebook header.
        first_line = JOBS_API_COLLECTOR_PATH.read_text(encoding="utf-8").splitlines()[0]
        self.assertNotEqual(first_line, "# Databricks notebook source")

    def test_source_files_do_not_look_up_file_paths(self) -> None:
        for source_path in (REPOSITORY_ROOT / "src").rglob("*.py"):
            with self.subTest(source_path=source_path):
                self.assertNotIn(
                    "__file__",
                    source_path.read_text(encoding="utf-8"),
                )

    def test_storage_creation_respects_selected_mode(self) -> None:
        class RecordingSparkSession:
            def __init__(self) -> None:
                self.statements: list[str] = []

            def sql(self, statement: str) -> None:
                self.statements.append(statement)

        bring_your_own_spark_session = RecordingSparkSession()
        _prepare_collector_storage(
            bring_your_own_spark_session,
            "existing_catalog",
            "existing_schema",
            False,
        )
        bring_your_own_sql = "\n".join(bring_your_own_spark_session.statements)
        self.assertNotIn("CREATE CATALOG", bring_your_own_sql)
        self.assertNotIn("CREATE SCHEMA", bring_your_own_sql)
        self.assertEqual(bring_your_own_sql.count("CREATE TABLE IF NOT EXISTS"), 3)

        automatic_spark_session = RecordingSparkSession()
        _prepare_collector_storage(
            automatic_spark_session,
            "workflow_monitoring",
            "lakeflow_jobs",
            True,
        )
        automatic_sql = "\n".join(automatic_spark_session.statements)
        self.assertIn("CREATE CATALOG IF NOT EXISTS", automatic_sql)
        self.assertIn("CREATE SCHEMA IF NOT EXISTS", automatic_sql)
        self.assertEqual(automatic_sql.count("CREATE TABLE IF NOT EXISTS"), 3)
        self.assertIn("workflow_sla_config", automatic_sql)

    def test_sla_config_write_replaces_one_workspace_atomically(self) -> None:
        recorded_write: dict[str, Any] = {}

        class RecordingConfigWriter:
            def format(self, format_name: str) -> RecordingConfigWriter:
                recorded_write["format"] = format_name
                return self

            def mode(self, write_mode: str) -> RecordingConfigWriter:
                recorded_write["mode"] = write_mode
                return self

            def option(self, option_name: str, option_value: str) -> RecordingConfigWriter:
                recorded_write[option_name] = option_value
                return self

            def saveAsTable(self, table_name: str) -> None:
                recorded_write["table"] = table_name

        class RecordingConfigDataFrame:
            def __init__(self) -> None:
                self.write = RecordingConfigWriter()

        class StoredConfigTable:
            schema = "stored schema"

            def __init__(self, rows: list[SimpleNamespace]) -> None:
                self.rows = rows

            def where(self, predicate: str) -> StoredConfigTable:
                recorded_write["where"] = predicate
                return self

            def collect(self) -> list[SimpleNamespace]:
                return self.rows

        def create_data_frame(rows: list[dict[str, Any]], schema: Any) -> RecordingConfigDataFrame:
            recorded_write["rows"] = rows
            recorded_write["schema"] = schema
            return RecordingConfigDataFrame()

        changed_at = datetime(2026, 10, 29, 1, 2, 3)
        configuration = WorkflowMonitoringConfiguration(
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
        )
        spark_session = SimpleNamespace(
            createDataFrame=create_data_frame,
            table=lambda _table_name: StoredConfigTable([]),
        )
        _replace_workspace_sla_config(
            spark_session,
            "monitoring",
            "jobs",
            "111",
            configuration,
            changed_at,
        )

        self.assertEqual(recorded_write["where"], "workspace_id = '111'")
        self.assertEqual(recorded_write["format"], "delta")
        self.assertEqual(recorded_write["mode"], "overwrite")
        self.assertEqual(recorded_write["replaceWhere"], "workspace_id = '111'")
        self.assertEqual(recorded_write["table"], "`monitoring`.`jobs`.`workflow_sla_config`")
        self.assertEqual(recorded_write["schema"], "stored schema")
        self.assertEqual(recorded_write["rows"][0]["configured_job_id"], "42")
        self.assertEqual(recorded_write["rows"][0]["config_changed_at"], changed_at)

    def test_sla_config_write_is_skipped_when_rows_do_not_change(self) -> None:
        changed_at = datetime(2026, 10, 29, 1, 2, 3)
        configuration = WorkflowMonitoringConfiguration(
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
        )
        stored_rows = [
            SimpleNamespace(
                asDict=lambda: {
                    **workflow_sla_config_rows(configuration, "111", changed_at)[0],
                    "config_changed_at": datetime(2026, 10, 29, 0, 0, 0),
                }
            )
        ]

        class StoredConfigTable:
            schema = "stored schema"

            def where(self, _predicate: str) -> StoredConfigTable:
                return self

            def collect(self) -> list[SimpleNamespace]:
                return stored_rows

        create_data_frame = Mock()
        spark_session = SimpleNamespace(
            createDataFrame=create_data_frame,
            table=lambda _table_name: StoredConfigTable(),
        )

        _replace_workspace_sla_config(
            spark_session,
            "monitoring",
            "jobs",
            "111",
            configuration,
            changed_at,
        )

        create_data_frame.assert_not_called()

    def test_sla_config_row_keys_match_the_ddl_columns(self) -> None:
        class RecordingSparkSession:
            def __init__(self) -> None:
                self.statements: list[str] = []

            def sql(self, statement: str) -> None:
                self.statements.append(statement)

        spark_session = RecordingSparkSession()
        _prepare_collector_storage(
            spark_session,
            "workflow_monitoring",
            "lakeflow_jobs",
            True,
        )
        sla_ddl = next(
            statement
            for statement in spark_session.statements
            if "workflow_sla_config" in statement
        )
        ddl_columns = re.findall(
            r"^\s+([a-z_]+) (?:STRING|INT|DATE|TIMESTAMP)", sla_ddl, re.MULTILINE
        )
        configuration = WorkflowMonitoringConfiguration(
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
        )
        row = workflow_sla_config_rows(
            configuration,
            "111",
            datetime(2026, 10, 29, 1, 2, 3),
        )[0]

        self.assertEqual(list(row), ddl_columns)

    def test_collector_environment_pins_match_uv_lock(self) -> None:
        collector_job = yaml.safe_load(COLLECTOR_JOB_PATH.read_text(encoding="utf-8"))["resources"][
            "jobs"
        ]["workflow_monitoring_jobs_api_collector"]

        dependencies = collector_job["environments"][0]["spec"]["dependencies"]
        with (REPOSITORY_ROOT / "uv.lock").open("rb") as lock_file:
            lock_document = tomllib.load(lock_file)
        locked_versions = {
            package["name"]: package["version"] for package in lock_document["package"]
        }
        for dependency in dependencies:
            dependency_name, dependency_version = dependency.split("==")
            self.assertEqual(dependency_version, locked_versions[dependency_name])

    def test_invalid_configuration_writes_nothing(self) -> None:
        with (
            patch.object(
                collector,
                "load_workflow_monitoring_configuration",
                side_effect=ValueError("configuration is invalid"),
            ),
            patch.object(collector, "_spark_session") as spark_session,
            patch.object(collector, "_prepare_collector_storage") as prepare_storage,
            patch.object(collector, "_replace_workspace_sla_config") as replace_config,
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
            self.assertRaisesRegex(ValueError, "configuration is invalid"),
        ):
            collector.main()

        spark_session.assert_not_called()
        prepare_storage.assert_not_called()
        replace_config.assert_not_called()

    def test_empty_configuration_keeps_retention_and_skips_jobs_api(self) -> None:
        sdk_module = ModuleType("databricks.sdk")
        sdk_module.WorkspaceClient = lambda: SimpleNamespace(get_workspace_id=lambda: 111)
        databricks_module = ModuleType("databricks")
        databricks_module.__path__ = []
        empty_configuration = WorkflowMonitoringConfiguration(active_workflows=())

        with (
            patch.dict(
                sys.modules,
                {
                    "databricks": databricks_module,
                    "databricks.sdk": sdk_module,
                },
            ),
            patch.object(
                collector,
                "load_workflow_monitoring_configuration",
                return_value=empty_configuration,
            ),
            patch.object(collector, "_spark_session"),
            patch.object(collector, "_prepare_collector_storage"),
            patch.object(collector, "_replace_workspace_sla_config") as replace_config,
            patch.object(collector, "_delete_expired_run_state") as delete_expired_state,
            patch.object(
                collector,
                "_last_successful_collection_by_workspace_and_job",
            ) as last_successful_collection,
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
        ):
            collector.main()

        replace_config.assert_called_once()
        delete_expired_state.assert_called_once()
        last_successful_collection.assert_not_called()

    def test_collection_clock_is_utc_without_a_zone(self) -> None:
        collected_at = _utc_now_naive()

        self.assertIsNone(collected_at.tzinfo)
        self.assertLess(
            abs((datetime.now(UTC).replace(tzinfo=None) - collected_at).total_seconds()),
            5,
        )

    def test_runtime_workspace_and_retention_are_scoped(self) -> None:
        self.assertEqual(validated_workspace_id("111"), "111")
        with self.assertRaisesRegex(ValueError, "must contain only digits"):
            validated_workspace_id("workspace-name")

        recorded_statements: list[str] = []
        spark_session = SimpleNamespace(sql=recorded_statements.append)
        _delete_expired_run_state(
            spark_session,
            "monitoring",
            "jobs",
            "111",
            datetime(2026, 6, 26),
        )
        retention_statement = recorded_statements[0]
        self.assertIn(
            "DELETE FROM `monitoring`.`jobs`.`workflow_run_api_state`", retention_statement
        )
        self.assertIn("workspace_id = '111'", retention_statement)
        self.assertIn("period_start_time < TIMESTAMP '2026-06-26 00:00:00'", retention_statement)

    def test_unretrievable_run_deletion_is_scoped_and_batched(self) -> None:
        recorded_statements: list[str] = []
        spark_session = SimpleNamespace(sql=recorded_statements.append)
        _delete_unretrievable_run_state(
            spark_session,
            "`monitoring`.`jobs`.`workflow_run_api_state`",
            "111",
            42,
            [],
        )
        self.assertEqual(recorded_statements, [])

        _delete_unretrievable_run_state(
            spark_session,
            "`monitoring`.`jobs`.`workflow_run_api_state`",
            "111",
            42,
            list(range(1, RUN_STATE_WRITE_BATCH_SIZE + 2)),
        )
        missing_run_statement = recorded_statements[0]
        self.assertIn("workspace_id = '111'", missing_run_statement)
        self.assertIn("job_id = '42'", missing_run_statement)
        self.assertIn("run_id IN (1, 2, 3", missing_run_statement)
        self.assertTrue(missing_run_statement.endswith("499, 500)"))
        self.assertTrue(recorded_statements[1].endswith("run_id IN (501)"))
