"""Reject AI coding agent attribution (Claude, Codex, Gemini) in commit messages and commit identities.

Runs as a pre-commit ``commit-msg`` hook on the message being written, and in CI with ``--range`` on every commit a pull request adds.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Git appends the staged diff below this line for ``git commit --verbose`` and strips it from the final message.
SCISSORS_LINE = "# ------------------------ >8 ------------------------"
MESSAGE_PATTERNS = (
    re.compile(r"^\s*co-authored-by:.*\b(claude|anthropic|codex|openai|gemini)\b", re.IGNORECASE),
    re.compile(r"^\W*generated with \[?(claude code|codex)\b", re.IGNORECASE),
    re.compile(r"claude\.ai/code/session_|chatgpt\.com/codex/tasks/", re.IGNORECASE),
)
IDENTITY_PATTERN = re.compile(r"@(anthropic|openai)\.com>$", re.IGNORECASE)


def message_violations(message: str) -> list[str]:
    """Return the commit message lines that attribute the work to an AI coding agent."""
    message = message.split(SCISSORS_LINE, 1)[0]
    return [
        line
        for line in message.splitlines()
        if not line.startswith("#") and any(pattern.search(line) for pattern in MESSAGE_PATTERNS)
    ]


def range_violations(revision_range: str) -> list[str]:
    """Return a description of every attribution in the commits of a revision range."""
    log = subprocess.run(
        ["git", "log", "-z", "--format=%h%n%an <%ae>%n%cn <%ce>%n%B", revision_range],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    violations = []
    for entry in filter(None, log.split("\0")):
        commit, author, committer, message = entry.split("\n", 3)
        violations.extend(
            f"{commit}: {role} {identity}"
            for role, identity in (("author", author), ("committer", committer))
            if IDENTITY_PATTERN.search(identity)
        )
        violations.extend(f"{commit}: {line.strip()}" for line in message_violations(message))
    return violations


def main() -> int:
    """Check one commit message file or every commit in a revision range."""
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("message_file", nargs="?", type=Path, help="commit message file passed to commit-msg")
    target.add_argument("--range", dest="revision_range", help="git revision range, for example origin/main..HEAD")
    args = parser.parse_args()

    if args.revision_range:
        violations = range_violations(args.revision_range)
    else:
        violations = message_violations(args.message_file.read_text(encoding="utf-8"))
    if not violations:
        return 0
    print("AI agent attribution is not allowed in commits:", file=sys.stderr)
    for violation in violations:
        print(f"  {violation}", file=sys.stderr)
    print("Remove the attribution lines and commit identities before committing or merging.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
