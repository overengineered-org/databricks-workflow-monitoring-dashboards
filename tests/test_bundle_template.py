"""Tests for the Databricks bundle template."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from tests._paths import REPOSITORY_ROOT

# isort: split
from scripts.build_template import (
    PRODUCT_DIRECTORY,
    TEMPLATE_PROJECT,
    VERSION_PATH,
    build_template,
    project_version,
)
from workflow_monitoring_config import load_workflow_monitoring_configuration

# isort: split
TEMPLATE_ROOT = REPOSITORY_ROOT / "template"
TEMPLATE_INPUT_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "template-input.json"
TEMPLATE_CONFIGURATION_PATH = TEMPLATE_PROJECT / "workflow-monitoring.yml.tmpl"


class TemplateBuildTests(unittest.TestCase):
    def test_product_files_match_their_sources(self) -> None:
        self.assertEqual(build_template(check=True), [])

    def test_version_matches_pyproject_and_schema_url(self) -> None:
        version = project_version()
        self.assertEqual(VERSION_PATH.read_text(encoding="utf-8").strip(), version)
        self.assertIn(
            f"/v{version}/template/template/%7B%7B.project_name%7D%7D/"
            "workflow_monitoring/workflow-monitoring.schema.json",
            TEMPLATE_CONFIGURATION_PATH.read_text(encoding="utf-8"),
        )

    def test_readme_release_tag_matches_pyproject_version(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        release_tags = re.findall(r"--tag (v[0-9]+\.[0-9]+\.[0-9]+)", readme)
        self.assertEqual(release_tags, [f"v{project_version()}"])

    def test_schema_does_not_prompt_for_warehouse_or_profile(self) -> None:
        schema = json.loads(
            (TEMPLATE_ROOT / "databricks_template_schema.json").read_text(encoding="utf-8")
        )
        properties = schema["properties"]
        self.assertNotIn("warehouse_id", properties)
        self.assertNotIn("profile", properties)
        self.assertEqual(
            set(properties),
            {
                "project_name",
                "catalog",
                "schema",
                "create_catalog_and_schema_if_missing",
                "default_timezone",
                "dashboard_viewer_group",
            },
        )


def _render_template(output_directory: Path) -> Path:
    input_values = json.loads(TEMPLATE_INPUT_PATH.read_text(encoding="utf-8"))
    render_environment = os.environ.copy()
    for variable_name in ("DATABRICKS_CONFIG_FILE", "DATABRICKS_CONFIG_PROFILE"):
        render_environment.pop(variable_name, None)
    # The pinned CLI asks for credentials even when the template uses no workspace helper.
    # Fake values keep the render offline and independent of the developer profile.
    render_environment.update(
        {
            "DATABRICKS_HOST": "https://example.invalid",
            "DATABRICKS_TOKEN": "fake",
        }
    )
    render_process = subprocess.run(
        [
            "databricks",
            "bundle",
            "init",
            str(TEMPLATE_ROOT),
            "--config-file",
            str(TEMPLATE_INPUT_PATH),
            "--output-dir",
            str(output_directory),
        ],
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=render_environment,
    )
    assert render_process.returncode == 0, render_process.stderr
    return output_directory / input_values["project_name"]


def _require_databricks_cli() -> None:
    """Fail under CI when the CLI is missing. Skip the render tests only locally."""
    if shutil.which("databricks"):
        return
    if os.environ.get("CI"):
        raise AssertionError(
            "Databricks CLI is missing while CI is set. The render gate must not pass."
        )
    raise unittest.SkipTest("Databricks CLI is required")


class TemplateRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        _require_databricks_cli()

    def test_rendered_product_files_are_byte_identical_and_prompt_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            rendered_project = _render_template(output_directory)
            rendered_product_directory = rendered_project / "workflow_monitoring"
            product_files = sorted(path for path in PRODUCT_DIRECTORY.rglob("*") if path.is_file())
            self.assertTrue(product_files)
            for product_file in product_files:
                relative_path = product_file.relative_to(PRODUCT_DIRECTORY)
                with self.subTest(product_file=str(relative_path)):
                    self.assertEqual(
                        (rendered_product_directory / relative_path).read_bytes(),
                        product_file.read_bytes(),
                    )

            rendered_gitignore = rendered_project / ".gitignore"
            self.assertTrue(rendered_gitignore.is_file())
            self.assertEqual(
                rendered_gitignore.read_bytes(),
                (TEMPLATE_PROJECT / ".gitignore").read_bytes(),
            )
            self.assertEqual(rendered_gitignore.read_text(encoding="utf-8"), ".databricks/\n")

    def test_rendered_configuration_loads_with_the_python_validator(self) -> None:
        # Replaces the deleted "Test CLI with the Python validator" step: the configuration
        # that bundle init renders must load with the Python validator.
        with tempfile.TemporaryDirectory() as temporary_directory:
            rendered_project = _render_template(Path(temporary_directory))
            configuration = load_workflow_monitoring_configuration(
                rendered_project / "workflow-monitoring.yml",
                rendered_project / "workflow_monitoring" / "workflow-monitoring.schema.json",
            )
            self.assertEqual(configuration.active_workflows, ())

    def test_every_rendered_yaml_file_parses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            rendered_project = _render_template(Path(temporary_directory))
            rendered_yaml_files = sorted(rendered_project.rglob("*.yml"))
            self.assertTrue(rendered_yaml_files)
            for rendered_yaml_file in rendered_yaml_files:
                with self.subTest(
                    rendered_yaml_file=str(rendered_yaml_file.relative_to(rendered_project))
                ):
                    yaml_document = yaml.safe_load(rendered_yaml_file.read_text(encoding="utf-8"))
                    self.assertIsInstance(yaml_document, dict)


class TemplateLayoutTests(unittest.TestCase):
    def test_user_owned_files_sit_outside_the_product_folder(self) -> None:
        user_owned_files = (
            TEMPLATE_PROJECT / ".gitignore",
            TEMPLATE_PROJECT / "databricks.yml.tmpl",
            TEMPLATE_PROJECT / "workflow-monitoring.yml.tmpl",
            TEMPLATE_PROJECT / "resources/workflow-monitoring.dashboard.yml.tmpl",
            TEMPLATE_PROJECT / "resources/workflow-monitoring.collector.job.yml.tmpl",
        )
        product_files = (
            PRODUCT_DIRECTORY / "VERSION",
            PRODUCT_DIRECTORY / "README.md",
            PRODUCT_DIRECTORY / "collector/collect_workflow_monitoring_jobs_api.py",
            PRODUCT_DIRECTORY / "collector/workflow_monitoring_config.py",
            PRODUCT_DIRECTORY / "dashboard/workflow-monitoring.lvdash.json",
            PRODUCT_DIRECTORY / "workflow-monitoring.schema.json",
        )
        for path in (*user_owned_files, *product_files):
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), f"missing {path}")
        for path in user_owned_files:
            self.assertNotIn("workflow_monitoring/", str(path.relative_to(TEMPLATE_PROJECT)))


if __name__ == "__main__":
    unittest.main()
