"""Tests for configuration, generation, and dashboard scaffold bindings."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import yaml

from src import collect_workflow_monitoring_jobs_api as collector
from src.collect_workflow_monitoring_jobs_api import (
    RUN_STATE_MERGE_BATCH_SIZE,
    _delete_expired_run_state,
    _job_name_from_job_details,
    _last_successful_collection_by_workspace_and_job,
    _merge_job_runs_in_batches,
    _normalized_run_state,
    _prepare_collector_storage,
    _runtime_workspace_id,
    _stored_nonterminal_run_ids,
    _validated_runtime_workspace_id,
)
from workflow_monitoring_dashboard import (
    CUSTOM_VISUALIZATION_FILES,
    ActiveWorkflow,
    CollectorStorageConfiguration,
    ServiceLevelAgreement,
    WorkflowMonitoringConfiguration,
    _load_custom_visualization,
    _validate_schedule_fields,
    generate_collector_job_resource,
    generate_dashboard,
    load_workflow_monitoring_configuration,
)
from workflow_monitoring_dashboard import (
    main as dashboard_main,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY_ROOT / "schema/workflow-monitoring.schema.json"
SCAFFOLD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.scaffold.lvdash.json"
GENERATED_DASHBOARD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.lvdash.json"
CUSTOM_VISUALIZATION_DIRECTORY = REPOSITORY_ROOT / "src/visualizations"
JOBS_API_COLLECTOR_PATH = REPOSITORY_ROOT / "src/collect_workflow_monitoring_jobs_api.py"
RELEASE_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/release.sh"
LOCAL_VALIDATION_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/run-local-validation.sh"
SQL_VALIDATION_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/validate-sql-datasets.sh"
AUTOMATIC_COLLECTOR_STORAGE = CollectorStorageConfiguration(
    catalog="workflow_monitoring",
    schema="lakeflow_jobs",
    create_catalog_and_schema_if_missing=True,
)


class ExpectedMissingRun(Exception):
    """Stand in for the Jobs SDK's ResourceDoesNotExist error."""


class ConfigurationTests(unittest.TestCase):
    """Check every public schedule and important invalid input."""

    def test_supported_schedules_and_invalid_inputs(self) -> None:
        test_cases = (
            (
                "malformed YAML",
                "version: [",
                None,
                "parse workflow monitoring configuration",
            ),
            (
                "all supported schedules",
                """
                version: 2
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: daily
                      completion_time: "06:00"
                  - job_id: 2
                    monitoring_status: active
                    sla:
                      frequency: weekly
                      completion_time: "07:00"
                      day_of_week: monday
                  - job_id: 3
                    monitoring_status: active
                    sla:
                      frequency: fortnightly
                      completion_time: "08:00"
                      first_deadline_date: "2026-01-12"
                  - job_id: 4
                    monitoring_status: active
                    sla:
                      frequency: monthly
                      completion_time: "09:00"
                      timezone: Australia/Melbourne
                      day_of_month: 31
                """,
                4,
                None,
            ),
            (
                "inactive workflow remains fully valid",
                """
                version: 2
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - job_id: 8
                    monitoring_status: inactive
                    sla: {frequency: daily, completion_time: "06:00"}
                """,
                0,
                None,
            ),
            (
                "duplicate job ID",
                """
                version: 2
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - job_id: 9
                    monitoring_status: active
                    sla: {frequency: daily, completion_time: "06:00"}
                  - job_id: 9
                    monitoring_status: active
                    sla: {frequency: daily, completion_time: "07:00"}
                """,
                None,
                "job_id duplicates workflow",
            ),
            (
                "daily rejects weekly field",
                """
                version: 2
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: daily
                      completion_time: "06:00"
                      day_of_week: monday
                """,
                None,
                "daily SLA only accepts",
            ),
            (
                "invalid timezone",
                """
                version: 2
                workspace_id: "123456789"
                default_timezone: Nowhere/Invalid
                workflows: []
                """,
                None,
                "is not an IANA timezone",
            ),
        )

        for case_name, configuration_yaml, expected_workflow_count, expected_error in test_cases:
            with self.subTest(case_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    configuration_path = Path(temporary_directory) / "workflow-monitoring.yml"
                    configuration_path.write_text(
                        textwrap.dedent(configuration_yaml),
                        encoding="utf-8",
                    )
                    if expected_error:
                        with self.assertRaisesRegex(ValueError, expected_error):
                            load_workflow_monitoring_configuration(configuration_path, SCHEMA_PATH)
                        continue

                    configuration = load_workflow_monitoring_configuration(
                        configuration_path,
                        SCHEMA_PATH,
                    )
                    self.assertEqual(
                        len(configuration.active_workflows),
                        expected_workflow_count,
                    )

    def test_schedule_specific_fields_are_required_and_exclusive(self) -> None:
        invalid_schedule_cases = (
            (
                {"frequency": "weekly", "completion_time": "06:00"},
                "weekly SLA requires a lowercase day_of_week",
            ),
            (
                {
                    "frequency": "weekly",
                    "completion_time": "06:00",
                    "day_of_week": "monday",
                    "day_of_month": 1,
                },
                "weekly SLA does not accept first_deadline_date or day_of_month",
            ),
            (
                {"frequency": "fortnightly", "completion_time": "06:00"},
                "fortnightly SLA requires first_deadline_date",
            ),
            (
                {
                    "frequency": "fortnightly",
                    "completion_time": "06:00",
                    "first_deadline_date": "2026-01-12",
                    "day_of_week": "monday",
                },
                "fortnightly SLA does not accept day_of_week or day_of_month",
            ),
            (
                {"frequency": "monthly", "completion_time": "06:00"},
                "monthly SLA requires day_of_month from 1 to 31",
            ),
            (
                {
                    "frequency": "monthly",
                    "completion_time": "06:00",
                    "day_of_month": 1,
                    "first_deadline_date": "2026-01-12",
                },
                "monthly SLA does not accept day_of_week or first_deadline_date",
            ),
            (
                {"frequency": "hourly", "completion_time": "06:00"},
                "sla.frequency must be daily, weekly, fortnightly, or monthly",
            ),
        )

        for schedule_document, expected_error in invalid_schedule_cases:
            with self.subTest(schedule_document=schedule_document):
                with self.assertRaisesRegex(ValueError, expected_error):
                    _validate_schedule_fields(schedule_document)

    def test_invalid_schema_document_reports_the_schema_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            configuration_path = temporary_root / "workflow-monitoring.yml"
            configuration_path.write_text("version: 2\n", encoding="utf-8")
            invalid_schema_path = temporary_root / "workflow-monitoring.schema.json"
            invalid_schema_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "load workflow monitoring schema"):
                load_workflow_monitoring_configuration(configuration_path, invalid_schema_path)

    def test_jobs_api_storage_modes_and_job_id_are_explicit(self) -> None:
        configuration_cases = (
            (
                "every workflow requires job ID",
                """
                version: 2
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - monitoring_status: inactive
                    sla: {frequency: daily, completion_time: "06:00"}
                """,
                None,
                "'job_id' is a required property",
            ),
            (
                "default collector storage",
                """
                version: 2
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - job_id: 42
                    monitoring_status: active
                    sla: {frequency: daily, completion_time: "06:00"}
                """,
                ("workflow_monitoring", "lakeflow_jobs", True),
                None,
            ),
            (
                "job name is Jobs API metadata",
                """
                version: 2
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - job_id: 42
                    job_name: orders
                    monitoring_status: active
                    sla: {frequency: daily, completion_time: "06:00"}
                """,
                None,
                "Additional properties are not allowed.*job_name",
            ),
            (
                "bring your own collector storage",
                """
                version: 2
                jobs_api_config:
                  catalog: monitoring_catalog
                  schema: monitoring_schema
                workspace_id: "123456789"
                default_timezone: UTC
                workflows: []
                """,
                ("monitoring_catalog", "monitoring_schema", False),
                None,
            ),
            (
                "bring your own storage requires both names",
                """
                version: 2
                jobs_api_config:
                  catalog: monitoring_catalog
                workspace_id: "123456789"
                default_timezone: UTC
                workflows: []
                """,
                None,
                "'schema' is a required property",
            ),
        )

        for case_name, configuration_yaml, expected_storage, expected_error in configuration_cases:
            with self.subTest(case_name), tempfile.TemporaryDirectory() as temporary_directory:
                configuration_path = Path(temporary_directory) / "workflow-monitoring.yml"
                configuration_path.write_text(
                    textwrap.dedent(configuration_yaml),
                    encoding="utf-8",
                )
                if expected_error:
                    with self.assertRaisesRegex(ValueError, expected_error):
                        load_workflow_monitoring_configuration(configuration_path, SCHEMA_PATH)
                    continue

                configuration = load_workflow_monitoring_configuration(
                    configuration_path,
                    SCHEMA_PATH,
                )
                self.assertEqual(
                    (
                        configuration.collector_storage.catalog,
                        configuration.collector_storage.schema,
                        configuration.collector_storage.create_catalog_and_schema_if_missing,
                    ),
                    expected_storage,
                )


