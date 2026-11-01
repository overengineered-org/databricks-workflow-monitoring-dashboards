"""Build the Databricks dashboard JSON from the scaffold, dataset SQL, and Vega files."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from string import Template
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCAFFOLD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.scaffold.lvdash.json"
DEFAULT_DASHBOARD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.lvdash.json"
DATASET_SQL_FILES = {
    "workflow_status": REPOSITORY_ROOT / "src/dashboards/sql/workflow_status.sql",
    "run_history": REPOSITORY_ROOT / "src/dashboards/sql/run_history.sql",
    "sla_history": REPOSITORY_ROOT / "src/dashboards/sql/sla_history.sql",
}
FRAGMENT_FILES = {
    "weekday_number": REPOSITORY_ROOT / "src/dashboards/sql/fragments/weekday_number.sql",
    "deadline_dates": REPOSITORY_ROOT / "src/dashboards/sql/fragments/deadline_dates.sql",
    "deadline_instant": REPOSITORY_ROOT / "src/dashboards/sql/fragments/deadline_instant.sql",
    "deadline_status": REPOSITORY_ROOT / "src/dashboards/sql/fragments/deadline_status.sql",
}
DEADLINE_DATE_WINDOWS = {
    "workflow_status": {
        "daily_window_start_days": 2,
        "daily_window_end_days": 1,
        "weekly_anchor_date": "date_add(local_today, -pmod(dayofweek(local_today) - target_day, 7))",
        "weekly_offset_start": -2,
        "weekly_offset_end": 1,
        "fortnightly_offset_start": -2,
        "fortnightly_offset_end": 1,
        "monthly_offset_start": -2,
        "monthly_offset_end": 1,
    },
    "sla_history": {
        "daily_window_start_days": 61,
        "daily_window_end_days": 31,
        "weekly_anchor_date": (
            "date_add(date_sub(local_today, 61), pmod(target_day - dayofweek(date_sub(local_today, 61)), 7))"
        ),
        "weekly_offset_start": -1,
        "weekly_offset_end": 14,
        "fortnightly_offset_start": -5,
        "fortnightly_offset_end": 3,
        "monthly_offset_start": -3,
        "monthly_offset_end": 2,
    },
}
CUSTOM_VISUALIZATION_FILES = {
    "{{SLA_DELIVERY_CALENDAR_VEGA}}": (
        REPOSITORY_ROOT / "src/visualizations/sla-delivery-calendar.vega.json"
    ),
    "{{DELIVERY_MARGIN_TIMELINE_VEGA}}": (
        REPOSITORY_ROOT / "src/visualizations/delivery-margin-timeline.vega.json"
    ),
    "{{WORKFLOW_RISK_MAP_VEGA}}": (
        REPOSITORY_ROOT / "src/visualizations/workflow-risk-map.vega.json"
    ),
    "{{RUN_DURATION_LANES_VEGA}}": (
        REPOSITORY_ROOT / "src/visualizations/run-duration-lanes.vega.json"
    ),
}


def build_dashboard(scaffold_path: Path, output_path: Path) -> None:
    """Write the deterministic dashboard JSON built from the scaffold, SQL, and Vega files."""

    try:
        dashboard_document = json.loads(scaffold_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"parse dashboard scaffold: {error}") from error

    _inject_dataset_query_lines(dashboard_document)

    for visualization_marker, visualization_path in CUSTOM_VISUALIZATION_FILES.items():
        visualization_replacement_count = _replace_text_marker(
            dashboard_document,
            visualization_marker,
            _load_custom_visualization(visualization_path),
        )
        if visualization_replacement_count == 0:
            raise ValueError(
                f"dashboard scaffold is missing custom visualization marker {visualization_marker}"
            )
        if visualization_replacement_count > 1:
            raise ValueError(
                f"dashboard scaffold contains duplicate custom visualization marker "
                f"{visualization_marker}"
            )

    built_dashboard_json = json.dumps(
        dashboard_document,
        ensure_ascii=False,
        indent=2,
    )
    temporary_output_path = output_path.with_name(f".{output_path.name}.tmp")
    try:
        temporary_output_path.write_text(built_dashboard_json + "\n", encoding="utf-8")
        os.replace(temporary_output_path, output_path)
    except BaseException:
        temporary_output_path.unlink(missing_ok=True)
        raise


def _inject_dataset_query_lines(dashboard_document: Any) -> None:
    """Replace each scaffold dataset's queryLines with the lines from its SQL source file."""

    datasets = dashboard_document.get("datasets")
    if datasets is None:
        return

    scaffold_dataset_names = {dataset["name"] for dataset in datasets}
    if scaffold_dataset_names != set(DATASET_SQL_FILES):
        raise ValueError(
            f"dashboard scaffold datasets {sorted(scaffold_dataset_names)} "
            f"must match {sorted(DATASET_SQL_FILES)}"
        )

    for dataset_index, dataset in enumerate(datasets):
        if "queryLines" in dataset:
            raise ValueError(
                f"dataset {dataset['name']} embeds queryLines in the scaffold; "
                f"keep its SQL in {DATASET_SQL_FILES[dataset['name']]}"
            )
        rebuilt_dataset: dict[str, Any] = {}
        for key, value in dataset.items():
            rebuilt_dataset[key] = value
            if key == "displayName":
                rebuilt_dataset["queryLines"] = _read_dataset_query_lines(dataset["name"])
        if "queryLines" not in rebuilt_dataset:
            raise ValueError(f"dataset {dataset['name']} is missing displayName")
        datasets[dataset_index] = rebuilt_dataset


