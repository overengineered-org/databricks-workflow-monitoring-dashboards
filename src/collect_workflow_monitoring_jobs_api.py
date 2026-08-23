# Databricks notebook source
"""Poll configured Lakeflow Jobs into two governed managed Delta tables."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from databricks.sdk import WorkspaceClient
from delta.tables import DeltaTable
from pyspark.sql import Row
from pyspark.sql.types import (
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

# Databricks injects these notebook globals at runtime.
spark: Any = globals()["spark"]
dbutils: Any = globals()["dbutils"]

RUN_STATE_TABLE_NAME = "workflow_run_api_state"
COLLECTION_STATUS_TABLE_NAME = "workflow_api_collection_status"
JOBS_API_RETENTION_DAYS = 60
INCREMENTAL_COLLECTION_OVERLAP_MINUTES = 15
UNITY_CATALOG_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
TERMINAL_STATE_ALIASES = {
    "SUCCESS": "SUCCEEDED",
    "CANCELED": "CANCELLED",
    "TIMEDOUT": "TIMED_OUT",
}

RUN_STATE_SCHEMA = StructType(
    [
        StructField("workspace_id", StringType(), False),
        StructField("job_id", StringType(), False),
        StructField("run_id", LongType(), False),
        StructField("period_start_time", TimestampType(), False),
        StructField("period_end_time", TimestampType(), True),
        StructField("result_state", StringType(), False),
        StructField("trigger_type", StringType(), True),
        StructField("run_page_url", StringType(), True),
        StructField("collected_at", TimestampType(), False),
    ]
)

COLLECTION_STATUS_SCHEMA = StructType(
    [
        StructField("workspace_id", StringType(), False),
        StructField("job_id", StringType(), False),
        StructField("name", StringType(), False),
        StructField("delete_time", TimestampType(), True),
        StructField("change_time", TimestampType(), False),
        StructField("collection_state", StringType(), False),
        StructField("last_successful_collection_time", TimestampType(), True),
        StructField("last_error_message", StringType(), True),
    ]
)


def _validated_unity_catalog_identifier(identifier_name: str, identifier_value: str) -> str:
    if not UNITY_CATALOG_IDENTIFIER_PATTERN.fullmatch(identifier_value):
        raise ValueError(f"{identifier_name} is not a supported Unity Catalog identifier")
    return identifier_value


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
    legacy_run_state = getattr(job_run, "state", None)
    legacy_result_state = _enum_text(getattr(legacy_run_state, "result_state", None))
    if legacy_result_state:
        return TERMINAL_STATE_ALIASES.get(legacy_result_state, legacy_result_state)

    current_run_status = getattr(job_run, "status", None)
    current_status_state = _enum_text(getattr(current_run_status, "state", None))
    if current_status_state:
        return TERMINAL_STATE_ALIASES.get(current_status_state, current_status_state)

    legacy_lifecycle_state = _enum_text(getattr(legacy_run_state, "life_cycle_state", None))
    return TERMINAL_STATE_ALIASES.get(legacy_lifecycle_state, legacy_lifecycle_state) or "UNKNOWN"


def _sanitized_error_message(collection_error: Exception) -> str:
    return " ".join(str(collection_error).split())[:500]


def _create_governed_storage(catalog_name: str, schema_name: str) -> None:
    qualified_schema_name = f"{_quoted_identifier(catalog_name)}.{_quoted_identifier(schema_name)}"
    visible_catalog_names = {
        catalog_row.catalog for catalog_row in spark.sql("SHOW CATALOGS").collect()
    }
    if catalog_name not in visible_catalog_names:
        spark.sql(
            f"CREATE CATALOG {_quoted_identifier(catalog_name)} "
            "COMMENT 'Governed workflow monitoring data'"
        )
    visible_schema_names = {
        schema_row.databaseName
        for schema_row in spark.sql(f"SHOW SCHEMAS IN {_quoted_identifier(catalog_name)}").collect()
    }
    if schema_name not in visible_schema_names:
        spark.sql(
            f"CREATE SCHEMA {qualified_schema_name} COMMENT 'Lakeflow Jobs API monitoring state'"
        )
    spark.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {_qualified_table_name(catalog_name, schema_name, RUN_STATE_TABLE_NAME)} (
          workspace_id STRING NOT NULL COMMENT 'Databricks workspace ID',
          job_id STRING NOT NULL COMMENT 'Lakeflow Job ID',
          run_id BIGINT NOT NULL COMMENT 'Lakeflow Job run ID',
          period_start_time TIMESTAMP NOT NULL COMMENT 'Run start time in UTC',
          period_end_time TIMESTAMP COMMENT 'Terminal run end time in UTC',
          result_state STRING NOT NULL COMMENT 'Current normalized run state',
          trigger_type STRING COMMENT 'Run trigger type',
          run_page_url STRING COMMENT 'Workspace run details URL',
          collected_at TIMESTAMP NOT NULL COMMENT 'Latest API observation time in UTC'
        ) USING DELTA
        COMMENT 'Latest Jobs API state for each monitored workflow run'
        """
    )
    spark.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {_qualified_table_name(catalog_name, schema_name, COLLECTION_STATUS_TABLE_NAME)} (
          workspace_id STRING NOT NULL COMMENT 'Databricks workspace ID',
          job_id STRING NOT NULL COMMENT 'Lakeflow Job ID',
          name STRING NOT NULL COMMENT 'Current Lakeflow Job name',
          delete_time TIMESTAMP COMMENT 'Reserved compatibility field for dashboard resolution',
          change_time TIMESTAMP NOT NULL COMMENT 'Latest collection attempt time in UTC',
          collection_state STRING NOT NULL COMMENT 'Latest collection outcome',
          last_successful_collection_time TIMESTAMP COMMENT 'Latest successful collection time in UTC',
          last_error_message STRING COMMENT 'Sanitized latest collection error'
        ) USING DELTA
        COMMENT 'Latest Jobs API collection health for each monitored workflow'
        """
    )


def _merge_run_state_rows(
    qualified_table_name: str,
    run_state_rows: list[Row],
) -> None:
    if not run_state_rows:
        return
    run_state_dataframe = spark.createDataFrame(run_state_rows, RUN_STATE_SCHEMA)
    (
        DeltaTable.forName(spark, qualified_table_name)
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


def _merge_collection_status_rows(
    qualified_table_name: str,
    collection_status_rows: list[Row],
) -> None:
    if not collection_status_rows:
        return
    collection_status_dataframe = spark.createDataFrame(
        collection_status_rows,
        COLLECTION_STATUS_SCHEMA,
    )
    (
        DeltaTable.forName(spark, qualified_table_name)
        .alias("stored")
        .merge(
            collection_status_dataframe.alias("incoming"),
            "stored.workspace_id = incoming.workspace_id AND stored.job_id = incoming.job_id",
        )
        .whenMatchedUpdate(
            set={
                "name": "incoming.name",
                "delete_time": "incoming.delete_time",
                "change_time": "incoming.change_time",
                "collection_state": "incoming.collection_state",
                "last_successful_collection_time": (
                    "COALESCE(incoming.last_successful_collection_time, "
                    "stored.last_successful_collection_time)"
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
    collected_at: datetime,
) -> Row:
    run_start_time = _timestamp_from_epoch_milliseconds(getattr(job_run, "start_time", None))
    return Row(
        workspace_id=workspace_id,
        job_id=str(job_id),
        run_id=int(job_run.run_id),
        period_start_time=run_start_time or collected_at,
        period_end_time=_timestamp_from_epoch_milliseconds(getattr(job_run, "end_time", None)),
        result_state=_normalized_run_state(job_run),
        trigger_type=_enum_text(getattr(job_run, "trigger", None)) or None,
        run_page_url=getattr(job_run, "run_page_url", None),
        collected_at=collected_at,
    )


def _last_successful_collection_by_job(
    catalog_name: str,
    schema_name: str,
) -> dict[str, datetime]:
    collection_status_table = _qualified_table_name(
        catalog_name,
        schema_name,
        COLLECTION_STATUS_TABLE_NAME,
    )
    return {
        collection_status_row.job_id: collection_status_row.last_successful_collection_time
        for collection_status_row in spark.table(collection_status_table)
        .select("job_id", "last_successful_collection_time")
        .where("last_successful_collection_time IS NOT NULL")
        .collect()
    }


def main() -> None:
    catalog_name = _validated_unity_catalog_identifier(
        "catalog_name", dbutils.widgets.get("catalog_name")
    )
    schema_name = _validated_unity_catalog_identifier(
        "schema_name", dbutils.widgets.get("schema_name")
    )
    workspace_id = dbutils.widgets.get("workspace_id")
    monitored_jobs = json.loads(dbutils.widgets.get("monitored_jobs_json"))
    if not isinstance(monitored_jobs, list):
        raise ValueError("monitored_jobs_json must contain a list")

    _create_governed_storage(catalog_name, schema_name)
    jobs_api_client = WorkspaceClient()
    collected_at = datetime.now(UTC).replace(tzinfo=None)
    retention_start_datetime = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=JOBS_API_RETENTION_DAYS
    )
    last_successful_collection_by_job = _last_successful_collection_by_job(
        catalog_name,
        schema_name,
    )
    collected_run_rows: list[Row] = []
    collection_status_rows: list[Row] = []
    failed_job_ids: list[int] = []

    for monitored_job in monitored_jobs:
        job_id = int(monitored_job["job_id"])
        configured_job_name = str(monitored_job["job_name"])
        try:
            job_settings = jobs_api_client.jobs.get(job_id=job_id).settings
            current_job_name = job_settings.name or configured_job_name
            previous_success_time = last_successful_collection_by_job.get(str(job_id))
            incremental_start_datetime = retention_start_datetime
            if previous_success_time is not None:
                incremental_start_datetime = max(
                    retention_start_datetime,
                    previous_success_time
                    - timedelta(minutes=INCREMENTAL_COLLECTION_OVERLAP_MINUTES),
                )
            recent_job_runs = jobs_api_client.jobs.list_runs(
                job_id=job_id,
                start_time_from=int(
                    incremental_start_datetime.replace(tzinfo=UTC).timestamp() * 1000
                ),
                expand_tasks=False,
            )
            active_job_runs = jobs_api_client.jobs.list_runs(
                job_id=job_id,
                active_only=True,
                expand_tasks=False,
            )
            job_runs_by_id = {
                int(job_run.run_id): job_run for job_run in (*recent_job_runs, *active_job_runs)
            }
            collected_run_rows.extend(
                _run_state_row(workspace_id, job_id, job_run, collected_at)
                for job_run in job_runs_by_id.values()
            )
            collection_status_rows.append(
                Row(
                    workspace_id=workspace_id,
                    job_id=str(job_id),
                    name=current_job_name,
                    delete_time=None,
                    change_time=collected_at,
                    collection_state="SUCCEEDED",
                    last_successful_collection_time=collected_at,
                    last_error_message=None,
                )
            )
        except Exception as collection_error:
            failed_job_ids.append(job_id)
            collection_status_rows.append(
                Row(
                    workspace_id=workspace_id,
                    job_id=str(job_id),
                    name=configured_job_name,
                    delete_time=None,
                    change_time=collected_at,
                    collection_state="FAILED",
                    last_successful_collection_time=None,
                    last_error_message=_sanitized_error_message(collection_error),
                )
            )

    _merge_run_state_rows(
        _qualified_table_name(catalog_name, schema_name, RUN_STATE_TABLE_NAME),
        collected_run_rows,
    )
    _merge_collection_status_rows(
        _qualified_table_name(catalog_name, schema_name, COLLECTION_STATUS_TABLE_NAME),
        collection_status_rows,
    )
    if failed_job_ids:
        failed_job_id_text = ", ".join(str(job_id) for job_id in failed_job_ids)
        raise RuntimeError(f"Jobs API collection failed for job IDs: {failed_job_id_text}")


main()
