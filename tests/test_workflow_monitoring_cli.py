"""Tests for the workflow-monitoring CLI with a fake Databricks CLI on PATH."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import tests._paths  # noqa: F401  # Puts the repository root and src/ on sys.path.

# isort: split
import workflow_monitoring_cli as cli
from scripts.build_template import PRODUCT_DIRECTORY, TEMPLATE_PROJECT
from workflow_monitoring_config import load_workflow_monitoring_configuration

# The rendered configuration as bundle init writes it with the default timezone.
TEMPLATE_CONFIGURATION = (
    (TEMPLATE_PROJECT / "workflow-monitoring.yml.tmpl")
    .read_text(encoding="utf-8")
    .replace("{{.default_timezone}}", "UTC")
)
WORKSPACE_JOBS = [
    {"job_id": 30, "settings": {"name": "orders nightly"}},
    {"job_id": 10, "settings": {"name": "Billing\nexport"}},
    {"job_id": 123456, "settings": {"name": "placeholder twin"}},
    {"job_id": 20},
]

# The fake CLI logs its arguments, then acts like `jobs list` or `bundle init`.
FAKE_DATABRICKS = """\
import json, os, sys
from pathlib import Path
arguments = sys.argv[1:]
with open(os.environ["FAKE_DATABRICKS_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(arguments) + "\\n")
exit_code = int(os.environ.get("FAKE_DATABRICKS_EXIT", "0"))
if exit_code:
    sys.exit(exit_code)
if arguments[:2] == ["jobs", "list"]:
    print(os.environ["FAKE_DATABRICKS_JOBS"])
elif arguments[:2] == ["bundle", "init"]:
    project = Path(arguments[arguments.index("--output-dir") + 1]) / "new_project"
    project.mkdir(parents=True)
    (project / "workflow-monitoring.yml").write_text("workflows:\\n", encoding="utf-8")
"""


class FakeDatabricksTestCase(unittest.TestCase):
    """Run each test with a fake databricks command and a project built from the template."""

    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)

        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        fake_command = bin_directory / "databricks"
        fake_command.write_text(f"#!{sys.executable}\n{FAKE_DATABRICKS}", encoding="utf-8")
        fake_command.chmod(0o755)
        self.log_path = self.root / "databricks.log"
        environment = {
            "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DATABRICKS_LOG": str(self.log_path),
            "FAKE_DATABRICKS_JOBS": json.dumps(WORKSPACE_JOBS),
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.project = self.root / "project"
        shutil.copytree(PRODUCT_DIRECTORY, self.project / "workflow_monitoring")
        self.configuration_path = self.project / cli.CONFIGURATION_FILE_NAME
        self.configuration_path.write_text(TEMPLATE_CONFIGURATION, encoding="utf-8")

    def logged_commands(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def select(
        self,
        chosen_job_ids: list[int],
        *,
        confirmed: bool = True,
        name_contains: str | None = None,
        completion_time: str = cli.DEFAULT_COMPLETION_TIME,
        dry_run: bool = False,
    ) -> tuple[int, str, mock.Mock]:
        choose_jobs = mock.Mock(return_value=chosen_job_ids)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.select_jobs(
                self.project,
                "test-profile",
                name_contains=name_contains,
                completion_time=completion_time,
                dry_run=dry_run,
                choose_jobs=choose_jobs,
                confirm=lambda _message: confirmed,
            )
        return exit_code, output.getvalue(), choose_jobs


class SelectJobsTests(FakeDatabricksTestCase):
    def test_ticked_jobs_are_appended_and_pass_the_collector_rules(self) -> None:
        exit_code, output, _choose_jobs = self.select([30, 10], completion_time="07:45")

        self.assertEqual(exit_code, 0)
        self.assertIn("Added 2 Jobs", output)
        self.assertEqual(
            self.logged_commands(),
            [["jobs", "list", "--output", "json", "--profile", "test-profile"]],
        )
        expected_entries = (
            "  # Billing export\n"
            "  - job_id: 10\n"
            "    monitoring_status: active\n"
            "    sla:\n"
            "      frequency: daily\n"
            '      completion_time: "07:45"\n'
            "  # orders nightly\n"
            "  - job_id: 30\n"
            "    monitoring_status: active\n"
            "    sla:\n"
            "      frequency: daily\n"
            '      completion_time: "07:45"\n'
        )
        self.assertEqual(
            self.configuration_path.read_text(encoding="utf-8"),
            TEMPLATE_CONFIGURATION + expected_entries,
        )
        configuration = load_workflow_monitoring_configuration(
            self.configuration_path, self.project / cli.SCHEMA_RELATIVE_PATH
        )
        self.assertEqual([workflow.job_id for workflow in configuration.active_workflows], [10, 30])
        self.assertEqual(list(self.project.glob(".workflow-monitoring.*")), [])

    def test_picker_gets_sorted_jobs_and_marks_monitored_ids(self) -> None:
        _exit_code, _output, choose_jobs = self.select([])

        choose_jobs.assert_called_once_with(
            [
                cli.WorkspaceJob(20, "(unnamed)"),
                cli.WorkspaceJob(10, "Billing\nexport"),
                cli.WorkspaceJob(30, "orders nightly"),
                cli.WorkspaceJob(123456, "placeholder twin"),
            ],
            {123456},
        )

    def test_name_filter_ignores_case(self) -> None:
        _exit_code, _output, choose_jobs = self.select([], name_contains="ORDERS")

        self.assertEqual(choose_jobs.call_args.args[0], [cli.WorkspaceJob(30, "orders nightly")])

    def test_nothing_changes_without_a_new_confirmed_selection(self) -> None:
        cases = (
            ("no ticks", [], {}, "No new Jobs selected"),
            ("only monitored", [123456], {}, "No new Jobs selected"),
            ("cancelled", [30], {"confirmed": False}, "Cancelled"),
            ("dry run", [30], {"dry_run": True}, "  - job_id: 30\n"),
            ("no match", [30], {"name_contains": "absent"}, "No Jobs found"),
        )
        for label, chosen, options, message in cases:
            with self.subTest(label):
                exit_code, output, _choose_jobs = self.select(chosen, **options)
                self.assertEqual(exit_code, 0)
                self.assertIn(message, output)
                self.assertEqual(
                    self.configuration_path.read_text(encoding="utf-8"), TEMPLATE_CONFIGURATION
                )
                self.assertEqual(list(self.project.glob(".workflow-monitoring.*")), [])

    def test_errors_leave_the_file_unchanged(self) -> None:
        invalid_timezone = TEMPLATE_CONFIGURATION.replace('"UTC"', '"Mars/Base"')
        cases = (
            ("bad time", TEMPLATE_CONFIGURATION, {"completion_time": "6am"}, {}, "HH:MM"),
            ("cli fails", TEMPLATE_CONFIGURATION, {}, {"FAKE_DATABRICKS_EXIT": "3"}, "failed"),
            ("bad json", TEMPLATE_CONFIGURATION, {}, {"FAKE_DATABRICKS_JOBS": "x"}, "invalid JSON"),
            ("cli missing", TEMPLATE_CONFIGURATION, {}, {"PATH": "/nonexistent"}, "not found"),
            ("flow list", "version: 3\nworkflows: []\n", {}, {}, "block-style"),
            ("invalid result", invalid_timezone, {}, {}, "Nothing changed"),
        )
        for label, configuration_text, options, environment, message in cases:
            with self.subTest(label):
                self.configuration_path.write_text(configuration_text, encoding="utf-8")
                with mock.patch.dict(os.environ, environment):
                    with self.assertRaisesRegex(cli.CommandError, message):
                        self.select([30], **options)
                self.assertEqual(
                    self.configuration_path.read_text(encoding="utf-8"), configuration_text
                )

    def test_structure_and_time_errors_fail_before_the_jobs_list_call(self) -> None:
        self.configuration_path.write_text("workflows: []\n", encoding="utf-8")
        with self.assertRaises(cli.CommandError):
            self.select([30])
        with self.assertRaises(cli.CommandError):
            self.select([30], completion_time="25:00")

        self.assertEqual(self.logged_commands(), [])

    def test_folder_without_project_files_is_rejected(self) -> None:
        (self.project / cli.LOADER_RELATIVE_PATH).unlink()

        with self.assertRaisesRegex(cli.CommandError, "not a workflow monitoring project"):
            self.select([30])
        self.assertEqual(self.logged_commands(), [])


class InsertionPointTests(unittest.TestCase):
    entries_yaml = "    - job_id: 7\n"

    def insert(self, configuration_text: str) -> str:
        offset, indent = cli.workflows_insertion_point(configuration_text)
        self.assertEqual(indent, "    ")
        return cli.insert_entries(configuration_text, offset, self.entries_yaml)

    def test_entries_go_before_the_next_key_and_its_comment(self) -> None:
        configuration_text = textwrap.dedent(
            """\
            workflows:
                - job_id: 1

            # Trailing note.
            default_timezone: UTC
            """
        )

        self.assertEqual(
            self.insert(configuration_text),
            "workflows:\n    - job_id: 1\n    - job_id: 7\n\n# Trailing note.\ndefault_timezone: UTC\n",
        )

    def test_missing_final_newline_is_added(self) -> None:
        self.assertEqual(
            self.insert("workflows:\n    - job_id: 1"),
            "workflows:\n    - job_id: 1\n    - job_id: 7\n",
        )

    def test_empty_list_uses_two_spaces(self) -> None:
        self.assertEqual(cli.workflows_insertion_point("version: 3\nworkflows:\n"), (22, "  "))


class InitProjectTests(FakeDatabricksTestCase):
    def init(self, *, confirmed: bool) -> tuple[int, mock.Mock, str]:
        pick_jobs = mock.Mock(return_value=5)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.init_project(
                self.root / "out",
                "test-profile",
                confirm=lambda _message: confirmed,
                pick_jobs=pick_jobs,
            )
        return exit_code, pick_jobs, output.getvalue()

    def test_init_renders_the_cli_release_tag_then_offers_the_picker(self) -> None:
        exit_code, pick_jobs, _output = self.init(confirmed=True)

        self.assertEqual(exit_code, 5)
        pick_jobs.assert_called_once_with(self.root / "out/new_project")
        self.assertEqual(
            self.logged_commands(),
            [
                [
                    "bundle",
                    "init",
                    cli.REPOSITORY_URL,
                    "--tag",
                    f"v{cli.version(cli.DISTRIBUTION_NAME)}",
                    "--template-dir",
                    "template",
                    "--output-dir",
                    str(self.root / "out"),
                    "--profile",
                    "test-profile",
                ]
            ],
        )

    def test_declined_picker_prints_the_next_step(self) -> None:
        exit_code, pick_jobs, output = self.init(confirmed=False)

        self.assertEqual(exit_code, 0)
        pick_jobs.assert_not_called()
        self.assertIn("select-jobs", output)

    def test_failed_bundle_init_returns_its_exit_code(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_EXIT": "4"}):
            exit_code, pick_jobs, _output = self.init(confirmed=True)

        self.assertEqual(exit_code, 4)
        pick_jobs.assert_not_called()


class MainTests(FakeDatabricksTestCase):
    def test_profile_is_required(self) -> None:
        for command in ("init", "select-jobs"):
            with self.subTest(command):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main([command])
                self.assertEqual(raised.exception.code, 2)

    def test_command_error_prints_one_line_and_exits_1(self) -> None:
        error_output = io.StringIO()
        with contextlib.redirect_stderr(error_output):
            exit_code = cli.main(
                ["select-jobs", "--profile", "p", "--project-dir", str(self.root / "missing")]
            )

        self.assertEqual(exit_code, 1)
        self.assertTrue(error_output.getvalue().startswith("error: "))


if __name__ == "__main__":
    unittest.main()
