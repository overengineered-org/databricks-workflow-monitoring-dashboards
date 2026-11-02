"""Tests for the workflow-monitoring CLI with a fake Databricks CLI on PATH."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
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
WAREHOUSES = [{"id": "wh-1", "name": "Starter"}, {"id": "wh-2", "name": "Large"}]
PROFILES = {
    "profiles": [
        {"name": "DEFAULT", "host": "https://example.invalid", "valid": True},
        {"name": "expired", "host": "https://old.invalid", "valid": False},
    ]
}
JOBS_LIST = ["jobs", "list", "--profile", "test-profile", "--output", "json"]
DEV = ["-t", "dev", "--profile", "test-profile"]

# The fake CLI logs its arguments and folder, then answers like the real commands.
FAKE_DATABRICKS = """\
import json, os, shutil, sys
from pathlib import Path
arguments = sys.argv[1:]
with open(os.environ["FAKE_DATABRICKS_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"arguments": arguments, "cwd": os.getcwd()}) + "\\n")
if " ".join(arguments).startswith(os.environ.get("FAKE_DATABRICKS_FAIL", "never")):
    print("fake failure", file=sys.stderr)
    sys.exit(3)
command = arguments[:2]
if command == ["version", "--output"]:
    print(json.dumps({"Version": os.environ.get("FAKE_DATABRICKS_VERSION", "1.22.1")}))
elif command == ["auth", "profiles"]:
    print(os.environ["FAKE_DATABRICKS_PROFILES"])
elif command == ["current-user", "me"]:
    sys.exit(1 if "signed-out" in arguments else 0)
elif command == ["jobs", "list"]:
    print(os.environ["FAKE_DATABRICKS_JOBS"])
elif command == ["warehouses", "list"]:
    print(os.environ["FAKE_DATABRICKS_WAREHOUSES"])
elif command == ["bundle", "init"]:
    output_directory = Path(arguments[arguments.index("--output-dir") + 1])
    shutil.copytree(os.environ["FAKE_PROJECT_SOURCE"], output_directory / "new_project")
"""


def write_project(project_directory: Path) -> None:
    """Build a project the way bundle init renders it, from the real template files."""

    shutil.copytree(PRODUCT_DIRECTORY, project_directory / "workflow_monitoring")
    (project_directory / cli.BUNDLE_FILE_NAME).write_text("bundle: {}\n", encoding="utf-8")
    (project_directory / cli.CONFIGURATION_FILE_NAME).write_text(
        TEMPLATE_CONFIGURATION, encoding="utf-8"
    )


class FakeDatabricksTestCase(unittest.TestCase):
    """Run each test with a fake databricks command and a project built from the template."""

    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()

        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        fake_command = bin_directory / "databricks"
        fake_command.write_text(f"#!{sys.executable}\n{FAKE_DATABRICKS}", encoding="utf-8")
        fake_command.chmod(0o755)
        self.log_path = self.root / "databricks.log"
        self.project = self.root / "project"
        write_project(self.project)
        self.configuration_path = self.project / cli.CONFIGURATION_FILE_NAME

        environment = {
            "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DATABRICKS_LOG": str(self.log_path),
            "FAKE_DATABRICKS_JOBS": json.dumps(WORKSPACE_JOBS),
            "FAKE_DATABRICKS_WAREHOUSES": json.dumps(WAREHOUSES),
            "FAKE_DATABRICKS_PROFILES": json.dumps(PROFILES),
            "FAKE_PROJECT_SOURCE": str(self.project),
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.choose_one = mock.Mock(side_effect=lambda _message, choices: choices[0][1])
        self.choose_jobs = mock.Mock(return_value=[])
        self.confirm = mock.Mock(return_value=True)
        self.prompts = cli.Prompts(
            choose_one=self.choose_one, choose_jobs=self.choose_jobs, confirm=self.confirm
        )

    def logged(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def logged_commands(self) -> list[list[str]]:
        return [entry["arguments"] for entry in self.logged()]

    def run_quietly(self, function, *arguments, **options) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = function(*arguments, **options)
        return exit_code, output.getvalue()

    def select(self, chosen_job_ids: list[int], **options) -> tuple[int, str]:
        self.choose_jobs.return_value = chosen_job_ids
        settings = {"name_contains": None, "completion_time": cli.DEFAULT_COMPLETION_TIME}
        settings.update({"dry_run": False, **options})
        return self.run_quietly(
            cli.select_jobs, self.project, "test-profile", prompts=self.prompts, **settings
        )


class SelectJobsTests(FakeDatabricksTestCase):
    def test_ticked_jobs_are_appended_and_pass_the_collector_rules(self) -> None:
        exit_code, output = self.select([30, 10], completion_time="07:45")

        self.assertEqual(exit_code, 0)
        self.assertIn("Added 2 Jobs", output)
        self.assertEqual(self.logged_commands(), [JOBS_LIST])
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
        self.select([])

        self.choose_jobs.assert_called_once_with(
            [
                cli.WorkspaceJob(20, "(unnamed)"),
                cli.WorkspaceJob(10, "Billing\nexport"),
                cli.WorkspaceJob(30, "orders nightly"),
                cli.WorkspaceJob(123456, "placeholder twin"),
            ],
            {123456},
        )

    def test_name_filter_ignores_case(self) -> None:
        self.select([], name_contains="ORDERS")

        self.assertEqual(
            self.choose_jobs.call_args.args[0], [cli.WorkspaceJob(30, "orders nightly")]
        )

    def test_nothing_changes_without_a_new_confirmed_selection(self) -> None:
        cases = (
            ("no ticks", [], {}, True, "No new Jobs selected"),
            ("only monitored", [123456], {}, True, "No new Jobs selected"),
            ("cancelled", [30], {}, False, "Cancelled"),
            ("dry run", [30], {"dry_run": True}, True, "  - job_id: 30\n"),
            ("no match", [30], {"name_contains": "absent"}, True, "No Jobs found"),
        )
        for label, chosen, options, confirmed, message in cases:
            with self.subTest(label):
                self.confirm.return_value = confirmed
                exit_code, output = self.select(chosen, **options)
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
            ("cli fails", TEMPLATE_CONFIGURATION, {}, {"FAKE_DATABRICKS_FAIL": "jobs"}, "failed"),
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
        with self.assertRaises(cli.CommandError):
            self.select([30], completion_time="25:00")
        self.configuration_path.write_text("workflows: []\n", encoding="utf-8")
        with self.assertRaises(cli.CommandError):
            self.select([30])

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
        self.assertEqual(
            self.insert("workflows:\n    - job_id: 1\n\n# Note.\ndefault_timezone: UTC\n"),
            "workflows:\n    - job_id: 1\n    - job_id: 7\n\n# Note.\ndefault_timezone: UTC\n",
        )

    def test_missing_final_newline_is_added(self) -> None:
        self.assertEqual(
            self.insert("workflows:\n    - job_id: 1"),
            "workflows:\n    - job_id: 1\n    - job_id: 7\n",
        )

    def test_empty_list_uses_two_spaces(self) -> None:
        self.assertEqual(cli.workflows_insertion_point("version: 3\nworkflows:\n"), (22, "  "))


class ProfileAndCliTests(FakeDatabricksTestCase):
    def test_explicit_profile_must_be_signed_in(self) -> None:
        self.assertEqual(cli.resolve_profile("test-profile", self.prompts), "test-profile")
        with self.assertRaisesRegex(cli.CommandError, "databricks auth login"):
            cli.resolve_profile("signed-out", self.prompts)
        self.choose_one.assert_not_called()

    def test_user_picks_from_signed_in_profiles_only(self) -> None:
        self.assertEqual(cli.resolve_profile(None, self.prompts), "DEFAULT")

        self.choose_one.assert_called_once_with(
            "Databricks profile:", [("DEFAULT (https://example.invalid)", "DEFAULT")]
        )

    def test_profile_errors(self) -> None:
        no_valid = json.dumps({"profiles": [PROFILES["profiles"][1]]})
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_PROFILES": no_valid}):
            with self.assertRaisesRegex(cli.CommandError, "No signed-in Databricks profile"):
                cli.resolve_profile(None, self.prompts)
        self.choose_one.side_effect = None
        self.choose_one.return_value = None
        with self.assertRaisesRegex(cli.CommandError, "No profile selected"):
            cli.resolve_profile(None, self.prompts)

    def test_old_databricks_cli_is_rejected(self) -> None:
        cli.check_databricks_cli()
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_VERSION": "1.21.9"}):
            with self.assertRaisesRegex(cli.CommandError, "too old"):
                cli.check_databricks_cli()


class WarehouseTests(FakeDatabricksTestCase):
    def test_choice_is_saved_for_each_target_and_keeps_other_values(self) -> None:
        dev_overrides = cli.variable_overrides_path(self.project, "dev")
        dev_overrides.parent.mkdir(parents=True)
        dev_overrides.write_text('{"catalog": "mine"}', encoding="utf-8")

        cli.choose_warehouse(self.project, "test-profile", self.prompts, cli.BUNDLE_TARGETS)

        self.assertEqual(
            json.loads(dev_overrides.read_text()), {"catalog": "mine", "warehouse_id": "wh-1"}
        )
        self.assertEqual(cli.saved_warehouse_id(self.project, "prod"), "wh-1")
        self.assertEqual(
            self.choose_one.call_args.args[1],
            [("Starter (wh-1)", "wh-1"), ("Large (wh-2)", "wh-2")],
        )

    def test_workspace_without_warehouses_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_WAREHOUSES": "[]"}):
            with self.assertRaisesRegex(cli.CommandError, "No SQL warehouse"):
                cli.choose_warehouse(self.project, "test-profile", self.prompts, ("dev",))


class DeployTests(FakeDatabricksTestCase):
    def deploy(self, target: str = "dev", assume_yes: bool = False) -> tuple[int, str]:
        return self.run_quietly(
            cli.deploy_project,
            self.project,
            "test-profile",
            target=target,
            assume_yes=assume_yes,
            prompts=self.prompts,
        )

    def test_dev_deploy_asks_for_a_missing_warehouse_then_loads_first_data(self) -> None:
        exit_code, output = self.deploy()

        self.assertEqual(exit_code, 0)
        self.assertIn("runs the collector once on serverless compute", output)
        self.confirm.assert_called_once_with("Deploy to dev?")
        self.assertEqual(cli.saved_warehouse_id(self.project, "dev"), "wh-1")
        self.assertEqual(
            self.logged_commands(),
            [
                ["warehouses", "list", "--profile", "test-profile", "--output", "json"],
                ["bundle", "validate", "--strict", *DEV],
                ["bundle", "deploy", *DEV],
                ["bundle", "run", cli.COLLECTOR_JOB_KEY, *DEV],
            ],
        )
        self.assertTrue(all(entry["cwd"] == str(self.project) for entry in self.logged()[1:]))

    def test_prod_deploy_runs_nothing_and_saved_warehouse_is_reused(self) -> None:
        cli.choose_warehouse(self.project, "test-profile", self.prompts, ("prod",))
        self.log_path.unlink()

        exit_code, output = self.deploy("prod", assume_yes=True)

        self.assertEqual(exit_code, 0)
        self.assertIn("every 5 minutes", output)
        self.confirm.assert_not_called()
        prod = ["-t", "prod", "--profile", "test-profile"]
        self.assertEqual(
            self.logged_commands(),
            [["bundle", "validate", "--strict", *prod], ["bundle", "deploy", *prod]],
        )

    def test_nothing_deploys_after_a_failed_validate_or_a_no(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_FAIL": "bundle validate"}):
            self.assertEqual(self.deploy()[0], 3)
        self.confirm.return_value = False
        self.assertEqual(self.deploy()[0], 0)

        deploy_commands = [command for command in self.logged_commands() if command[1] == "deploy"]
        self.assertEqual(deploy_commands, [])


class InitProjectTests(FakeDatabricksTestCase):
    def init(self) -> tuple[int, str]:
        return self.run_quietly(
            cli.init_project,
            self.root / "out",
            "test-profile",
            name_contains=None,
            completion_time=cli.DEFAULT_COMPLETION_TIME,
            prompts=self.prompts,
        )

    def test_init_creates_saves_picks_and_validates(self) -> None:
        self.choose_jobs.return_value = [30]

        exit_code, output = self.init()

        new_project = self.root / "out/new_project"
        self.assertEqual(exit_code, 0)
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
                ],
                ["warehouses", "list", "--profile", "test-profile", "--output", "json"],
                JOBS_LIST,
                ["bundle", "validate", "--strict", *DEV],
            ],
        )
        self.assertEqual(self.logged()[-1]["cwd"], str(new_project))
        for target in cli.BUNDLE_TARGETS:
            self.assertEqual(cli.saved_warehouse_id(new_project, target), "wh-1")
        self.assertIn("- job_id: 30", (new_project / cli.CONFIGURATION_FILE_NAME).read_text())
        self.assertIn("workflow-monitoring deploy --profile test-profile", output)

    def test_failed_bundle_init_stops_with_its_exit_code(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_FAIL": "bundle init"}):
            self.assertEqual(self.init()[0], 3)

        self.choose_one.assert_not_called()


class MainTests(FakeDatabricksTestCase):
    def test_main_checks_the_cli_then_asks_for_a_profile(self) -> None:
        self.choose_jobs.return_value = []
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = cli.main(["select-jobs", "--project-dir", str(self.project)], self.prompts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [command[:2] for command in self.logged_commands()],
            [["version", "--output"], ["auth", "profiles"], ["jobs", "list"]],
        )

    def test_command_error_prints_one_line_and_exits_1(self) -> None:
        error_output = io.StringIO()
        with contextlib.redirect_stderr(error_output):
            exit_code = cli.main(
                ["deploy", "--profile", "test-profile", "--project-dir", str(self.root / "none")],
                self.prompts,
            )

        self.assertEqual(exit_code, 1)
        self.assertTrue(error_output.getvalue().startswith("error: "))


if __name__ == "__main__":
    unittest.main()
