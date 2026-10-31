"""Tests for the run-time workflow monitoring configuration."""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from jsonschema import Draft202012Validator

from src.workflow_monitoring_config import (
    CONFIGURATION_VERSION_2_MESSAGE,
    _validate_schedule_fields,
    load_workflow_monitoring_configuration,
    workflow_sla_config_rows,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY_ROOT / "src/workflow-monitoring.schema.json"


def _load_configuration_yaml(configuration_yaml: str):
    with tempfile.TemporaryDirectory() as temporary_directory:
        configuration_path = Path(temporary_directory) / "workflow-monitoring.yml"
        configuration_path.write_text(textwrap.dedent(configuration_yaml), encoding="utf-8")
        return load_workflow_monitoring_configuration(configuration_path, SCHEMA_PATH)


class ConfigurationTests(unittest.TestCase):
    """Check every public schedule and important invalid input."""

    def test_supported_schedules_and_invalid_inputs(self) -> None:
        test_cases = (
            (
                "malformed YAML",
                "version: [",
                None,
                "parse workflow monitoring configuration",
            ),
            (
                "all supported schedules",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: daily
                      completion_time: "06:00"
                  - job_id: 2
                    monitoring_status: active
                    sla:
                      frequency: weekly
                      completion_time: "07:00"
                      day_of_week: monday
                  - job_id: 3
                    monitoring_status: active
                    sla:
                      frequency: fortnightly
                      completion_time: "08:00"
                      first_deadline_date: "2026-01-12"
                  - job_id: 4
                    monitoring_status: active
                    sla:
                      frequency: monthly
                      completion_time: "09:00"
                      timezone: Australia/Melbourne
                      day_of_month: 31
                """,
                4,
                None,
            ),
            (
                "inactive workflow remains fully valid",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 8
                    monitoring_status: inactive
                    sla: {frequency: daily, completion_time: "06:00"}
                """,
                0,
                None,
            ),
            (
                "duplicate job ID",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 9
                    monitoring_status: active
                    sla: {frequency: daily, completion_time: "06:00"}
                  - job_id: 9
                    monitoring_status: active
                    sla: {frequency: daily, completion_time: "07:00"}
                """,
                None,
                r"workflows\[1\]\.job_id: duplicates workflows\[0\]",
            ),
            (
                "daily rejects weekly field",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: daily
                      completion_time: "06:00"
                      day_of_week: monday
                """,
                None,
                r"workflows\[0\]\.sla\.day_of_week: not allowed when frequency is daily",
            ),
            (
                "invalid timezone",
                """
                version: 3
                default_timezone: Nowhere/Invalid
                workflows: []
                """,
                None,
                "is not an IANA timezone",
            ),
        )

        for case_name, configuration_yaml, expected_workflow_count, expected_error in test_cases:
            with self.subTest(case_name):
                if expected_error:
                    with self.assertRaisesRegex(ValueError, expected_error):
                        _load_configuration_yaml(configuration_yaml)
                    continue

                configuration = _load_configuration_yaml(configuration_yaml)
                self.assertEqual(
                    len(configuration.active_workflows),
                    expected_workflow_count,
                )

    def test_schedule_specific_fields_are_required_and_exclusive(self) -> None:
        invalid_schedule_cases = (
            (
                {"frequency": "weekly", "completion_time": "06:00"},
                "weekly SLA requires a lowercase day_of_week",
            ),
            (
                {
                    "frequency": "weekly",
                    "completion_time": "06:00",
                    "day_of_week": "monday",
                    "day_of_month": 1,
                },
                "weekly SLA does not accept first_deadline_date or day_of_month",
            ),
            (
                {"frequency": "fortnightly", "completion_time": "06:00"},
                "fortnightly SLA requires first_deadline_date",
            ),
            (
                {
                    "frequency": "fortnightly",
                    "completion_time": "06:00",
                    "first_deadline_date": "2026-01-12",
                    "day_of_week": "monday",
                },
                "fortnightly SLA does not accept day_of_week or day_of_month",
            ),
            (
                {"frequency": "monthly", "completion_time": "06:00"},
                "monthly SLA requires day_of_month from 1 to 31",
            ),
            (
                {
                    "frequency": "monthly",
                    "completion_time": "06:00",
                    "day_of_month": 1,
                    "first_deadline_date": "2026-01-12",
                },
                "monthly SLA does not accept day_of_week or first_deadline_date",
            ),
        )

        for schedule_document, expected_error in invalid_schedule_cases:
            with self.subTest(schedule_document=schedule_document):
                with self.assertRaisesRegex(ValueError, expected_error):
                    _validate_schedule_fields(schedule_document)

    def test_invalid_schema_document_reports_the_schema_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            configuration_path = temporary_root / "workflow-monitoring.yml"
            configuration_path.write_text("version: 3\n", encoding="utf-8")
            invalid_schema_path = temporary_root / "workflow-monitoring.schema.json"
            invalid_schema_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "load workflow monitoring schema"):
                load_workflow_monitoring_configuration(configuration_path, invalid_schema_path)

    def test_version_2_reports_the_migration_message(self) -> None:
        configuration_yaml = """
            version: 2
            workspace_id: "123456789"
            default_timezone: UTC
            workflows: []
        """

        with self.assertRaisesRegex(ValueError, CONFIGURATION_VERSION_2_MESSAGE):
            _load_configuration_yaml(configuration_yaml)

    def test_schema_reports_every_error_with_its_path(self) -> None:
        configuration_yaml = """
            version: 3
            default_timezone: UTC
            workflows:
              - monitoring_status: active
                sla:
                  frequency: daily
                  completion_time: "25:00"
        """

        with self.assertRaises(ValueError) as raised_error:
            _load_configuration_yaml(configuration_yaml)

        error_message = str(raised_error.exception)
        self.assertIn("workflows[0]: 'job_id' is a required property", error_message)
        self.assertIn(
            "workflows[0].sla.completion_time: '25:00' does not match",
            error_message,
        )

    def test_cross_field_errors_report_every_workflow_path(self) -> None:
        configuration_yaml = """
            version: 3
            default_timezone: UTC
            workflows:
              - job_id: 1
                monitoring_status: active
                sla:
                  frequency: daily
                  completion_time: "06:00"
                  day_of_week: monday
              - job_id: 2
                monitoring_status: active
                sla:
                  frequency: weekly
                  completion_time: "07:00"
        """

        with self.assertRaises(ValueError) as raised_error:
            _load_configuration_yaml(configuration_yaml)

        self.assertEqual(
            str(raised_error.exception),
            "workflow monitoring configuration does not match schema:\n"
            "  workflows[0].sla.day_of_week: not allowed when frequency is daily\n"
            "  workflows[1].sla: 'day_of_week' is a required property",
        )

    def test_schedule_field_errors_report_the_exact_collector_message(self) -> None:
        test_cases = (
            (
                "daily forbids day_of_week",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: daily
                      completion_time: "06:00"
                      day_of_week: monday
                """,
                "workflows[0].sla.day_of_week: not allowed when frequency is daily",
            ),
            (
                "weekly requires day_of_week",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: weekly
                      completion_time: "06:00"
                """,
                "workflows[0].sla: 'day_of_week' is a required property",
            ),
            (
                "weekly forbids first_deadline_date",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: weekly
                      completion_time: "06:00"
                      day_of_week: monday
                      first_deadline_date: "2026-01-12"
                """,
                "workflows[0].sla.first_deadline_date: not allowed when frequency is weekly",
            ),
            (
                "fortnightly requires first_deadline_date",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: fortnightly
                      completion_time: "06:00"
                """,
                "workflows[0].sla: 'first_deadline_date' is a required property",
            ),
            (
                "fortnightly forbids day_of_week",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: fortnightly
                      completion_time: "06:00"
                      first_deadline_date: "2026-01-12"
                      day_of_week: monday
                """,
                "workflows[0].sla.day_of_week: not allowed when frequency is fortnightly",
            ),
            (
                "monthly requires day_of_month",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: monthly
                      completion_time: "06:00"
                """,
                "workflows[0].sla: 'day_of_month' is a required property",
            ),
            (
                "monthly forbids day_of_week",
                """
                version: 3
                default_timezone: UTC
                workflows:
                  - job_id: 1
                    monitoring_status: active
                    sla:
                      frequency: monthly
                      completion_time: "06:00"
                      day_of_month: 31
                      day_of_week: monday
                """,
                "workflows[0].sla.day_of_week: not allowed when frequency is monthly",
            ),
        )

        for case_name, configuration_yaml, expected_line in test_cases:
            with self.subTest(case_name):
                with self.assertRaises(ValueError) as raised_error:
                    _load_configuration_yaml(configuration_yaml)
                self.assertEqual(
                    str(raised_error.exception),
                    "workflow monitoring configuration does not match schema:\n  " + expected_line,
                )

    def test_only_active_workflows_become_table_rows(self) -> None:
        configuration_yaml = """
            version: 3
            default_timezone: UTC
            workflows:
              - job_id: 10
                monitoring_status: inactive
                sla:
                  frequency: weekly
                  completion_time: "05:00"
                  day_of_week: monday
              - job_id: 42
                monitoring_status: active
                sla:
                  frequency: monthly
                  completion_time: "06:00"
                  timezone: Australia/Melbourne
                  day_of_month: 31
              - job_id: 43
                monitoring_status: active
                sla:
                  frequency: daily
                  completion_time: "07:00"
        """
        configuration = _load_configuration_yaml(configuration_yaml)
        changed_at = datetime(2026, 10, 29, 1, 2, 3, tzinfo=UTC)

        rows = workflow_sla_config_rows(configuration, "123456789", changed_at)

        self.assertEqual(
            rows,
            [
                {
                    "workspace_id": "123456789",
                    "configured_order": 1,
                    "configured_job_id": "42",
                    "frequency": "monthly",
                    "completion_time": "06:00",
                    "timezone": "Australia/Melbourne",
                    "day_of_week": None,
                    "first_deadline_date": None,
                    "day_of_month": 31,
                    "config_changed_at": changed_at,
                },
                {
                    "workspace_id": "123456789",
                    "configured_order": 2,
                    "configured_job_id": "43",
                    "frequency": "daily",
                    "completion_time": "07:00",
                    "timezone": "UTC",
                    "day_of_week": None,
                    "first_deadline_date": None,
                    "day_of_month": None,
                    "config_changed_at": changed_at,
                },
            ],
        )

    def test_fortnightly_rows_use_date_values(self) -> None:
        configuration_yaml = """
            version: 3
            default_timezone: UTC
            workflows:
              - job_id: 42
                monitoring_status: active
                sla:
                  frequency: fortnightly
                  completion_time: "06:00"
                  first_deadline_date: "2026-01-12"
        """
        configuration = _load_configuration_yaml(configuration_yaml)
        rows = workflow_sla_config_rows(
            configuration,
            "123456789",
            datetime(2026, 10, 29, 1, 2, 3, tzinfo=UTC),
        )

        self.assertEqual(rows[0]["first_deadline_date"], date(2026, 1, 12))


