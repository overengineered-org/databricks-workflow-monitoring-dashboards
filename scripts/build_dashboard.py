"""Build the Databricks dashboard JSON from the scaffold and Vega files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCAFFOLD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.scaffold.lvdash.json"
DEFAULT_DASHBOARD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.lvdash.json"
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
    """Insert the custom Vega visualizations and write deterministic dashboard JSON."""

    try:
        dashboard_document = json.loads(scaffold_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"parse dashboard scaffold: {error}") from error

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
