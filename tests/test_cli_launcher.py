from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import NamedTuple
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CLI_LAUNCHER_PATH = REPOSITORY_ROOT / "scripts" / "run_workflow_monitoring.py"
ROOT_LAUNCHER_PATH = REPOSITORY_ROOT / "workflow-monitoring"

launcher_specification = importlib.util.spec_from_file_location(
    "run_workflow_monitoring",
    CLI_LAUNCHER_PATH,
)
if launcher_specification is None or launcher_specification.loader is None:
    raise RuntimeError("could not load CLI launcher")
launcher_module = importlib.util.module_from_spec(launcher_specification)
launcher_specification.loader.exec_module(launcher_module)


class ReleaseTargetTests(unittest.TestCase):
    def test_supported_platform_names_match_release_assets(self) -> None:
        test_cases = {
            ("Darwin", "arm64"): ("darwin", "arm64", "tar.gz"),
            ("Darwin", "x86_64"): ("darwin", "amd64", "tar.gz"),
            ("Linux", "aarch64"): ("linux", "arm64", "tar.gz"),
            ("Linux", "amd64"): ("linux", "amd64", "tar.gz"),
            ("Windows", "AMD64"): ("windows", "amd64", "zip"),
        }
        for platform_values, expected_target_values in test_cases.items():
            with self.subTest(platform_values=platform_values):
                release_target = launcher_module.resolve_release_target(*platform_values)
                self.assertEqual(release_target[:3], expected_target_values)

    def test_unsupported_platform_fails_before_download(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported platform: windows-arm64"):
            launcher_module.resolve_release_target("Windows", "arm64")


class RepositoryLauncherTests(unittest.TestCase):
    PROJECT_VERSION = "0.1.3"
    RELEASE_TARGETS = (
        launcher_module.ReleaseTarget("linux", "amd64", "tar.gz", "workflow-monitoring"),
        launcher_module.ReleaseTarget("windows", "amd64", "zip", "workflow-monitoring.exe"),
    )

    def test_trusted_digest_comes_from_exact_github_release_asset(self) -> None:
        archive_name = "workflow-monitoring-linux-amd64.tar.gz"
        expected_sha256 = "a" * 64
        release_metadata_response = io.BytesIO(
            json.dumps(
                {
                    "tag_name": f"v{self.PROJECT_VERSION}",
                    "assets": [
                        {
                            "name": archive_name,
                            "digest": f"sha256:{expected_sha256}",
                        }
                    ],
                }
            ).encode("utf-8")
        )
        with mock.patch.object(
            launcher_module.urllib.request,
            "urlopen",
            return_value=release_metadata_response,
        ):
            actual_sha256 = launcher_module.fetch_trusted_release_sha256(
                self.PROJECT_VERSION,
                archive_name,
            )
        self.assertEqual(actual_sha256, expected_sha256)

    def test_missing_trusted_digest_fails_closed(self) -> None:
        release_metadata_response = io.BytesIO(
            json.dumps(
                {
                    "tag_name": f"v{self.PROJECT_VERSION}",
                    "assets": [
                        {
                            "name": "workflow-monitoring-linux-amd64.tar.gz",
                            "digest": None,
                        }
                    ],
                }
            ).encode("utf-8")
        )
        with (
            mock.patch.object(
                launcher_module.urllib.request,
                "urlopen",
                return_value=release_metadata_response,
            ),
            self.assertRaisesRegex(RuntimeError, "trusted SHA-256 digest is unavailable"),
        ):
            launcher_module.fetch_trusted_release_sha256(
                self.PROJECT_VERSION,
                "workflow-monitoring-linux-amd64.tar.gz",
            )

    def test_valid_release_assets_install_for_tar_and_zip(self) -> None:
        for release_target in self.RELEASE_TARGETS:
            with self.subTest(archive_extension=release_target.archive_extension):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    release_fixture = self._create_release_fixture(
                        Path(temporary_directory),
                        release_target,
                    )
                    launcher_module.ensure_cached_cli(
                        release_fixture.cli_path,
                        self.PROJECT_VERSION,
                        release_target,
                        release_fixture.release_download_root.as_uri(),
                        release_fixture.archive_sha256,
                    )
                    self.assertEqual(
                        release_fixture.cli_path.read_bytes(),
                        release_fixture.binary_contents,
                    )
                    self.assertTrue(release_fixture.cached_archive_path.is_file())

    def test_corrupted_release_assets_leave_no_runnable_cache_for_tar_and_zip(self) -> None:
        for release_target in self.RELEASE_TARGETS:
            with self.subTest(archive_extension=release_target.archive_extension):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    release_fixture = self._create_release_fixture(
                        Path(temporary_directory),
                        release_target,
                    )
                    release_fixture.source_archive_path.write_bytes(
                        release_fixture.source_archive_path.read_bytes() + b"corrupted"
                    )
                    with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                        launcher_module.ensure_cached_cli(
                            release_fixture.cli_path,
                            self.PROJECT_VERSION,
                            release_target,
                            release_fixture.release_download_root.as_uri(),
                            release_fixture.archive_sha256,
                        )
                    self.assertFalse(release_fixture.cli_path.exists())
                    self.assertFalse(release_fixture.cached_archive_path.exists())

    def test_tampered_cached_binaries_restore_from_verified_tar_and_zip(self) -> None:
        for release_target in self.RELEASE_TARGETS:
            with self.subTest(archive_extension=release_target.archive_extension):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    release_fixture = self._create_release_fixture(
                        Path(temporary_directory),
                        release_target,
                    )
                    launcher_module.ensure_cached_cli(
                        release_fixture.cli_path,
                        self.PROJECT_VERSION,
                        release_target,
                        release_fixture.release_download_root.as_uri(),
                        release_fixture.archive_sha256,
                    )
                    release_fixture.cli_path.write_bytes(b"tampered cached binary")
                    release_fixture.source_archive_path.unlink()
                    launcher_module.ensure_cached_cli(
                        release_fixture.cli_path,
                        self.PROJECT_VERSION,
                        release_target,
                        release_fixture.release_download_root.as_uri(),
                        release_fixture.archive_sha256,
                    )
                    self.assertEqual(
                        release_fixture.cli_path.read_bytes(),
                        release_fixture.binary_contents,
                    )

    def test_tampered_cached_archives_remove_executable_for_tar_and_zip(self) -> None:
        for release_target in self.RELEASE_TARGETS:
            with self.subTest(archive_extension=release_target.archive_extension):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    release_fixture = self._create_release_fixture(
                        Path(temporary_directory),
                        release_target,
                    )
                    launcher_module.ensure_cached_cli(
                        release_fixture.cli_path,
                        self.PROJECT_VERSION,
                        release_target,
                        release_fixture.release_download_root.as_uri(),
                        release_fixture.archive_sha256,
                    )
                    release_fixture.cached_archive_path.write_bytes(b"tampered cached archive")
                    release_fixture.source_archive_path.unlink()
                    with self.assertRaisesRegex(RuntimeError, "could not download"):
                        launcher_module.ensure_cached_cli(
                            release_fixture.cli_path,
                            self.PROJECT_VERSION,
                            release_target,
                            release_fixture.release_download_root.as_uri(),
                            release_fixture.archive_sha256,
                        )
                    self.assertFalse(release_fixture.cli_path.exists())
                    self.assertFalse(release_fixture.cached_archive_path.exists())

    def test_launcher_passes_arguments_and_exit_code_to_verified_cli(self) -> None:
        release_target = self.RELEASE_TARGETS[0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            release_fixture = self._create_release_fixture(
                Path(temporary_directory),
                release_target,
            )
            completed_cli_command = subprocess.CompletedProcess([], 7)
            launcher_environment = {
                "WORKFLOW_MONITORING_CLI_CACHE_DIRECTORY": str(release_fixture.cli_path.parents[2]),
                "WORKFLOW_MONITORING_RELEASE_DOWNLOAD_ROOT": (
                    release_fixture.release_download_root.as_uri()
                ),
            }
            with (
                mock.patch.dict(os.environ, launcher_environment),
                mock.patch.object(
                    launcher_module,
                    "resolve_release_target",
                    return_value=release_target,
                ),
                mock.patch.object(
                    launcher_module,
                    "fetch_trusted_release_sha256",
                    return_value=release_fixture.archive_sha256,
                ),
                mock.patch.object(
                    launcher_module.subprocess,
                    "run",
                    return_value=completed_cli_command,
                ) as run_cli_command,
            ):
                exit_code = launcher_module.run_repository_cli(["fail"])
            self.assertEqual(exit_code, 7)
            run_cli_command.assert_called_once_with(
                [str(release_fixture.cli_path), "fail"],
                check=False,
            )

    @unittest.skipIf(os.name == "nt", "Windows uses the Python launcher directly")
    def test_root_launcher_is_valid_shell(self) -> None:
        syntax_check = subprocess.run(
            ["sh", "-n", str(ROOT_LAUNCHER_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(syntax_check.returncode, 0, syntax_check.stderr)

    @classmethod
    def _create_release_fixture(
        cls,
        temporary_root: Path,
        release_target: launcher_module.ReleaseTarget,
    ) -> ReleaseFixture:
        release_download_root = temporary_root / "releases"
        release_directory = release_download_root / f"v{cls.PROJECT_VERSION}"
        release_directory.mkdir(parents=True)
        binary_contents = f"verified {release_target.archive_extension} binary".encode()
        fake_cli_path = temporary_root / release_target.binary_name
        fake_cli_path.write_bytes(binary_contents)
        fake_cli_path.chmod(0o755)
        source_archive_path = release_directory / launcher_module.release_archive_name(
            release_target
        )
        cls._create_release_archive(source_archive_path, fake_cli_path, release_target)
        archive_sha256 = hashlib.sha256(source_archive_path.read_bytes()).hexdigest()
        cli_cache_directory = temporary_root / "cli-cache"
        cli_path = launcher_module.cached_cli_path(
            cli_cache_directory,
            cls.PROJECT_VERSION,
            release_target,
        )
        cached_archive_path = launcher_module.cached_release_archive_path(
            cli_path,
            release_target,
        )
        return ReleaseFixture(
            release_download_root,
            source_archive_path,
            cli_path,
            cached_archive_path,
            archive_sha256,
            binary_contents,
        )

    @staticmethod
    def _create_release_archive(
        archive_path: Path,
        fake_cli_path: Path,
        release_target: launcher_module.ReleaseTarget,
    ) -> None:
        if release_target.archive_extension == "zip":
            with zipfile.ZipFile(archive_path, mode="w") as release_archive:
                release_archive.write(fake_cli_path, arcname=release_target.binary_name)
            return
        with tarfile.open(archive_path, mode="w:gz") as release_archive:
            release_archive.add(fake_cli_path, arcname=release_target.binary_name)


class ReleaseFixture(NamedTuple):
    release_download_root: Path
    source_archive_path: Path
    cli_path: Path
    cached_archive_path: Path
    archive_sha256: str
    binary_contents: bytes


if __name__ == "__main__":
    unittest.main()
