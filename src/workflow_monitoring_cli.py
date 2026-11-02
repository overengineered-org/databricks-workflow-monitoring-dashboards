"""Set up, update, and deploy a Databricks workflow monitoring project."""

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
from typing import Any

import yaml

REPOSITORY_URL = "https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard"
DISTRIBUTION_NAME = "databricks-jobs-monitoring-dashboard"
MINIMUM_DATABRICKS_CLI_VERSION = (1, 22, 1)
CONFIGURATION_FILE_NAME = "workflow-monitoring.yml"
BUNDLE_FILE_NAME = "databricks.yml"
# The project's own product files define the rules, so the CLI checks the file exactly
# like the collector version that the project deploys.
SCHEMA_RELATIVE_PATH = Path("workflow_monitoring/workflow-monitoring.schema.json")
LOADER_RELATIVE_PATH = Path("workflow_monitoring/collector/workflow_monitoring_config.py")
COLLECTOR_JOB_KEY = "workflow_monitoring_jobs_api_collector"
BUNDLE_TARGETS = ("dev", "prod")
DEFAULT_COMPLETION_TIME = "06:00"
WORKFLOWS_KEY_PATTERN = re.compile(r"^workflows:[ \t]*(#.*)?$")
LIST_ITEM_INDENT_PATTERN = re.compile(r"^([ \t]*)- ")
SIGN_IN_HINT = (
    "Sign in to your workspace with 'databricks auth login --host <workspace-url>', "
    "then run this command again."
)


class CommandError(Exception):
    """A failure that the CLI reports as one message and exit code 1."""


@dataclass(frozen=True)
class WorkspaceJob:
    """One Job returned by the Jobs API."""

    job_id: int
    name: str


@dataclass(frozen=True)
class Prompts:
    """The interactive questions, replaceable in tests."""

    choose_one: Callable[[str, Sequence[tuple[str, str]]], str | None]
    choose_jobs: Callable[[Sequence[WorkspaceJob], set[int]], list[int]]
    confirm: Callable[[str], bool]


def _run_databricks(
    arguments: list[str], *, capture: bool, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["databricks", *arguments], capture_output=capture, text=True, check=False, cwd=cwd
        )
    except FileNotFoundError as error:
        raise CommandError(
            "Databricks CLI not found. Install it from "
            "https://docs.databricks.com/dev-tools/cli/install, then try again."
        ) from error


def _databricks_json(arguments: list[str]) -> Any:
    completed = _run_databricks([*arguments, "--output", "json"], capture=True)
    command_text = " ".join(["databricks", *arguments[:2]])
    if completed.returncode != 0:
        raise CommandError(f"{command_text} failed: {completed.stderr.strip()}")
    try:
        return json.loads(completed.stdout or "null")
    except json.JSONDecodeError as error:
        raise CommandError(f"{command_text} returned invalid JSON: {error}") from error


def check_databricks_cli() -> None:
    """Stop early when the Databricks CLI is missing or older than the bundle needs."""

    version_document = _databricks_json(["version"])
    installed = tuple(int(part) for part in re.findall(r"\d+", version_document["Version"])[:3])
    if installed < MINIMUM_DATABRICKS_CLI_VERSION:
        minimum = ".".join(str(part) for part in MINIMUM_DATABRICKS_CLI_VERSION)
        raise CommandError(
            f"Databricks CLI {version_document['Version']} is too old. Install {minimum} or later."
        )


def resolve_profile(profile: str | None, prompts: Prompts) -> str:
    """Return a signed-in profile. Ask the user to pick one; never pick one for them."""

    if profile:
        if _run_databricks(["current-user", "me", "--profile", profile], capture=True).returncode:
            raise CommandError(f"Profile '{profile}' is not signed in. {SIGN_IN_HINT}")
        return profile

    profiles = _databricks_json(["auth", "profiles"]).get("profiles") or []
    choices = [
        (f"{entry['name']} ({entry.get('host', 'unknown host')})", entry["name"])
        for entry in profiles
        if entry.get("valid")
    ]
    if not choices:
        raise CommandError(f"No signed-in Databricks profile found. {SIGN_IN_HINT}")
    selected = prompts.choose_one("Databricks profile:", choices)
    if not selected:
        raise CommandError("No profile selected.")
    return selected


def list_workspace_jobs(profile: str) -> list[WorkspaceJob]:
    """Read every Job in the workspace with the read-only Jobs list API."""

    job_documents = _databricks_json(["jobs", "list", "--profile", profile]) or []
    jobs = [
        WorkspaceJob(
            job_id=int(job_document["job_id"]),
            name=str(job_document.get("settings", {}).get("name") or "(unnamed)"),
        )
        for job_document in job_documents
    ]
    return sorted(jobs, key=lambda job: (job.name.casefold(), job.job_id))


