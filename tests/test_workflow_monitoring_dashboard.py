"""Tests for configuration, generation, and dashboard scaffold bindings."""

from __future__ import annotations

import json
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

from workflow_monitoring_dashboard import (
    CUSTOM_VISUALIZATION_FILES,
    ActiveWorkflow,
    ServiceLevelAgreement,
    WorkflowMonitoringConfiguration,
    generate_dashboard,
    load_workflow_monitoring_configuration,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY_ROOT / "schema/workflow-monitoring.schema.json"
SCAFFOLD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.scaffold.lvdash.json"
CUSTOM_VISUALIZATION_DIRECTORY = REPOSITORY_ROOT / "src/visualizations"


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
                version: 1
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - job_name: daily
                    job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: daily
                      completion_time: "06:00"
                  - job_name: weekly
                    monitoring_status: active
                    sla:
                      frequency: weekly
                      completion_time: "07:00"
                      day_of_week: monday
                  - job_name: fortnightly
                    monitoring_status: active
                    sla:
                      frequency: fortnightly
                      completion_time: "08:00"
                      first_deadline_date: "2026-01-12"
                  - job_name: monthly
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
                "inactive workflow skips malformed fields",
                """
                version: 1
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - monitoring_status: inactive
                    job_name: paused
                    sla: malformed-but-ignored
                """,
                0,
                None,
            ),
            (
                "duplicate job ID",
                """
                version: 1
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - job_name: first
                    job_id: 9
                    monitoring_status: active
                    sla: {frequency: daily, completion_time: "06:00"}
                  - job_name: renamed
                    job_id: 9
                    monitoring_status: active
                    sla: {frequency: daily, completion_time: "07:00"}
                """,
                None,
                "job_id duplicates active workflow",
            ),
            (
                "duplicate job name",
                """
                version: 1
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - job_name: duplicate
                    job_id: 1
                    monitoring_status: active
                    sla: {frequency: daily, completion_time: "06:00"}
                  - job_name: duplicate
                    job_id: 2
                    monitoring_status: active
                    sla: {frequency: daily, completion_time: "07:00"}
                """,
                None,
                "job_name duplicates active workflow",
            ),
            (
                "daily rejects weekly field",
                """
                version: 1
                workspace_id: "123456789"
                default_timezone: UTC
                workflows:
                  - job_name: invalid
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
                version: 1
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


class DashboardGenerationTests(unittest.TestCase):
    """Check SQL escaping, paused monitoring, markers, and stable output."""

    def test_generation_is_safe_and_deterministic(self) -> None:
        monthly_day = 31
        configurations = (
            (
                "active workflow",
                WorkflowMonitoringConfiguration(
                    workspace_id="123456789",
                    active_workflows=(
                        ActiveWorkflow(
                            job_name="owner's monthly workflow",
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
                ("owner''s monthly workflow", "CAST('42' AS STRING)", "CAST(31 AS INT)"),
            ),
            (
                "zero active workflows",
                WorkflowMonitoringConfiguration(
                    workspace_id="123456789",
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
                                        ") SELECT * FROM configured_workflows",
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


class DashboardScaffoldTests(unittest.TestCase):
    """Protect names and field bindings used by dashboard widgets and filters."""

    def test_scaffold_references_are_stable_and_valid(self) -> None:
        scaffold = json.loads(SCAFFOLD_PATH.read_text(encoding="utf-8"))
        expected_dataset_names = {
            "workflow_status",
            "run_history",
            "sla_history",
            "daily_cost",
        }
        actual_dataset_names = {dataset["name"] for dataset in scaffold["datasets"]}
        self.assertEqual(actual_dataset_names, expected_dataset_names)

        for dataset in scaffold["datasets"]:
            dataset_sql = "".join(dataset["queryLines"])
            self.assertEqual(dataset_sql.count("{{CONFIGURED_WORKFLOWS}}"), 1)

        expected_page_names = {"operations", "trends-cost"}
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
        self.assertEqual(run_outcome_colours, {"Success": "#009E73", "Failure": "#D55E00"})

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
            "'FAILED', 'ERROR', 'TIMED_OUT', 'BLOCKED', 'CANCELLED', 'SKIPPED'",
            "system.billing.list_prices",
            "late_recoveries AS",
            "run_metrics_30d AS",
            "estimated_list_cost_30d",
        )
        for required_sql_fragment in required_sql_fragments:
            self.assertIn(required_sql_fragment, scaffold_text)

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


if __name__ == "__main__":
    unittest.main()
