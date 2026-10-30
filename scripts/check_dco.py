#!/usr/bin/env python3
"""Check that every pull request commit carries a matching DCO sign-off.

The check uses only the Python standard library. It reads commit messages from
git and compares each ``Signed-off-by`` email with the commit author email,
case-insensitive. Merge commits and bot authors are skipped.
"""

from __future__ import annotations

import re
import subprocess
import sys

SIGN_OFF = re.compile(
    r"^signed-off-by:\s*.*?<([^<>]+)>\s*$",
    re.IGNORECASE | re.MULTILINE,
)
BOT_SUFFIX = "[bot]@users.noreply.github.com"
FIX_COMMANDS = (
    "git rebase --signoff origin/main",
    "git push --force-with-lease",
)


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or f"git {' '.join(arguments)} failed"
        raise RuntimeError(message)
    return result.stdout


def commit_hashes(base_sha: str, head_sha: str) -> list[str]:
    """Return the non-merge commits between the base and the head."""
    output = _git("log", "--no-merges", "--format=%H", f"{base_sha}..{head_sha}")
    return [line for line in output.splitlines() if line]


def commit_details(commit: str) -> tuple[str, str, str, str]:
    """Return the short sha, author email, subject, and full message."""
    output = _git("show", "-s", "--format=%h%x00%ae%x00%s%x00%B", commit)
    short_sha, author_email, subject, message = output.split("\x00", 3)
    return short_sha, author_email, subject, message


def signed_off_emails(message: str) -> list[str]:
    """Return every Signed-off-by email in the commit message."""
    return [match.group(1).strip() for match in SIGN_OFF.finditer(message)]


def is_signed_off(author_email: str, message: str) -> bool:
    """Return True when the author signed off, or when the author is a bot."""
    if author_email.casefold().endswith(BOT_SUFFIX):
        return True
    return any(email.casefold() == author_email.casefold() for email in signed_off_emails(message))


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        print("usage: check_dco.py <base-sha> <head-sha>", file=sys.stderr)
        return 2
    base_sha, head_sha = arguments
    try:
        failures = [
            f"{short_sha} {subject}: missing or mismatched Signed-off-by"
            for short_sha, author_email, subject, message in (
                commit_details(commit) for commit in commit_hashes(base_sha, head_sha)
            )
            if not is_signed_off(author_email, message)
        ]
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not failures:
        return 0
    print("\n".join(failures))
    print("Add a sign-off to each commit, then update the branch:")
    for command in FIX_COMMANDS:
        print(f"  {command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
