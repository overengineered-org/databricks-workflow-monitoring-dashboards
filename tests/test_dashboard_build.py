"""Tests for dashboard generation and the collector job resource contract."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.build_dashboard import (
    CUSTOM_VISUALIZATION_FILES,
    _load_custom_visualization,
    build_dashboard,
)
from tests._paths import REPOSITORY_ROOT

TRACKED_CONFIGURATION_PATH = REPOSITORY_ROOT / "workflow-monitoring.yml"
COLLECTOR_JOB_PATH = REPOSITORY_ROOT / "resources/workflow-monitoring.collector.job.yml"
BUNDLE_PATH = REPOSITORY_ROOT / "databricks.yml"
SCAFFOLD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.scaffold.lvdash.json"
GENERATED_DASHBOARD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.lvdash.json"
CUSTOM_VISUALIZATION_DIRECTORY = REPOSITORY_ROOT / "src/visualizations"


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
        bundle_variables = yaml.safe_load(BUNDLE_PATH.read_text(encoding="utf-8"))["variables"]
        tracked_configuration = yaml.safe_load(
            TRACKED_CONFIGURATION_PATH.read_text(encoding="utf-8")
        )
        configured_values = (
            bundle_variables["catalog"]["default"],
            bundle_variables["schema"]["default"],
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
            "../src/collect_workflow_monitoring_jobs_api.py",
        )
        self.assertEqual(
            collector_task["spark_python_task"]["parameters"],
            [
                "--config-path",
                "${workspace.file_path}/workflow-monitoring.yml",
                "--schema-path",
                "${workspace.file_path}/src/workflow-monitoring.schema.json",
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
    """Keep the tracked configuration structurally valid."""

    def test_tracked_configuration_has_required_keys(self) -> None:
        configuration = yaml.safe_load(TRACKED_CONFIGURATION_PATH.read_text(encoding="utf-8"))
        self.assertEqual(configuration["version"], 3)
        self.assertEqual(configuration["default_timezone"], "UTC")
        # A fresh clone monitors nothing until the user ticks Jobs.
        self.assertEqual(configuration["workflows"], [])
