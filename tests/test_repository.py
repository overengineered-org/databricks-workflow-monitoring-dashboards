"""Tests for repository documentation and release tool contracts."""

from __future__ import annotations

import re
import subprocess
import unittest

import yaml

from tests._paths import REPOSITORY_ROOT

# isort: split
from workflow_monitoring_cli import MINIMUM_DATABRICKS_CLI_VERSION

BUNDLE_PATH = REPOSITORY_ROOT / "databricks.yml"
RELEASE_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/release.sh"
LOCAL_VALIDATION_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/run-local-validation.sh"
ACT_DOCKERFILE_PATH = REPOSITORY_ROOT / ".act/Dockerfile"


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


class BundleAndCliVersionContractTests(unittest.TestCase):
    """Keep the bundle minimum, the wrapper minimum, and the Act CLI pin on one number."""

    def test_bundle_cli_minimum_matches_wrapper_and_act_pin(self) -> None:
        bundle_configuration = yaml.safe_load(BUNDLE_PATH.read_text(encoding="utf-8"))
        minimum_requirement = bundle_configuration["bundle"]["databricks_cli_version"]
        minimum_version = minimum_requirement.removeprefix(">=").strip()

        act_dockerfile = ACT_DOCKERFILE_PATH.read_text(encoding="utf-8")
        dockerfile_pin = re.search(r"setup-cli/v([^/]+)/install\.sh", act_dockerfile)
        self.assertIsNotNone(dockerfile_pin, "act Dockerfile must pin the setup-cli version")

        wrapper_minimum = ".".join(str(part) for part in MINIMUM_DATABRICKS_CLI_VERSION)

        self.assertEqual(minimum_version, dockerfile_pin.group(1))
        self.assertEqual(minimum_version, wrapper_minimum)


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
