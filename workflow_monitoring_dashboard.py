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
CONFIGURED_WORKFLOWS_MARKER = "{{CONFIGURED_WORKFLOWS}}"
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

    job_name: str
    job_id: int | None
    sla: ServiceLevelAgreement


@dataclass(frozen=True)
class WorkflowMonitoringConfiguration:
    """Validated values that are safe to place in dashboard SQL."""

    workspace_id: str
    active_workflows: tuple[ActiveWorkflow, ...]


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
    default_timezone = configuration_document["default_timezone"]
    _validate_iana_timezone("default_timezone", default_timezone)

    active_workflows: list[ActiveWorkflow] = []
    active_job_name_locations: dict[str, int] = {}
    active_job_id_locations: dict[int, int] = {}
    for workflow_index, workflow_document in enumerate(configuration_document["workflows"]):
        # Inactive entries intentionally skip active-workflow field validation.
        if workflow_document["monitoring_status"] == "inactive":
            continue

        active_workflow = _validate_active_workflow(workflow_document, default_timezone)
        if active_workflow.job_name in active_job_name_locations:
            previous_index = active_job_name_locations[active_workflow.job_name]
            raise ValueError(
                f"workflows[{workflow_index}]: job_name duplicates active workflow "
                f"from workflows[{previous_index}]"
            )
        active_job_name_locations[active_workflow.job_name] = workflow_index

        if active_workflow.job_id is not None:
            if active_workflow.job_id in active_job_id_locations:
                previous_index = active_job_id_locations[active_workflow.job_id]
                raise ValueError(
                    f"workflows[{workflow_index}]: job_id duplicates active workflow "
                    f"from workflows[{previous_index}]"
                )
            active_job_id_locations[active_workflow.job_id] = workflow_index
        active_workflows.append(active_workflow)

    return WorkflowMonitoringConfiguration(
        workspace_id=workspace_id,
        active_workflows=tuple(active_workflows),
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
    error_path = ".".join(str(path_part) for path_part in first_error.absolute_path) or "root"
    raise ValueError(
        f"workflow monitoring configuration does not match schema at {error_path}: {first_error.message}"
    )


def _validate_iana_timezone(field_name: str, timezone_name: str) -> None:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f'{field_name} "{timezone_name}" is not an IANA timezone') from error


def _validate_active_workflow(
    workflow_document: dict[str, Any], default_timezone: str
) -> ActiveWorkflow:
    """Check active-workflow rules that depend on more than one YAML field."""

    job_name = workflow_document["job_name"].strip()
    if not job_name:
        raise ValueError("job_name is required")

    sla_document = workflow_document["sla"]
    timezone_name = sla_document.get("timezone") or default_timezone
    _validate_iana_timezone("sla.timezone", timezone_name)

    completion_time = sla_document["completion_time"]
    _validate_schedule_fields(sla_document)
    return ActiveWorkflow(
        job_name=job_name,
        job_id=workflow_document.get("job_id"),
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


def _render_configured_workflows_sql(configuration: WorkflowMonitoringConfiguration) -> str:
    if not configuration.active_workflows:
        # Typed empty columns keep every dataset valid while all workflows are paused.
        return "\n".join(
            (
                "SELECT CAST(NULL AS INT) AS configured_order,",
                "       CAST(NULL AS STRING) AS workspace_id,",
                "       CAST(NULL AS STRING) AS configured_job_name,",
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
        job_id_sql = "CAST(NULL AS STRING)"
        if workflow.job_id is not None:
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
            f"'{_escape_sql_string(workflow.job_name)}' AS configured_job_name, "
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
