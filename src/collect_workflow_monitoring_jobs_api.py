# Databricks notebook source
"""Poll configured Lakeflow Jobs into two governed managed Delta tables."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

RUN_STATE_TABLE_NAME = "workflow_run_api_state"
JOB_STATE_TABLE_NAME = "workflow_job_api_state"
JOBS_API_RETENTION_DAYS = 60
INCREMENTAL_COLLECTION_OVERLAP_MINUTES = 15
UNITY_CATALOG_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
WORKSPACE_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
SUCCESS_TERMINATION_CODES = {"SUCCESS"}
CANCELLED_TERMINATION_CODES = {"CANCELED", "USER_CANCELED"}
SKIPPED_TERMINATION_CODES = {
    "DISABLED",
    "SKIPPED",
    "MAX_CONCURRENT_RUNS_EXCEEDED",
    "MAX_JOB_QUEUE_SIZE_EXCEEDED",
}
TERMINAL_RUN_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED"}


def _run_state_schema() -> Any:
    from pyspark.sql.types import (  # type: ignore[import-not-found]
        LongType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    return StructType(
        [
            StructField("workspace_id", StringType(), False),
            StructField("job_id", StringType(), False),
            StructField("run_id", LongType(), False),
            StructField("period_start_time", TimestampType(), False),
            StructField("period_end_time", TimestampType(), True),
            StructField("result_state", StringType(), False),
        ]
    )


def _job_state_schema() -> Any:
    from pyspark.sql.types import (  # type: ignore[import-not-found]
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    return StructType(
        [
            StructField("workspace_id", StringType(), False),
            StructField("job_id", StringType(), False),
            StructField("job_name", StringType(), True),
            StructField("last_collection_attempt_at", TimestampType(), False),
            StructField("last_successful_collection_at", TimestampType(), True),
            StructField("last_error_message", StringType(), True),
        ]
    )


def _validated_unity_catalog_identifier(identifier_name: str, identifier_value: str) -> str:
    if not UNITY_CATALOG_IDENTIFIER_PATTERN.fullmatch(identifier_value):
        raise ValueError(f"{identifier_name} is not a supported Unity Catalog identifier")
    return identifier_value


def _validated_workspace_id(workspace_id: str) -> str:
    if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise ValueError("workspace_id must contain only digits and cannot start with zero")
    return workspace_id


def _quoted_identifier(identifier_value: str) -> str:
    return f"`{identifier_value}`"


def _qualified_table_name(catalog_name: str, schema_name: str, table_name: str) -> str:
    return ".".join(
        _quoted_identifier(identifier_part)
        for identifier_part in (catalog_name, schema_name, table_name)
    )


def _timestamp_from_epoch_milliseconds(
    epoch_milliseconds: int | None,
) -> datetime | None:
    if not epoch_milliseconds:
        return None
    return datetime.fromtimestamp(epoch_milliseconds / 1000, tz=UTC).replace(tzinfo=None)


def _enum_text(enum_value: Any) -> str:
    if enum_value is None:
        return ""
    return str(getattr(enum_value, "value", enum_value)).upper()


def _normalized_run_state(job_run: Any) -> str:
    """Map the current Jobs API lifecycle and termination detail to dashboard states."""

    run_status = getattr(job_run, "status", None)
    lifecycle_state = _enum_text(getattr(run_status, "state", None))
    if not lifecycle_state:
        return "UNKNOWN"
    if lifecycle_state == "INTERNAL_ERROR":
        return "FAILED"
    if lifecycle_state != "TERMINATED":
        return lifecycle_state

    termination_details = getattr(run_status, "termination_details", None)
    termination_code = _enum_text(getattr(termination_details, "code", None))
    if termination_code in SUCCESS_TERMINATION_CODES:
        return "SUCCEEDED"
    if termination_code in CANCELLED_TERMINATION_CODES:
        return "CANCELLED"
    if termination_code in SKIPPED_TERMINATION_CODES:
        return "SKIPPED"
    return "FAILED"


def _sanitized_error_message(collection_error: Exception) -> str:
    return " ".join(str(collection_error).split())[:500]


def _job_name_from_job_details(job_id: int, job_details: Any) -> str:
    job_name = str(getattr(getattr(job_details, "settings", None), "name", "")).strip()
    if not job_name:
        raise ValueError(f"Jobs API returned no name for job ID {job_id}")
    return job_name


def _prepare_collector_storage(
    spark_session: Any,
    catalog_name: str,
    schema_name: str,
    create_catalog_and_schema_if_missing: bool,
) -> None:
    if create_catalog_and_schema_if_missing:
        qualified_schema_name = (
            f"{_quoted_identifier(catalog_name)}.{_quoted_identifier(schema_name)}"
        )
        spark_session.sql(
            f"CREATE CATALOG IF NOT EXISTS {_quoted_identifier(catalog_name)} "
            "COMMENT 'Governed workflow monitoring data'"
        )
        spark_session.sql(
            f"CREATE SCHEMA IF NOT EXISTS {qualified_schema_name} "
            "COMMENT 'Lakeflow Jobs API monitoring state'"
        )
    spark_session.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {_qualified_table_name(catalog_name, schema_name, RUN_STATE_TABLE_NAME)} (
          workspace_id STRING NOT NULL COMMENT 'Databricks workspace ID',
          job_id STRING NOT NULL COMMENT 'Lakeflow Job ID',
          run_id BIGINT NOT NULL COMMENT 'Lakeflow Job run ID',
          period_start_time TIMESTAMP NOT NULL COMMENT 'Run start time in UTC',
          period_end_time TIMESTAMP COMMENT 'Terminal run end time in UTC',
          result_state STRING NOT NULL COMMENT 'Current normalized run state'
        ) USING DELTA
        COMMENT 'Latest Jobs API state for each monitored workflow run'
        """
    )
    spark_session.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {_qualified_table_name(catalog_name, schema_name, JOB_STATE_TABLE_NAME)} (
          workspace_id STRING NOT NULL COMMENT 'Databricks workspace ID',
          job_id STRING NOT NULL COMMENT 'Lakeflow Job ID',
          job_name STRING COMMENT 'Latest Lakeflow Job name returned by the Jobs API',
          last_collection_attempt_at TIMESTAMP NOT NULL COMMENT 'Latest collection attempt in UTC',
          last_successful_collection_at TIMESTAMP COMMENT 'Latest successful collection in UTC',
          last_error_message STRING COMMENT 'Sanitized latest collection error'
        ) USING DELTA
        COMMENT 'Latest Jobs API metadata and collection state for each monitored workflow'
        """
    )


def _merge_run_state_rows(
    spark_session: Any,
    qualified_table_name: str,
    run_state_rows: list[dict[str, Any]],
) -> None:
    if not run_state_rows:
        return
    from delta.tables import DeltaTable  # type: ignore[import-not-found]

    run_state_dataframe = spark_session.createDataFrame(run_state_rows, _run_state_schema())
    (
        DeltaTable.forName(spark_session, qualified_table_name)
        .alias("stored")
        .merge(
            run_state_dataframe.alias("incoming"),
            "stored.workspace_id = incoming.workspace_id "
            "AND stored.job_id = incoming.job_id AND stored.run_id = incoming.run_id",
        )
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )


def _merge_job_state_rows(
    spark_session: Any,
    qualified_table_name: str,
    job_state_rows: list[dict[str, Any]],
) -> None:
    if not job_state_rows:
        return
    from delta.tables import DeltaTable  # type: ignore[import-not-found]

    job_state_dataframe = spark_session.createDataFrame(
        job_state_rows,
        _job_state_schema(),
    )
    (
        DeltaTable.forName(spark_session, qualified_table_name)
        .alias("stored")
        .merge(
            job_state_dataframe.alias("incoming"),
            "stored.workspace_id = incoming.workspace_id AND stored.job_id = incoming.job_id",
        )
        .whenMatchedUpdate(
            set={
                "job_name": "COALESCE(incoming.job_name, stored.job_name)",
                "last_collection_attempt_at": "incoming.last_collection_attempt_at",
                "last_successful_collection_at": (
                    "COALESCE(incoming.last_successful_collection_at, "
                    "stored.last_successful_collection_at)"
                ),
                "last_error_message": "incoming.last_error_message",
            }
        )
        .whenNotMatchedInsertAll()
        .execute()
    )


def _run_state_row(
    workspace_id: str,
    job_id: int,
    job_run: Any,
    collection_attempt_at: datetime,
) -> dict[str, Any]:
    run_start_time = _timestamp_from_epoch_milliseconds(getattr(job_run, "start_time", None))
    return {
        "workspace_id": workspace_id,
        "job_id": str(job_id),
        "run_id": int(job_run.run_id),
        "period_start_time": run_start_time or collection_attempt_at,
        "period_end_time": _timestamp_from_epoch_milliseconds(getattr(job_run, "end_time", None)),
        "result_state": _normalized_run_state(job_run),
    }


def _last_successful_collection_by_workspace_and_job(
    spark_session: Any,
    catalog_name: str,
    schema_name: str,
    workspace_id: str,
) -> dict[tuple[str, str], datetime]:
    job_state_table = _qualified_table_name(
        catalog_name,
        schema_name,
        JOB_STATE_TABLE_NAME,
    )
    checkpoint_rows = (
        spark_session.table(job_state_table)
        .select("workspace_id", "job_id", "last_successful_collection_at")
        .where(f"workspace_id = '{workspace_id}'")
        .where("last_successful_collection_at IS NOT NULL")
        .collect()
    )
    return {
        (checkpoint_row.workspace_id, checkpoint_row.job_id): (
            checkpoint_row.last_successful_collection_at
        )
        for checkpoint_row in checkpoint_rows
    }


def _nonterminal_run_ids_by_job(
    spark_session: Any,
    catalog_name: str,
    schema_name: str,
    workspace_id: str,
    monitored_job_ids: list[int],
    api_retention_start_at: datetime,
) -> dict[int, set[int]]:
    if not monitored_job_ids:
        return {}

    run_state_table = _qualified_table_name(
        catalog_name,
        schema_name,
        RUN_STATE_TABLE_NAME,
    )
    monitored_job_id_sql = ", ".join(f"'{job_id}'" for job_id in monitored_job_ids)
    terminal_state_sql = ", ".join(f"'{run_state}'" for run_state in sorted(TERMINAL_RUN_STATES))
    api_retention_start_sql = api_retention_start_at.isoformat(sep=" ")
    nonterminal_run_rows = (
        spark_session.table(run_state_table)
        .select("job_id", "run_id", "period_start_time")
        .where(f"workspace_id = '{workspace_id}'")
        .where(f"job_id IN ({monitored_job_id_sql})")
        .where(f"period_start_time >= TIMESTAMP '{api_retention_start_sql}'")
        .where(f"result_state NOT IN ({terminal_state_sql})")
        .collect()
    )
    nonterminal_run_ids_by_job: dict[int, set[int]] = {}
    for nonterminal_run_row in nonterminal_run_rows:
        nonterminal_run_ids_by_job.setdefault(int(nonterminal_run_row.job_id), set()).add(
            int(nonterminal_run_row.run_id)
        )
    return nonterminal_run_ids_by_job


def main() -> None:
    from databricks.sdk import WorkspaceClient  # type: ignore[import-not-found]

    spark_session = globals()["spark"]
    notebook_utilities = globals()["dbutils"]
    catalog_name = _validated_unity_catalog_identifier(
        "catalog_name", notebook_utilities.widgets.get("catalog_name")
    )
    schema_name = _validated_unity_catalog_identifier(
        "schema_name", notebook_utilities.widgets.get("schema_name")
    )
    create_catalog_and_schema_if_missing_value = notebook_utilities.widgets.get(
        "create_catalog_and_schema_if_missing"
    )
    if create_catalog_and_schema_if_missing_value not in {"true", "false"}:
        raise ValueError("create_catalog_and_schema_if_missing must be true or false")
    create_catalog_and_schema_if_missing = create_catalog_and_schema_if_missing_value == "true"
    workspace_id = _validated_workspace_id(notebook_utilities.widgets.get("workspace_id"))
    monitored_job_ids = json.loads(notebook_utilities.widgets.get("monitored_job_ids_json"))
    if not isinstance(monitored_job_ids, list) or any(
        isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0
        for job_id in monitored_job_ids
    ):
        raise ValueError("monitored_job_ids_json must contain positive integer job IDs")

    _prepare_collector_storage(
        spark_session,
        catalog_name,
        schema_name,
        create_catalog_and_schema_if_missing,
    )
    jobs_api_client = WorkspaceClient()
    collection_attempt_at = datetime.now(UTC).replace(tzinfo=None)
    retention_start_at = collection_attempt_at - timedelta(days=JOBS_API_RETENTION_DAYS)
    last_success_by_workspace_and_job = _last_successful_collection_by_workspace_and_job(
        spark_session,
        catalog_name,
        schema_name,
        workspace_id,
    )
    nonterminal_run_ids_by_job = _nonterminal_run_ids_by_job(
        spark_session,
        catalog_name,
        schema_name,
        workspace_id,
        monitored_job_ids,
        retention_start_at,
    )
    collected_run_rows: list[dict[str, Any]] = []
    job_state_rows: list[dict[str, Any]] = []
    failed_job_ids: list[int] = []

    for monitored_job_id in monitored_job_ids:
        job_id = int(monitored_job_id)
        job_name: str | None = None
        try:
            job_name = _job_name_from_job_details(
                job_id,
                jobs_api_client.jobs.get(job_id=job_id),
            )
            previous_success_at = last_success_by_workspace_and_job.get((workspace_id, str(job_id)))
            incremental_start_at = retention_start_at
            if previous_success_at is not None:
                incremental_start_at = max(
                    retention_start_at,
                    previous_success_at - timedelta(minutes=INCREMENTAL_COLLECTION_OVERLAP_MINUTES),
                )
            job_runs_by_id = {
                int(job_run.run_id): job_run
                for job_run in jobs_api_client.jobs.list_runs(
                    job_id=job_id,
                    start_time_from=int(
                        incremental_start_at.replace(tzinfo=UTC).timestamp() * 1000
                    ),
                    expand_tasks=False,
                )
            }
            job_runs_by_id.update(
                {
                    int(job_run.run_id): job_run
                    for job_run in jobs_api_client.jobs.list_runs(
                        job_id=job_id,
                        active_only=True,
                        expand_tasks=False,
                    )
                }
            )
            job_runs_by_id.update(
                {
                    stored_run_id: jobs_api_client.jobs.get_run(run_id=stored_run_id)
                    for stored_run_id in nonterminal_run_ids_by_job.get(job_id, set())
                }
            )
            collected_run_rows.extend(
                _run_state_row(workspace_id, job_id, job_run, collection_attempt_at)
                for job_run in job_runs_by_id.values()
            )
            job_state_rows.append(
                {
                    "workspace_id": workspace_id,
                    "job_id": str(job_id),
                    "job_name": job_name,
                    "last_collection_attempt_at": collection_attempt_at,
                    "last_successful_collection_at": collection_attempt_at,
                    "last_error_message": None,
                }
            )
        except Exception as collection_error:
            failed_job_ids.append(job_id)
            job_state_rows.append(
                {
                    "workspace_id": workspace_id,
                    "job_id": str(job_id),
                    "job_name": job_name,
                    "last_collection_attempt_at": collection_attempt_at,
                    "last_successful_collection_at": None,
                    "last_error_message": _sanitized_error_message(collection_error),
                }
            )

    _merge_run_state_rows(
        spark_session,
        _qualified_table_name(catalog_name, schema_name, RUN_STATE_TABLE_NAME),
        collected_run_rows,
    )
    _merge_job_state_rows(
        spark_session,
        _qualified_table_name(catalog_name, schema_name, JOB_STATE_TABLE_NAME),
        job_state_rows,
    )
    if failed_job_ids:
        failed_job_id_text = ", ".join(str(job_id) for job_id in failed_job_ids)
        raise RuntimeError(f"Jobs API collection failed for job IDs: {failed_job_id_text}")


if __name__ == "__main__":
    main()
