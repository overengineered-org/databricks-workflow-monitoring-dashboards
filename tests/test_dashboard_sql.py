"""Tests for dashboard scaffold SQL and the read-only SQL validation script."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

import tests._paths  # noqa: F401 -- puts the repository root and src/ on sys.path

# isort: split
from collect_workflow_monitoring_jobs_api import RUN_HISTORY_RETENTION_DAYS
from scripts.build_dashboard import CUSTOM_VISUALIZATION_FILES
from scripts.dashboard_constants import RUN_WINDOW_DAYS
from tests._paths import REPOSITORY_ROOT

SCAFFOLD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.scaffold.lvdash.json"
GENERATED_DASHBOARD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.lvdash.json"
SQL_VALIDATION_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/validate-sql-datasets.sh"


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

        dashboard = json.loads(GENERATED_DASHBOARD_PATH.read_text(encoding="utf-8"))
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

        dashboard = json.loads(GENERATED_DASHBOARD_PATH.read_text(encoding="utf-8"))
        dataset_sql_by_name = {
            dataset["name"]: "".join(dataset["queryLines"]) for dataset in dashboard["datasets"]
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
        built_dashboard = json.loads(GENERATED_DASHBOARD_PATH.read_text(encoding="utf-8"))
        expected_dataset_names = {
            "workflow_status",
            "run_history",
            "sla_history",
        }
        actual_dataset_names = {dataset["name"] for dataset in scaffold["datasets"]}
        self.assertEqual(actual_dataset_names, expected_dataset_names)

        for dataset in built_dashboard["datasets"]:
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
        built_dashboard_text = GENERATED_DASHBOARD_PATH.read_text(encoding="utf-8")
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
            self.assertIn(required_sql_fragment, built_dashboard_text)
        self.assertNotIn("system.", built_dashboard_text)
        self.assertNotIn("date_sub(current_date(), 365)", built_dashboard_text)
        self.assertNotIn("date_sub(local_today, 380)", built_dashboard_text)
        self.assertNotIn("FROM deadline_dates WHERE", built_dashboard_text)
        for stale_terminal_state in ("'ERROR'", "'TIMED_OUT'", "'BLOCKED'"):
            self.assertNotIn(stale_terminal_state, built_dashboard_text)
        self.assertNotIn("),\nSELECT", built_dashboard_text)

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


class DashboardConstantTests(unittest.TestCase):
    """Keep the dashboard windows linked to the collector retention."""

    def test_run_window_days_match_the_collector_retention(self) -> None:
        self.assertEqual(RUN_WINDOW_DAYS, RUN_HISTORY_RETENTION_DAYS)


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
