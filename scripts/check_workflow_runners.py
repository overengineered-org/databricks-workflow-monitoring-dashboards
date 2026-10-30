#!/usr/bin/env python3
"""Reject workflow jobs that could start a billed runner.

The check uses only the Python standard library. It reads YAML as text so it can
report a line number for each violation and run before any setup action.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
GUARD_JOB_NAME = "runner-guard"
GUARD_SCRIPT = "scripts/check_workflow_runners.py"
ALLOWED_RUNNER = "ubuntu-latest"

_JOB_KEY = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+|'[^']+'|\"[^\"]+\")\s*:\s*(?P<value>.*)$")
_ANCHOR = re.compile(r"(?:^|[\s\[{])&[A-Za-z0-9_-]+")
_ALIAS = re.compile(r"(?:^|[\s\[{,])[*][A-Za-z_][A-Za-z0-9_-]*")
_MERGE_KEY = re.compile(r"(?:^|\s)<<\s*:")
_BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*$")


@dataclass(frozen=True)
class SourceLine:
    number: int
    indent: int
    text: str


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def _strip_comment(text: str) -> str:
    quote = ""
    for position, character in enumerate(text):
        if quote:
            if character == quote:
                quote = ""
        elif character in {"'", '"'}:
            quote = character
        elif character == "#" and (position == 0 or text[position - 1].isspace()):
            return text[:position]
    return text


def _is_block_scalar_header(text: str) -> bool:
    _, separator, value = text.partition(":")
    if not separator:
        return False
    return bool(_BLOCK_SCALAR.fullmatch(value.strip()))


def _source_lines(text: str) -> list[SourceLine]:
    lines: list[SourceLine] = []
    block_scalar_indent: int | None = None
    for number, raw_line in enumerate(text.splitlines(), start=1):
        content = _strip_comment(raw_line).rstrip()
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        if block_scalar_indent is not None:
            if indent > block_scalar_indent:
                continue
            block_scalar_indent = None
        source_line = SourceLine(number=number, indent=indent, text=content[indent:])
        lines.append(source_line)
        if _is_block_scalar_header(source_line.text):
            block_scalar_indent = indent
    return lines


def _is_key(text: str, key: str) -> bool:
    return text.startswith(f"{key}:")


def _value_after_key(text: str) -> str:
    _, _, value = text.partition(":")
    return value.strip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_inline_list(value: str) -> list[str]:
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return [_unquote(item.strip()) for item in inner.split(",") if item.strip()]


def _check_yaml_features(path: Path, lines: list[SourceLine]) -> list[Violation]:
    violations: list[Violation] = []
    for line in lines:
        if _MERGE_KEY.search(line.text):
            violations.append(Violation(path, line.number, "YAML merge key is forbidden"))
        elif _ANCHOR.search(line.text):
            violations.append(Violation(path, line.number, "YAML anchor is forbidden"))
        elif _ALIAS.search(line.text):
            violations.append(Violation(path, line.number, "YAML alias is forbidden"))
    return violations


def _job_block_end(lines: list[SourceLine], start: int, end: int, job_indent: int) -> int:
    position = start
    while position < end:
        candidate = lines[position]
        if candidate.indent == job_indent and not candidate.text.startswith("- "):
            return position
        position += 1
    return end


def _check_runs_on(
    path: Path,
    job_name: str,
    job_level: list[SourceLine],
) -> list[Violation]:
    violations: list[Violation] = []
    runs_on_line = next(
        (line for line in job_level if _is_key(line.text, "runs-on")),
        None,
    )
    if runs_on_line is None:
        violations.append(
            Violation(
                path, job_level[0].number if job_level else 0, f"job '{job_name}' has no runs-on"
            )
        )
        return violations
    value = _value_after_key(runs_on_line.text)
    if _unquote(value) != ALLOWED_RUNNER:
        violations.append(
            Violation(
                path,
                runs_on_line.number,
                f"job '{job_name}' runs-on is not exactly {ALLOWED_RUNNER}",
            )
        )
    return violations


def _needs_values(block: list[SourceLine], needs_line: SourceLine) -> list[str]:
    value = _value_after_key(needs_line.text)
    if value:
        return _parse_inline_list(value)
    values: list[str] = []
    needs_index = block.index(needs_line)
    for line in block[needs_index + 1 :]:
        if line.indent <= needs_line.indent:
            break
        if line.text.startswith("- "):
            values.append(_unquote(line.text[2:].strip()))
    return values


def _check_needs(
    path: Path,
    job_name: str,
    job_level: list[SourceLine],
    block: list[SourceLine],
) -> list[Violation]:
    if job_name == GUARD_JOB_NAME:
        return []
    needs_line = next(
        (line for line in job_level if _is_key(line.text, "needs")),
        None,
    )
    if needs_line is None:
        return [
            Violation(
                path,
                job_level[0].number if job_level else 0,
                f"job '{job_name}' has no needs: {GUARD_JOB_NAME}",
            )
        ]
    if GUARD_JOB_NAME not in _needs_values(block, needs_line):
        return [
            Violation(
                path,
                needs_line.number,
                f"job '{job_name}' does not need {GUARD_JOB_NAME}",
            )
        ]
    return []


def _step_chunks(block: list[SourceLine], steps_line: SourceLine) -> list[list[SourceLine]]:
    steps_index = block.index(steps_line)
    remaining = block[steps_index + 1 :]
    item_indent = min(
        (line.indent for line in remaining if line.text.startswith("- ")),
        default=None,
    )
    if item_indent is None:
        return []
    steps: list[list[SourceLine]] = []
    current: list[SourceLine] = []
    for line in remaining:
        if line.indent == item_indent and line.text.startswith("- "):
            if current:
                steps.append(current)
            current = [line]
        elif current and line.indent > item_indent:
            current.append(line)
    if current:
        steps.append(current)
    return steps


def _check_guard_job(
    path: Path,
    job_level: list[SourceLine],
    block: list[SourceLine],
) -> list[Violation]:
    steps_line = next(
        (line for line in job_level if _is_key(line.text, "steps")),
        None,
    )
    if steps_line is None:
        return [
            Violation(
                path,
                job_level[0].number if job_level else 0,
                "runner-guard job must have exactly 2 steps",
            )
        ]
    steps = _step_chunks(block, steps_line)
    if len(steps) != 2:
        return [
            Violation(
                path,
                steps_line.number,
                "runner-guard job must have exactly 2 steps: actions/checkout and "
                f"python3 {GUARD_SCRIPT}",
            )
        ]
    first_step = "\n".join(line.text for line in steps[0])
    second_step = "\n".join(line.text for line in steps[1])
    first_is_checkout = "uses: actions/checkout@" in first_step
    second_is_guard = f"python3 {GUARD_SCRIPT}" in second_step
    if first_is_checkout and second_is_guard:
        return []
    return [
        Violation(
            path,
            steps_line.number,
            f"runner-guard job steps must be actions/checkout and python3 {GUARD_SCRIPT}",
        )
    ]


def _check_job(
    path: Path,
    job_name: str,
    job_line: SourceLine,
    block: list[SourceLine],
) -> list[Violation]:
    job_level = [line for line in block if line.indent > job_line.indent]
    child_indent = min((line.indent for line in job_level), default=None)
    if child_indent is None:
        return [
            Violation(path, job_line.number, f"job '{job_name}' has no runs-on"),
        ]
    direct_children = [line for line in job_level if line.indent == child_indent]

    reusable_workflow = next(
        (line for line in direct_children if _is_key(line.text, "uses")),
        None,
    )
    if reusable_workflow is not None:
        return [
            Violation(
                path,
                reusable_workflow.number,
                f"job '{job_name}' uses a reusable workflow",
            )
        ]

    violations = _check_runs_on(path, job_name, direct_children)
    violations.extend(_check_needs(path, job_name, direct_children, block))
    if job_name == GUARD_JOB_NAME:
        violations.extend(_check_guard_job(path, direct_children, block))
    return violations


def check_workflow_file(path: Path) -> list[Violation]:
    lines = _source_lines(path.read_text(encoding="utf-8"))
    violations = _check_yaml_features(path, lines)

    jobs_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.indent == 0 and _is_key(line.text, "jobs")
        ),
        None,
    )
    if jobs_index is None:
        return violations

    section_end = len(lines)
    for index in range(jobs_index + 1, len(lines)):
        if lines[index].indent <= lines[jobs_index].indent:
            section_end = index
            break

    job_start = jobs_index + 1
    if job_start >= section_end:
        return violations
    job_indent = lines[job_start].indent

    position = job_start
    while position < section_end:
        line = lines[position]
        if line.indent != job_indent or line.text.startswith("- "):
            position += 1
            continue
        match = _JOB_KEY.match(line.text)
        if match is None:
            position += 1
            continue
        job_name = _unquote(match.group("name"))
        block_end = _job_block_end(lines, position + 1, section_end, job_indent)
        violations.extend(_check_job(path, job_name, line, lines[position + 1 : block_end]))
        position = block_end
    return violations


def check_workflows(directory: Path) -> list[Violation]:
    violations: list[Violation] = []
    for pattern in ("*.yml", "*.yaml"):
        for path in sorted(directory.glob(pattern)):
            violations.extend(check_workflow_file(path))
    return sorted(violations, key=lambda violation: (str(violation.path), violation.line))


def main() -> int:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else WORKFLOW_DIRECTORY
    violations = check_workflows(directory)
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
