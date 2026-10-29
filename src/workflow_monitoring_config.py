"""Load and validate the workflow monitoring configuration at run time."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

CONFIGURATION_VERSION = 3
CONFIGURATION_VERSION_2_MESSAGE = (
    "workflow-monitoring.yml uses version 2. Remove workspace_id and jobs_api_config, "
    "set version: 3, and set catalog and schema as bundle variables."
)


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
    """One active workflow read from the configuration."""

    job_id: int
    sla: ServiceLevelAgreement


@dataclass(frozen=True)
class WorkflowMonitoringConfiguration:
    """Validated run-time configuration."""

    active_workflows: tuple[ActiveWorkflow, ...]


def load_workflow_monitoring_configuration(
    configuration_path: Path, schema_path: Path
) -> WorkflowMonitoringConfiguration:
    """Load YAML, apply JSON Schema, then check rules involving multiple fields."""

    try:
        configuration_document = yaml.safe_load(configuration_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"parse workflow monitoring configuration: {error}") from error

    if isinstance(configuration_document, dict) and configuration_document.get("version") == 2:
        raise ValueError(CONFIGURATION_VERSION_2_MESSAGE)

    _validate_configuration_schema(configuration_document, schema_path)

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

    return WorkflowMonitoringConfiguration(active_workflows=tuple(active_workflows))


def workflow_sla_config_rows(
    configuration: WorkflowMonitoringConfiguration,
    workspace_id: str,
    config_loaded_at: datetime,
) -> list[dict[str, Any]]:
    """Build one table row for each active workflow."""

    rows: list[dict[str, Any]] = []
    for configured_order, workflow in enumerate(configuration.active_workflows, start=1):
        first_deadline_date = None
        if workflow.sla.first_deadline_date:
            first_deadline_date = date.fromisoformat(workflow.sla.first_deadline_date)
        rows.append(
            {
                "workspace_id": workspace_id,
                "configured_order": configured_order,
                "configured_job_id": str(workflow.job_id),
                "frequency": workflow.sla.frequency,
                "completion_time": workflow.sla.completion_time,
                "timezone": workflow.sla.timezone,
                "day_of_week": workflow.sla.day_of_week or None,
                "first_deadline_date": first_deadline_date,
                "day_of_month": workflow.sla.day_of_month,
                "config_loaded_at": config_loaded_at,
            }
        )
    return rows


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

    error_lines = [
        f"{_schema_error_path(validation_error)}: {validation_error.message}"
        for validation_error in validation_errors
    ]
    raise ValueError(
        "workflow monitoring configuration does not match schema:\n  " + "\n  ".join(error_lines)
    )


def _schema_error_path(validation_error: Any) -> str:
    path_parts: list[str] = []
    for path_part in validation_error.absolute_path:
        if isinstance(path_part, int):
            if path_parts:
                path_parts[-1] = f"{path_parts[-1]}[{path_part}]"
            else:
                path_parts.append(f"[{path_part}]")
        else:
            path_parts.append(str(path_part))
    return ".".join(path_parts) or "root"


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
