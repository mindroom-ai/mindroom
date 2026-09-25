"""Learner-owned workspace skill mutations with read-before-write, history, and archival.

Every operation goes through no-follow descriptors below the resolved workspace, because worker code shares it.
Like Hermes' ``created_by: agent`` usage records, ownership lives outside SKILL.md: a skill the learner created stays
learner-owned when anyone later rewrites the file. Adding ``metadata.mindroom.learned: true`` hands a skill to the
learner, and ``metadata.mindroom.pinned: true`` takes any skill away from the learner and the curator.
"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from yaml import YAMLError

from mindroom.atomic_file import atomic_write_bytes_at, existing_file_mode
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.redaction import find_credential
from mindroom.tool_system.workspace_skills import (
    MAX_SKILL_FILE_BYTES,
    SKILL_FILENAME,
    SkillUsage,
    forget_missing_skill_usage,
    list_entries,
    list_support_files,
    load_skill_usage,
    open_skills_root,
    parse_skill_markdown,
    parse_skill_metadata,
    read_text_at,
    update_skill_usage,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = get_logger(__name__)

_MAX_NAME_CHARS = 64
_MAX_DESCRIPTION_CHARS = 1024
# Hermes SKILL_PROMPT_DESC_LIMIT: new skills must fit the one-line skill index every prompt carries.
_NEW_DESCRIPTION_CHARS = 60
_MAX_SKILL_MARKDOWN_CHARS = 100_000
# Only the support files the agent's skill tools can serve; Hermes also has templates/ and assets/.
_SUPPORT_DIRECTORIES = frozenset({"references", "scripts"})
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_HISTORY_DIRNAME = ".history"
_ARCHIVE_DIRNAME = ".archive"
_HISTORY_KEEP = 10


class SkillEditError(ValueError):
    """A refused learner edit, worded for the reviewer model."""


@dataclass(frozen=True)
class SkillFile:
    """One workspace skill file as the reviewer saw it."""

    content: str
    digest: str
    learned: bool
    name: str


def content_digest(content: str) -> str:
    """Return the revision identity used by read-before-write checks."""
    return hashlib.sha256(content.encode()).hexdigest()


def learner_owns(frontmatter: dict[str, object], usage: SkillUsage, *, path: str) -> bool:
    """Return whether the learner created or was handed this skill and nobody pinned it."""
    mindroom = (parse_skill_metadata(frontmatter.get("metadata"), path=path) or {}).get("mindroom")
    flags = mindroom if isinstance(mindroom, dict) else {}
    if flags.get("pinned") is True:
        return False
    return usage.created_by == "learner" or flags.get("learned") is True


def _validate_skill_name(name: str) -> None:
    """Accept only lowercase hyphenated directory names."""
    if len(name) > _MAX_NAME_CHARS or not _NAME.fullmatch(name):
        msg = f"Invalid skill name {name!r}: use lowercase letters, digits and single hyphens, at most 64 characters."
        raise SkillEditError(msg)


def _validate_markdown(name: str, content: str, *, new: bool) -> None:
    """Check a learned SKILL.md: frontmatter ``name`` must stay ``name``, and new skills need a short description."""
    if len(content) > _MAX_SKILL_MARKDOWN_CHARS:
        msg = (
            f"SKILL.md is {len(content)} characters; the limit is {_MAX_SKILL_MARKDOWN_CHARS}. "
            "Move depth into references/."
        )
        raise SkillEditError(msg)
    if not content.startswith("---"):
        msg = "SKILL.md must start with YAML frontmatter (---)."
        raise SkillEditError(msg)
    try:
        frontmatter, body = parse_skill_markdown(content)
    except (TypeError, YAMLError) as exc:
        msg = f"SKILL.md frontmatter is not a valid YAML mapping: {exc}"
        raise SkillEditError(msg) from exc
    description = frontmatter.get("description")
    if frontmatter.get("name") != name:
        msg = f"Frontmatter name must be exactly {name!r}."
        raise SkillEditError(msg)
    if not isinstance(description, str) or not description.strip():
        msg = "Frontmatter must include a non-empty description."
        raise SkillEditError(msg)
    if len(description) > _MAX_DESCRIPTION_CHARS or (new and len(description.strip()) > _NEW_DESCRIPTION_CHARS):
        limit = _NEW_DESCRIPTION_CHARS if new else _MAX_DESCRIPTION_CHARS
        msg = f"Description exceeds {limit} characters; keep one trigger-first sentence and move detail into the body."
        raise SkillEditError(msg)
    if not body:
        msg = "SKILL.md must contain instructions after the frontmatter."
        raise SkillEditError(msg)
    if new and not learner_owns(frontmatter, SkillUsage(), path=name):
        msg = "A new learned skill needs `metadata: {mindroom: {learned: true}}` in its frontmatter."
        raise SkillEditError(msg)


def _validate_content(relative_path: str, content: str) -> None:
    if len(content.encode()) > MAX_SKILL_FILE_BYTES:
        msg = f"{relative_path} exceeds {MAX_SKILL_FILE_BYTES} bytes."
        raise SkillEditError(msg)
    if (position := find_credential(content)) is not None:
        line = content.count("\n", 0, position) + 1
        msg = (
            f"Line {line} of {relative_path} looks like a literal credential; replace it with a placeholder such as "
            "<your token> and describe how to obtain it."
        )
        raise SkillEditError(msg)


def _split_relative_path(relative_path: str) -> tuple[str | None, str]:
    """Return ``(support directory, filename)``; SKILL.md has no support directory."""
    if relative_path == SKILL_FILENAME:
        return None, SKILL_FILENAME
    directory, _, filename = relative_path.partition("/")
    if directory not in _SUPPORT_DIRECTORIES or not filename or "/" in filename or filename.startswith("."):
        msg = f"file_path must be SKILL.md or one file directly under {', '.join(sorted(_SUPPORT_DIRECTORIES))}/."
        raise SkillEditError(msg)
    return directory, filename


@contextmanager
def _open_skill(root_fd: int, directory: str) -> Iterator[int]:
    """Open one visible skill directory; new learned skills are named by the stricter creation rule."""
    if "/" in directory or directory.startswith("."):
        msg = f"Invalid skill directory {directory!r}."
        raise SkillEditError(msg)
    with open_directory_within_root(root_fd, directory) as skill_fd:
        yield skill_fd


def read_skill_file(skills_root: Path, name: str, relative_path: str = SKILL_FILENAME) -> SkillFile | None:
    """Return one workspace skill file and whether its skill is learner-owned, or None when absent."""
    _split_relative_path(relative_path)
    try:
        with open_skills_root(skills_root) as root_fd, _open_skill(root_fd, name) as skill_fd:
            return _read_skill_file(skill_fd, name, relative_path, load_skill_usage(root_fd).get(name, SkillUsage()))
    except FileNotFoundError:
        return None


def _read_skill_file(skill_fd: int, name: str, relative_path: str, usage: SkillUsage) -> SkillFile | None:
    markdown = read_text_at(skill_fd, SKILL_FILENAME)
    content = markdown if relative_path == SKILL_FILENAME else read_text_at(skill_fd, relative_path)
    if content is None:
        return None
    try:
        frontmatter = parse_skill_markdown(markdown)[0] if markdown is not None else {}
    except (TypeError, YAMLError):
        frontmatter = {}
    skill_name = frontmatter.get("name")
    return SkillFile(
        content=content,
        digest=content_digest(content),
        learned=learner_owns(frontmatter, usage, path=name),
        name=skill_name if isinstance(skill_name, str) else name,
    )


def support_file_paths(skills_root: Path, name: str) -> list[str]:
    """Return every visible support file of one workspace skill as ``directory/filename``."""
    with open_skills_root(skills_root) as root_fd, _open_skill(root_fd, name) as skill_fd:
        return [
            f"{directory}/{filename}"
            for directory in sorted(_SUPPORT_DIRECTORIES)
            for filename in list_support_files(skill_fd, directory)
        ]


def create_skill(skills_root: Path, name: str, content: str, *, reserved_names: frozenset[str]) -> None:
    """Create a new learner-owned skill whose name no configured or workspace skill already uses."""
    _validate_skill_name(name)
    _validate_markdown(name, content, new=True)
    _validate_content(SKILL_FILENAME, content)
    if name in reserved_names:
        msg = f"A skill named {name!r} already exists; update it or choose a class-level name."
        raise SkillEditError(msg)
    now = datetime.now(UTC)
    # Workspaces of shared agents without file memory exist only once something is written into them.
    skills_root.parent.mkdir(parents=True, exist_ok=True)
    with open_skills_root(skills_root, create=True) as root_fd:
        if name in {entry.lower() for entry in list_entries(root_fd, directories=True)}:
            msg = f"A workspace skill directory named {name!r} already exists."
            raise SkillEditError(msg)
        os.mkdir(name, dir_fd=root_fd)
        with open_directory_within_root(root_fd, name) as skill_fd:
            atomic_write_bytes_at(skill_fd, SKILL_FILENAME, content.encode())
        update_skill_usage(
            root_fd,
            name,
            lambda usage: usage.model_copy(update={"created_by": "learner", "created_at": now}),
        )


def write_skill_file(
    skills_root: Path,
    name: str,
    relative_path: str,
    content: str,
    *,
    expected_digest: str | None,
) -> None:
    """Replace or add one file of a learner-owned skill that the reviewer read in its current state."""
    directory, filename = _split_relative_path(relative_path)
    _validate_content(relative_path, content)
    with open_skills_root(skills_root) as root_fd, _open_skill(root_fd, name) as skill_fd:
        markdown, current = _require_writable(root_fd, skill_fd, name, relative_path, expected_digest)
        if current is not None and current.content == content:
            # Like Hermes, an unchanged file is refused, so it never reads as an update or resets the skill's age.
            msg = f"No change was made because the new {relative_path} is identical to the current one."
            raise SkillEditError(msg)
        if directory is None:
            # An edit keeps the skill's identity, which may differ from its directory for an adopted skill.
            _validate_markdown(markdown.name, content, new=False)
        if current is not None:
            _save_history(root_fd, name, relative_path, current.content)
        if directory is None:
            _write_keeping_mode(skill_fd, filename, content)
        else:
            if directory not in list_entries(skill_fd, directories=True):
                os.mkdir(directory, dir_fd=skill_fd)
            with open_directory_within_root(skill_fd, directory) as support_fd:
                _write_keeping_mode(support_fd, filename, content)
        _record_patch(root_fd, name)


def remove_skill_file(skills_root: Path, name: str, relative_path: str, *, expected_digest: str | None) -> None:
    """Remove one support file of a learner-owned skill after the reviewer read it."""
    directory, filename = _split_relative_path(relative_path)
    if directory is None:
        msg = "SKILL.md cannot be removed; only support files can."
        raise SkillEditError(msg)
    with open_skills_root(skills_root) as root_fd, _open_skill(root_fd, name) as skill_fd:
        _markdown, current = _require_writable(root_fd, skill_fd, name, relative_path, expected_digest)
        if current is None:
            msg = f"{relative_path} does not exist."
            raise SkillEditError(msg)
        _save_history(root_fd, name, relative_path, current.content)
        with open_directory_within_root(skill_fd, directory) as support_fd:
            os.unlink(filename, dir_fd=support_fd)
        _record_patch(root_fd, name)


def _require_writable(
    root_fd: int,
    skill_fd: int,
    name: str,
    relative_path: str,
    expected_digest: str | None,
) -> tuple[SkillFile, SkillFile | None]:
    """Return the learner-owned SKILL.md and the current target, which must match the reviewer's last read."""
    usage = load_skill_usage(root_fd).get(name, SkillUsage())
    markdown = _read_skill_file(skill_fd, name, SKILL_FILENAME, usage)
    if markdown is None or not markdown.learned:
        msg = (
            f"Skill {name!r} is not learner-owned. It belongs to its human owner; mention the needed change in "
            "your reply instead of editing it."
        )
        raise SkillEditError(msg)
    current = _read_skill_file(skill_fd, name, relative_path, usage)
    if current is not None and current.digest != expected_digest:
        msg = (
            f"The current {relative_path} of {name!r} has not been loaded in this review. Call "
            "skill_view for it, then retry using the content just returned."
        )
        raise SkillEditError(msg)
    return markdown, current


