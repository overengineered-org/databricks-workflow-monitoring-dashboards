"""Tests for configuration, generation, and dashboard scaffold bindings."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import textwrap
import unittest
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import yaml

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
    generate_collector_job_resource,
    generate_dashboard,
    load_workflow_monitoring_configuration,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY_ROOT / "schema/workflow-monitoring.schema.json"
SCAFFOLD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.scaffold.lvdash.json"
CUSTOM_VISUALIZATION_DIRECTORY = REPOSITORY_ROOT / "src/visualizations"
JOBS_API_COLLECTOR_PATH = REPOSITORY_ROOT / "src/collect_workflow_monitoring_jobs_api.py"
RELEASE_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/release.sh"
LOCAL_VALIDATION_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/run-local-validation.sh"
AUTOMATIC_COLLECTOR_STORAGE = CollectorStorageConfiguration(
    catalog="workflow_monitoring",
    schema="lakeflow_jobs",
    create_catalog_and_schema_if_missing=True,
)


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


class DashboardScaffoldTests(unittest.TestCase):
    """Protect names and field bindings used by dashboard widgets and filters."""

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
        api_retention_start_at = datetime(2026, 6, 26)

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
                    selected_retention_start_at = datetime.fromisoformat(predicate.split("'")[1])
                    self.rows = [
                        row
                        for row in self.rows
                        if row.period_start_time >= selected_retention_start_at
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
                api_retention_start_at,
            )
        )
        self.assertEqual(nonterminal_run_ids, [900])


class RepositoryContractTests(unittest.TestCase):
    """Keep deployment and release tools on one reviewed contract."""

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
