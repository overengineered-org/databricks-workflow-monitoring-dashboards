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

from tests._paths import REPOSITORY_ROOT

# isort: split
import workflow_monitoring_cli as cli
from workflow_monitoring_config import load_workflow_monitoring_configuration

SCHEMA_PATH = REPOSITORY_ROOT / "src/workflow-monitoring.schema.json"
TEMPLATE_CONFIGURATION = textwrap.dedent(
    """\
    # Keep this comment.
    version: 3

    default_timezone: "UTC"

    workflows:
      # Job names come from the Jobs API.
      - job_id: 123456
        monitoring_status: inactive
        sla:
          frequency: daily
          completion_time: "06:00"
    """
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
    print("fake failure", file=sys.stderr)
    sys.exit(exit_code)
if arguments[:2] == ["jobs", "list"]:
    print(os.environ["FAKE_DATABRICKS_JOBS"])
elif arguments[:2] == ["bundle", "init"]:
    project = Path(arguments[arguments.index("--output-dir") + 1]) / "new_project"
    (project / "workflow_monitoring").mkdir(parents=True)
    (project / "workflow-monitoring.yml").write_text("workflows:\\n", encoding="utf-8")
"""


class FakeDatabricksTestCase(unittest.TestCase):
    """Run each test with a fake databricks command and a rendered project folder."""

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
        (self.project / "workflow_monitoring").mkdir(parents=True)
        shutil.copyfile(SCHEMA_PATH, self.project / cli.SCHEMA_RELATIVE_PATH)
        self.configuration_path = self.project / cli.CONFIGURATION_FILE_NAME
        self.configuration_path.write_text(TEMPLATE_CONFIGURATION, encoding="utf-8")

    def logged_commands(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def select(self, chosen_job_ids, *, confirmed=True, **options) -> tuple[int, str]:
        settings = {"name_contains": None, "completion_time": "06:00", "dry_run": False}
        settings.update(options)
        self.offered_jobs: list[cli.WorkspaceJob] = []
        self.offered_monitored: set[int] = set()

        def choose_jobs(jobs, monitored_job_ids):
            self.offered_jobs = list(jobs)
            self.offered_monitored = set(monitored_job_ids)
            return chosen_job_ids

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.select_jobs(
                self.project,
                "test-profile",
                choose_jobs=choose_jobs,
                confirm=lambda _message: confirmed,
                **settings,
            )
        return exit_code, output.getvalue()


class SelectJobsTests(FakeDatabricksTestCase):
    def test_ticked_jobs_are_appended_and_pass_the_collector_rules(self) -> None:
        exit_code, output = self.select([30, 10], completion_time="07:45")

        self.assertEqual(exit_code, 0)
        self.assertIn("Added 2 Jobs", output)
        self.assertEqual(
            self.logged_commands(),
            [["jobs", "list", "--output", "json", "--profile", "test-profile"]],
        )
        updated_text = self.configuration_path.read_text(encoding="utf-8")
        self.assertTrue(updated_text.startswith(TEMPLATE_CONFIGURATION))
        self.assertTrue(
            updated_text.endswith(
                textwrap.indent(
                    textwrap.dedent(
                        """\
                        # Billing export
                        - job_id: 10
                          monitoring_status: active
                          sla:
                            frequency: daily
                            completion_time: "07:45"
                        # orders nightly
                        - job_id: 30
                          monitoring_status: active
                          sla:
                            frequency: daily
                            completion_time: "07:45"
                        """
                    ),
                    "  ",
                )
            )
        )
        configuration = load_workflow_monitoring_configuration(self.configuration_path, SCHEMA_PATH)
        self.assertEqual([workflow.job_id for workflow in configuration.active_workflows], [10, 30])
        self.assertEqual(list(self.project.glob(".workflow-monitoring.*")), [])

    def test_picker_gets_sorted_jobs_and_marks_monitored_ids(self) -> None:
        self.select([])

        self.assertEqual(
            self.offered_jobs,
            [
                cli.WorkspaceJob(20, "(unnamed)"),
                cli.WorkspaceJob(10, "Billing\nexport"),
                cli.WorkspaceJob(30, "orders nightly"),
                cli.WorkspaceJob(123456, "placeholder twin"),
            ],
        )
        self.assertEqual(self.offered_monitored, {123456})

    def test_name_filter_ignores_case(self) -> None:
        self.select([], name_contains="ORDERS")

        self.assertEqual(self.offered_jobs, [cli.WorkspaceJob(30, "orders nightly")])

    def test_nothing_changes_without_a_new_confirmed_selection(self) -> None:
        cases = (
            ("no ticks", [], {}, "No new Jobs selected"),
            ("only monitored", [123456], {}, "No new Jobs selected"),
            ("cancelled", [30], {"confirmed": False}, "Cancelled"),
            ("dry run", [30], {"dry_run": True}, "Dry run"),
            ("no match", [30], {"name_contains": "absent"}, "No Jobs found"),
        )
        for label, chosen, options, message in cases:
            with self.subTest(label):
                exit_code, output = self.select(chosen, **options)
                self.assertEqual(exit_code, 0)
                self.assertIn(message, output)
                self.assertEqual(
                    self.configuration_path.read_text(encoding="utf-8"), TEMPLATE_CONFIGURATION
                )

    def test_dry_run_prints_the_entries(self) -> None:
        _exit_code, output = self.select([30], dry_run=True)

        self.assertIn("  - job_id: 30\n", output)

    def test_errors_leave_the_file_unchanged(self) -> None:
        cases = (
            ("bad time", {"completion_time": "6am"}, {}, None, "HH:MM"),
            ("cli fails", {}, {"FAKE_DATABRICKS_EXIT": "3"}, None, "fake failure"),
            ("bad json", {}, {"FAKE_DATABRICKS_JOBS": "not json"}, None, "invalid JSON"),
            ("cli missing", {}, {"PATH": str(self.root / "empty")}, None, "not found"),
            ("flow list", {}, {}, "version: 3\nworkflows: []\n", "block-style"),
            (
                "invalid result",
                {},
                {},
                TEMPLATE_CONFIGURATION.replace('"UTC"', '"Mars/Base"'),
                "Nothing changed",
            ),
        )
        for label, options, environment, configuration_text, message in cases:
            with self.subTest(label):
                original_text = configuration_text or TEMPLATE_CONFIGURATION
                self.configuration_path.write_text(original_text, encoding="utf-8")
                with mock.patch.dict(os.environ, environment):
                    with self.assertRaisesRegex(cli.CommandError, message):
                        self.select([30], **options)
                self.assertEqual(self.configuration_path.read_text(encoding="utf-8"), original_text)

    def test_folder_without_project_files_is_rejected(self) -> None:
        (self.project / cli.SCHEMA_RELATIVE_PATH).unlink()

        with self.assertRaisesRegex(cli.CommandError, "not a workflow monitoring project"):
            self.select([30])
        self.assertEqual(self.logged_commands(), [])


class AddWorkflowEntriesTests(unittest.TestCase):
    jobs = [cli.WorkspaceJob(7, "seven")]

    def test_entries_go_before_the_next_top_level_key_with_the_list_indent(self) -> None:
        configuration_text = textwrap.dedent(
            """\
            workflows:
                - job_id: 1
                  monitoring_status: inactive
                  sla: {frequency: daily, completion_time: "06:00"}

            # Trailing note.
            default_timezone: UTC
            version: 3
            """
        )

        updated_text = cli.add_workflow_entries(configuration_text, self.jobs, "06:00")

        self.assertIn(
            '"06:00"}\n    # seven\n    - job_id: 7\n',
            updated_text,
        )
        self.assertTrue(
            updated_text.endswith("\n# Trailing note.\ndefault_timezone: UTC\nversion: 3\n")
        )

    def test_empty_list_and_missing_final_newline(self) -> None:
        self.assertEqual(
            cli.add_workflow_entries("version: 3\nworkflows:", self.jobs, "06:00"),
            "version: 3\nworkflows:\n" + cli.workflow_entries_yaml(self.jobs, "06:00", "  "),
        )


class InitProjectTests(FakeDatabricksTestCase):
    def init(self, *, confirmed: bool) -> tuple[int, list[Path], str]:
        picked: list[Path] = []
        output = io.StringIO()

        def pick_jobs(project_directory: Path) -> int:
            picked.append(project_directory)
            return 5

        with contextlib.redirect_stdout(output):
            exit_code = cli.init_project(
                self.root / "out",
                "test-profile",
                confirm=lambda _message: confirmed,
                pick_jobs=pick_jobs,
            )
        return exit_code, picked, output.getvalue()

    def test_init_renders_the_cli_release_tag_then_offers_the_picker(self) -> None:
        exit_code, picked, _output = self.init(confirmed=True)

        self.assertEqual(exit_code, 5)
        self.assertEqual(picked, [self.root / "out/new_project"])
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
        exit_code, picked, output = self.init(confirmed=False)

        self.assertEqual((exit_code, picked), (0, []))
        self.assertIn("select-jobs", output)

    def test_failed_bundle_init_returns_its_exit_code(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_EXIT": "4"}):
            exit_code, picked, _output = self.init(confirmed=True)

        self.assertEqual((exit_code, picked), (4, []))


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
