#!/usr/bin/env python3
"""Download and run the repository-matched workflow-monitoring CLI."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
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
TRUSTED_RELEASE_API_ROOT = (
    "https://api.github.com/repos/overengineered-org/"
    "databricks-workflow-monitoring-dashboards/releases/tags"
)
PROJECT_VERSION_PATTERN = re.compile(r'^version = "([^"]+)"$', re.MULTILINE)
SHA256_DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")


class ReleaseTarget(NamedTuple):
    operating_system: str
    architecture: str
    archive_extension: str
    binary_name: str


def read_project_version(repository_root: Path) -> str:
    project_configuration = (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    project_version_match = PROJECT_VERSION_PATTERN.search(project_configuration)
    if project_version_match is None:
        raise RuntimeError("pyproject.toml does not contain project.version")
    return project_version_match.group(1)


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


def cached_release_archive_path(
    cli_path: Path,
    release_target: ReleaseTarget,
) -> Path:
    return cli_path.parent / release_archive_name(release_target)


def fetch_trusted_release_sha256(project_version: str, archive_name: str) -> str:
    release_metadata_url = f"{TRUSTED_RELEASE_API_ROOT}/v{project_version}"
    release_metadata_request = urllib.request.Request(
        release_metadata_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "workflow-monitoring-repository-launcher",
        },
    )
    try:
        with urllib.request.urlopen(release_metadata_request, timeout=60) as metadata_response:
            release_metadata = json.load(metadata_response)
    except (
        OSError,
        UnicodeDecodeError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as metadata_error:
        raise RuntimeError(
            f"trusted release digest is unavailable from {release_metadata_url}: {metadata_error}"
        ) from metadata_error

    if not isinstance(release_metadata, dict):
        raise RuntimeError(f"trusted release digest is unavailable from {release_metadata_url}")
    if release_metadata.get("tag_name") != f"v{project_version}":
        raise RuntimeError(f"trusted release metadata does not match v{project_version}")

    release_assets = release_metadata.get("assets")
    if not isinstance(release_assets, list):
        raise RuntimeError(f"trusted release metadata has no assets for v{project_version}")
    for release_asset in release_assets:
        if not isinstance(release_asset, dict) or release_asset.get("name") != archive_name:
            continue
        asset_digest = release_asset.get("digest")
        digest_match = (
            SHA256_DIGEST_PATTERN.fullmatch(asset_digest) if isinstance(asset_digest, str) else None
        )
        if digest_match is None:
            raise RuntimeError(f"trusted SHA-256 digest is unavailable for {archive_name}")
        return digest_match.group(1)

    raise RuntimeError(f"trusted release metadata has no asset named {archive_name}")


def download_release_archive(archive_url: str, archive_path: Path) -> None:
    try:
        with urllib.request.urlopen(archive_url, timeout=60) as archive_response:
            with archive_path.open("wb") as archive_file:
                shutil.copyfileobj(archive_response, archive_file)
    except (OSError, urllib.error.URLError) as download_error:
        raise RuntimeError(
            f"could not download {archive_url}: {download_error}"
        ) from download_error


def calculate_file_sha256(file_path: Path) -> str:
    sha256_calculation = hashlib.sha256()
    with file_path.open("rb") as file_contents:
        for file_chunk in iter(lambda: file_contents.read(1024 * 1024), b""):
            sha256_calculation.update(file_chunk)
    return sha256_calculation.hexdigest()


def verify_release_archive_sha256(archive_path: Path, expected_sha256: str) -> None:
    actual_sha256 = calculate_file_sha256(archive_path)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"SHA-256 mismatch for {archive_path.name}: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )


def read_binary_from_archive(archive_path: Path, release_target: ReleaseTarget) -> bytes:
    try:
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
                raise RuntimeError(
                    f"release archive entry is not a file: {release_target.binary_name}"
                )
            binary_file = release_archive.extractfile(binary_member)
            if binary_file is None:
                raise RuntimeError(
                    f"could not read {release_target.binary_name} from release archive"
                )
            return binary_file.read()
    except (tarfile.TarError, zipfile.BadZipFile) as archive_error:
        raise RuntimeError(f"could not read release archive {archive_path.name}") from archive_error


def write_cached_cli_binary(binary_contents: bytes, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="workflow-monitoring-install-",
        dir=destination_path.parent,
    ) as temporary_directory:
        temporary_binary_path = Path(temporary_directory) / destination_path.name
        temporary_binary_path.write_bytes(binary_contents)
        temporary_binary_path.chmod(
            temporary_binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        os.replace(temporary_binary_path, destination_path)


def remove_cached_release_files(cli_path: Path, archive_path: Path) -> None:
    for cached_path in (cli_path, archive_path):
        if cached_path.is_file() or cached_path.is_symlink():
            cached_path.unlink()


def install_cached_cli(
    destination_path: Path,
    project_version: str,
    release_target: ReleaseTarget,
    release_download_root: str,
    expected_archive_sha256: str,
) -> None:
    archive_name = release_archive_name(release_target)
    archive_url = f"{release_download_root.rstrip('/')}/v{project_version}/{archive_name}"
    cached_archive_path = cached_release_archive_path(destination_path, release_target)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="workflow-monitoring-download-",
        dir=destination_path.parent,
    ) as temporary_directory:
        temporary_directory_path = Path(temporary_directory)
        archive_path = temporary_directory_path / archive_name
        temporary_binary_path = temporary_directory_path / release_target.binary_name
        download_release_archive(archive_url, archive_path)
        try:
            verify_release_archive_sha256(archive_path, expected_archive_sha256)
        except RuntimeError:
            remove_cached_release_files(destination_path, cached_archive_path)
            raise
        temporary_binary_path.write_bytes(read_binary_from_archive(archive_path, release_target))
        temporary_binary_path.chmod(
            temporary_binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        os.replace(archive_path, cached_archive_path)
        os.replace(temporary_binary_path, destination_path)

    print(
        f"Downloaded workflow-monitoring {project_version} for "
        f"{release_target.operating_system}-{release_target.architecture}.",
        file=sys.stderr,
    )


def ensure_cached_cli(
    destination_path: Path,
    project_version: str,
    release_target: ReleaseTarget,
    release_download_root: str,
    expected_archive_sha256: str,
) -> None:
    cached_archive_path = cached_release_archive_path(destination_path, release_target)
    cached_archive_is_regular_file = (
        cached_archive_path.is_file() and not cached_archive_path.is_symlink()
    )
    if cached_archive_is_regular_file:
        try:
            verify_release_archive_sha256(cached_archive_path, expected_archive_sha256)
        except RuntimeError:
            remove_cached_release_files(destination_path, cached_archive_path)
        else:
            verified_binary_contents = read_binary_from_archive(
                cached_archive_path,
                release_target,
            )
            expected_binary_sha256 = hashlib.sha256(verified_binary_contents).hexdigest()
            cached_binary_is_verified = (
                destination_path.is_file()
                and not destination_path.is_symlink()
                and calculate_file_sha256(destination_path) == expected_binary_sha256
            )
            if cached_binary_is_verified:
                return
            if destination_path.is_file() or destination_path.is_symlink():
                destination_path.unlink()
            write_cached_cli_binary(verified_binary_contents, destination_path)
            print(
                f"Restored workflow-monitoring {project_version} from verified cache.",
                file=sys.stderr,
            )
            return
    elif (
        cached_archive_path.is_symlink()
        or destination_path.is_file()
        or destination_path.is_symlink()
    ):
        remove_cached_release_files(destination_path, cached_archive_path)

    install_cached_cli(
        destination_path,
        project_version,
        release_target,
        release_download_root,
        expected_archive_sha256,
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
    archive_name = release_archive_name(release_target)
    expected_archive_sha256 = fetch_trusted_release_sha256(project_version, archive_name)
    ensure_cached_cli(
        cli_path,
        project_version,
        release_target,
        release_download_root,
        expected_archive_sha256,
    )
    completed_command = subprocess.run([str(cli_path), *command_arguments], check=False)
    return completed_command.returncode


def main() -> int:
    try:
        return run_repository_cli(sys.argv[1:])
    except (OSError, RuntimeError) as launcher_error:
        print(f"workflow-monitoring: {launcher_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
