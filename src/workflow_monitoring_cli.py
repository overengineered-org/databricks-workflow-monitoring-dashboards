"""Create a workflow monitoring project and pick the Jobs it monitors."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import yaml

REPOSITORY_URL = "https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard"
DISTRIBUTION_NAME = "databricks-jobs-monitoring-dashboard"
CONFIGURATION_FILE_NAME = "workflow-monitoring.yml"
# The project's own product files define the rules, so the CLI checks the file exactly
# like the collector version that the project deploys.
SCHEMA_RELATIVE_PATH = Path("workflow_monitoring/workflow-monitoring.schema.json")
LOADER_RELATIVE_PATH = Path("workflow_monitoring/collector/workflow_monitoring_config.py")
DEFAULT_COMPLETION_TIME = "06:00"
WORKFLOWS_KEY_PATTERN = re.compile(r"^workflows:[ \t]*(#.*)?$")
LIST_ITEM_INDENT_PATTERN = re.compile(r"^([ \t]*)- ")


class CommandError(Exception):
    """A failure that the CLI reports as one message and exit code 1."""


@dataclass(frozen=True)
class WorkspaceJob:
    """One Job returned by the Jobs API."""

    job_id: int
    name: str


JobChooser = Callable[[Sequence[WorkspaceJob], set[int]], list[int]]
Confirmer = Callable[[str], bool]


def _run_databricks(arguments: list[str], *, capture: bool) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["databricks", *arguments], capture_output=capture, text=True, check=False
        )
    except FileNotFoundError as error:
        raise CommandError("Databricks CLI not found. Install it, then try again.") from error


def list_workspace_jobs(profile: str) -> list[WorkspaceJob]:
    """Read every Job in the workspace with the read-only Jobs list API."""

    completed = _run_databricks(
        ["jobs", "list", "--output", "json", "--profile", profile], capture=True
    )
    if completed.returncode != 0:
        raise CommandError(f"databricks jobs list failed: {completed.stderr.strip()}")
    try:
        job_documents = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as error:
        raise CommandError(f"databricks jobs list returned invalid JSON: {error}") from error
    jobs = [
        WorkspaceJob(
            job_id=int(job_document["job_id"]),
            name=str(job_document.get("settings", {}).get("name") or "(unnamed)"),
        )
        for job_document in job_documents
    ]
    return sorted(jobs, key=lambda job: (job.name.casefold(), job.job_id))


def configured_job_ids(configuration_text: str) -> set[int]:
    """Return the Job IDs that the configuration already lists, active or inactive."""

    document = yaml.safe_load(configuration_text) or {}
    workflows = document.get("workflows") if isinstance(document, dict) else None
    return {
        workflow["job_id"]
        for workflow in workflows or []
        if isinstance(workflow, dict) and isinstance(workflow.get("job_id"), int)
    }


def workflows_insertion_point(configuration_text: str) -> tuple[int, str]:
    """Return the text offset after the last workflows item and the list item indent."""

    lines = configuration_text.splitlines(keepends=True)
    workflows_line_index = next(
        (index for index, line in enumerate(lines) if WORKFLOWS_KEY_PATTERN.match(line)),
        None,
    )
    if workflows_line_index is None:
        raise CommandError(
            f"{CONFIGURATION_FILE_NAME} has no block-style top-level 'workflows:' key. "
            "Add the Jobs by hand."
        )

    block_end_index = len(lines)
    indent = "  "
    for index in range(workflows_line_index + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line[0].isspace() and not line.startswith("#"):
            block_end_index = index
            break
        list_item = LIST_ITEM_INDENT_PATTERN.match(line)
        if list_item:
            indent = list_item.group(1)

    # Blank lines and top-level comments before the next key stay after the new entries.
    while block_end_index > workflows_line_index + 1 and (
        not lines[block_end_index - 1].strip() or lines[block_end_index - 1].startswith("#")
    ):
        block_end_index -= 1
    return sum(len(line) for line in lines[:block_end_index]), indent


def workflow_entries_yaml(jobs: Sequence[WorkspaceJob], completion_time: str, indent: str) -> str:
    """Render one active daily workflow entry for each Job."""

    entry_lines: list[str] = []
    for job in jobs:
        # A Job name can contain a line break, which would end the comment early.
        comment_name = " ".join(job.name.split())
        entry_lines += [
            f"{indent}# {comment_name}",
            f"{indent}- job_id: {job.job_id}",
            f"{indent}  monitoring_status: active",
            f"{indent}  sla:",
            f"{indent}    frequency: daily",
            f'{indent}    completion_time: "{completion_time}"',
        ]
    return "".join(f"{line}\n" for line in entry_lines)


def insert_entries(configuration_text: str, offset: int, entries_yaml: str) -> str:
    """Insert rendered entries at the offset from workflows_insertion_point."""

    head = configuration_text[:offset]
    if not head.endswith("\n"):
        head += "\n"
    return head + entries_yaml + configuration_text[offset:]


@contextmanager
def validated_temporary_configuration(
    project_directory: Path, configuration_text: str
) -> Iterator[Path]:
    """Yield a temporary copy of the text that passed the project's collector rules."""

    loader_spec = importlib.util.spec_from_file_location(
        "project_workflow_monitoring_config", project_directory / LOADER_RELATIVE_PATH
    )
    loader_module = importlib.util.module_from_spec(loader_spec)
    # Dataclasses look up their module in sys.modules while the module runs.
    sys.modules[loader_spec.name] = loader_module
    loader_spec.loader.exec_module(loader_module)

    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=project_directory, prefix=".workflow-monitoring.", suffix=".yml"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(configuration_text)
        try:
            loader_module.load_workflow_monitoring_configuration(
                temporary_path, project_directory / SCHEMA_RELATIVE_PATH
            )
        except ValueError as error:
            raise CommandError(
                f"the updated configuration is invalid. Nothing changed.\n{error}"
            ) from error
        yield temporary_path
    finally:
        temporary_path.unlink(missing_ok=True)