class DashboardGenerationTests(unittest.TestCase):
    """Check SQL escaping, paused monitoring, markers, and stable output."""

    def test_generation_is_safe_and_deterministic(self) -> None:
        monthly_day = 31
        configurations = (
            (
                "active workflow",
                WorkflowMonitoringConfiguration(
                    workspace_id="123456789",
                    collector_storage=AUTOMATIC_COLLECTOR_STORAGE,
                    active_workflows=(
                        ActiveWorkflow(
                            job_id=42,
                            sla=ServiceLevelAgreement(
                                frequency="monthly",
                                completion_time="06:00",
                                timezone="UTC",
                                day_of_month=monthly_day,
                            ),
                        ),
                    ),
                ),
                ("CAST('42' AS STRING)", "CAST(31 AS INT)"),
            ),
            (
                "fortnightly workflow",
                WorkflowMonitoringConfiguration(
                    workspace_id="123456789",
                    collector_storage=AUTOMATIC_COLLECTOR_STORAGE,
                    active_workflows=(
                        ActiveWorkflow(
                            job_id=43,
                            sla=ServiceLevelAgreement(
                                frequency="fortnightly",
                                completion_time="06:00",
                                timezone="UTC",
                                first_deadline_date="2026-01-12",
                            ),
                        ),
                    ),
                ),
                ("CAST('43' AS STRING)", "CAST('2026-01-12' AS DATE)"),
            ),
            (
                "weekly workflow",
                WorkflowMonitoringConfiguration(
                    workspace_id="123456789",
                    collector_storage=AUTOMATIC_COLLECTOR_STORAGE,
                    active_workflows=(
                        ActiveWorkflow(
                            job_id=44,
                            sla=ServiceLevelAgreement(
                                frequency="weekly",
                                completion_time="06:00",
                                timezone="UTC",
                                day_of_week="monday",
                            ),
                        ),
                    ),
                ),
                ("CAST('44' AS STRING)", "'monday' AS day_of_week"),
            ),
            (
                "zero active workflows",
                WorkflowMonitoringConfiguration(
                    workspace_id="123456789",
                    collector_storage=AUTOMATIC_COLLECTOR_STORAGE,
                    active_workflows=(),
                ),
                ("WHERE false", "CAST(NULL AS STRING) AS configured_job_id"),
            ),
        )

        for case_name, configuration, expected_texts in configurations:
            with self.subTest(case_name), tempfile.TemporaryDirectory() as temporary_directory:
                temporary_path = Path(temporary_directory)
                scaffold_path = temporary_path / "scaffold.json"
                first_output_path = temporary_path / "dashboard.json"
                second_output_path = temporary_path / "dashboard-second.json"
                scaffold_path.write_text(
                    json.dumps(
                        {
                            "datasets": [
                                {
                                    "queryLines": [
                                        "WITH configured_workflows AS (\n",
                                        "{{CONFIGURED_WORKFLOWS}}\n",
                                        ") {{WORKFLOW_METADATA}} "
                                        "JOIN {{COLLECTED_RUNS_TABLE}} r ON true",
                                    ]
                                }
                            ],
                            "pages": [],
                        }
                    ),
                    encoding="utf-8",
                )

                generate_dashboard(configuration, scaffold_path, first_output_path)
                first_output = first_output_path.read_text(encoding="utf-8")
                self.assertNotIn("{{CONFIGURED_WORKFLOWS}}", first_output)
                for expected_text in expected_texts:
                    self.assertIn(expected_text, first_output)

                # Repeated generation must produce byte-for-byte identical JSON.
                generate_dashboard(configuration, scaffold_path, second_output_path)
                self.assertEqual(
                    first_output_path.read_bytes(),
                    second_output_path.read_bytes(),
                )

    def test_generation_requires_a_scaffold_marker(self) -> None:
        configuration = WorkflowMonitoringConfiguration(
            workspace_id="123456789",
            active_workflows=(),
            collector_storage=AUTOMATIC_COLLECTOR_STORAGE,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            scaffold_path = temporary_path / "scaffold.json"
            scaffold_path.write_text('{"datasets": [], "pages": []}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "contains no configured workflow marker"):
                generate_dashboard(
                    configuration,
                    scaffold_path,
                    temporary_path / "dashboard.json",
                )

    def test_generation_rejects_invalid_scaffolds_and_duplicate_markers(self) -> None:
        configuration = WorkflowMonitoringConfiguration(
            workspace_id="123456789",
            active_workflows=(),
            collector_storage=AUTOMATIC_COLLECTOR_STORAGE,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            output_path = temporary_root / "dashboard.json"

            invalid_json_path = temporary_root / "invalid-json.json"
            invalid_json_path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "parse dashboard scaffold"):
                generate_dashboard(configuration, invalid_json_path, output_path)

            missing_source_marker_path = temporary_root / "missing-source-marker.json"
            missing_source_marker_path.write_text(
                json.dumps({"query": "{{CONFIGURED_WORKFLOWS}}"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "no source marker.*WORKFLOW_METADATA"):
                generate_dashboard(configuration, missing_source_marker_path, output_path)

            visualization_path = temporary_root / "visualization.json"
            visualization_path.write_text(
                json.dumps(
                    {
                        "data": {"name": "databricks_query"},
                        "width": "container",
                        "height": "container",
                    }
                ),
                encoding="utf-8",
            )
            duplicate_visualization_marker_path = temporary_root / "duplicate-marker.json"
            duplicate_visualization_marker_path.write_text(
                json.dumps(
                    {
                        "query": "{{CONFIGURED_WORKFLOWS}} {{WORKFLOW_METADATA}} "
                        "{{COLLECTED_RUNS_TABLE}}",
                        "first_visualization": "{{TEST_VISUALIZATION}}",
                        "second_visualization": "{{TEST_VISUALIZATION}}",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(
                    CUSTOM_VISUALIZATION_FILES,
                    {"{{TEST_VISUALIZATION}}": visualization_path},
                    clear=True,
                ),
                self.assertRaisesRegex(ValueError, "duplicate custom visualization marker"),
            ):
                generate_dashboard(
                    configuration,
                    duplicate_visualization_marker_path,
                    output_path,
                )

    def test_custom_visualizations_reject_invalid_contracts(self) -> None:
        invalid_visualization_cases = (
            ("{", "parse custom visualization"),
            (
                json.dumps(
                    {
                        "data": {"name": "other_query"},
                        "width": "container",
                        "height": "container",
                    }
                ),
                "must read from databricks_query",
            ),
            (
                json.dumps(
                    {
                        "data": {"name": "databricks_query"},
                        "width": 400,
                        "height": "container",
                    }
                ),
                "must use container width",
            ),
            (
                json.dumps(
                    {
                        "data": {"name": "databricks_query"},
                        "width": "container",
                        "height": 300,
                    }
                ),
                "must use container height",
            ),
        )

        for visualization_text, expected_error in invalid_visualization_cases:
            with self.subTest(expected_error=expected_error):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    visualization_path = Path(temporary_directory) / "visualization.json"
                    visualization_path.write_text(visualization_text, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, expected_error):
                        _load_custom_visualization(visualization_path)

    def test_generation_injects_valid_custom_visualizations(self) -> None:
        configuration = WorkflowMonitoringConfiguration(
            workspace_id="123456789",
            active_workflows=(),
            collector_storage=AUTOMATIC_COLLECTOR_STORAGE,
        )
        expected_visualizations = {
            "sla-delivery-calendar": "sla-delivery-calendar.vega.json",
            "delivery-margin-timeline": "delivery-margin-timeline.vega.json",
            "workflow-risk-map": "workflow-risk-map.vega.json",
            "run-duration-lanes": "run-duration-lanes.vega.json",
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "dashboard.json"
            generate_dashboard(configuration, SCAFFOLD_PATH, output_path)
            generated_dashboard = json.loads(output_path.read_text(encoding="utf-8"))
            generated_widgets = {
                layout_entry["widget"]["name"]: layout_entry["widget"]
                for page in generated_dashboard["pages"]
                for layout_entry in page["layout"]
            }

            for widget_name, visualization_filename in expected_visualizations.items():
                with self.subTest(widget_name):
                    visualization_path = CUSTOM_VISUALIZATION_DIRECTORY / visualization_filename
                    expected_visualization = json.loads(
                        visualization_path.read_text(encoding="utf-8")
                    )
                    visualization_text = generated_widgets[widget_name]["spec"]["jsonSpec"]["spec"]
                    self.assertIsInstance(visualization_text, str)
                    self.assertEqual(json.loads(visualization_text), expected_visualization)
                    self.assertEqual(
                        expected_visualization["data"],
                        {"name": "databricks_query"},
                    )
                    self.assertEqual(expected_visualization["width"], "container")
                    self.assertEqual(expected_visualization["height"], "container")
                    self.assertNotIn("layer", expected_visualization)
                    serialized_visualization = json.dumps(expected_visualization)
                    referenced_fields = {
                        direct_field or transformed_field
                        for direct_field, transformed_field in re.findall(
                            r'"field":\s*"([A-Za-z0-9_]+)"|datum\.([A-Za-z0-9_]+)',
                            serialized_visualization,
                        )
                    }
                    derived_fields = set(
                        re.findall(
                            r'"as":\s*"([A-Za-z0-9_]+)"',
                            serialized_visualization,
                        )
                    )
                    encoded_fields = {
                        encoded_field["fieldName"]
                        for encoded_field in generated_widgets[widget_name]["spec"]["encodings"][
                            "fields"
                        ]
                    }
                    self.assertLessEqual(referenced_fields - derived_fields, encoded_fields)

    def test_generation_uses_governed_tables_and_collector_job(self) -> None:
        workflow_monitoring_configuration = WorkflowMonitoringConfiguration(
            workspace_id="123456789",
            collector_storage=CollectorStorageConfiguration(
                catalog="monitoring_catalog",
                schema="monitoring_schema",
                create_catalog_and_schema_if_missing=False,
            ),
            active_workflows=(
                ActiveWorkflow(
                    job_id=42,
                    sla=ServiceLevelAgreement(
                        frequency="daily",
                        completion_time="06:00",
                        timezone="UTC",
                    ),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            dashboard_path = temporary_path / "dashboard.json"
            resource_path = temporary_path / "workflow-monitoring.jobs-api.job.yml"
            generate_dashboard(workflow_monitoring_configuration, SCAFFOLD_PATH, dashboard_path)
            generate_collector_job_resource(workflow_monitoring_configuration, resource_path)

            dashboard_text = dashboard_path.read_text(encoding="utf-8")
            self.assertIn(
                "`monitoring_catalog`.`monitoring_schema`.`workflow_run_api_state`",
                dashboard_text,
            )
            self.assertIn(
                "`monitoring_catalog`.`monitoring_schema`.`workflow_job_api_state`",
                dashboard_text,
            )
            self.assertIn("Jobs API collection failed", dashboard_text)
            self.assertIn("Jobs API data stale", dashboard_text)
            self.assertIn("c.configured_job_id AS job_id", dashboard_text)
            self.assertIn("COALESCE(collected_job.job_name", dashboard_text)
            resource_text = resource_path.read_text(encoding="utf-8")
            self.assertIn(
                "# Set workspace_id in workflow-monitoring.yml or with the "
                "workflow-monitoring CLI.",
                resource_text,
            )
            resource_document = yaml.safe_load(resource_text)
            collector_job = resource_document["resources"]["jobs"][
                "workflow_monitoring_jobs_api_collector"
            ]
            self.assertEqual(collector_job["schedule"]["quartz_cron_expression"], "0 0/5 * * * ?")
            self.assertEqual(collector_job["schedule"]["pause_status"], "UNPAUSED")
            self.assertEqual(collector_job["max_concurrent_runs"], 1)
            self.assertNotIn("queue", collector_job)
            self.assertNotIn("parameters", collector_job)
            collector_task_parameters = collector_job["tasks"][0]["notebook_task"][
                "base_parameters"
            ]
            self.assertEqual(collector_task_parameters["monitored_job_ids_json"], "[42]")
            self.assertEqual(
                collector_task_parameters["create_catalog_and_schema_if_missing"], "false"
            )
            self.assertEqual(collector_job["tasks"][1]["run_if"], "ALL_DONE")
            self.assertEqual(
                collector_job["tasks"][1]["dashboard_task"]["dashboard_id"],
                "${resources.dashboards.workflow_monitoring.id}",
            )

    def test_zero_active_workflows_pause_collector(self) -> None:
        configuration = WorkflowMonitoringConfiguration(
            workspace_id="123456789",
            collector_storage=AUTOMATIC_COLLECTOR_STORAGE,
            active_workflows=(),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            resource_path = Path(temporary_directory) / "workflow-monitoring.jobs-api.job.yml"
            generate_collector_job_resource(configuration, resource_path)
            collector_job = yaml.safe_load(resource_path.read_text(encoding="utf-8"))["resources"][
                "jobs"
            ]["workflow_monitoring_jobs_api_collector"]
            self.assertEqual(collector_job["schedule"]["pause_status"], "PAUSED")
            self.assertNotIn("parameters", collector_job)
            collector_task_parameters = collector_job["tasks"][0]["notebook_task"][
                "base_parameters"
            ]
            self.assertEqual(collector_task_parameters["monitored_job_ids_json"], "[]")
            self.assertEqual(
                collector_task_parameters["create_catalog_and_schema_if_missing"], "true"
            )


class DashboardCommandTests(unittest.TestCase):
    """Exercise successful and failed CLI command paths."""

    def test_validate_and_generate_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            configuration_path = temporary_root / "workflow-monitoring.yml"
            configuration_path.write_text(
                textwrap.dedent(
                    """\
                    version: 2
                    workspace_id: "123456789"
                    default_timezone: UTC
                    workflows: []
                    """
                ),
                encoding="utf-8",
            )

            validation_stdout = StringIO()
            with redirect_stdout(validation_stdout):
                validation_exit_code = dashboard_main(
                    [
                        "validate",
                        "--config",
                        str(configuration_path),
                        "--schema",
                        str(SCHEMA_PATH),
                    ]
                )
            self.assertEqual(validation_exit_code, 0)
            self.assertEqual(
                validation_stdout.getvalue(), "valid configuration: 0 active workflows\n"
            )

            dashboard_output_path = temporary_root / "dashboard.json"
            collector_resource_output_path = temporary_root / "collector.yml"
            generation_stdout = StringIO()
            with redirect_stdout(generation_stdout):
                generation_exit_code = dashboard_main(
                    [
                        "generate",
                        "--config",
                        str(configuration_path),
                        "--schema",
                        str(SCHEMA_PATH),
                        "--scaffold",
                        str(SCAFFOLD_PATH),
                        "--output",
                        str(dashboard_output_path),
                        "--jobs-api-resource-output",
                        str(collector_resource_output_path),
                    ]
                )
            self.assertEqual(generation_exit_code, 0)
            self.assertEqual(
                generation_stdout.getvalue(),
                f"generated dashboard: {dashboard_output_path} (0 active workflows)\n",
            )
            self.assertTrue(dashboard_output_path.is_file())
            self.assertTrue(collector_resource_output_path.is_file())

    def test_command_reports_configuration_read_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_configuration_path = Path(temporary_directory) / "missing-configuration.yml"
            command_stderr = StringIO()
            with redirect_stderr(command_stderr):
                command_exit_code = dashboard_main(
                    [
                        "validate",
                        "--config",
                        str(missing_configuration_path),
                        "--schema",
                        str(SCHEMA_PATH),
                    ]
                )

        self.assertEqual(command_exit_code, 1)
        self.assertIn(str(missing_configuration_path), command_stderr.getvalue())


class DashboardScaffoldTests(unittest.TestCase):
    """Protect names and field bindings used by dashboard widgets and filters."""

    def test_current_status_keeps_a_predecessor_for_every_schedule(self) -> None:
        expected_deadline_window_fragments = (
            "sequence(date_sub(local_today, 2), date_add(local_today, 1))",
            "offset * 7) AS deadline_date FROM workflow_local_dates w "
            "LATERAL VIEW EXPLODE(sequence(-2, 1)) o AS offset WHERE frequency = 'weekly'",
            "+ offset) * 14) AS deadline_date FROM workflow_local_dates w "
            "LATERAL VIEW EXPLODE(sequence(-2, 1)) o AS offset WHERE frequency = "
            "'fortnightly'",
            "last_day(add_months(local_today, offset)))) - 1) AS deadline_date FROM "
            "workflow_local_dates w LATERAL VIEW EXPLODE(sequence(-2, 1)) o AS offset "
            "WHERE frequency = 'monthly'",
        )

        for dashboard_path in (SCAFFOLD_PATH, GENERATED_DASHBOARD_PATH):
            with self.subTest(dashboard_path=dashboard_path):
                dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
                workflow_status_sql = "".join(
                    next(
                        dataset["queryLines"]
                        for dataset in dashboard["datasets"]
                        if dataset["name"] == "workflow_status"
                    )
                )
                for expected_deadline_window_fragment in expected_deadline_window_fragments:
                    self.assertIn(expected_deadline_window_fragment, workflow_status_sql)

    def test_scaffold_references_are_stable_and_valid(self) -> None:
        scaffold = json.loads(SCAFFOLD_PATH.read_text(encoding="utf-8"))
        expected_dataset_names = {
            "workflow_status",
            "run_history",
            "sla_history",
        }
        actual_dataset_names = {dataset["name"] for dataset in scaffold["datasets"]}
        self.assertEqual(actual_dataset_names, expected_dataset_names)

        for dataset in scaffold["datasets"]:
            dataset_sql = "".join(dataset["queryLines"])
            self.assertEqual(dataset_sql.count("{{CONFIGURED_WORKFLOWS}}"), 1)
            self.assertEqual(dataset_sql.count("{{WORKFLOW_METADATA}}"), 1)

        expected_page_names = {"operations", "trends"}
        self.assertEqual({page["name"] for page in scaffold["pages"]}, expected_page_names)

        seen_widget_names: set[str] = set()
        for page in scaffold["pages"]:
            self._assert_widgets_do_not_overlap(page)
            for layout_entry in page["layout"]:
                widget = layout_entry["widget"]
                self.assertNotIn(widget["name"], seen_widget_names)
                seen_widget_names.add(widget["name"])
                self._validate_widget_references(widget, expected_dataset_names)

        self.assertNotIn("workflow-compute-breakdown", seen_widget_names)
        self.assertNotIn("compute-section", seen_widget_names)
        self.assertIn("sla-compliance-trend", seen_widget_names)
        self.assertIn("current-workflow-health", seen_widget_names)
        self.assertNotIn("sla-history-matrix", seen_widget_names)
        for custom_widget_name in (
            "sla-delivery-calendar",
            "delivery-margin-timeline",
            "workflow-risk-map",
            "run-duration-lanes",
        ):
            self.assertIn(custom_widget_name, seen_widget_names)

        widgets_by_name = {
            layout_entry["widget"]["name"]: layout_entry["widget"]
            for page in scaffold["pages"]
            for layout_entry in page["layout"]
        }
        run_outcome_colours = {
            mapping["value"]: mapping["color"]
            for mapping in widgets_by_name["daily-run-outcomes"]["spec"]["encodings"]["color"][
                "scale"
            ]["mappings"]
        }
        self.assertEqual(
            run_outcome_colours,
            {"Success": "#009E73", "Not successful": "#D55E00"},
        )

        workflow_health_colours = {
            mapping["value"]: mapping["color"]
            for mapping in widgets_by_name["current-workflow-health"]["spec"]["encodings"]["color"][
                "scale"
            ]["mappings"]
        }
        self.assertEqual(workflow_health_colours["Met"], "#009E73")
        self.assertEqual(workflow_health_colours["Missed"], "#D55E00")
        self.assertEqual(workflow_health_colours["Pending"], "#E69F00")

        scaffold_text = SCAFFOLD_PATH.read_text(encoding="utf-8")
        required_sql_fragments = (
            "to_utc_timestamp",
            "LEAST(day_of_month, day(last_day(add_months(local_today, offset))))",
            "'SUCCEEDED', 'FAILED', 'CANCELLED', 'SKIPPED'",
            "late_recoveries AS",
            "run_metrics_30d AS",
            "sequence(-1, 14)",
            "date_sub(current_date(), 100)",
            "unsuccessful_runs_30d",
            "{{COLLECTED_RUNS_TABLE}}",
        )
        for required_sql_fragment in required_sql_fragments:
            self.assertIn(required_sql_fragment, scaffold_text)
        self.assertNotIn("system.", scaffold_text)
        self.assertNotIn("date_sub(current_date(), 365)", scaffold_text)
        self.assertNotIn("date_sub(local_today, 380)", scaffold_text)
        self.assertNotIn("FROM deadline_dates WHERE", scaffold_text)
        for stale_terminal_state in ("'ERROR'", "'TIMED_OUT'", "'BLOCKED'"):
            self.assertNotIn(stale_terminal_state, scaffold_text)
        self.assertNotIn("),\nSELECT", scaffold_text)

        for visualization_marker in CUSTOM_VISUALIZATION_FILES:
            self.assertEqual(scaffold_text.count(visualization_marker), 1)

    def _assert_widgets_do_not_overlap(self, page: dict[str, Any]) -> None:
        positioned_widgets = page["layout"]
        for widget_index, first_widget in enumerate(positioned_widgets):
            first_position = first_widget["position"]
            for second_widget in positioned_widgets[widget_index + 1 :]:
                second_position = second_widget["position"]
                separated = (
                    first_position["x"] + first_position["width"] <= second_position["x"]
                    or second_position["x"] + second_position["width"] <= first_position["x"]
                    or first_position["y"] + first_position["height"] <= second_position["y"]
                    or second_position["y"] + second_position["height"] <= first_position["y"]
                )
                self.assertTrue(
                    separated,
                    f"{first_widget['widget']['name']} overlaps "
                    f"{second_widget['widget']['name']} on {page['name']}",
                )

    def _validate_widget_references(
        self,
        widget: dict[str, Any],
        expected_dataset_names: set[str],
    ) -> None:
        query_fields: dict[str, set[str]] = {}
        for widget_query in widget.get("queries", []):
            query = widget_query["query"]
            self.assertIn(query["datasetName"], expected_dataset_names)
            query_fields[widget_query["name"]] = {field["name"] for field in query["fields"]}
        widget_spec = widget.get("spec", {})
        self._validate_spec_field_references(widget_spec, query_fields)
        if widget_spec.get("widgetType") == "custom-vega-viz":
            self.assertTrue(widget["queries"][0]["query"]["disaggregated"])
            self.assertEqual(widget_spec["data"], {"queryName": "main_query"})
            encoded_fields = {
                encoded_field["fieldName"] for encoded_field in widget_spec["encodings"]["fields"]
            }
            self.assertEqual(encoded_fields, query_fields["main_query"])

    def _validate_spec_field_references(
        self,
        current_spec_node: Any,
        query_fields: dict[str, set[str]],
    ) -> None:
        # Widget types nest field references differently, so inspect every node.
        if isinstance(current_spec_node, dict):
            if "fieldName" in current_spec_node:
                query_name = current_spec_node.get("queryName")
                if query_name is None and len(query_fields) == 1:
                    query_name = next(iter(query_fields))
                self.assertIn(current_spec_node["fieldName"], query_fields[query_name])
            for child_spec_node in current_spec_node.values():
                self._validate_spec_field_references(child_spec_node, query_fields)
        elif isinstance(current_spec_node, list):
            for child_spec_node in current_spec_node:
                self._validate_spec_field_references(child_spec_node, query_fields)


class JobsApiCollectorTests(unittest.TestCase):
    """Protect the collector's durable storage and polling guarantees."""

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
        self.assertEqual(bring_your_own_sql.count("CREATE TABLE IF NOT EXISTS"), 2)

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
        self.assertEqual(automatic_sql.count("CREATE TABLE IF NOT EXISTS"), 2)

    def test_runtime_workspace_and_retention_are_scoped(self) -> None:
        workspace_id_value = SimpleNamespace(get=lambda: "111")
        notebook_context = SimpleNamespace(workspaceId=lambda: workspace_id_value)
        notebook_handle = SimpleNamespace(getContext=lambda: notebook_context)
        runtime_utilities = SimpleNamespace(notebook=lambda: notebook_handle)
        notebook_utilities = SimpleNamespace(
            notebook=SimpleNamespace(
                entry_point=SimpleNamespace(getDbutils=lambda: runtime_utilities)
            )
        )
        self.assertEqual(_runtime_workspace_id(notebook_utilities), "111")
        self.assertEqual(
            _validated_runtime_workspace_id("111", notebook_utilities),
            "111",
        )
        with self.assertRaisesRegex(ValueError, "does not match runtime workspace 111"):
            _validated_runtime_workspace_id("222", notebook_utilities)

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

    def test_collector_source_is_valid_and_idempotent(self) -> None:
        collector_source = JOBS_API_COLLECTOR_PATH.read_text(encoding="utf-8")
        compile(collector_source, str(JOBS_API_COLLECTOR_PATH), "exec")

        required_collector_fragments = (
            'RUN_STATE_TABLE_NAME = "workflow_run_api_state"',
            'JOB_STATE_TABLE_NAME = "workflow_job_api_state"',
            "active_only=True",
            "jobs.get(job_id=job_id)",
            "jobs.get_run(run_id=stored_run_id)",
            "INCREMENTAL_COLLECTION_OVERLAP_MINUTES = 15",
            "RUN_STATE_MERGE_BATCH_SIZE = 500",
            "CREATE CATALOG IF NOT EXISTS",
            "CREATE SCHEMA IF NOT EXISTS",
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
        )
        for removed_fragment in removed_complexity:
            self.assertNotIn(removed_fragment, collector_source)

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
            for run_id in range(1, RUN_STATE_MERGE_BATCH_SIZE + 2)
        )
        with patch(
            "src.collect_workflow_monitoring_jobs_api._merge_run_state_rows"
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
        self.assertEqual(merged_batch_sizes, [RUN_STATE_MERGE_BATCH_SIZE, 1])

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

    def test_nonterminal_run_reconciliation_is_scoped_and_streamed(self) -> None:
        api_history_start_at = datetime(2026, 6, 26)

        def run_state_row(
            workspace_id: str,
            job_id: str,
            run_id: int,
            result_state: str,
            period_start_time: datetime = datetime(2026, 8, 24),
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
        ]

        class RunStateDataFrame:
            def __init__(self, rows: list[SimpleNamespace]) -> None:
                self.rows = rows

            def select(self, *_column_names: str) -> RunStateDataFrame:
                return self

            def where(self, predicate: str) -> RunStateDataFrame:
                if predicate.startswith("workspace_id ="):
                    selected_workspace_id = predicate.split("'")[1]
                    self.rows = [
                        row for row in self.rows if row.workspace_id == selected_workspace_id
                    ]
                elif predicate.startswith("job_id ="):
                    selected_job_id = predicate.split("'")[1]
                    self.rows = [row for row in self.rows if row.job_id == selected_job_id]
                elif predicate.startswith("result_state NOT IN"):
                    terminal_states = set(re.findall(r"'([A-Z_]+)'", predicate))
                    self.rows = [
                        row for row in self.rows if row.result_state not in terminal_states
                    ]
                elif predicate.startswith("period_start_time >= TIMESTAMP"):
                    selected_history_start_at = datetime.fromisoformat(predicate.split("'")[1])
                    self.rows = [
                        row
                        for row in self.rows
                        if row.period_start_time >= selected_history_start_at
                    ]
                return self

            def toLocalIterator(self) -> Iterator[SimpleNamespace]:
                yield from self.rows

        spark_session = SimpleNamespace(
            table=lambda _table_name: RunStateDataFrame(run_state_rows.copy())
        )
        nonterminal_run_ids = list(
            _stored_nonterminal_run_ids(
                spark_session,
                "monitoring",
                "jobs",
                "111",
                42,
                api_history_start_at,
            )
        )
        self.assertEqual(nonterminal_run_ids, [900])

    def _collect_with_stored_run_scenario(
        self,
        missing_run_error: Exception,
        run_merge_error: Exception | None = None,
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

        class StoredRunDataFrame:
            def __init__(self) -> None:
                self.rows = list(stored_run_rows.values())

            def where(self, predicate: str) -> StoredRunDataFrame:
                if predicate.startswith("workspace_id ="):
                    self.rows = [
                        row for row in self.rows if row.workspace_id == predicate.split("'")[1]
                    ]
                elif predicate.startswith("job_id ="):
                    self.rows = [row for row in self.rows if row.job_id == predicate.split("'")[1]]
                elif predicate.startswith("period_start_time >= TIMESTAMP"):
                    history_start_at = datetime.fromisoformat(predicate.split("'")[1])
                    self.rows = [
                        row for row in self.rows if row.period_start_time >= history_start_at
                    ]
                elif predicate.startswith("result_state NOT IN"):
                    terminal_states = set(re.findall(r"'([A-Z_]+)'", predicate))
                    self.rows = [
                        row for row in self.rows if row.result_state not in terminal_states
                    ]
                return self

            def select(self, *_column_names: str) -> StoredRunDataFrame:
                return self

            def toLocalIterator(self) -> Iterator[SimpleNamespace]:
                yield from self.rows

        def job_run(run_id: int, age_days: int) -> SimpleNamespace:
            run_start_at = (collection_time - timedelta(days=age_days)).replace(tzinfo=UTC)
            return SimpleNamespace(
                run_id=run_id,
                start_time=int(run_start_at.timestamp() * 1000),
                end_time=int((run_start_at + timedelta(minutes=1)).timestamp() * 1000),
                status=SimpleNamespace(
                    state="TERMINATED",
                    termination_details=SimpleNamespace(code="SUCCESS"),
                ),
            )

        retrieved_run_ids: list[int] = []

        def retrieve_stored_run(*, run_id: int) -> SimpleNamespace:
            retrieved_run_ids.append(run_id)
            if run_id == 800:
                raise missing_run_error
            return job_run(run_id, 2)

        jobs_api = SimpleNamespace(
            get=Mock(return_value=SimpleNamespace(settings=SimpleNamespace(name="Daily Orders"))),
            list_runs=Mock(
                side_effect=lambda **options: (
                    [] if options.get("active_only") else [job_run(1000, 0)]
                )
            ),
            get_run=Mock(side_effect=retrieve_stored_run),
        )
        jobs_api_client = SimpleNamespace(jobs=jobs_api)
        sdk_module = ModuleType("databricks.sdk")
        sdk_module.__path__ = []
        sdk_module.WorkspaceClient = lambda: jobs_api_client
        sdk_errors_module = ModuleType("databricks.sdk.errors")
        sdk_errors_module.ResourceDoesNotExist = ExpectedMissingRun
        databricks_module = ModuleType("databricks")
        databricks_module.__path__ = []

        notebook_context = SimpleNamespace(workspaceId=lambda: SimpleNamespace(get=lambda: "111"))
        notebook_handle = SimpleNamespace(getContext=lambda: notebook_context)
        runtime_utilities = SimpleNamespace(notebook=lambda: notebook_handle)
        widget_values = {
            "catalog_name": "monitoring",
            "schema_name": "jobs",
            "create_catalog_and_schema_if_missing": "false",
            "workspace_id": "111",
            "monitored_job_ids_json": "[42]",
        }
        notebook_utilities = SimpleNamespace(
            widgets=SimpleNamespace(get=widget_values.__getitem__),
            notebook=SimpleNamespace(
                entry_point=SimpleNamespace(getDbutils=lambda: runtime_utilities)
            ),
        )
        spark_session = SimpleNamespace(table=lambda _table_name: StoredRunDataFrame())
        deleted_run_state_cutoffs: list[datetime] = []
        job_state_updates: list[dict[str, Any]] = []

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
            patch.object(collector, "spark", spark_session, create=True),
            patch.object(collector, "dbutils", notebook_utilities, create=True),
            patch.object(collector, "_prepare_collector_storage"),
            patch.object(
                collector,
                "_delete_expired_run_state",
                side_effect=lambda *args: deleted_run_state_cutoffs.append(args[-1]),
            ),
            patch.object(
                collector,
                "_last_successful_collection_by_workspace_and_job",
                return_value={("111", "42"): collection_time - timedelta(days=65)},
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
            job_state_updates=job_state_updates,
            collector_output=collector_output.getvalue(),
        )

    def test_expired_and_missing_runs_do_not_block_newer_state_or_checkpoint(self) -> None:
        collection = self._collect_with_stored_run_scenario(
            ExpectedMissingRun("run expired or deleted")
        )
        self.assertIsNone(collection.collection_error)
        self.assertEqual(collection.retrieved_run_ids, [800, 900])
        self.assertEqual(collection.stored_run_rows[700].result_state, "RUNNING")
        self.assertEqual(collection.stored_run_rows[800].result_state, "RUNNING")
        self.assertEqual(collection.stored_run_rows[900].result_state, "SUCCEEDED")
        self.assertEqual(collection.stored_run_rows[1000].result_state, "SUCCEEDED")
        self.assertIn("Stored run ID 800 unavailable", collection.collector_output)
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
                - timedelta(days=100)
            ),
            timedelta(minutes=1),
        )
        requested_start_at = datetime.fromtimestamp(
            collection.list_runs_requests[0].kwargs["start_time_from"] / 1000, tz=UTC
        ).replace(tzinfo=None)
        self.assertLess(
            abs(collection.collection_time - requested_start_at - timedelta(days=60)),
            timedelta(minutes=1),
        )

    def test_unexpected_api_and_storage_errors_fail_collection(self) -> None:
        for unexpected_error, run_merge_error in (
            (RuntimeError("Jobs API unavailable"), None),
            (ExpectedMissingRun("run expired or deleted"), OSError("Delta merge failed")),
        ):
            with self.subTest(error=str(unexpected_error), storage_error=str(run_merge_error)):
                collection = self._collect_with_stored_run_scenario(
                    unexpected_error, run_merge_error
                )
                self.assertRegex(str(collection.collection_error), "failed for job IDs: 42")
                self.assertIsNone(collection.job_state_updates[-1]["last_successful_collection_at"])
                expected_error = run_merge_error or unexpected_error
                self.assertIn(
                    str(expected_error), collection.job_state_updates[-1]["last_error_message"]
                )


class SqlValidationScriptTests(unittest.TestCase):
    """Exercise the read-only fixed-date SQL validation path."""

    def test_submits_fixed_date_regressions_with_the_selected_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            validation_process, submitted_request = self._run_sql_validation(
                Path(temporary_directory),
                statement_state="SUCCEEDED",
            )

        self.assertEqual(validation_process.returncode, 0, validation_process.stderr)
        self.assertEqual(validation_process.stdout, "sla_regressions: SQL valid\n")
        self.assertEqual(
            submitted_request["command_arguments"][:3],
            ["api", "post", "/api/2.0/sql/statements"],
        )
        self.assertEqual(submitted_request["profile"], "selected-profile")
        self.assertEqual(submitted_request["warehouse_id"], "warehouse-123")
        self.assertEqual(submitted_request["wait_timeout"], "50s")
        self.assertEqual(submitted_request["on_wait_timeout"], "CANCEL")
        self.assertEqual(submitted_request["row_limit"], 1)

        regression_statement = submitted_request["statement"]
        for fixed_date_case in (
            "daily_miss",
            "monthly_miss",
            "weekly_miss",
            "fortnightly_miss",
            "daily_success",
        ):
            self.assertIn(fixed_date_case, regression_statement)
        self.assertIn("current_latest_due_status = history_latest_due_status", regression_statement)
        self.assertIsNone(
            re.search(
                r"\b(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|MERGE|COPY|PUT|REMOVE|GRANT|REVOKE)\b",
                regression_statement,
                re.IGNORECASE,
            )
        )

    def test_reports_fixed_date_regression_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            validation_process, _ = self._run_sql_validation(
                Path(temporary_directory),
                statement_state="FAILED",
            )

        self.assertEqual(validation_process.returncode, 1)
        self.assertEqual(
            validation_process.stderr,
            "sla_regressions: FAILED: synthetic SQL failure\n",
        )

    @staticmethod
    def _run_sql_validation(
        temporary_root: Path,
        statement_state: str,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        dashboard_fixture_path = temporary_root / "dashboard.json"
        dashboard_fixture_path.write_text('{"datasets": []}\n', encoding="utf-8")
        submitted_request_log_path = temporary_root / "submitted-request.jsonl"

        fake_binary_directory = temporary_root / "bin"
        fake_binary_directory.mkdir()
        fake_databricks_path = fake_binary_directory / "databricks"
        fake_databricks_path.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys

                command_arguments = sys.argv[1:]
                request_path = command_arguments[command_arguments.index("--json") + 1][1:]
                selected_profile = command_arguments[command_arguments.index("--profile") + 1]
                with open(request_path, encoding="utf-8") as request_file:
                    request_document = json.load(request_file)
                request_document["command_arguments"] = command_arguments
                request_document["profile"] = selected_profile
                with open(os.environ["SUBMITTED_REQUEST_LOG_PATH"], "w", encoding="utf-8") as log:
                    json.dump(request_document, log)

                statement_state = os.environ["FAKE_DATABRICKS_STATEMENT_STATE"]
                response_document = {"status": {"state": statement_state}}
                if statement_state != "SUCCEEDED":
                    response_document["status"]["error"] = {"message": "synthetic SQL failure"}
                print(json.dumps(response_document))
                """
            ),
            encoding="utf-8",
        )
        fake_databricks_path.chmod(0o755)

        validation_environment = os.environ.copy()
        validation_environment.update(
            {
                "FAKE_DATABRICKS_STATEMENT_STATE": statement_state,
                "PATH": os.pathsep.join(
                    (str(fake_binary_directory), validation_environment["PATH"])
                ),
                "SUBMITTED_REQUEST_LOG_PATH": str(submitted_request_log_path),
            }
        )
        validation_process = subprocess.run(
            [
                "bash",
                str(SQL_VALIDATION_SCRIPT_PATH),
                "selected-profile",
                "warehouse-123",
                str(dashboard_fixture_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=validation_environment,
        )
        submitted_request = json.loads(
            submitted_request_log_path.read_text(encoding="utf-8").strip()
        )
        return validation_process, submitted_request


class RepositoryContractTests(unittest.TestCase):
    """Keep deployment and release tools on one reviewed contract."""

    def test_local_documentation_links_resolve(self) -> None:
        documentation_paths = tuple(REPOSITORY_ROOT.rglob("*.md"))
        self.assertGreaterEqual(len(documentation_paths), 4)

        for documentation_path in documentation_paths:
            documentation_text = documentation_path.read_text(encoding="utf-8")
            relative_links = re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", documentation_text)
            for relative_link in relative_links:
                if relative_link.startswith(("http://", "https://", "mailto:")):
                    continue
                relative_target, _, target_anchor = relative_link.partition("#")
                linked_document_path = (
                    (documentation_path.parent / relative_target).resolve()
                    if relative_target
                    else documentation_path
                )
                self.assertTrue(
                    linked_document_path.is_file(),
                    f"{documentation_path}: missing linked file {relative_link}",
                )
                if not target_anchor:
                    continue
                linked_document_text = linked_document_path.read_text(encoding="utf-8")
                linked_headings = re.findall(r"^#{1,6}\s+(.+)$", linked_document_text, re.MULTILINE)
                linked_anchors = {
                    re.sub(r"[ _]+", "-", re.sub(r"[^a-z0-9 _-]", "", heading.lower()))
                    for heading in linked_headings
                }
                self.assertIn(
                    target_anchor,
                    linked_anchors,
                    f"{documentation_path}: missing linked section {relative_link}",
                )

    def test_cli_and_release_contracts_are_aligned(self) -> None:
        tracked_configuration_path = REPOSITORY_ROOT / "workflow-monitoring.yml"
        self.assertTrue(tracked_configuration_path.is_file())
        self.assertFalse((REPOSITORY_ROOT / "workflow-monitoring.template.yml").exists())
        self.assertTrue(
            (REPOSITORY_ROOT / "resources/workflow-monitoring.jobs-api.job.yml").is_file()
        )
        self.assertNotIn(
            "workflow-monitoring.yml",
            (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "resources/workflow-monitoring.jobs-api.job.yml",
            (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8"),
        )
        tracked_configuration = yaml.safe_load(
            tracked_configuration_path.read_text(encoding="utf-8")
        )
        self.assertTrue(
            all("job_id" in workflow for workflow in tracked_configuration["workflows"])
        )
        self.assertTrue(
            all("job_name" not in workflow for workflow in tracked_configuration["workflows"])
        )

        bundle_configuration = yaml.safe_load(
            (REPOSITORY_ROOT / "databricks.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            bundle_configuration["bundle"]["databricks_cli_version"],
            ">= 1.13.0",
        )

        act_dockerfile = (REPOSITORY_ROOT / ".act/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("databricks/setup-cli/v1.13.0/install.sh", act_dockerfile)

        release_notes_configuration = yaml.safe_load(
            (REPOSITORY_ROOT / ".github/release.yml").read_text(encoding="utf-8")
        )
        release_categories = release_notes_configuration["changelog"]["categories"]
        self.assertEqual(release_categories[-1]["labels"], ["*"])

        syntax_check = subprocess.run(
            ["bash", "-n", str(RELEASE_SCRIPT_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(syntax_check.returncode, 0, syntax_check.stderr)
        release_script = RELEASE_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("scripts/run-local-validation.sh", release_script)
        expected_cli_targets = {
            "darwin arm64 tar.gz",
            "darwin amd64 tar.gz",
            "linux arm64 tar.gz",
            "linux amd64 tar.gz",
            "windows amd64 zip",
        }
        for expected_cli_target in expected_cli_targets:
            self.assertIn(f"build_cli_asset {expected_cli_target}", release_script)
        self.assertIn('"${release_assets[@]}"', release_script)

        local_validation_syntax_check = subprocess.run(
            ["bash", "-n", str(LOCAL_VALIDATION_SCRIPT_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            local_validation_syntax_check.returncode,
            0,
            local_validation_syntax_check.stderr,
        )
        local_validation_script = LOCAL_VALIDATION_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("--bind", local_validation_script)
        self.assertIn("--reuse", local_validation_script)
        self.assertIn("docker image prune --force --filter dangling=true", local_validation_script)
        self.assertIn(
            'runner_image="databricks-workflow-monitoring-dashboards-act:local"',
            local_validation_script,
        )
        self.assertIn(
            'runner_repository_hash_label="org.overengineered.workflow-monitoring.repository-hash"',
            local_validation_script,
        )
        self.assertIn(
            'repository_root_hash="$(printf \'%s\' "$repository_root" | git hash-object --stdin)"',
            local_validation_script,
        )
        self.assertIn(
            '"$container_repository_root_hash" != "$repository_root_hash"',
            local_validation_script,
        )

        help_check = subprocess.run(
            ["bash", str(RELEASE_SCRIPT_PATH), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_check.returncode, 0, help_check.stderr)
        self.assertIn("--check|--publish", help_check.stdout)


if __name__ == "__main__":
    unittest.main()