class SchemaScheduleRuleTests(unittest.TestCase):
    """Each frequency rule must be enforced by the JSON Schema alone, for the editor."""

    @classmethod
    def setUpClass(cls) -> None:
        schema_document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.validator = Draft202012Validator(schema_document)

    def _sla_errors(self, schedule_document: dict) -> list:
        document = {
            "version": 3,
            "default_timezone": "UTC",
            "workflows": [
                {
                    "job_id": 1,
                    "monitoring_status": "active",
                    "sla": schedule_document,
                }
            ],
        }
        return list(self.validator.iter_errors(document))

    def _assert_forbidden(self, base_schedule: dict, field: str) -> None:
        field_values = {
            "day_of_week": "monday",
            "first_deadline_date": "2026-01-12",
            "day_of_month": 3,
        }
        errors = self._sla_errors({**base_schedule, field: field_values[field]})
        # The schema forbids the field with {"not": true}, which reports a "not" error
        # on the field's own path.
        self.assertTrue(
            any(
                error.validator == "not"
                and error.validator_value is True
                and error.absolute_path[-1] == field
                for error in errors
            ),
            f"forbidden field {field} was accepted: {errors}",
        )

    def _assert_missing_required(self, base_schedule: dict, field: str) -> None:
        errors = self._sla_errors(base_schedule)
        expected_message = f"'{field}' is a required property"
        self.assertTrue(
            any(error.message == expected_message for error in errors),
            f"missing field {field} was accepted: {errors}",
        )

    def test_daily_schema_rule(self) -> None:
        base_schedule = {"frequency": "daily", "completion_time": "06:00"}
        with self.subTest(case="correct document passes"):
            self.assertEqual(self._sla_errors(base_schedule), [])
        for forbidden_field in ("day_of_week", "first_deadline_date", "day_of_month"):
            with self.subTest(case=f"forbidden {forbidden_field}"):
                self._assert_forbidden(base_schedule, forbidden_field)

    def test_weekly_schema_rule(self) -> None:
        base_schedule = {
            "frequency": "weekly",
            "completion_time": "06:00",
            "day_of_week": "monday",
        }
        with self.subTest(case="correct document passes"):
            self.assertEqual(self._sla_errors(base_schedule), [])
        with self.subTest(case="missing day_of_week"):
            self._assert_missing_required(
                {"frequency": "weekly", "completion_time": "06:00"}, "day_of_week"
            )
        for forbidden_field in ("first_deadline_date", "day_of_month"):
            with self.subTest(case=f"forbidden {forbidden_field}"):
                self._assert_forbidden(base_schedule, forbidden_field)

    def test_fortnightly_schema_rule(self) -> None:
        base_schedule = {
            "frequency": "fortnightly",
            "completion_time": "06:00",
            "first_deadline_date": "2026-01-12",
        }
        with self.subTest(case="correct document passes"):
            self.assertEqual(self._sla_errors(base_schedule), [])
        with self.subTest(case="missing first_deadline_date"):
            self._assert_missing_required(
                {"frequency": "fortnightly", "completion_time": "06:00"}, "first_deadline_date"
            )
        for forbidden_field in ("day_of_week", "day_of_month"):
            with self.subTest(case=f"forbidden {forbidden_field}"):
                self._assert_forbidden(base_schedule, forbidden_field)

    def test_monthly_schema_rule(self) -> None:
        base_schedule = {"frequency": "monthly", "completion_time": "06:00", "day_of_month": 3}
        with self.subTest(case="correct document passes"):
            self.assertEqual(self._sla_errors(base_schedule), [])
        with self.subTest(case="missing day_of_month"):
            self._assert_missing_required(
                {"frequency": "monthly", "completion_time": "06:00"}, "day_of_month"
            )
        for forbidden_field in ("day_of_week", "first_deadline_date"):
            with self.subTest(case=f"forbidden {forbidden_field}"):
                self._assert_forbidden(base_schedule, forbidden_field)
