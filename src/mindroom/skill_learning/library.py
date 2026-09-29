"""Workspace skill mutations with ownership, read-before-write, history, and archival.

Every operation goes through no-follow descriptors below the resolved workspace, because worker code shares it.
Like Hermes' ``created_by: agent`` usage records, ownership lives outside SKILL.md: a skill the learner created stays
learner-owned when anyone later rewrites the file, and a skill created in chat belongs to its human owner. Adding
``metadata.mindroom.learned: true`` hands a skill to the learner, and ``metadata.mindroom.pinned: true`` takes any skill
away from the learner and the curator. The learner changes only learner-owned skills; chat changes any workspace skill.
"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mindroom.atomic_file import atomic_write_bytes_at, existing_file_mode
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root
from mindroom.tool_system.skill_usage import (
    SkillUsage,
    forget_missing_skill_usage,
    load_skill_usage,
    update_skill_usages,
)
from mindroom.tool_system.skills import (
    MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS,
    MAX_WORKSPACE_SKILL_FILE_BYTES,
    MAX_WORKSPACE_SKILL_NAME_CHARS,
    SKILL_FILENAME,
    SkillMarkdownError,
    parse_skill_markdown,
    parse_skill_metadata,
    workspace_skill_file_names,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

logger = get_logger(__name__)

# Hermes SKILL_PROMPT_DESC_LIMIT: new skills must fit the one-line skill index every prompt carries.
_NEW_DESCRIPTION_CHARS = 60
_MAX_SKILL_MARKDOWN_CHARS = 100_000
# Only the support files the agent's skill tools can serve; Hermes also has templates/ and assets/.
_SUPPORT_DIRECTORIES = frozenset({"references", "scripts"})
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_HISTORY_DIRNAME = ".history"
_ARCHIVE_DIRNAME = ".archive"
_HISTORY_KEEP = 10
# Hermes Agent's skill guard patterns for credentials written into skill content (tools/skills_guard.py, MIT).
_CREDENTIAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*[\"'](?!(?-i:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)[\"'])"
        r"[A-Za-z0-9+/=_-]{20,}",
        r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----",
        r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}",
        r"sk-[A-Za-z0-9]{20,}",
        r"sk-ant-[A-Za-z0-9_-]{90,}",
        r"AKIA[0-9A-Z]{16}",
        r"glpat-[A-Za-z0-9_\-]{20,}",
    )
)


class SkillEditError(ValueError):
    """A refused skill edit, worded for the model that asked for it."""


@dataclass(frozen=True)
class SkillFile:
    """One workspace skill file as a write saw it."""

    content: str
    digest: str
    learned: bool
    name: str


def content_digest(content: str) -> str:
    """Return the revision identity used by read-before-write checks."""
    return hashlib.sha256(content.encode()).hexdigest()


def _learner_owns(frontmatter: dict[str, object], usage: SkillUsage, *, path: str) -> bool:
    """Return whether the learner created or was handed this skill and nobody pinned it."""
    mindroom = (parse_skill_metadata(frontmatter.get("metadata"), path=path) or {}).get("mindroom")
    flags = mindroom if isinstance(mindroom, dict) else {}
    if flags.get("pinned") is True:
        return False
    return usage.created_by == "learner" or flags.get("learned") is True


def _validate_skill_name(name: str) -> None:
    """Accept only lowercase hyphenated directory names."""
    if len(name) > MAX_WORKSPACE_SKILL_NAME_CHARS or not _NAME.fullmatch(name):
        msg = (
            f"Invalid skill name {name!r}: use lowercase letters, digits and single hyphens, "
            f"at most {MAX_WORKSPACE_SKILL_NAME_CHARS} characters."
        )
        raise SkillEditError(msg)


def _parsed_markdown(content: str) -> tuple[dict[str, Any], str]:
    """Return an edited SKILL.md's frontmatter and body, refusing what skill loading could not read."""
    try:
        return parse_skill_markdown(content)
    except SkillMarkdownError as exc:
        msg = f"SKILL.md frontmatter is not a valid YAML mapping: {exc}"
        raise SkillEditError(msg) from exc