def variable_overrides_path(project_directory: Path, target: str) -> Path:
    """Return the uncommitted bundle variable file that the Databricks CLI reads per target."""

    return project_directory / ".databricks" / "bundle" / target / "variable-overrides.json"


def saved_warehouse_id(project_directory: Path, target: str) -> str | None:
    path = variable_overrides_path(project_directory, target)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("warehouse_id")


def choose_warehouse(
    project_directory: Path, profile: str, prompts: Prompts, targets: Sequence[str]
) -> str:
    """Let the user pick a SQL warehouse and save it for each target."""

    warehouses = _databricks_json(["warehouses", "list", "--profile", profile]) or []
    if not warehouses:
        raise CommandError("No SQL warehouse found. Create one in the workspace, then try again.")
    warehouse_id = prompts.choose_one(
        "SQL warehouse for the dashboard:",
        [(f"{warehouse['name']} ({warehouse['id']})", warehouse["id"]) for warehouse in warehouses],
    )
    if not warehouse_id:
        raise CommandError("No SQL warehouse selected.")
    for target in targets:
        path = variable_overrides_path(project_directory, target)
        overrides = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        overrides["warehouse_id"] = warehouse_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(overrides, indent=2) + "\n", encoding="utf-8")
    return warehouse_id


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


def _require_project(project_directory: Path) -> None:
    required = (
        BUNDLE_FILE_NAME,
        CONFIGURATION_FILE_NAME,
        SCHEMA_RELATIVE_PATH,
        LOADER_RELATIVE_PATH,
    )
    if not all((project_directory / path).is_file() for path in required):
        raise CommandError(
            f"{project_directory} is not a workflow monitoring project. "
            "Run this command in the folder that 'workflow-monitoring init' created, "
            "or pass --project-dir."
        )


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
    prompts: Prompts,
) -> int:
    """Let the user tick Jobs, then add them to the project configuration."""

    _require_project(project_directory)
    _require_valid_completion_time(project_directory / SCHEMA_RELATIVE_PATH, completion_time)
    configuration_path = project_directory / CONFIGURATION_FILE_NAME
    configuration_text = configuration_path.read_text(encoding="utf-8")
    offset, indent = workflows_insertion_point(configuration_text)
    monitored_job_ids = configured_job_ids(configuration_text)

    jobs = list_workspace_jobs(profile)
    if name_contains:
        jobs = [job for job in jobs if name_contains.casefold() in job.name.casefold()]
    if not jobs:
        print("No Jobs found. Nothing changed.")
        return 0

    selected_job_ids = set(prompts.choose_jobs(jobs, monitored_job_ids)) - monitored_job_ids
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
        job_count = f"{len(selected_jobs)} Job" + ("s" if len(selected_jobs) != 1 else "")
        if not prompts.confirm(f"Add {job_count} to {CONFIGURATION_FILE_NAME}?"):
            print("Cancelled. Nothing changed.")
            return 0
        os.replace(temporary_path, configuration_path)

    print(
        f"Added {job_count} with a daily {completion_time} SLA. To change a "
        f"schedule, edit {CONFIGURATION_FILE_NAME}. Then run 'workflow-monitoring deploy'."
    )
    return 0


def deploy_project(
    project_directory: Path,
    profile: str,
    *,
    target: str,
    assume_yes: bool,
    prompts: Prompts,
) -> int:
    """Validate and deploy the bundle. In dev, also run the collector once."""

    _require_project(project_directory)
    if not saved_warehouse_id(project_directory, target):
        choose_warehouse(project_directory, profile, prompts, (target,))

    bundle_options = ["-t", target, "--profile", profile]
    validated = _run_databricks(
        ["bundle", "validate", "--strict", *bundle_options], capture=False, cwd=project_directory
    )
    if validated.returncode:
        return validated.returncode

    if target == "dev":
        cost_note = (
            "This creates the collector Job, three tables, and the dashboard, then runs the "
            "collector once on serverless compute."
        )
    else:
        cost_note = (
            "This creates the prod resources and unpauses the collector schedule. The collector "
            "then runs on serverless compute every 5 minutes."
        )
    print(cost_note)
    if not assume_yes and not prompts.confirm(f"Deploy to {target}?"):
        print("Cancelled. Nothing deployed.")
        return 0

    deployed = _run_databricks(
        ["bundle", "deploy", *bundle_options], capture=False, cwd=project_directory
    )
    if deployed.returncode or target != "dev":
        return deployed.returncode
    return _run_databricks(
        ["bundle", "run", COLLECTOR_JOB_KEY, *bundle_options], capture=False, cwd=project_directory
    ).returncode