def _require_valid_completion_time(schema_path: Path, completion_time: str) -> None:
    # Check early with the schema pattern, so a typo fails before the user ticks Jobs.
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    pattern = schema["$defs"]["sla"]["properties"]["completion_time"]["pattern"]
    if not re.match(pattern, completion_time):
        raise CommandError("--completion-time must use HH:MM, for example 06:00.")


def select_jobs(
    project_directory: Path,
    profile: str,
    *,
    name_contains: str | None,
    completion_time: str,
    dry_run: bool,
    choose_jobs: JobChooser,
    confirm: Confirmer,
) -> int:
    """Let the user tick Jobs, then add them to the project configuration."""

    configuration_path = project_directory / CONFIGURATION_FILE_NAME
    schema_path = project_directory / SCHEMA_RELATIVE_PATH
    if not all(
        path.is_file()
        for path in (configuration_path, schema_path, project_directory / LOADER_RELATIVE_PATH)
    ):
        raise CommandError(
            f"{project_directory} is not a workflow monitoring project. It needs "
            f"{CONFIGURATION_FILE_NAME}, {SCHEMA_RELATIVE_PATH}, and {LOADER_RELATIVE_PATH}."
        )
    _require_valid_completion_time(schema_path, completion_time)
    configuration_text = configuration_path.read_text(encoding="utf-8")
    offset, indent = workflows_insertion_point(configuration_text)
    monitored_job_ids = configured_job_ids(configuration_text)

    jobs = list_workspace_jobs(profile)
    if name_contains:
        jobs = [job for job in jobs if name_contains.casefold() in job.name.casefold()]
    if not jobs:
        print("No Jobs found. Nothing changed.")
        return 0

    selected_job_ids = set(choose_jobs(jobs, monitored_job_ids)) - monitored_job_ids
    selected_jobs = [job for job in jobs if job.job_id in selected_job_ids]
    if not selected_jobs:
        print("No new Jobs selected. Nothing changed.")
        return 0

    entries_yaml = workflow_entries_yaml(selected_jobs, completion_time, indent)
    updated_text = insert_entries(configuration_text, offset, entries_yaml)
    with validated_temporary_configuration(project_directory, updated_text) as temporary_path:
        print(f"New entries for {configuration_path}:\n\n{entries_yaml}")
        if dry_run:
            print("Dry run. Nothing changed.")
            return 0
        if not confirm(f"Add {len(selected_jobs)} Jobs to {CONFIGURATION_FILE_NAME}?"):
            print("Cancelled. Nothing changed.")
            return 0
        os.replace(temporary_path, configuration_path)

    print(
        f"Added {len(selected_jobs)} Jobs with a daily {completion_time} SLA. "
        f"Edit {CONFIGURATION_FILE_NAME} to change a schedule, then run databricks bundle deploy."
    )
    return 0


