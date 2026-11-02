"""Tests for repository documentation and release tool contracts."""

from __future__ import annotations

import re
import subprocess
import tomllib
import unittest

import yaml

from tests._paths import REPOSITORY_ROOT

TEMPLATE_PROJECT_PATH = REPOSITORY_ROOT / "template/template/{{.project_name}}"
TRACKED_CONFIGURATION_PATH = TEMPLATE_PROJECT_PATH / "workflow-monitoring.yml.tmpl"
TEMPLATE_DATABRICKS_PATH = TEMPLATE_PROJECT_PATH / "databricks.yml.tmpl"
COLLECTOR_JOB_PATH = TEMPLATE_PROJECT_PATH / "resources/workflow-monitoring.collector.job.yml.tmpl"
RELEASE_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/release.sh"
LOCAL_VALIDATION_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/run-local-validation.sh"
ACT_DOCKERFILE_PATH = REPOSITORY_ROOT / ".act/Dockerfile"
VALIDATE_WORKFLOW_PATH = REPOSITORY_ROOT / ".github/workflows/validate.yml"


class RepositoryContractTests(unittest.TestCase):
    """Keep deployment and release tools on one reviewed contract."""

    def test_local_documentation_links_resolve(self) -> None:
        documentation_paths = tuple(REPOSITORY_ROOT.rglob("*.md"))
        self.assertGreaterEqual(len(documentation_paths), 4)

        for documentation_path in documentation_paths:
            documentation_text = documentation_path.read_text(encoding="utf-8")
            relative_links = re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", documentation_text)
            for relative_link in relative_links:
                if relative_link.startswith(("http://", "https://", "mailto:")):
                    continue
                relative_target, _, target_anchor = relative_link.partition("#")
                linked_document_path = (
                    (documentation_path.parent / relative_target).resolve()
                    if relative_target
                    else documentation_path
                )
                self.assertTrue(
                    linked_document_path.is_file(),
                    f"{documentation_path}: missing linked file {relative_link}",
                )
                if not target_anchor:
                    continue
                linked_document_text = linked_document_path.read_text(encoding="utf-8")
                linked_headings = re.findall(r"^#{1,6}\s+(.+)$", linked_document_text, re.MULTILINE)
                linked_anchors = {
                    re.sub(r"[ _]+", "-", re.sub(r"[^a-z0-9 _-]", "", heading.lower()))
                    for heading in linked_headings
                }
                self.assertIn(
                    target_anchor,
                    linked_anchors,
                    f"{documentation_path}: missing linked section {relative_link}",
                )


class TemplateAndCliVersionContractTests(unittest.TestCase):
    """Keep the template minimum CLI version and the pinned CLI versions on one number."""

    def test_template_cli_minimum_matches_act_and_validate_pins(self) -> None:
        bundle_configuration = yaml.safe_load(TEMPLATE_DATABRICKS_PATH.read_text(encoding="utf-8"))
        minimum_requirement = bundle_configuration["bundle"]["databricks_cli_version"]
        minimum_version = minimum_requirement.removeprefix(">=").strip()

        act_dockerfile = ACT_DOCKERFILE_PATH.read_text(encoding="utf-8")
        dockerfile_pin = re.search(r"setup-cli/v([^/]+)/install\.sh", act_dockerfile)
        self.assertIsNotNone(dockerfile_pin, "act Dockerfile must pin the setup-cli version")

        validate_workflow = VALIDATE_WORKFLOW_PATH.read_text(encoding="utf-8")
        workflow_pin = re.search(
            r"uses: databricks/setup-cli@[0-9a-f]+ # v(\d+\.\d+\.\d+)", validate_workflow
        )
        self.assertIsNotNone(workflow_pin, "validate workflow must comment the CLI version")
        workflow_version = re.search(
            r'uses: databricks/setup-cli@[0-9a-f]+ # v\d+\.\d+\.\d+\n\s+with:\n\s+version: "(\d+\.\d+\.\d+)"',
            validate_workflow,
        )
        self.assertIsNotNone(workflow_version, "validate workflow must set the CLI version")

        self.assertEqual(minimum_version, dockerfile_pin.group(1))
        self.assertEqual(minimum_version, workflow_pin.group(1))
        self.assertEqual(minimum_version, workflow_version.group(1))


class ReleaseVersionReferenceTests(unittest.TestCase):
    """Keep every tracked tag reference on the version from pyproject.toml."""

    def test_documented_tags_equal_project_version(self) -> None:
        pyproject_document = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        expected_tag = f"v{pyproject_document['project']['version']}"

        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        cli_install_tags = re.findall(r"uv tool install [^\s@]+@(v\d+\.\d+\.\d+)", readme)
        self.assertEqual(cli_install_tags, [expected_tag])
        manual_setup = (REPOSITORY_ROOT / "docs/manual-setup.md").read_text(encoding="utf-8")
        manual_tags = re.findall(r"--tag (v\d+\.\d+\.\d+)", manual_setup)
        self.assertEqual(manual_tags, [expected_tag])

        configuration_template = TRACKED_CONFIGURATION_PATH.read_text(encoding="utf-8")
        schema_tags = re.findall(
            r"raw\.githubusercontent\.com/[^/]+/[^/]+/(v\d+\.\d+\.\d+)/",
            configuration_template,
        )
        self.assertEqual(schema_tags, [expected_tag])


class ShellScriptContractTests(unittest.TestCase):
    """Keep the shell scripts parseable and the release help stable."""

    def test_release_and_validation_scripts_pass_bash_syntax_check(self) -> None:
        for script_path in (RELEASE_SCRIPT_PATH, LOCAL_VALIDATION_SCRIPT_PATH):
            with self.subTest(script_path=script_path):
                syntax_check = subprocess.run(
                    ["bash", "-n", str(script_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(syntax_check.returncode, 0, syntax_check.stderr)

    def test_release_script_help_documents_check_or_publish(self) -> None:
        help_check = subprocess.run(
            ["bash", str(RELEASE_SCRIPT_PATH), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_check.returncode, 0, help_check.stderr)
        self.assertIn("--check|--publish", help_check.stdout)


if __name__ == "__main__":
    unittest.main()