def _validate_markdown(name: str, content: str, *, new: bool, learner: bool) -> None:
    """Check a SKILL.md: frontmatter ``name`` must stay ``name``, and new skills need a short description."""
    if len(content) > _MAX_SKILL_MARKDOWN_CHARS:
        msg = (
            f"SKILL.md is {len(content)} characters; the limit is {_MAX_SKILL_MARKDOWN_CHARS}. "
            "Move depth into references/."
        )
        raise SkillEditError(msg)
    frontmatter, body = _parsed_markdown(content)
    description = frontmatter.get("description")
    frontmatter_name = frontmatter.get("name")
    # Like skill loading, surrounding whitespace is not part of the name.
    if not isinstance(frontmatter_name, str) or frontmatter_name.strip() != name:
        msg = f"Frontmatter name must be exactly {name!r}."
        raise SkillEditError(msg)
    # Skill loading drops a skill whose metadata it cannot read, so such an edit would silently remove the skill.
    if parse_skill_metadata(frontmatter.get("metadata"), path=name) is None:
        msg = "Frontmatter metadata must be a mapping or a JSON5 object string."
        raise SkillEditError(msg)
    if not isinstance(description, str) or not description.strip():
        msg = "Frontmatter must include a non-empty description."
        raise SkillEditError(msg)
    if len(description) > MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS or (
        new and len(description.strip()) > _NEW_DESCRIPTION_CHARS
    ):
        limit = _NEW_DESCRIPTION_CHARS if new else MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS
        msg = f"Description exceeds {limit} characters; keep one trigger-first sentence and move detail into the body."
        raise SkillEditError(msg)
    if not body:
        msg = "SKILL.md must contain instructions after the frontmatter."
        raise SkillEditError(msg)
    if new and learner and not _learner_owns(frontmatter, SkillUsage(), path=name):
        msg = "A new learned skill needs `metadata: {mindroom: {learned: true}}` in its frontmatter."
        raise SkillEditError(msg)


def _validate_content(relative_path: str, content: str) -> None:
    if len(content.encode()) > MAX_WORKSPACE_SKILL_FILE_BYTES:
        msg = f"{relative_path} exceeds {MAX_WORKSPACE_SKILL_FILE_BYTES} bytes."
        raise SkillEditError(msg)
    if match := next(filter(None, (pattern.search(content) for pattern in _CREDENTIAL_PATTERNS)), None):
        line = content.count("\n", 0, match.start()) + 1
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
def _open_skills_root(skills_root: Path, *, create: bool = False) -> Iterator[int]:
    """Pin ``<workspace>/skills`` below its workspace without following links."""
    with open_directory_within_root(skills_root.parent, skills_root.name, create=create) as root_fd:
        yield root_fd


def _entries(directory_fd: int, *, directories: bool) -> list[str]:
    """Return sorted visible real directories or regular files, never links."""
    with os.scandir(directory_fd) as entries:
        return sorted(
            entry.name
            for entry in entries
            if not entry.name.startswith(".")
            and (entry.is_dir(follow_symlinks=False) if directories else entry.is_file(follow_symlinks=False))
        )


def _read_text(directory_fd: int, relative_path: str) -> str | None:
    """Return one bounded UTF-8 regular file without following links, or None when it is absent."""
    try:
        data = read_regular_file_within_root(directory_fd, relative_path, max_bytes=MAX_WORKSPACE_SKILL_FILE_BYTES)
    except FileNotFoundError:
        return None
    return data.decode("utf-8")


def read_skill_file(skills_root: Path, name: str, relative_path: str = SKILL_FILENAME) -> SkillFile | None:
    """Return one workspace skill file and whether its skill is learner-owned, or None when absent."""
    _split_relative_path(relative_path)
    try:
        with _open_skills_root(skills_root) as root_fd, open_directory_within_root(root_fd, name) as skill_fd:
            return _read_skill_file(skill_fd, name, relative_path, load_skill_usage(root_fd).get(name, SkillUsage()))
    except FileNotFoundError:
        return None


def _read_skill_file(skill_fd: int, name: str, relative_path: str, usage: SkillUsage) -> SkillFile | None:
    markdown = _read_text(skill_fd, SKILL_FILENAME)
    content = markdown if relative_path == SKILL_FILENAME else _read_text(skill_fd, relative_path)
    if content is None:
        return None
    try:
        frontmatter = parse_skill_markdown(markdown)[0] if markdown is not None else {}
    except SkillMarkdownError:
        # A pin in frontmatter that cannot be parsed must still hold, so such a skill is never the learner's.
        return SkillFile(content=content, digest=content_digest(content), learned=False, name=name)
    skill_name = frontmatter.get("name")
    return SkillFile(
        content=content,
        digest=content_digest(content),
        learned=_learner_owns(frontmatter, usage, path=name),
        # An edit keeps the name the skill loads under, which may differ from its directory for an adopted skill.
        name=skill_name.strip() if isinstance(skill_name, str) and skill_name.strip() else name,
    )