def init_project(
    output_directory: Path,
    profile: str,
    *,
    confirm: Confirmer,
    pick_jobs: Callable[[Path], int],
) -> int:
    """Render the bundle template for this CLI version, then offer the Job picker."""

    output_directory.mkdir(parents=True, exist_ok=True)
    existing_projects = _project_directories(output_directory)
    # The tag is the CLI's own version, so a local development install needs a published tag.
    completed = _run_databricks(
        [
            "bundle",
            "init",
            REPOSITORY_URL,
            "--tag",
            f"v{version(DISTRIBUTION_NAME)}",
            "--template-dir",
            "template",
            "--output-dir",
            str(output_directory),
            "--profile",
            profile,
        ],
        capture=False,
    )
    if completed.returncode != 0:
        return completed.returncode

    new_projects = sorted(_project_directories(output_directory) - existing_projects)
    if len(new_projects) != 1:
        print("Run 'workflow-monitoring select-jobs' in the new project folder.")
        return 0
    if not confirm("Select the Jobs to monitor now?"):
        print(f"Run 'workflow-monitoring select-jobs' in {new_projects[0]} later.")
        return 0
    return pick_jobs(new_projects[0])


def _project_directories(output_directory: Path) -> set[Path]:
    return {path.parent for path in output_directory.glob(f"*/{CONFIGURATION_FILE_NAME}")}


def _ask_jobs(jobs: Sequence[WorkspaceJob], monitored_job_ids: set[int]) -> list[int]:
    import questionary

    choices = [
        questionary.Choice(
            title=f"{job.name} ({job.job_id})",
            value=job.job_id,
            disabled="already monitored" if job.job_id in monitored_job_ids else None,
        )
        for job in jobs
    ]
    selected = questionary.checkbox(
        "Tick the Jobs to monitor (space to tick, enter to confirm):", choices=choices
    ).ask()
    return list(selected or [])


def _ask_confirm(message: str) -> bool:
    import questionary

    return bool(questionary.confirm(message, default=False).ask())


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflow-monitoring", description=__doc__)
    parser.add_argument("--version", action="version", version=version(DISTRIBUTION_NAME))
    subcommands = parser.add_subparsers(dest="command", required=True)

    picker_options = argparse.ArgumentParser(add_help=False)
    # Never pick a Databricks profile for the user.
    picker_options.add_argument("--profile", required=True, help="Databricks CLI profile to use")
    picker_options.add_argument("--name-contains", help="show only Jobs whose name has this text")
    picker_options.add_argument(
        "--completion-time",
        default=DEFAULT_COMPLETION_TIME,
        help="daily SLA time for new Jobs, HH:MM",
    )

    init_parser = subcommands.add_parser(
        "init", parents=[picker_options], help="create a new monitoring project"
    )
    init_parser.add_argument("--output-dir", type=Path, default=Path("."))

    select_parser = subcommands.add_parser(
        "select-jobs", parents=[picker_options], help="tick Jobs to monitor"
    )
    select_parser.add_argument("--project-dir", type=Path, default=Path("."))
    select_parser.add_argument(
        "--dry-run", action="store_true", help="print the new entries, write nothing"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_argument_parser().parse_args(argv)

    def pick_jobs(project_directory: Path, dry_run: bool = False) -> int:
        return select_jobs(
            project_directory,
            arguments.profile,
            name_contains=arguments.name_contains,
            completion_time=arguments.completion_time,
            dry_run=dry_run,
            choose_jobs=_ask_jobs,
            confirm=_ask_confirm,
        )

    try:
        if arguments.command == "init":
            return init_project(
                arguments.output_dir,
                arguments.profile,
                confirm=_ask_confirm,
                pick_jobs=pick_jobs,
            )
        return pick_jobs(arguments.project_dir, arguments.dry_run)
    except CommandError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