def _read_dataset_query_lines(dataset_name: str) -> list[str]:
    """Render one dataset SQL file and split it into dashboard queryLines."""

    lines = _render_dataset_sql(dataset_name).split("\n")
    return [line + "\n" for line in lines[:-1]] + [lines[-1]]


def _render_dataset_sql(dataset_name: str) -> str:
    """Render one dataset SQL file with the shared fragments and window parameters."""

    sql_text = DATASET_SQL_FILES[dataset_name].read_text(encoding="utf-8")
    if sql_text.endswith("\n"):
        sql_text = sql_text[:-1]
    fragment_text_by_name = {}
    for fragment_name in FRAGMENT_FILES:
        fragment_parameters = None
        if fragment_name == "deadline_dates" and dataset_name in DEADLINE_DATE_WINDOWS:
            fragment_parameters = DEADLINE_DATE_WINDOWS[dataset_name]
        fragment_text_by_name[fragment_name] = _render_fragment(fragment_name, fragment_parameters)
    return Template(sql_text).substitute(**fragment_text_by_name)


def _render_fragment(fragment_name: str, fragment_parameters: Mapping[str, Any] | None) -> str:
    """Read one shared fragment file and render it with optional parameters."""

    fragment_text = _read_fragment_text(fragment_name)
    if fragment_parameters is None:
        return fragment_text
    return Template(fragment_text).substitute(**fragment_parameters)


def _read_fragment_text(fragment_name: str) -> str:
    """Read one shared fragment file without its trailing newline."""

    fragment_text = FRAGMENT_FILES[fragment_name].read_text(encoding="utf-8")
    if fragment_text.endswith("\n"):
        fragment_text = fragment_text[:-1]
    return fragment_text


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

    if isinstance(document, dict):
        child_nodes = document.items()
    elif isinstance(document, list):
        child_nodes = enumerate(document)
    else:
        return 0

    replacement_count = 0
    for key, child_node in child_nodes:
        if isinstance(child_node, str) and marker in child_node:
            document[key] = child_node.replace(marker, replacement_text)
            replacement_count += 1
        else:
            replacement_count += _replace_text_marker(child_node, marker, replacement_text)
    return replacement_count


def main() -> int:
    """Build the tracked dashboard from the tracked scaffold."""

    build_dashboard(DEFAULT_SCAFFOLD_PATH, DEFAULT_DASHBOARD_PATH)
    print(f"built dashboard: {DEFAULT_DASHBOARD_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
