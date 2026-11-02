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

from tests._paths import REPOSITORY_ROOT

# isort: split
import workflow_monitoring_cli as cli
from workflow_monitoring_config import load_workflow_monitoring_configuration

TRACKED_CONFIGURATION = (REPOSITORY_ROOT / cli.CONFIGURATION_FILE_NAME).read_text(encoding="utf-8")
# A configuration that already monitors Job 123456, as after a first select-jobs run.
MONITORED_CONFIGURATION = TRACKED_CONFIGURATION.replace(
    "workflows: []\n",
    "workflows:\n"
    "  - job_id: 123456\n"
    "    monitoring_status: inactive\n"
    "    sla:\n"
    "      frequency: daily\n"
    '      completion_time: "06:00"\n',
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
WAREHOUSES_LIST = ["warehouses", "list", "--profile", "test-profile", "--output", "json"]
DEV = ["-t", "dev", "--profile", "test-profile"]
PROD = ["-t", "prod", "--profile", "test-profile"]

# The fake CLI logs its arguments and folder, then answers like the real commands.
FAKE_DATABRICKS = """\
import json, os, sys
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
"""


class FakeDatabricksTestCase(unittest.TestCase):
    """Run each test with a fake databricks command and a copy of the repository bundle files."""

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

        self.project = self.root / "clone"
        for relative_path in (
            cli.BUNDLE_FILE_NAME,
            cli.CONFIGURATION_FILE_NAME,
            cli.SCHEMA_RELATIVE_PATH,
        ):
            (self.project / relative_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY_ROOT / relative_path, self.project / relative_path)
        self.configuration_path = self.project / cli.CONFIGURATION_FILE_NAME
        self.configuration_path.write_text(MONITORED_CONFIGURATION, encoding="utf-8")

        environment = {
            "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DATABRICKS_LOG": str(self.log_path),
            "FAKE_DATABRICKS_JOBS": json.dumps(WORKSPACE_JOBS),
            "FAKE_DATABRICKS_WAREHOUSES": json.dumps(WAREHOUSES),
            "FAKE_DATABRICKS_PROFILES": json.dumps(PROFILES),
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        for auth_variable in ("DATABRICKS_HOST", "DATABRICKS_CONFIG_PROFILE"):
            os.environ.pop(auth_variable, None)

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
            MONITORED_CONFIGURATION + expected_entries,
        )
        configuration = load_workflow_monitoring_configuration(
            self.configuration_path, self.project / cli.SCHEMA_RELATIVE_PATH
        )
        self.assertEqual([workflow.job_id for workflow in configuration.active_workflows], [10, 30])
        self.assertEqual(list(self.project.glob(".workflow-monitoring.*")), [])

    def test_fresh_clone_with_an_empty_list_becomes_block_style(self) -> None:
        self.configuration_path.write_text(TRACKED_CONFIGURATION, encoding="utf-8")

        exit_code, _output = self.select([30])

        self.assertEqual(exit_code, 0)
        self.choose_jobs.assert_called_once()
        self.assertEqual(self.choose_jobs.call_args.args[1], set())
        self.assertEqual(
            self.configuration_path.read_text(encoding="utf-8"),
            TRACKED_CONFIGURATION.replace(
                "workflows: []\n",
                "workflows:\n"
                + cli.workflow_entries_yaml(
                    [cli.WorkspaceJob(30, "orders nightly")], "06:00", "  "
                ),
            ),
        )
        configuration = load_workflow_monitoring_configuration(
            self.configuration_path, self.project / cli.SCHEMA_RELATIVE_PATH
        )
        self.assertEqual([workflow.job_id for workflow in configuration.active_workflows], [30])

    def test_one_job_message_is_singular(self) -> None:
        _exit_code, output = self.select([30])

        self.assertIn("Added 1 Job with", output)
        self.confirm.assert_called_once_with("Add 1 Job to workflow-monitoring.yml?")

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
                    self.configuration_path.read_text(encoding="utf-8"), MONITORED_CONFIGURATION
                )
                self.assertEqual(list(self.project.glob(".workflow-monitoring.*")), [])

    def test_errors_leave_the_file_unchanged(self) -> None:
        invalid_timezone = MONITORED_CONFIGURATION.replace('"UTC"', '"Mars/Base"')
        cases = (
            ("bad time", MONITORED_CONFIGURATION, {"completion_time": "6am"}, {}, "HH:MM"),
            ("cli fails", MONITORED_CONFIGURATION, {}, {"FAKE_DATABRICKS_FAIL": "jobs"}, "failed"),
            (
                "bad json",
                MONITORED_CONFIGURATION,
                {},
                {"FAKE_DATABRICKS_JOBS": "x"},
                "invalid JSON",
            ),
            ("cli missing", MONITORED_CONFIGURATION, {}, {"PATH": "/nonexistent"}, "not found"),
            ("flow list", "version: 3\nworkflows: [{job_id: 1}]\n", {}, {}, "block-style"),
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
        self.configuration_path.write_text("workflows: [{job_id: 1}]\n", encoding="utf-8")
        with self.assertRaises(cli.CommandError):
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

    def test_environment_auth_needs_no_profile(self) -> None:
        with mock.patch.dict(os.environ, {"DATABRICKS_HOST": "https://example.invalid"}):
            self.assertIsNone(cli.resolve_profile(None, self.prompts))

        self.assertEqual(self.logged_commands(), [["current-user", "me"]])
        self.choose_one.assert_not_called()

    def test_profile_variable_is_the_users_choice(self) -> None:
        with mock.patch.dict(os.environ, {"DATABRICKS_CONFIG_PROFILE": "test-profile"}):
            self.assertEqual(cli.resolve_profile(None, self.prompts), "test-profile")

        self.choose_one.assert_not_called()

    def test_profile_errors(self) -> None:
        no_valid = json.dumps({"profiles": [PROFILES["profiles"][1]]})
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_PROFILES": no_valid}):
            with self.assertRaisesRegex(cli.CommandError, "No signed-in Databricks profile"):
                cli.resolve_profile(None, self.prompts)
        self.choose_one.side_effect = None
        self.choose_one.return_value = None
        with self.assertRaisesRegex(cli.CommandError, "No profile selected"):
            cli.resolve_profile(None, self.prompts)

    def test_cli_version_check(self) -> None:
        for version_text in ("1.22.1", "1.30.0", "0.0.0-dev+abc"):
            with self.subTest(version_text):
                with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_VERSION": version_text}):
                    cli.check_databricks_cli()
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_VERSION": "1.21.9"}):
            with self.assertRaisesRegex(cli.CommandError, "too old"):
                cli.check_databricks_cli()


class WarehouseTests(FakeDatabricksTestCase):
    def test_saving_keeps_other_values_and_targets_stay_separate(self) -> None:
        dev_overrides = cli.variable_overrides_path(self.project, "dev")
        dev_overrides.parent.mkdir(parents=True)
        dev_overrides.write_text('{"catalog": "mine"}', encoding="utf-8")

        cli.save_warehouse_id(self.project, "dev", "wh-1")

        self.assertEqual(
            json.loads(dev_overrides.read_text()), {"catalog": "mine", "warehouse_id": "wh-1"}
        )
        self.assertIsNone(cli.saved_warehouse_id(self.project, "prod"))

    def test_broken_overrides_file_is_a_command_error(self) -> None:
        path = cli.variable_overrides_path(self.project, "dev")
        path.parent.mkdir(parents=True)
        for content, message in (('{"warehouse_id": abc}', "not valid JSON"), ("[]", "object")):
            with self.subTest(content):
                path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(cli.CommandError, message):
                    cli.saved_warehouse_id(self.project, "dev")

    def test_picker_lists_warehouses_and_rejects_an_empty_workspace(self) -> None:
        self.assertEqual(cli.choose_warehouse_id("test-profile", "prod", self.prompts), "wh-1")
        self.choose_one.assert_called_once_with(
            "SQL warehouse for the prod dashboard:",
            [("Starter (wh-1)", "wh-1"), ("Large (wh-2)", "wh-2")],
        )
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_WAREHOUSES": "[]"}):
            with self.assertRaisesRegex(cli.CommandError, "No SQL warehouse"):
                cli.choose_warehouse_id("test-profile", "dev", self.prompts)


class DeployTests(FakeDatabricksTestCase):
    def deploy(self, target: str = "dev", **options) -> tuple[int, str]:
        settings = {"warehouse_id": None, "assume_yes": False, **options}
        return self.run_quietly(
            cli.deploy_project,
            self.project,
            "test-profile",
            target=target,
            prompts=self.prompts,
            **settings,
        )

    def test_dev_deploy_asks_for_a_missing_warehouse_then_loads_first_data(self) -> None:
        exit_code, output = self.deploy()

        self.assertEqual(exit_code, 0)
        self.assertIn("Using SQL warehouse wh-1 for dev.", output)
        self.assertIn("runs the collector once on serverless compute", output)
        self.assertIn(f"databricks bundle open {cli.DASHBOARD_KEY} {' '.join(DEV)}", output)
        self.confirm.assert_called_once_with("Deploy to dev?")
        self.assertEqual(
            self.logged_commands(),
            [
                WAREHOUSES_LIST,
                ["bundle", "validate", "--strict", *DEV],
                ["bundle", "deploy", *DEV],
                ["bundle", "run", cli.COLLECTOR_JOB_KEY, *DEV],
            ],
        )
        self.assertTrue(all(entry["cwd"] == str(self.project) for entry in self.logged()[1:]))

    def test_prod_needs_its_own_warehouse_and_runs_nothing(self) -> None:
        cli.save_warehouse_id(self.project, "dev", "wh-dev")

        exit_code, output = self.deploy("prod")

        self.assertEqual(exit_code, 0)
        self.assertIn("every 5 minutes", output)
        self.assertEqual(cli.saved_warehouse_id(self.project, "prod"), "wh-1")
        self.assertEqual(
            self.logged_commands(),
            [
                WAREHOUSES_LIST,
                ["bundle", "validate", "--strict", *PROD],
                ["bundle", "deploy", *PROD],
            ],
        )

    def test_ci_deploy_with_warehouse_id_and_yes_asks_nothing(self) -> None:
        exit_code, _output = self.deploy("prod", warehouse_id="wh-ci", assume_yes=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(cli.saved_warehouse_id(self.project, "prod"), "wh-ci")
        self.choose_one.assert_not_called()
        self.confirm.assert_not_called()

    def test_yes_without_a_saved_warehouse_fails_without_asking(self) -> None:
        with self.assertRaisesRegex(cli.CommandError, "--warehouse-id"):
            self.deploy("prod", assume_yes=True)

        self.assertEqual(self.logged_commands(), [])

    def test_nothing_deploys_after_a_failed_validate_or_a_no(self) -> None:
        cli.save_warehouse_id(self.project, "dev", "wh-1")
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_FAIL": "bundle validate"}):
            self.assertEqual(self.deploy()[0], 3)
        self.confirm.return_value = False
        self.assertEqual(self.deploy()[0], 0)

        deploy_commands = [command for command in self.logged_commands() if command[1] == "deploy"]
        self.assertEqual(deploy_commands, [])

    def test_failed_collector_run_returns_its_exit_code(self) -> None:
        cli.save_warehouse_id(self.project, "dev", "wh-1")
        with mock.patch.dict(os.environ, {"FAKE_DATABRICKS_FAIL": "bundle run"}):
            exit_code, output = self.deploy()

        self.assertEqual(exit_code, 3)
        self.assertNotIn("Done.", output)


class MainTests(FakeDatabricksTestCase):
    def test_main_checks_the_cli_then_asks_for_a_profile(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = cli.main(["select-jobs", "--project-dir", str(self.project)], self.prompts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [command[:2] for command in self.logged_commands()],
            [["version", "--output"], ["auth", "profiles"], ["jobs", "list"]],
        )

    def test_wrong_folder_fails_before_any_databricks_call(self) -> None:
        error_output = io.StringIO()
        with contextlib.redirect_stderr(error_output):
            exit_code = cli.main(["deploy", "--project-dir", str(self.root / "none")], self.prompts)

        self.assertEqual(exit_code, 1)
        self.assertIn("is not the root", error_output.getvalue())
        self.assertEqual(self.logged_commands(), [])


if __name__ == "__main__":
    unittest.main()