def init_project(
    output_directory: Path,
    profile: str,
    *,
    name_contains: str | None,
    completion_time: str,
    prompts: Prompts,
) -> int:
    """Create the project, save the warehouse, tick Jobs, and validate the bundle."""

    output_directory.mkdir(parents=True, exist_ok=True)
    existing_projects = _project_directories(output_directory)
    # The tag is the CLI's own version, so a local install of an unreleased version cannot init.
    created = _run_databricks(
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
    if created.returncode:
        return created.returncode

    new_projects = sorted(_project_directories(output_directory) - existing_projects)
    if len(new_projects) != 1:
        raise CommandError(
            "Could not find the new project folder. Run 'workflow-monitoring select-jobs' in it."
        )
    project_directory = new_projects[0]

    choose_warehouse(project_directory, profile, prompts, BUNDLE_TARGETS)
    picked = select_jobs(
        project_directory,
        profile,
        name_contains=name_contains,
        completion_time=completion_time,
        dry_run=False,
        prompts=prompts,
    )
    if picked:
        return picked
    validated = _run_databricks(
        ["bundle", "validate", "--strict", "-t", "dev", "--profile", profile],
        capture=False,
        cwd=project_directory,
    )
    if validated.returncode:
        return validated.returncode

    print(
        f"\nProject ready in {project_directory}. Next:\n\n"
        f"  cd {project_directory}\n"
        f"  workflow-monitoring deploy --profile {profile}\n"
    )
    return 0


def _project_directories(output_directory: Path) -> set[Path]:
    return {path.parent for path in output_directory.glob(f"*/{CONFIGURATION_FILE_NAME}")}


def _ask_one(message: str, choices: Sequence[tuple[str, str]]) -> str | None:
    import questionary

    return questionary.select(
        message, choices=[questionary.Choice(title=title, value=value) for title, value in choices]
    ).ask()


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


INTERACTIVE_PROMPTS = Prompts(choose_one=_ask_one, choose_jobs=_ask_jobs, confirm=_ask_confirm)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflow-monitoring", description=__doc__)
    parser.add_argument("--version", action="version", version=version(DISTRIBUTION_NAME))
    subcommands = parser.add_subparsers(dest="command", required=True)

    profile_options = argparse.ArgumentParser(add_help=False)
    profile_options.add_argument(
        "--profile", help="Databricks CLI profile. Without it, the CLI asks you to pick one."
    )
    job_options = argparse.ArgumentParser(add_help=False)
    job_options.add_argument("--name-contains", help="show only Jobs whose name has this text")
    job_options.add_argument(
        "--completion-time",
        default=DEFAULT_COMPLETION_TIME,
        help="daily SLA time for new Jobs, HH:MM",
    )

    init_parser = subcommands.add_parser(
        "init", parents=[profile_options, job_options], help="create and set up a new project"
    )
    init_parser.add_argument("--output-dir", type=Path, default=Path("."))

    select_parser = subcommands.add_parser(
        "select-jobs", parents=[profile_options, job_options], help="tick more Jobs to monitor"
    )
    select_parser.add_argument("--project-dir", type=Path, default=Path("."))
    select_parser.add_argument(
        "--dry-run", action="store_true", help="print the new entries, write nothing"
    )

    deploy_parser = subcommands.add_parser(
        "deploy", parents=[profile_options], help="validate and deploy the project"
    )
    deploy_parser.add_argument("--project-dir", type=Path, default=Path("."))
    deploy_parser.add_argument("--target", choices=BUNDLE_TARGETS, default="dev")
    deploy_parser.add_argument("--yes", action="store_true", help="deploy without asking")
    return parser


def main(argv: Sequence[str] | None = None, prompts: Prompts = INTERACTIVE_PROMPTS) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    try:
        check_databricks_cli()
        profile = resolve_profile(arguments.profile, prompts)
        if arguments.command == "init":
            return init_project(
                arguments.output_dir,
                profile,
                name_contains=arguments.name_contains,
                completion_time=arguments.completion_time,
                prompts=prompts,
            )
        if arguments.command == "select-jobs":
            return select_jobs(
                arguments.project_dir,
                profile,
                name_contains=arguments.name_contains,
                completion_time=arguments.completion_time,
                dry_run=arguments.dry_run,
                prompts=prompts,
            )
        return deploy_project(
            arguments.project_dir,
            profile,
            target=arguments.target,
            assume_yes=arguments.yes,
            prompts=prompts,
        )
    except CommandError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
