"""Tests for the hook that rejects Claude attribution in commits."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).parent.parent / ".github" / "scripts" / "check_commit_attribution.py"
CLAUDE_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
SCISSORS_LINE = "# ------------------------ >8 ------------------------"


def _run_hook(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), *args],
        check=False,
        text=True,
        capture_output=True,
        cwd=cwd,
    )


def _check_message(tmp_path: Path, message: str) -> subprocess.CompletedProcess[str]:
    message_file = tmp_path / "COMMIT_EDITMSG"
    message_file.write_text(message, encoding="utf-8")
    return _run_hook(str(message_file))


@pytest.mark.parametrize(
    "message",
    [
        f"Fix the bug\n\n{CLAUDE_TRAILER}\n",
        "Fix the bug\n\nco-authored-by: Reviewer <reviewer@anthropic.com>\n",
        "Fix the bug\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n",
        "Fix the bug\n\nhttps://claude.ai/code/session_0123456789\n",
    ],
)
def test_commit_message_with_attribution_is_rejected(tmp_path: Path, message: str) -> None:
    """Claude trailers, footers, and session links fail the commit-msg hook."""
    result = _check_message(tmp_path, message)

    assert result.returncode == 1
    assert message.splitlines()[-1] in result.stderr


@pytest.mark.parametrize(
    "message",
    [
        "Fix the bug\n\nCo-authored-by: Ada <ada@example.com>\n",
        "Describe why Co-Authored-By: Claude trailers are rejected\n",
        f"Fix the bug\n# {CLAUDE_TRAILER}\n",
        f"Fix the bug\n{SCISSORS_LINE}\n {CLAUDE_TRAILER}\n",
    ],
)
def test_commit_message_without_attribution_is_accepted(tmp_path: Path, message: str) -> None:
    """Human co-authors, prose mentions, comments, and verbose diffs pass the commit-msg hook."""
    assert _check_message(tmp_path, message).returncode == 0


def _commit(repo: Path, message: str, email: str = "dev@example.com") -> None:
    subprocess.run(
        [
            "git",
            "-c",
            f"user.email={email}",
            "-c",
            "user.name=Dev",
            "commit",
            "--allow-empty",
            "--no-verify",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
        cwd=repo,
    )


def test_revision_range_reports_attributed_messages_and_identities(tmp_path: Path) -> None:
    """Range mode checks every commit message and author identity the range adds."""
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], check=True, cwd=tmp_path)
    _commit(tmp_path, f"Base commit\n\n{CLAUDE_TRAILER}")
    subprocess.run(["git", "switch", "--quiet", "--create", "feature"], check=True, cwd=tmp_path)
    _commit(tmp_path, "Clean commit")

    assert _run_hook("--range", "main..feature", cwd=tmp_path).returncode == 0

    _commit(tmp_path, f"Attributed commit\n\n{CLAUDE_TRAILER}")
    _commit(tmp_path, "Commit by Claude", email="noreply@anthropic.com")
    result = _run_hook("--range", "main..feature", cwd=tmp_path)

    assert result.returncode == 1
    assert CLAUDE_TRAILER in result.stderr
    assert "author Dev <noreply@anthropic.com>" in result.stderr
    assert "committer Dev <noreply@anthropic.com>" in result.stderr
    assert result.stderr.count(CLAUDE_TRAILER) == 1