def learned_skill_directories(skills_root: Path, directories: Iterable[str]) -> frozenset[str]:
    """Return which of the given workspace skill directories the learner owns, reading the usage file once."""
    directories = list(directories)
    if not directories:
        return frozenset()
    learned: set[str] = set()
    try:
        with _open_skills_root(skills_root) as root_fd:
            usage = load_skill_usage(root_fd)
            for directory in directories:
                try:
                    with open_directory_within_root(root_fd, directory) as skill_fd:
                        markdown = _read_skill_file(
                            skill_fd,
                            directory,
                            SKILL_FILENAME,
                            usage.get(directory, SkillUsage()),
                        )
                except (OSError, ValueError):
                    continue
                if markdown is not None and markdown.learned:
                    learned.add(directory)
    except OSError:
        # Skill loading already warned about a skills directory it could not open.
        return frozenset()
    return frozenset(learned)


def support_file_paths(skills_root: Path, name: str) -> list[str]:
    """Return the support files one workspace skill offers as ``directory/filename``, like its skill loading lists them."""
    with _open_skills_root(skills_root) as root_fd, open_directory_within_root(root_fd, name) as skill_fd:
        return [
            f"{directory}/{filename}"
            for directory in sorted(_SUPPORT_DIRECTORIES)
            for filename in workspace_skill_file_names(skill_fd, directory)
        ]


def create_skill(skills_root: Path, name: str, content: str, *, reserved_names: frozenset[str], learner: bool) -> None:
    """Create a skill, learner-owned when the learner writes it, whose name no configured or workspace skill uses."""
    _validate_skill_name(name)
    _validate_markdown(name, content, new=True, learner=learner)
    _validate_content(SKILL_FILENAME, content)
    if name in reserved_names:
        msg = f"A skill named {name!r} already exists; update it or choose a class-level name."
        raise SkillEditError(msg)
    now = datetime.now(UTC)
    # Workspaces of shared agents without file memory exist only once something is written into them.
    skills_root.parent.mkdir(parents=True, exist_ok=True)
    with _open_skills_root(skills_root, create=True) as root_fd:
        if SKILL_FILENAME in _entries(root_fd, directories=False):
            # Skill loading then reads skills/ as one skill, so a new skill directory would never load.
            msg = f"skills/{SKILL_FILENAME} makes skills/ one skill; move it into skills/<its name>/ first."
            raise SkillEditError(msg)
        directories = _entries(root_fd, directories=True)
        if name in {entry.lower() for entry in directories}:
            msg = f"A workspace skill directory named {name!r} already exists."
            raise SkillEditError(msg)
        os.mkdir(name, dir_fd=root_fd)
        with open_directory_within_root(root_fd, name) as skill_fd:
            atomic_write_bytes_at(skill_fd, SKILL_FILENAME, content.encode())
        # Like Hermes' record of a create, a new skill never inherits the record of a deleted one of the same name.
        update_skill_usages(
            root_fd,
            {name: lambda _usage: SkillUsage(created_by="learner" if learner else None, created_at=now)},
        )


def write_skill_file(
    skills_root: Path,
    name: str,
    relative_path: str,
    content: str,
    *,
    expected_digest: str | None,
    learner: bool,
) -> None:
    """Replace or add one file of a skill whose current version the write is based on."""
    directory, filename = _split_relative_path(relative_path)
    _validate_content(relative_path, content)
    with _open_skills_root(skills_root) as root_fd, open_directory_within_root(root_fd, name) as skill_fd:
        markdown, current = _require_writable(root_fd, skill_fd, name, relative_path, expected_digest, learner=learner)
        if current is not None and current.content == content:
            # Like Hermes, an unchanged file is refused, so it never reads as an update or resets the skill's age.
            msg = f"No change was made because the new {relative_path} is identical to the current one."
            raise SkillEditError(msg)
        if directory is None:
            # An edit keeps the skill's identity, which may differ from its directory for an adopted skill.
            _validate_markdown(markdown.name, content, new=False, learner=learner)
        if current is not None:
            _save_history(root_fd, name, relative_path, current.content)
        if directory is None:
            _write_keeping_mode(skill_fd, filename, content)
        else:
            if directory not in _entries(skill_fd, directories=True):
                os.mkdir(directory, dir_fd=skill_fd)
            with open_directory_within_root(skill_fd, directory) as support_fd:
                _write_keeping_mode(support_fd, filename, content)
        _record_patch(root_fd, name)


