from __future__ import annotations

import importlib.util
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

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


class RepositoryLauncherIntegrationTests(unittest.TestCase):
    def test_launcher_downloads_caches_and_runs_matching_release(self) -> None:
        project_version = launcher_module.read_project_version(REPOSITORY_ROOT)
        release_target = launcher_module.resolve_release_target(
            platform.system(),
            platform.machine(),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            release_download_root = temporary_root / "releases"
            release_directory = release_download_root / f"v{project_version}"
            release_directory.mkdir(parents=True)
            fake_cli_path = temporary_root / release_target.binary_name
            fake_cli_path.write_text(
                "#!/bin/sh\n"
                'if [ "${1:-}" = fail ]; then exit 7; fi\n'
                "printf 'repository CLI arguments: %s\\n' \"$*\"\n",
                encoding="utf-8",
            )
            fake_cli_path.chmod(0o755)
            archive_path = release_directory / launcher_module.release_archive_name(release_target)
            self._create_release_archive(archive_path, fake_cli_path, release_target)

            cli_cache_directory = temporary_root / "cli-cache"
            launcher_environment = os.environ.copy()
            launcher_environment.update(
                {
                    "WORKFLOW_MONITORING_CLI_CACHE_DIRECTORY": str(cli_cache_directory),
                    "WORKFLOW_MONITORING_RELEASE_DOWNLOAD_ROOT": release_download_root.as_uri(),
                }
            )
            launcher_command = (
                [sys.executable, str(CLI_LAUNCHER_PATH)]
                if os.name == "nt"
                else [str(ROOT_LAUNCHER_PATH)]
            )
            first_run = subprocess.run(
                [*launcher_command, "help", "add"],
                check=False,
                capture_output=True,
                text=True,
                env=launcher_environment,
            )
            self.assertEqual(first_run.returncode, 0, first_run.stderr)
            self.assertEqual(first_run.stdout, "repository CLI arguments: help add\n")
            self.assertIn(f"Downloaded workflow-monitoring {project_version}", first_run.stderr)

            archive_path.unlink()
            cached_run = subprocess.run(
                [sys.executable, str(CLI_LAUNCHER_PATH), "list"],
                check=False,
                capture_output=True,
                text=True,
                env=launcher_environment,
            )
            self.assertEqual(cached_run.returncode, 0, cached_run.stderr)
            self.assertEqual(cached_run.stdout, "repository CLI arguments: list\n")
            self.assertEqual(cached_run.stderr, "")

            failed_cli_run = subprocess.run(
                [sys.executable, str(CLI_LAUNCHER_PATH), "fail"],
                check=False,
                capture_output=True,
                text=True,
                env=launcher_environment,
            )
            self.assertEqual(failed_cli_run.returncode, 7)

    @unittest.skipIf(os.name == "nt", "Windows uses the Python launcher directly")
    def test_root_launcher_is_valid_shell(self) -> None:
        syntax_check = subprocess.run(
            ["sh", "-n", str(ROOT_LAUNCHER_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(syntax_check.returncode, 0, syntax_check.stderr)

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


if __name__ == "__main__":
    unittest.main()
