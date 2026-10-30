"""Copy product files into the bundle template and check that they are current."""

from __future__ import annotations

import argparse
import shutil
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PROJECT = REPOSITORY_ROOT / "template" / "template" / "{{.project_name}}"
PRODUCT_DIRECTORY = TEMPLATE_PROJECT / "workflow_monitoring"
PYPROJECT_PATH = REPOSITORY_ROOT / "pyproject.toml"
VERSION_PATH = PRODUCT_DIRECTORY / "VERSION"
COPY_FILES = {
    REPOSITORY_ROOT / "src/collect_workflow_monitoring_jobs_api.py": (
        PRODUCT_DIRECTORY / "collector/collect_workflow_monitoring_jobs_api.py"
    ),
    REPOSITORY_ROOT / "src/workflow_monitoring_config.py": (
        PRODUCT_DIRECTORY / "collector/workflow_monitoring_config.py"
    ),
    REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.lvdash.json": (
        PRODUCT_DIRECTORY / "dashboard/workflow-monitoring.lvdash.json"
    ),
    REPOSITORY_ROOT / "src/workflow-monitoring.schema.json": (
        PRODUCT_DIRECTORY / "workflow-monitoring.schema.json"
    ),
}


def project_version() -> str:
    """Return the project version from pyproject.toml."""
    pyproject_document = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    return pyproject_document["project"]["version"]


def build_template(*, check: bool) -> list[Path]:
    """Copy the product files, or return the template files that are stale."""
    stale: list[Path] = []
    for source_path, destination_path in COPY_FILES.items():
        source_bytes = source_path.read_bytes()
        if check:
            if not destination_path.is_file() or destination_path.read_bytes() != source_bytes:
                stale.append(destination_path)
            continue
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination_path)

    version_bytes = f"{project_version()}\n".encode()
    if check:
        if not VERSION_PATH.is_file() or VERSION_PATH.read_bytes() != version_bytes:
            stale.append(VERSION_PATH)
    else:
        VERSION_PATH.write_bytes(version_bytes)
    return stale


def main() -> int:
    """Build the template or check that the tracked copy is current."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report stale files and exit 1.")
    arguments = parser.parse_args()
    stale = build_template(check=arguments.check)
    if not arguments.check:
        print(f"built template: {PRODUCT_DIRECTORY}")
        return 0
    if not stale:
        return 0
    for path in stale:
        print(f"stale template file: {path.relative_to(REPOSITORY_ROOT)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
