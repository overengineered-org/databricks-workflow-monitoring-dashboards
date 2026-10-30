"""Tests for the workflow runner guard."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

from scripts.check_workflow_runners import check_workflow_file, check_workflows

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
FIXTURE_DIRECTORY = REPOSITORY_ROOT / "tests" / "fixtures" / "workflows"
DOCKERFILE_PATH = REPOSITORY_ROOT / ".act" / "Dockerfile"
VALIDATE_WORKFLOW_PATH = WORKFLOW_DIRECTORY / "validate.yml"

RULE_FIXTURES = {
    "missing_runs_on.yml": "has no runs-on",
    "invalid_runs_on.yml": "runs-on is not exactly ubuntu-latest",
    "reusable_workflow.yml": "uses a reusable workflow",
    "yaml_alias.yml": "YAML anchor is forbidden",
    "missing_needs.yml": "has no needs: runner-guard",
    "runner_guard_extra_step.yml": "exactly 2 steps",
}


def _workflow_jobs(path: Path) -> dict[str, dict[str, object]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document["jobs"]


def _step_with_prefix(jobs: dict[str, dict[str, object]], prefix: str) -> dict[str, object]:
    for job in jobs.values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith(f"{prefix}@"):
                return step
    raise AssertionError(f"no step uses {prefix}")


class RunnerGuardFixtureTests(unittest.TestCase):
    def test_each_failure_rule_has_a_failing_fixture(self) -> None:
        for fixture_name, expected_reason in RULE_FIXTURES.items():
            with self.subTest(fixture_name=fixture_name):
                violations = check_workflow_file(FIXTURE_DIRECTORY / fixture_name)
                self.assertTrue(
                    any(expected_reason in violation.reason for violation in violations),
                    f"{fixture_name} did not report {expected_reason}: {violations}",
                )
                for violation in violations:
                    self.assertIn(f"{fixture_name}:", str(violation))


class RepositoryWorkflowTests(unittest.TestCase):
    def test_real_workflow_files_pass(self) -> None:
        paths = sorted(WORKFLOW_DIRECTORY.glob("*.yml"))
        self.assertEqual(
            {path.name for path in paths},
            {"runner-guard.yml", "validate.yml"},
        )
        self.assertEqual(check_workflows(WORKFLOW_DIRECTORY), [])

    def test_pyyaml_cross_check_finds_the_same_runner_rule(self) -> None:
        for path in sorted(WORKFLOW_DIRECTORY.glob("*.yml")):
            with self.subTest(workflow=path.name):
                for job_name, job in _workflow_jobs(path).items():
                    self.assertEqual(
                        job.get("runs-on"),
                        "ubuntu-latest",
                        f"{path.name} job {job_name} does not use ubuntu-latest",
                    )

    def test_tool_versions_match_the_local_dockerfile(self) -> None:
        jobs = _workflow_jobs(VALIDATE_WORKFLOW_PATH)
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

        uv_version = _step_with_prefix(jobs, "astral-sh/setup-uv")["with"]["version"]
        go_version = _step_with_prefix(jobs, "actions/setup-go")["with"]["go-version"]
        golangci_version = _step_with_prefix(jobs, "golangci/golangci-lint-action")["with"][
            "version"
        ]
        databricks_version = _step_with_prefix(jobs, "databricks/setup-cli")["with"]["version"]

        self.assertEqual(uv_version, _dockerfile_value(dockerfile, r"astral-sh/uv:([0-9.]+)"))
        self.assertEqual(go_version, _dockerfile_value(dockerfile, r"golang:([0-9.]+)-bookworm"))
        self.assertEqual(
            golangci_version,
            _dockerfile_value(dockerfile, r"golangci/golangci-lint:(v[0-9.]+)"),
        )
        self.assertEqual(
            databricks_version,
            _dockerfile_value(dockerfile, r"databricks/setup-cli/v([0-9.]+)/install\.sh"),
        )


def _dockerfile_value(dockerfile: str, pattern: str) -> str:
    match = re.search(pattern, dockerfile)
    if match is None:
        raise AssertionError(f"no Dockerfile match for {pattern}")
    return match.group(1)


if __name__ == "__main__":
    unittest.main()
