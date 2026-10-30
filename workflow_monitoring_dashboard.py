"""Validate workflow monitoring YAML and generate the Databricks dashboard JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from src.workflow_monitoring_config import load_workflow_monitoring_configuration

DEFAULT_CONFIGURATION_PATH = Path("workflow-monitoring.yml")
DEFAULT_SCHEMA_PATH = Path("src/workflow-monitoring.schema.json")
DEFAULT_SCAFFOLD_PATH = Path("src/dashboards/workflow-monitoring.scaffold.lvdash.json")
DEFAULT_DASHBOARD_PATH = Path("src/dashboards/workflow-monitoring.lvdash.json")
DEFAULT_JOBS_API_RESOURCE_PATH = Path("resources/workflow-monitoring.jobs-api.job.yml")
WORKFLOW_METADATA_MARKER = "{{WORKFLOW_METADATA}}"
COLLECTED_RUNS_TABLE_MARKER = "{{COLLECTED_RUNS_TABLE}}"
JOBS_API_RUN_STATE_TABLE = "workflow_run_api_state"
JOBS_API_JOB_STATE_TABLE = "workflow_job_api_state"
CUSTOM_VISUALIZATION_FILES = {
    "{{SLA_DELIVERY_CALENDAR_VEGA}}": Path("src/visualizations/sla-delivery-calendar.vega.json"),
    "{{DELIVERY_MARGIN_TIMELINE_VEGA}}": Path(
        "src/visualizations/delivery-margin-timeline.vega.json"
    ),
    "{{WORKFLOW_RISK_MAP_VEGA}}": Path("src/visualizations/workflow-risk-map.vega.json"),
    "{{RUN_DURATION_LANES_VEGA}}": Path("src/visualizations/run-duration-lanes.vega.json"),
}


def generate_dashboard(scaffold_path: Path, output_path: Path) -> None:
    """Replace scaffold markers and write deterministic dashboard JSON."""

    try:
        dashboard_document = json.loads(scaffold_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"parse dashboard scaffold: {error}") from error

    source_markers = {
        WORKFLOW_METADATA_MARKER: _render_workflow_metadata_sql(),
        COLLECTED_RUNS_TABLE_MARKER: JOBS_API_RUN_STATE_TABLE,
    }
    for source_marker, source_sql in source_markers.items():
        source_replacement_count = _replace_text_marker(
            dashboard_document,
            source_marker,
            source_sql,
        )
        if source_replacement_count == 0:
            raise ValueError(f"dashboard scaffold contains no source marker {source_marker}")

    for visualization_marker, visualization_path in CUSTOM_VISUALIZATION_FILES.items():
        visualization_replacement_count = _replace_text_marker(
            dashboard_document,
            visualization_marker,
            _load_custom_visualization(visualization_path),
        )
        if visualization_replacement_count > 1:
            raise ValueError(
                f"dashboard scaffold contains duplicate custom visualization marker "
                f"{visualization_marker}"
            )

    generated_dashboard_json = json.dumps(
        dashboard_document,
        ensure_ascii=False,
        indent=2,
    )
    output_path.write_text(generated_dashboard_json + "\n", encoding="utf-8")


def generate_collector_job_resource(generated_resource_path: Path) -> None:
    """Create the scheduled Jobs API collector and dashboard refresh Job."""

    generated_job_resource_document = {
        "resources": {
            "jobs": {
                "workflow_monitoring_jobs_api_collector": {
                    "name": "${var.dashboard_display_name} Jobs API collector",
                    "description": (
                        "Polls configured Lakeflow Jobs into managed Delta tables, then "
                        "refreshes the monitoring dashboard."
                    ),
                    "max_concurrent_runs": 1,
                    "schedule": {
                        "quartz_cron_expression": "0 0/5 * * * ?",
                        "timezone_id": "UTC",
                    },
                    "environments": [
                        {
                            "environment_key": "workflow_monitoring_collector",
                            "spec": {
                                "environment_version": "6",
                                "dependencies": [
                                    "jsonschema==4.26.0",
                                    "pyyaml==6.0.3",
                                ],
                            },
                        }
                    ],
                    "tasks": [
                        {
                            "task_key": "collect_jobs_api_state",
                            "environment_key": "workflow_monitoring_collector",
                            "spark_python_task": {
                                "python_file": "../src/collect_workflow_monitoring_jobs_api.py",
                                "parameters": [
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
                            },
                            "timeout_seconds": 240,
                            "max_retries": 2,
                            "min_retry_interval_millis": 30000,
                        },
                        {
                            "task_key": "refresh_workflow_monitoring_dashboard",
                            "depends_on": [{"task_key": "collect_jobs_api_state"}],
                            "run_if": "ALL_DONE",
                            "dashboard_task": {
                                "dashboard_id": "${resources.dashboards.workflow_monitoring.id}",
                                "warehouse_id": "${var.warehouse_id}",
                            },
                        },
                    ],
                }
            }
        }
    }
    generated_resource_path.parent.mkdir(parents=True, exist_ok=True)
    generated_resource_path.write_text(
        "# Generated by workflow_monitoring_dashboard.py. Do not edit.\n"
        + yaml.safe_dump(generated_job_resource_document, sort_keys=False),
        encoding="utf-8",
    )


def _render_workflow_metadata_sql() -> str:
    job_state_table = JOBS_API_JOB_STATE_TABLE
    return "\n".join(
        (
            "-- Join configured job IDs with Jobs API metadata and collection state.",
            "collected_jobs AS (",
            "  SELECT workspace_id, job_id, job_name, last_collection_attempt_at,",
            "    last_successful_collection_at, last_error_message",
            f"  FROM {job_state_table}",
            "  WHERE workspace_id IN (SELECT DISTINCT workspace_id FROM configured_workflows)",
            "),",
            "monitored_workflows AS (",
            "  SELECT c.*, c.configured_job_id AS job_id,",
            "    COALESCE(collected_job.job_name, CONCAT('Job ', c.configured_job_id)) AS workflow_name,",
            "    CASE",
            "      WHEN collected_job.job_id IS NULL THEN 'Jobs API collection pending'",
            "      WHEN collected_job.last_error_message IS NOT NULL THEN 'Jobs API collection failed'",
            "      WHEN collected_job.last_successful_collection_at < current_timestamp() - INTERVAL 20 MINUTES THEN 'Jobs API data stale'",
            "    END AS configuration_problem",
            "  FROM configured_workflows c",
            "  LEFT JOIN collected_jobs collected_job",
            "    ON c.workspace_id = collected_job.workspace_id",
            "    AND c.configured_job_id = collected_job.job_id",
            "),",
        )
    )


def _load_custom_visualization(visualization_path: Path) -> str:
    """Load one readable Vega-Lite file and serialize it for dashboard JSON."""

    try:
        visualization_document = json.loads(visualization_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"parse custom visualization {visualization_path}: {error}") from error

    if visualization_document.get("data") != {"name": "databricks_query"}:
        raise ValueError(
            f"custom visualization {visualization_path} must read from databricks_query"
        )
    if visualization_document.get("width") != "container":
        raise ValueError(f"custom visualization {visualization_path} must use container width")
    if visualization_document.get("height") != "container":
        raise ValueError(f"custom visualization {visualization_path} must use container height")

    return json.dumps(visualization_document, ensure_ascii=False, separators=(",", ":"))


def _replace_text_marker(document: Any, marker: str, replacement_text: str) -> int:
    """Replace a text marker anywhere inside nested dashboard JSON."""

    replacement_count = 0
    if isinstance(document, dict):
        for key, child_node in document.items():
            if isinstance(child_node, str) and marker in child_node:
                document[key] = child_node.replace(marker, replacement_text)
                replacement_count += 1
            else:
                replacement_count += _replace_text_marker(
                    child_node,
                    marker,
                    replacement_text,
                )
    elif isinstance(document, list):
        for index, child_node in enumerate(document):
            if isinstance(child_node, str) and marker in child_node:
                document[index] = child_node.replace(marker, replacement_text)
                replacement_count += 1
            else:
                replacement_count += _replace_text_marker(
                    child_node,
                    marker,
                    replacement_text,
                )
    return replacement_count


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate workflow monitoring YAML and generate dashboard JSON."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate_parser = commands.add_parser("validate", help="validate workflow monitoring YAML")
    validate_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIGURATION_PATH)
    validate_parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)

    generate_parser = commands.add_parser("generate", help="generate the dashboard JSON")
    generate_parser.add_argument("--scaffold", type=Path, default=DEFAULT_SCAFFOLD_PATH)
    generate_parser.add_argument("--output", type=Path, default=DEFAULT_DASHBOARD_PATH)
    generate_parser.add_argument(
        "--jobs-api-resource-output",
        type=Path,
        default=DEFAULT_JOBS_API_RESOURCE_PATH,
    )
    return parser


def main(command_arguments: list[str] | None = None) -> int:
    """Run one CLI command and print errors once."""

    parsed_arguments = _build_argument_parser().parse_args(command_arguments)
    try:
        if parsed_arguments.command == "validate":
            configuration = load_workflow_monitoring_configuration(
                parsed_arguments.config,
                parsed_arguments.schema,
            )
            print(f"valid configuration: {len(configuration.active_workflows)} active workflows")
            return 0

        generate_dashboard(
            parsed_arguments.scaffold,
            parsed_arguments.output,
        )
        generate_collector_job_resource(parsed_arguments.jobs_api_resource_output)
        print(f"generated dashboard: {parsed_arguments.output}")
        return 0
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
