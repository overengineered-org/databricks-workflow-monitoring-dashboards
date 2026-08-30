#!/usr/bin/env python3
"""Download and run the repository-matched workflow-monitoring CLI."""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import NamedTuple

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RELEASE_DOWNLOAD_ROOT = (
    "https://github.com/overengineered-org/"
    "databricks-workflow-monitoring-dashboards/releases/download"
)


class ReleaseTarget(NamedTuple):
    operating_system: str
    architecture: str
    archive_extension: str
    binary_name: str


def read_project_version(repository_root: Path) -> str:
    with (repository_root / "pyproject.toml").open("rb") as project_file:
        project_configuration = tomllib.load(project_file)
    return str(project_configuration["project"]["version"])


def resolve_release_target(system_name: str, machine_architecture: str) -> ReleaseTarget:
    operating_system_names = {
        "darwin": "darwin",
        "linux": "linux",
        "windows": "windows",
    }
    architecture_names = {
        "aarch64": "arm64",
        "amd64": "amd64",
        "arm64": "arm64",
        "x86_64": "amd64",
    }
    operating_system = operating_system_names.get(system_name.lower())
    architecture = architecture_names.get(machine_architecture.lower())
    if operating_system is None or architecture is None:
        raise RuntimeError(
            f"unsupported platform: {system_name.lower()}-{machine_architecture.lower()}"
        )
    if operating_system == "windows" and architecture != "amd64":
        raise RuntimeError(f"unsupported platform: {operating_system}-{architecture}")
    if operating_system == "windows":
        return ReleaseTarget(operating_system, architecture, "zip", "workflow-monitoring.exe")
    return ReleaseTarget(operating_system, architecture, "tar.gz", "workflow-monitoring")


def release_archive_name(release_target: ReleaseTarget) -> str:
    return (
        f"workflow-monitoring-{release_target.operating_system}-"
        f"{release_target.architecture}.{release_target.archive_extension}"
    )


def cached_cli_path(
    cache_directory: Path,
    project_version: str,
    release_target: ReleaseTarget,
) -> Path:
    target_directory = (
        cache_directory
        / f"v{project_version}"
        / f"{release_target.operating_system}-{release_target.architecture}"
    )
    return target_directory / release_target.binary_name


def download_release_archive(archive_url: str, archive_path: Path) -> None:
    try:
        with urllib.request.urlopen(archive_url, timeout=60) as archive_response:
            with archive_path.open("wb") as archive_file:
                shutil.copyfileobj(archive_response, archive_file)
    except (OSError, urllib.error.URLError) as download_error:
        raise RuntimeError(
            f"could not download {archive_url}: {download_error}"
        ) from download_error


def read_binary_from_archive(archive_path: Path, release_target: ReleaseTarget) -> bytes:
    if release_target.archive_extension == "zip":
        with zipfile.ZipFile(archive_path) as release_archive:
            try:
                return release_archive.read(release_target.binary_name)
            except KeyError as archive_error:
                raise RuntimeError(
                    f"release archive does not contain {release_target.binary_name}"
                ) from archive_error

    with tarfile.open(archive_path, mode="r:gz") as release_archive:
        try:
            binary_member = release_archive.getmember(release_target.binary_name)
        except KeyError as archive_error:
            raise RuntimeError(
                f"release archive does not contain {release_target.binary_name}"
            ) from archive_error
        if not binary_member.isfile():
            raise RuntimeError(f"release archive entry is not a file: {release_target.binary_name}")
        binary_file = release_archive.extractfile(binary_member)
        if binary_file is None:
            raise RuntimeError(f"could not read {release_target.binary_name} from release archive")
        return binary_file.read()


def install_cached_cli(
    destination_path: Path,
    project_version: str,
    release_target: ReleaseTarget,
    release_download_root: str,
) -> None:
    archive_name = release_archive_name(release_target)
    archive_url = f"{release_download_root.rstrip('/')}/v{project_version}/{archive_name}"
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="workflow-monitoring-download-",
        dir=destination_path.parent,
    ) as temporary_directory:
        temporary_directory_path = Path(temporary_directory)
        archive_path = temporary_directory_path / archive_name
        temporary_binary_path = temporary_directory_path / release_target.binary_name
        download_release_archive(archive_url, archive_path)
        temporary_binary_path.write_bytes(read_binary_from_archive(archive_path, release_target))
        temporary_binary_path.chmod(
            temporary_binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        os.replace(temporary_binary_path, destination_path)

    print(
        f"Downloaded workflow-monitoring {project_version} for "
        f"{release_target.operating_system}-{release_target.architecture}.",
        file=sys.stderr,
    )


def run_repository_cli(command_arguments: list[str]) -> int:
    project_version = read_project_version(REPOSITORY_ROOT)
    release_target = resolve_release_target(platform.system(), platform.machine())
    configured_cache_directory = os.environ.get("WORKFLOW_MONITORING_CLI_CACHE_DIRECTORY")
    cache_directory = (
        Path(configured_cache_directory).expanduser()
        if configured_cache_directory
        else REPOSITORY_ROOT / ".workflow-monitoring" / "cli"
    )
    release_download_root = os.environ.get(
        "WORKFLOW_MONITORING_RELEASE_DOWNLOAD_ROOT",
        DEFAULT_RELEASE_DOWNLOAD_ROOT,
    )
    cli_path = cached_cli_path(cache_directory, project_version, release_target)
    if not cli_path.is_file():
        install_cached_cli(
            cli_path,
            project_version,
            release_target,
            release_download_root,
        )
    completed_command = subprocess.run([str(cli_path), *command_arguments], check=False)
    return completed_command.returncode


def main() -> int:
    try:
        return run_repository_cli(sys.argv[1:])
    except (OSError, RuntimeError, KeyError, tomllib.TOMLDecodeError) as launcher_error:
        print(f"workflow-monitoring: {launcher_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
