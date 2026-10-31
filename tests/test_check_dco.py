"""Tests for the DCO sign-off check."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._paths import REPOSITORY_ROOT

SCRIPT = REPOSITORY_ROOT / "scripts" / "check_dco.py"
AUTHOR_EMAIL = "author@example.com"
BOT_EMAIL = "49699333+dependabot[bot]@users.noreply.github.com"


def _environment(author_email: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": author_email,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    return environment


def _git(
    repository: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return result.stdout


def _commit(
    repository: Path,
    message: str,
    *,
    author_email: str = AUTHOR_EMAIL,
    signoff_email: str | None = None,
) -> str:
    body = message
    if signoff_email is not None:
        body = f"{message}\n\nSigned-off-by: Test Author <{signoff_email}>"
    environment = _environment(author_email)
    _git(repository, "commit", "-q", "--allow-empty", "-m", body, environment=environment)
    return _git(repository, "rev-parse", "HEAD").strip()


class DcoCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        _git(self.repository, "init", "-q", "-b", "main")
        self.base = _commit(self.repository, "chore: base", signoff_email=AUTHOR_EMAIL)

    def _run(self) -> subprocess.CompletedProcess[str]:
        head = _git(self.repository, "rev-parse", "HEAD").strip()
        return subprocess.run(
            [sys.executable, str(SCRIPT), self.base, head],
            cwd=self.repository,
            capture_output=True,
            text=True,
        )

    def test_signed_off_commit_passes(self) -> None:
        _commit(self.repository, "feat: signed", signoff_email=AUTHOR_EMAIL)

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_commit_without_sign_off_fails(self) -> None:
        _commit(self.repository, "feat: unsigned")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing or mismatched Signed-off-by", result.stdout)
        self.assertIn("git rebase --signoff origin/main", result.stdout)
        self.assertIn("git push --force-with-lease", result.stdout)

    def test_sign_off_with_a_different_email_fails(self) -> None:
        _commit(self.repository, "feat: wrong email", signoff_email="other@example.com")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing or mismatched Signed-off-by", result.stdout)

    def test_merge_commit_is_skipped(self) -> None:
        _git(self.repository, "checkout", "-q", "-b", "feature")
        _commit(self.repository, "feat: feature", signoff_email=AUTHOR_EMAIL)
        _git(self.repository, "checkout", "-q", "main")
        _commit(self.repository, "feat: mainline", signoff_email=AUTHOR_EMAIL)
        _git(
            self.repository,
            "merge",
            "-q",
            "--no-ff",
            "feature",
            "-m",
            "Merge branch 'feature'",
            environment=_environment(AUTHOR_EMAIL),
        )

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bot_author_is_skipped(self) -> None:
        _commit(self.repository, "chore: bot update", author_email=BOT_EMAIL)

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
