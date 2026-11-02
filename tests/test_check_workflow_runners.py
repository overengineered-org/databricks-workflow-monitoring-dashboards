"""Tests for the workflow runner guard."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

from tests._paths import REPOSITORY_ROOT

# isort: split
from scripts.check_workflow_runners import check_workflow_file, check_workflows

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
    "flow_style_jobs.yml": "jobs must be a block mapping",
    "quoted_jobs_key.yml": "jobs must be a block mapping",
    "inline_job_value.yml": "job key 'evil' must not have an inline value",
    "quoted_job_key.yml": "quoted key is forbidden under jobs",
    "explicit_job_key.yml": "explicit key is forbidden under jobs",
    "guard_run_substring.yml": "runner-guard step run must equal",
    "guard_continue_on_error.yml": "runner-guard job key 'continue-on-error' is not allowed",
    "guard_step_if.yml": "runner-guard step 2 key 'if' is not allowed",
    "later_job_always.yml": "job 'validate' must not have if",
    "notsuccess.yml": "job 'validate' must not have if",
    "blockif.yml": "job 'validate' must not have if",
    "checkoutref.yml": "runner-guard step 1 key 'with' is not allowed",
    "shell.yml": "runner-guard step 2 key 'shell' is not allowed",
    "bashenv.yml": "workflow key 'env' is not allowed",
    "defaults.yml": "runner-guard job key 'defaults' is not allowed",
    "workflow_defaults.yml": "workflow key 'defaults' is not allowed",
    "permissions_missing.yml": "workflow has no top-level permissions",
    "permissions_write.yml": "workflow permissions must be exactly contents: read",
    "job_permissions_write.yml": "job 'release' must not set permissions",
    "release/other_job_write/release.yml": "job 'validate' must not set permissions",
    "release/extra_scope/release.yml": "release job permissions must be exactly contents: write",
}


def _workflow_paths(directory: Path) -> list[Path]:
    paths: list[Path] = []
    for pattern in ("*.yml", "*.yaml"):
        paths.extend(directory.glob(pattern))
    return sorted(paths)


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

    def test_pyyaml_cross_check_applies_to_every_fixture(self) -> None:
        for path in _workflow_paths(FIXTURE_DIRECTORY):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            jobs = document.get("jobs") if isinstance(document, dict) else None
            if not isinstance(jobs, dict):
                continue
            non_ubuntu_jobs = {
                job_name
                for job_name, job in jobs.items()
                if isinstance(job, dict) and job.get("runs-on") != "ubuntu-latest"
            }
            if not non_ubuntu_jobs:
                continue
            with self.subTest(fixture=path.name):
                self.assertTrue(
                    check_workflow_file(path),
                    f"{path.name} has non-ubuntu jobs {sorted(non_ubuntu_jobs)} but the guard passed",
                )

    def test_release_fixture_passes(self) -> None:
        passing_release = FIXTURE_DIRECTORY / "release" / "passing" / "release.yml"
        self.assertEqual(check_workflow_file(passing_release), [])


class RepositoryWorkflowTests(unittest.TestCase):
    def test_real_workflow_files_pass(self) -> None:
        paths = _workflow_paths(WORKFLOW_DIRECTORY)
        self.assertEqual(
            {path.name for path in paths},
            {"dco.yml", "release.yml", "runner-guard.yml", "validate.yml"},
        )
        self.assertEqual(check_workflows(WORKFLOW_DIRECTORY), [])

    def test_pyyaml_cross_check_finds_the_same_runner_rule(self) -> None:
        for path in _workflow_paths(WORKFLOW_DIRECTORY):
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

        self.assertEqual(uv_version, _dockerfile_value(dockerfile, r"astral-sh/uv:([0-9.]+)"))


def _dockerfile_value(dockerfile: str, pattern: str) -> str:
    match = re.search(pattern, dockerfile)
    if match is None:
        raise AssertionError(f"no Dockerfile match for {pattern}")
    return match.group(1)


if __name__ == "__main__":
    unittest.main()
