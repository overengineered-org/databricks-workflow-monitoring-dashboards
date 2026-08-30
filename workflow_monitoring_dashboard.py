"""Validate workflow monitoring YAML and generate the Databricks dashboard JSON."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

DEFAULT_CONFIGURATION_PATH = Path("workflow-monitoring.yml")
DEFAULT_SCHEMA_PATH = Path("schema/workflow-monitoring.schema.json")
DEFAULT_SCAFFOLD_PATH = Path("src/dashboards/workflow-monitoring.scaffold.lvdash.json")
DEFAULT_DASHBOARD_PATH = Path("src/dashboards/workflow-monitoring.lvdash.json")
DEFAULT_JOBS_API_RESOURCE_PATH = Path("resources/workflow-monitoring.jobs-api.job.yml")
CONFIGURED_WORKFLOWS_MARKER = "{{CONFIGURED_WORKFLOWS}}"
WORKFLOW_METADATA_MARKER = "{{WORKFLOW_METADATA}}"
COLLECTED_RUNS_TABLE_MARKER = "{{COLLECTED_RUNS_TABLE}}"
AUTOMATIC_STORAGE_CATALOG = "workflow_monitoring"
AUTOMATIC_STORAGE_SCHEMA = "lakeflow_jobs"
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


@dataclass(frozen=True)
class ServiceLevelAgreement:
    """A validated workflow completion schedule."""

    frequency: str
    completion_time: str
    timezone: str
    day_of_week: str = ""
    first_deadline_date: str = ""
    day_of_month: int | None = None


@dataclass(frozen=True)
class ActiveWorkflow:
    """One workflow included in dashboard queries."""

    job_id: int
    sla: ServiceLevelAgreement


@dataclass(frozen=True)
class CollectorStorageConfiguration:
    """Governed storage used by the Jobs API collector."""

    catalog: str
    schema: str
    create_catalog_and_schema_if_missing: bool


@dataclass(frozen=True)
class WorkflowMonitoringConfiguration:
    """Validated values that are safe to place in dashboard SQL."""

    workspace_id: str
    active_workflows: tuple[ActiveWorkflow, ...]
    collector_storage: CollectorStorageConfiguration


def load_workflow_monitoring_configuration(
    configuration_path: Path, schema_path: Path
) -> WorkflowMonitoringConfiguration:
    """Load YAML, apply JSON Schema, then check rules involving multiple fields."""

    try:
        configuration_document = yaml.safe_load(configuration_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"parse workflow monitoring configuration: {error}") from error

    _validate_configuration_schema(configuration_document, schema_path)

    workspace_id = configuration_document["workspace_id"]
    collector_storage_document = configuration_document.get("jobs_api_config")
    if collector_storage_document is None:
        collector_storage = CollectorStorageConfiguration(
            catalog=AUTOMATIC_STORAGE_CATALOG,
            schema=AUTOMATIC_STORAGE_SCHEMA,
            create_catalog_and_schema_if_missing=True,
        )
    else:
        collector_storage = CollectorStorageConfiguration(
            catalog=collector_storage_document["catalog"],
            schema=collector_storage_document["schema"],
            create_catalog_and_schema_if_missing=False,
        )
    default_timezone = configuration_document["default_timezone"]
    _validate_iana_timezone("default_timezone", default_timezone)

    active_workflows: list[ActiveWorkflow] = []
    job_id_locations: dict[int, int] = {}
    for workflow_index, workflow_document in enumerate(configuration_document["workflows"]):
        configured_workflow = _validate_workflow(workflow_document, default_timezone)
        if configured_workflow.job_id in job_id_locations:
            previous_index = job_id_locations[configured_workflow.job_id]
            raise ValueError(
                f"workflows[{workflow_index}]: job_id duplicates workflow "
                f"from workflows[{previous_index}]"
            )
        job_id_locations[configured_workflow.job_id] = workflow_index
        if workflow_document["monitoring_status"] == "active":
            active_workflows.append(configured_workflow)

    return WorkflowMonitoringConfiguration(
        workspace_id=workspace_id,
        active_workflows=tuple(active_workflows),
        collector_storage=collector_storage,
    )


def _validate_configuration_schema(configuration_document: Any, schema_path: Path) -> None:
    """Use the same schema that provides editor completion."""

    try:
        schema_document = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema_document)
    except (json.JSONDecodeError, SchemaError) as error:
        raise ValueError(f"load workflow monitoring schema: {error}") from error

    schema_validator = Draft202012Validator(
        schema_document,
        format_checker=FormatChecker(),
    )
    validation_errors = sorted(
        schema_validator.iter_errors(configuration_document),
        key=lambda error: tuple(str(path_part) for path_part in error.absolute_path),
    )
    if not validation_errors:
        return

    first_error = validation_errors[0]
    required_field_errors = [
        nested_error for nested_error in first_error.context if nested_error.validator == "required"
    ]
    if required_field_errors:
        first_error = required_field_errors[0]
    error_path = ".".join(str(path_part) for path_part in first_error.absolute_path) or "root"
    raise ValueError(
        f"workflow monitoring configuration does not match schema at {error_path}: {first_error.message}"
    )


def _validate_iana_timezone(field_name: str, timezone_name: str) -> None:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f'{field_name} "{timezone_name}" is not an IANA timezone') from error


def _validate_workflow(workflow_document: dict[str, Any], default_timezone: str) -> ActiveWorkflow:
    """Check workflow rules that depend on more than one YAML field."""

    sla_document = workflow_document["sla"]
    timezone_name = sla_document.get("timezone") or default_timezone
    _validate_iana_timezone("sla.timezone", timezone_name)

    completion_time = sla_document["completion_time"]
    _validate_schedule_fields(sla_document)
    return ActiveWorkflow(
        job_id=workflow_document["job_id"],
        sla=ServiceLevelAgreement(
            frequency=sla_document["frequency"],
            completion_time=completion_time,
            timezone=timezone_name,
            day_of_week=sla_document.get("day_of_week", ""),
            first_deadline_date=sla_document.get("first_deadline_date", ""),
            day_of_month=sla_document.get("day_of_month"),
        ),
    )


def _validate_schedule_fields(sla_document: dict[str, Any]) -> None:
    """Reject schedule fields that do not belong to the selected frequency."""

    frequency = sla_document["frequency"]
    day_of_week = sla_document.get("day_of_week", "")
    first_deadline_date = sla_document.get("first_deadline_date", "")
    day_of_month = sla_document.get("day_of_month")

    if frequency == "daily":
        if day_of_week or first_deadline_date or day_of_month is not None:
            raise ValueError("daily SLA only accepts completion_time and timezone")
        return
    if frequency == "weekly":
        if not day_of_week:
            raise ValueError("weekly SLA requires a lowercase day_of_week")
        if first_deadline_date or day_of_month is not None:
            raise ValueError("weekly SLA does not accept first_deadline_date or day_of_month")
        return
    if frequency == "fortnightly":
        if not first_deadline_date:
            raise ValueError("fortnightly SLA requires first_deadline_date")
        if day_of_week or day_of_month is not None:
            raise ValueError("fortnightly SLA does not accept day_of_week or day_of_month")
        return
    if frequency == "monthly":
        if day_of_month is None:
            raise ValueError("monthly SLA requires day_of_month from 1 to 31")
        if day_of_week or first_deadline_date:
            raise ValueError("monthly SLA does not accept day_of_week or first_deadline_date")
        return
    raise ValueError("sla.frequency must be daily, weekly, fortnightly, or monthly")


def generate_dashboard(
    configuration: WorkflowMonitoringConfiguration, scaffold_path: Path, output_path: Path
) -> None:
    """Replace scaffold markers and write deterministic dashboard JSON."""

    try:
        dashboard_document = json.loads(scaffold_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"parse dashboard scaffold: {error}") from error

    configured_workflows_sql = _render_configured_workflows_sql(configuration)
    configured_workflow_replacement_count = _replace_text_marker(
        dashboard_document,
        CONFIGURED_WORKFLOWS_MARKER,
        configured_workflows_sql,
    )
    if configured_workflow_replacement_count == 0:
        raise ValueError("dashboard scaffold contains no configured workflow marker")

    source_markers = {
        WORKFLOW_METADATA_MARKER: _render_workflow_metadata_sql(configuration),
        COLLECTED_RUNS_TABLE_MARKER: _qualified_collector_table(
            configuration, JOBS_API_RUN_STATE_TABLE
        ),
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


def generate_collector_job_resource(
    configuration: WorkflowMonitoringConfiguration,
    generated_resource_path: Path,
) -> None:
    """Create the scheduled Jobs API collector and dashboard refresh Job."""

    monitored_job_ids = [workflow.job_id for workflow in configuration.active_workflows]
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
                        "pause_status": (
                            "UNPAUSED" if configuration.active_workflows else "PAUSED"
                        ),
                    },
                    "tasks": [
                        {
                            "task_key": "collect_jobs_api_state",
                            "notebook_task": {
                                "notebook_path": "../src/collect_workflow_monitoring_jobs_api.py",
                                "base_parameters": {
                                    "catalog_name": configuration.collector_storage.catalog,
                                    "schema_name": configuration.collector_storage.schema,
                                    "create_catalog_and_schema_if_missing": str(
                                        configuration.collector_storage.create_catalog_and_schema_if_missing
                                    ).lower(),
                                    "workspace_id": configuration.workspace_id,
                                    "monitored_job_ids_json": json.dumps(
                                        monitored_job_ids, separators=(",", ":")
                                    ),
                                },
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
        "# Set workspace_id in workflow-monitoring.yml or with the workflow-monitoring CLI.\n"
        + yaml.safe_dump(generated_job_resource_document, sort_keys=False),
        encoding="utf-8",
    )


def _render_workflow_metadata_sql(
    configuration: WorkflowMonitoringConfiguration,
) -> str:
    job_state_table = _qualified_collector_table(configuration, JOBS_API_JOB_STATE_TABLE)
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


def _qualified_collector_table(
    configuration: WorkflowMonitoringConfiguration,
    table_name: str,
) -> str:
    return (
        f"`{configuration.collector_storage.catalog}`."
        f"`{configuration.collector_storage.schema}`.`{table_name}`"
    )


def _render_configured_workflows_sql(configuration: WorkflowMonitoringConfiguration) -> str:
    if not configuration.active_workflows:
        # Typed empty columns keep every dataset valid while all workflows are paused.
        return "\n".join(
            (
                "SELECT CAST(NULL AS INT) AS configured_order,",
                "       CAST(NULL AS STRING) AS workspace_id,",
                "       CAST(NULL AS STRING) AS configured_job_id,",
                "       CAST(NULL AS STRING) AS frequency,",
                "       CAST(NULL AS STRING) AS completion_time,",
                "       CAST(NULL AS STRING) AS timezone,",
                "       CAST(NULL AS STRING) AS day_of_week,",
                "       CAST(NULL AS DATE) AS first_deadline_date,",
                "       CAST(NULL AS INT) AS day_of_month",
                "WHERE false",
            )
        )

    configured_workflow_selects: list[str] = []
    for workflow_index, workflow in enumerate(configuration.active_workflows, start=1):
        job_id_sql = f"CAST('{workflow.job_id}' AS STRING)"

        first_deadline_date_sql = "CAST(NULL AS DATE)"
        if workflow.sla.first_deadline_date:
            first_deadline_date_sql = f"CAST('{workflow.sla.first_deadline_date}' AS DATE)"

        day_of_month_sql = "CAST(NULL AS INT)"
        if workflow.sla.day_of_month is not None:
            day_of_month_sql = f"CAST({workflow.sla.day_of_month} AS INT)"

        configured_workflow_selects.append(
            "SELECT "
            f"{workflow_index} AS configured_order, "
            f"'{_escape_sql_string(configuration.workspace_id)}' AS workspace_id, "
            f"{job_id_sql} AS configured_job_id, "
            f"'{_escape_sql_string(workflow.sla.frequency)}' AS frequency, "
            f"'{_escape_sql_string(workflow.sla.completion_time)}' AS completion_time, "
            f"'{_escape_sql_string(workflow.sla.timezone)}' AS timezone, "
            f"{_nullable_sql_string(workflow.sla.day_of_week)} AS day_of_week, "
            f"{first_deadline_date_sql} AS first_deadline_date, "
            f"{day_of_month_sql} AS day_of_month"
        )
    return "\nUNION ALL\n".join(configured_workflow_selects)


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


def _escape_sql_string(sql_text: str) -> str:
    """Prevent configured text from ending a SQL string literal."""

    return sql_text.replace("'", "''")


def _nullable_sql_string(optional_text: str) -> str:
    if not optional_text:
        return "CAST(NULL AS STRING)"
    return f"'{_escape_sql_string(optional_text)}'"


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate workflow monitoring YAML and generate dashboard JSON."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate_parser = commands.add_parser("validate", help="validate workflow monitoring YAML")
    validate_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIGURATION_PATH)
    validate_parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)

    generate_parser = commands.add_parser("generate", help="generate the dashboard JSON")
    generate_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIGURATION_PATH)
    generate_parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
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
        configuration = load_workflow_monitoring_configuration(
            parsed_arguments.config,
            parsed_arguments.schema,
        )
        if parsed_arguments.command == "validate":
            print(f"valid configuration: {len(configuration.active_workflows)} active workflows")
            return 0

        generate_dashboard(
            configuration,
            parsed_arguments.scaffold,
            parsed_arguments.output,
        )
        generate_collector_job_resource(
            configuration,
            parsed_arguments.jobs_api_resource_output,
        )
        print(
            f"generated dashboard: {parsed_arguments.output} "
            f"({len(configuration.active_workflows)} active workflows)"
        )
        return 0
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
