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
import tomllib
import unittest
from collections.abc import Iterator
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import yaml

from scripts.build_dashboard import (
    CUSTOM_VISUALIZATION_FILES,
    _load_custom_visualization,
    build_dashboard,
)
from src import collect_workflow_monitoring_jobs_api as collector
from src.collect_workflow_monitoring_jobs_api import (
    RUN_STATE_WRITE_BATCH_SIZE,
    _delete_expired_run_state,
    _delete_unretrievable_run_state,
    _job_name_from_job_details,
    _last_successful_collection_by_workspace_and_job,
    _merge_job_runs_in_batches,
    _normalized_run_state,
    _prepare_collector_storage,
    _replace_workspace_sla_config,
    _stored_nonterminal_run_ids_for_reconciliation,
    _utc_now_naive,
)
from src.workflow_monitoring_config import (
    ConfiguredWorkflow,
    ServiceLevelAgreement,
    WorkflowMonitoringConfiguration,
    validated_workspace_id,
    workflow_sla_config_rows,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY_ROOT / "src/workflow-monitoring.schema.json"
TEMPLATE_PROJECT_PATH = REPOSITORY_ROOT / "template/template/{{.project_name}}"
TRACKED_CONFIGURATION_PATH = TEMPLATE_PROJECT_PATH / "workflow-monitoring.yml.tmpl"
TEMPLATE_DATABRICKS_PATH = TEMPLATE_PROJECT_PATH / "databricks.yml.tmpl"
TEMPLATE_INPUT_PATH = REPOSITORY_ROOT / "tests/fixtures/template-input.json"
SCAFFOLD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.scaffold.lvdash.json"
GENERATED_DASHBOARD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.lvdash.json"
COLLECTOR_JOB_PATH = TEMPLATE_PROJECT_PATH / "resources/workflow-monitoring.collector.job.yml.tmpl"
CUSTOM_VISUALIZATION_DIRECTORY = REPOSITORY_ROOT / "src/visualizations"
JOBS_API_COLLECTOR_PATH = REPOSITORY_ROOT / "src/collect_workflow_monitoring_jobs_api.py"
RELEASE_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/release.sh"
LOCAL_VALIDATION_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/run-local-validation.sh"
SQL_VALIDATION_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/validate-sql-datasets.sh"


class ExpectedMissingRun(Exception):
    """Stand in for the Jobs SDK's ResourceDoesNotExist error."""


class StoredRunStateDataFrame:
    """Apply the collector's Spark filters to in-memory run rows."""

    def __init__(self, run_state_rows: list[SimpleNamespace]) -> None:
        self.rows = run_state_rows

    def where(self, predicate: str) -> StoredRunStateDataFrame:
        if predicate.startswith("workspace_id ="):
            workspace_id = predicate.split("'")[1]
            self.rows = [row for row in self.rows if row.workspace_id == workspace_id]
        elif predicate.startswith("job_id ="):
            job_id = predicate.split("'")[1]
            self.rows = [row for row in self.rows if row.job_id == job_id]
        elif predicate.startswith("period_start_time >= TIMESTAMP"):
            run_history_start_at = datetime.fromisoformat(predicate.split("'")[1])
            self.rows = [row for row in self.rows if row.period_start_time >= run_history_start_at]
        elif predicate.startswith("period_start_time < TIMESTAMP"):
            incremental_start_at = datetime.fromisoformat(predicate.split("'")[1])
            self.rows = [row for row in self.rows if row.period_start_time < incremental_start_at]
        elif predicate.startswith("result_state NOT IN"):
            terminal_states = set(re.findall(r"'([A-Z_]+)'", predicate))
            self.rows = [row for row in self.rows if row.result_state not in terminal_states]
        return self

    def select(self, *_column_names: str) -> StoredRunStateDataFrame:
        return self

    def toLocalIterator(self) -> Iterator[SimpleNamespace]:
        yield from self.rows


class DashboardGenerationTests(unittest.TestCase):
    """Check markers, stable output, and static dashboard SQL."""

    def test_build_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            first_output = temporary_root / "first-dashboard.json"
            second_output = temporary_root / "second-dashboard.json"

            build_dashboard(SCAFFOLD_PATH, first_output)
            build_dashboard(SCAFFOLD_PATH, second_output)

            self.assertEqual(first_output.read_bytes(), second_output.read_bytes())

    def test_build_script_runs_from_the_scripts_directory(self) -> None:
        dashboard_bytes = GENERATED_DASHBOARD_PATH.read_bytes()
        try:
            build_process = subprocess.run(
                [sys.executable, "build_dashboard.py"],
                cwd=REPOSITORY_ROOT / "scripts",
                check=False,
                capture_output=True,
                text=True,
            )
        finally:
            GENERATED_DASHBOARD_PATH.write_bytes(dashboard_bytes)

        self.assertEqual(build_process.returncode, 0, build_process.stderr)

    def test_built_dashboard_reads_config_table_and_has_no_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "dashboard.json"
            build_dashboard(SCAFFOLD_PATH, output_path)
            dashboard_text = output_path.read_text(encoding="utf-8")

        self.assertEqual(dashboard_text.count("FROM workflow_sla_config"), 3)
        for marker in CUSTOM_VISUALIZATION_FILES:
            self.assertNotIn(marker, dashboard_text)

    def test_built_dashboard_contains_no_configured_values(self) -> None:
        template_input = json.loads(TEMPLATE_INPUT_PATH.read_text(encoding="utf-8"))
        tracked_configuration = yaml.safe_load(
            TRACKED_CONFIGURATION_PATH.read_text(encoding="utf-8")
        )
        configured_values = (
            template_input["catalog"],
            template_input["schema"],
            *(str(workflow["job_id"]) for workflow in tracked_configuration["workflows"]),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "dashboard.json"
            build_dashboard(SCAFFOLD_PATH, output_path)
            dashboard_text = output_path.read_text(encoding="utf-8")

        for configured_value in configured_values:
            self.assertNotIn(configured_value, dashboard_text)

    def test_build_rejects_invalid_scaffolds_and_duplicate_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            output_path = temporary_root / "dashboard.json"

            invalid_json_path = temporary_root / "invalid-json.json"
            invalid_json_path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "parse dashboard scaffold"):
                build_dashboard(invalid_json_path, output_path)

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
                        "query": "safe SQL",
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
                build_dashboard(
                    duplicate_visualization_marker_path,
                    output_path,
                )

    def test_build_rejects_a_missing_visualization_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
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
            missing_marker_path = temporary_root / "missing-marker.json"
            missing_marker_path.write_text(
                json.dumps({"query": "safe SQL"}),
                encoding="utf-8",
            )
            with (
                patch.dict(
                    CUSTOM_VISUALIZATION_FILES,
                    {"{{TEST_VISUALIZATION}}": visualization_path},
                    clear=True,
                ),
                self.assertRaisesRegex(ValueError, "missing custom visualization marker"),
            ):
                build_dashboard(missing_marker_path, temporary_root / "dashboard.json")

    def test_build_keeps_the_existing_dashboard_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            output_path = temporary_root / "dashboard.json"
            output_path.write_text("previous dashboard\n", encoding="utf-8")

            with (
                patch("scripts.build_dashboard.os.replace", side_effect=OSError("disk full")),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                build_dashboard(SCAFFOLD_PATH, output_path)

            self.assertEqual(output_path.read_text(encoding="utf-8"), "previous dashboard\n")
            self.assertEqual(
                sorted(temporary_path.name for temporary_path in temporary_root.iterdir()),
                ["dashboard.json"],
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

    def test_build_injects_valid_custom_visualizations(self) -> None:
        expected_visualizations = {
            "sla-delivery-calendar": "sla-delivery-calendar.vega.json",
            "delivery-margin-timeline": "delivery-margin-timeline.vega.json",
            "workflow-risk-map": "workflow-risk-map.vega.json",
            "run-duration-lanes": "run-duration-lanes.vega.json",
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "dashboard.json"
            build_dashboard(SCAFFOLD_PATH, output_path)
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

    def test_build_uses_governed_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            dashboard_path = Path(temporary_directory) / "dashboard.json"
            build_dashboard(SCAFFOLD_PATH, dashboard_path)

            dashboard_text = dashboard_path.read_text(encoding="utf-8")
            self.assertIn("INNER JOIN workflow_run_api_state t", dashboard_text)
            self.assertIn("FROM workflow_job_api_state", dashboard_text)
            self.assertNotIn("monitoring_catalog", dashboard_text)
            self.assertIn("Jobs API collection failed", dashboard_text)
            self.assertIn("Jobs API data stale", dashboard_text)
            self.assertIn("c.configured_job_id AS job_id", dashboard_text)
            self.assertIn("COALESCE(collected_job.job_name", dashboard_text)

    def test_collector_job_resource_matches_runtime_contract(self) -> None:
        resource_text = COLLECTOR_JOB_PATH.read_text(encoding="utf-8")
        self.assertNotIn("# Generated by", resource_text)
        collector_job = yaml.safe_load(resource_text)["resources"]["jobs"][
            "workflow_monitoring_jobs_api_collector"
        ]
        self.assertEqual(collector_job["schedule"]["quartz_cron_expression"], "0 0/5 * * * ?")
        self.assertEqual(collector_job["max_concurrent_runs"], 1)
        self.assertNotIn("queue", collector_job)
        self.assertEqual(
            collector_job["environments"],
            [
                {
                    "environment_key": "workflow_monitoring_collector",
                    "spec": {
                        "environment_version": "6",
                        "dependencies": ["jsonschema==4.26.0", "pyyaml==6.0.3"],
                    },
                }
            ],
        )
        collector_task = collector_job["tasks"][0]
        self.assertEqual(collector_task["environment_key"], "workflow_monitoring_collector")
        self.assertNotIn("parameters", collector_job)
        self.assertEqual(
            collector_task["spark_python_task"]["python_file"],
            "../workflow_monitoring/collector/collect_workflow_monitoring_jobs_api.py",
        )
        self.assertEqual(
            collector_task["spark_python_task"]["parameters"],
            [
                "--config-path",
                "${workspace.file_path}/workflow-monitoring.yml",
                "--schema-path",
                "${workspace.file_path}/workflow_monitoring/workflow-monitoring.schema.json",
                "--catalog",
                "${var.catalog}",
                "--schema",
                "${var.schema}",
                "--create-catalog-and-schema-if-missing",
                "${var.create_catalog_and_schema_if_missing}",
            ],
        )
        self.assertEqual(collector_job["tasks"][1]["run_if"], "ALL_DONE")
        self.assertEqual(
            collector_job["tasks"][1]["dashboard_task"]["dashboard_id"],
            "${resources.dashboards.workflow_monitoring.id}",
        )

    def test_collector_job_does_not_set_pause_status(self) -> None:
        collector_job = yaml.safe_load(COLLECTOR_JOB_PATH.read_text(encoding="utf-8"))["resources"][
            "jobs"
        ]["workflow_monitoring_jobs_api_collector"]
        self.assertNotIn("pause_status", collector_job["schedule"])


class ConfigurationContractTests(unittest.TestCase):
    """Keep the template configuration structurally valid."""

    def test_template_configuration_has_required_keys(self) -> None:
        configuration = yaml.safe_load(TRACKED_CONFIGURATION_PATH.read_text(encoding="utf-8"))
        self.assertEqual(configuration["version"], 3)
        self.assertEqual(configuration["default_timezone"], "{{.default_timezone}}")
        self.assertTrue(configuration["workflows"])
        for workflow in configuration["workflows"]:
            self.assertIn("job_id", workflow)
            self.assertNotIn("job_name", workflow)


class DashboardScaffoldTests(unittest.TestCase):
    """Protect names and field bindings used by dashboard widgets and filters."""

    def test_current_status_keeps_a_predecessor_for_every_schedule(self) -> None:
        expected_deadline_window_fragments = (
            "sequence(date_sub(local_today, 2), date_add(local_today, 1))",
            "offset * 7) AS deadline_date FROM workflow_local_dates w "
            "LATERAL VIEW EXPLODE(sequence(-2, 1)) o AS offset WHERE frequency = 'weekly'",
            ", 0) + offset) * 14) AS deadline_date FROM workflow_local_dates w "
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

    def test_fortnightly_deadlines_start_at_first_deadline_date(self) -> None:
        recurrence_anchor_fragment = (
            "GREATEST(CAST(FLOOR(datediff(local_today, first_deadline_date) / 14.0) "
            "AS INT), 0) + offset"
        )
        eligible_deadline_fragment = (
            "d.frequency <> 'fortnightly' OR d.deadline_date >= d.first_deadline_date"
        )

        for dashboard_path in (SCAFFOLD_PATH, GENERATED_DASHBOARD_PATH):
            with self.subTest(dashboard_path=dashboard_path):
                dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
                dataset_sql_by_name = {
                    dataset["name"]: "".join(dataset["queryLines"])
                    for dataset in dashboard["datasets"]
                }
                self.assertEqual(
                    dataset_sql_by_name["workflow_status"].count(recurrence_anchor_fragment),
                    1,
                )
                self.assertEqual(
                    dataset_sql_by_name["workflow_status"].count(eligible_deadline_fragment),
                    1,
                )
                self.assertEqual(
                    dataset_sql_by_name["sla_history"].count(recurrence_anchor_fragment),
                    1,
                )
                self.assertEqual(
                    dataset_sql_by_name["sla_history"].count(eligible_deadline_fragment),
                    2,
                )

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
            self.assertEqual(dataset_sql.count("FROM workflow_sla_config"), 1)
            self.assertEqual(dataset_sql.count("FROM workflow_job_api_state"), 1)
            self.assertEqual(dataset_sql.count("workflow_run_api_state"), 1)
            self.assertNotIn("{{WORKFLOW_METADATA}}", dataset_sql)
            self.assertNotIn("{{COLLECTED_RUNS_TABLE}}", dataset_sql)

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
            "workflow_run_api_state",
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
        self.assertEqual(submitted_request["catalog"], "workflow_monitoring")
        self.assertEqual(submitted_request["schema"], "lakeflow_jobs")
        self.assertEqual(submitted_request["wait_timeout"], "50s")
        self.assertEqual(submitted_request["on_wait_timeout"], "CANCEL")
        self.assertEqual(submitted_request["row_limit"], 1)

        regression_statement = submitted_request["statement"]
        for fixed_date_case in (
            "daily_miss",
            "monthly_miss",
            "weekly_miss",
            "fortnightly_miss",
            "fortnightly_before_anchor",
            "fortnightly_on_anchor",
            "fortnightly_after_anchor",
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

    def test_requires_the_schema_argument(self) -> None:
        validation_process = subprocess.run(
            [
                "bash",
                str(SQL_VALIDATION_SCRIPT_PATH),
                "selected-profile",
                "warehouse-123",
                "workflow_monitoring",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(validation_process.returncode, 2)
        self.assertIn("usage: validate-sql-datasets.sh", validation_process.stderr)

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
                "workflow_monitoring",
                "lakeflow_jobs",
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
        tracked_configuration_path = TRACKED_CONFIGURATION_PATH
        self.assertTrue(tracked_configuration_path.is_file())
        self.assertFalse((REPOSITORY_ROOT / "workflow-monitoring.template.yml").exists())
        self.assertTrue(COLLECTOR_JOB_PATH.is_file())
        self.assertNotIn(
            "workflow-monitoring.yml.tmpl",
            (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "workflow-monitoring.collector.job.yml.tmpl",
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

        bundle_configuration = yaml.safe_load(TEMPLATE_DATABRICKS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            bundle_configuration["bundle"]["databricks_cli_version"],
            ">= 1.22.1",
        )
        self.assertNotIn("presets", bundle_configuration["targets"]["dev"])
        self.assertEqual(
            bundle_configuration["targets"]["prod"]["presets"]["trigger_pause_status"],
            "UNPAUSED",
        )

        act_dockerfile = (REPOSITORY_ROOT / ".act/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("databricks/setup-cli/v1.22.1/install.sh", act_dockerfile)

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
