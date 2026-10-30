"""Tests for the Databricks bundle template."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.build_template import (
    PRODUCT_DIRECTORY,
    TEMPLATE_PROJECT,
    VERSION_PATH,
    build_template,
    project_version,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "template"
TEMPLATE_INPUT_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "template-input.json"
GENERATED_DASHBOARD_PATH = REPOSITORY_ROOT / "src/dashboards/workflow-monitoring.lvdash.json"
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


@unittest.skipUnless(shutil.which("databricks"), "Databricks CLI is required")
class TemplateRenderTests(unittest.TestCase):
    def test_rendered_dashboard_is_byte_identical_and_prompt_free(self) -> None:
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
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
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
            self.assertEqual(render_process.returncode, 0, render_process.stderr)

            rendered_project = output_directory / input_values["project_name"]
            rendered_dashboard = (
                rendered_project
                / "workflow_monitoring"
                / "dashboard"
                / "workflow-monitoring.lvdash.json"
            )
            self.assertEqual(
                rendered_dashboard.read_bytes(),
                GENERATED_DASHBOARD_PATH.read_bytes(),
            )


class TemplateLayoutTests(unittest.TestCase):
    def test_user_owned_files_sit_outside_the_product_folder(self) -> None:
        user_owned_files = (
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