def remove_skill_file(
    skills_root: Path,
    name: str,
    relative_path: str,
    *,
    expected_digest: str | None,
    learner: bool,
) -> None:
    """Remove one support file of a skill whose current version the removal is based on."""
    directory, filename = _split_relative_path(relative_path)
    if directory is None:
        msg = "SKILL.md cannot be removed; only support files can."
        raise SkillEditError(msg)
    with _open_skills_root(skills_root) as root_fd, open_directory_within_root(root_fd, name) as skill_fd:
        _markdown, current = _require_writable(root_fd, skill_fd, name, relative_path, expected_digest, learner=learner)
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
    *,
    learner: bool,
) -> tuple[SkillFile, SkillFile | None]:
    """Return the skill's SKILL.md and the current target, which must be the version the write is based on."""
    usage = load_skill_usage(root_fd).get(name, SkillUsage())
    markdown = _read_skill_file(skill_fd, name, SKILL_FILENAME, usage)
    if markdown is None:
        msg = f"Skill {name!r} has no SKILL.md."
        raise SkillEditError(msg)
    if learner and not markdown.learned:
        msg = (
            f"Skill {name!r} is not learner-owned. It belongs to its human owner; mention the needed change in "
            "your reply instead of editing it."
        )
        raise SkillEditError(msg)
    current = _read_skill_file(skill_fd, name, relative_path, usage)
    if current is not None and current.digest != expected_digest:
        loader = (
            "get_skill_instructions" if relative_path == SKILL_FILENAME else "get_skill_reference or get_skill_script"
        )
        msg = (
            f"The current {relative_path} of {name!r} is not the version this change is based on. Load it with "
            f"{loader}, then retry using the content just returned."
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
    update_skill_usages(
        root_fd,
        {name: lambda usage: usage.model_copy(update={"last_patched_at": now})},
    )


def _save_history(root_fd: int, name: str, relative_path: str, content: str) -> None:
    """Keep the replaced file as plain text so a person can restore it by copying it back."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    with open_directory_within_root(root_fd, f"{_HISTORY_DIRNAME}/{name}", create=True) as history_fd:
        atomic_write_bytes_at(history_fd, f"{stamp}--{relative_path.replace('/', '--')}", content.encode())
        for stale in _entries(history_fd, directories=False)[:-_HISTORY_KEEP]:
            os.unlink(stale, dir_fd=history_fd)


def archive_unused_skills(skills_root: Path, *, archive_after_days: int, now: datetime) -> list[str]:
    """Move learner-owned skills without recent activity into ``skills/.archive``; never delete them.

    Like Hermes forgetting deleted skills, records of archived or deleted directories are dropped here, before each
    review, so a name restored or reused after that starts over instead of inheriting the old ownership and
    inactivity; a skill recreated with skill_manage starts over at once, and one recreated with other tools before this
    pass keeps the old record.
    """
    if not skills_root.is_dir():
        return []
    with _open_skills_root(skills_root) as root_fd:
        directories = _entries(root_fd, directories=True)
        archived = _archive_inactive(root_fd, directories, archive_after_days=archive_after_days, now=now)
        forget_missing_skill_usage(root_fd, set(directories) - set(archived))
    return archived


def _archive_inactive(root_fd: int, directories: list[str], *, archive_after_days: int, now: datetime) -> list[str]:
    if archive_after_days <= 0:
        return []
    archived: list[str] = []
    first_seen: list[str] = []
    usage = load_skill_usage(root_fd)
    for name in directories:
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
            first_seen.append(name)
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
    # One write for every first-seen skill, instead of rewriting the usage file for each.
    update_skill_usages(
        root_fd,
        dict.fromkeys(first_seen, lambda record: record.model_copy(update={"created_at": now})),
    )
    return archived