def _write_keeping_mode(directory_fd: int, filename: str, content: str) -> None:
    """Replace a file atomically; an existing file keeps the permissions its owner gave it."""
    atomic_write_bytes_at(
        directory_fd,
        filename,
        content.encode(),
        file_mode=existing_file_mode(directory_fd, filename),
    )


def _record_patch(root_fd: int, name: str) -> None:
    now = datetime.now(UTC)
    update_skill_usage(
        root_fd,
        name,
        lambda usage: usage.model_copy(update={"patch_count": usage.patch_count + 1, "last_patched_at": now}),
    )


def _save_history(root_fd: int, name: str, relative_path: str, content: str) -> None:
    """Keep the replaced file as plain text so a person can restore it by copying it back."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    with open_directory_within_root(root_fd, f"{_HISTORY_DIRNAME}/{name}", create=True) as history_fd:
        atomic_write_bytes_at(history_fd, f"{stamp}--{relative_path.replace('/', '--')}", content.encode())
        for stale in list_entries(history_fd, directories=False)[:-_HISTORY_KEEP]:
            os.unlink(stale, dir_fd=history_fd)


def archive_unused_skills(skills_root: Path, *, archive_after_days: int, now: datetime) -> list[str]:
    """Move learner-owned skills without recent activity into ``skills/.archive``; never delete them.

    Like Hermes forgetting deleted skills, records of archived or deleted directories are dropped, so a restored
    or reused name starts over instead of inheriting the old ownership and inactivity.
    """
    if not skills_root.is_dir():
        return []
    with open_skills_root(skills_root) as root_fd:
        archived = _archive_inactive(root_fd, archive_after_days=archive_after_days, now=now)
        forget_missing_skill_usage(root_fd)
    return archived


def _archive_inactive(root_fd: int, *, archive_after_days: int, now: datetime) -> list[str]:
    if archive_after_days <= 0:
        return []
    archived: list[str] = []
    usage = load_skill_usage(root_fd)
    for name in list_entries(root_fd, directories=True):
        try:
            with open_directory_within_root(root_fd, name) as skill_fd:
                markdown = _read_skill_file(skill_fd, name, SKILL_FILENAME, usage.get(name, SkillUsage()))
        except (OSError, ValueError) as exc:
            # One unreadable user skill must not block archival, and with it every review of the workspace.
            logger.warning("Skipping unreadable workspace skill during archival", skill=name, error=str(exc))
            continue
        if markdown is None or not markdown.learned:
            continue
        last_activity = usage.get(name, SkillUsage()).last_activity_at()
        if last_activity is None:
            # First sight of an adopted or restored skill starts its inactivity clock, like Hermes' seeded records.
            update_skill_usage(root_fd, name, lambda record: record.model_copy(update={"created_at": now}))
            continue
        if (now - last_activity).days < archive_after_days:
            continue
        with open_directory_within_root(root_fd, _ARCHIVE_DIRNAME, create=True) as archive_fd:
            os.rename(
                name,
                f"{name}--{now.strftime('%Y%m%dT%H%M%SZ')}",
                src_dir_fd=root_fd,
                dst_dir_fd=archive_fd,
            )
        archived.append(name)
    return archived
