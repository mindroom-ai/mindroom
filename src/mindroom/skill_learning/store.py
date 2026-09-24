"""Constrained Markdown publication with replay recovery and rollback history."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

from mindroom import yaml_io
from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.skills import list_skill_listings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.+)\Z", re.DOTALL)
_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}"
    r"|(?i:password|api[_-]?key|access[_-]?token|authorization)\s*[:=]\s*[\"']?[^\s\"']{8,}",
)


def digest(content: str) -> str:
    """Return a stable content revision without storing the source text."""
    return hashlib.sha256(content.encode()).hexdigest()


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    """Open or create a directory without following any symlink component."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            with suppress(FileExistsError):
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        msg = "Unsafe skill path"
        os.close(descriptor)
        raise ValueError(msg) from exc
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _read(descriptor: int, filename: str) -> str | None:
    try:
        file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        msg = "Unsafe skill file"
        raise ValueError(msg) from exc
    with os.fdopen(file_fd) as stream:
        return stream.read()


class SkillStore:
    """Publish only learner-owned skills in one resolved workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.absolute()

    def snapshot(self) -> dict[str, str]:
        """Capture all existing workspace names, including manually authored skills."""
        return {
            entry.name: digest(self.read_skill(entry.path))
            for entry in list_skill_listings([self.workspace / "skills"])
        }

    def read_skill(self, path: Path) -> str:
        """Read workspace Markdown without following an escaped path."""
        if not path.is_relative_to(self.workspace / "skills"):
            msg = "Unsafe skill path"
            raise ValueError(msg)
        with _directory(path.parent) as descriptor:
            return _read(descriptor, "SKILL.md") or ""

    def owned_names(self) -> set[str]:
        """Identify unedited learner-owned files for review context."""
        with _directory(self.workspace) as descriptor:
            state = json.loads(_read(descriptor, ".skill-learning.json") or "{}")
        snapshot = self.snapshot()
        return {name for name, entry in state.items() if snapshot.get(name) == digest(entry["markdown"])}

    def publish(
        self,
        name: str,
        markdown: str,
        *,
        action: str,
        expected: dict[str, str],
        source: str,
        max_chars: int = 12000,
    ) -> bool:
        """Validate, journal, and atomically publish; replay never duplicates a version."""
        self.validate(name, markdown, max_chars)
        if name in {entry.name.lower() for entry in list_skill_listings()}:
            msg = "Protected skill name"
            raise ValueError(msg)
        with _directory(self.workspace) as workspace_fd:
            lock_fd = os.open(
                ".skill-learning.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=workspace_fd,
            )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                return self._publish_locked(workspace_fd, name, markdown, action, expected, source)
            finally:
                os.close(lock_fd)

    def _publish_locked(
        self,
        workspace_fd: int,
        name: str,
        markdown: str,
        action: str,
        expected: dict[str, str],
        source: str,
    ) -> bool:
        state = json.loads(_read(workspace_fd, ".skill-learning.json") or "{}")
        previous = state.get(name)
        with _directory(self.workspace / "skills" / name) as skill_fd:
            current = _read(skill_fd, "SKILL.md")
            current_hash = digest(current) if current is not None else None
            if previous and previous["source"] == source and previous["markdown"] == markdown and current == markdown:
                return False
            if current_hash != expected.get(name):
                msg = "Skill changed during review"
                raise ValueError(msg)
            recovering = bool(previous and previous["source"] == source and previous["markdown"] == markdown)
            if recovering:
                if current_hash != previous.get("base_hash"):
                    msg = "Skill changed during recovery"
                    raise ValueError(msg)
            elif action == "create":
                if current is not None or name in {existing.lower() for existing in self.snapshot()}:
                    msg = "Skill already exists"
                    raise ValueError(msg)
            elif action != "update" or not previous or current_hash != digest(previous["markdown"]):
                msg = "Skill is not learner-owned or was edited"
                raise ValueError(msg)
            if not recovering:
                history = previous["previous"] + [previous["markdown"]] if previous else []
                state[name] = {
                    "source": source,
                    "markdown": markdown,
                    "previous": history[-5:],
                    "base_hash": current_hash,
                }
            # Journal first: replay can recognize a published file after a crash before queue acknowledgement.
            atomic_write_bytes_at(workspace_fd, ".skill-learning.json", json.dumps(state).encode(), file_mode=0o600)
            atomic_write_bytes_at(skill_fd, "SKILL.md", markdown.encode(), file_mode=0o600)
        return True

    @staticmethod
    def validate(name: str, markdown: str, max_chars: int) -> None:
        """Reject unsafe identities, oversized output, credentials and malformed frontmatter."""
        if (
            len(name) > 64
            or not _NAME.fullmatch(name)
            or len(markdown) > max_chars
            or _SECRET.search(markdown)
            or redact_sensitive_text(markdown) != markdown
        ):
            msg = "Invalid skill name, size, or credential-like content"
            raise ValueError(msg)
        match = _FRONTMATTER.fullmatch(markdown)
        metadata = yaml_io.safe_load(match.group(1)) if match else None
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"name", "description"}
            or metadata["name"] != name
            or not isinstance(metadata["description"], str)
            or not metadata["description"].strip()
        ):
            msg = "Invalid skill frontmatter"
            raise ValueError(msg)
