"""Workspace skill files read through no-follow descriptors, plus their usage telemetry.

Worker code shares agent workspaces, so the primary process never opens a workspace skill by pathname.
Hidden entries under ``skills/`` (usage, history, archive) are never discovered as skills.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import json5
from agno.skills.skill import Skill
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError
from yaml import YAMLError

from mindroom import yaml_io
from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root, open_regular_file_within_root

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

logger = get_logger(__name__)

SKILL_FILENAME = "SKILL.md"
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
MAX_SKILL_FILE_BYTES = 1_048_576
_USAGE_FILENAME = ".usage.json"
_USAGE_LOCK = threading.Lock()


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# Hand-written or worker-written telemetry may omit the offset, which would break comparisons with aware times.
_UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class SkillUsage(BaseModel):
    """Provenance and activity for one workspace skill directory; worker-writable, so it never grants access."""

    # Like Hermes, fields a person or another tool added survive rewrites of the record.
    model_config = ConfigDict(extra="allow")

    created_by: Literal["learner"] | None = None
    created_at: _UtcDatetime | None = None
    use_count: int = 0
    last_used_at: _UtcDatetime | None = None
    patch_count: int = 0
    last_patched_at: _UtcDatetime | None = None

    def last_activity_at(self) -> datetime | None:
        """Return the newest creation, use, or learner edit."""
        moments = [moment for moment in (self.created_at, self.last_used_at, self.last_patched_at) if moment]
        return max(moments, default=None)


@contextmanager
def open_skills_root(skills_root: Path, *, create: bool = False) -> Iterator[int]:
    """Pin ``<workspace>/skills`` below its trusted workspace root without following links."""
    with open_directory_within_root(skills_root.parent, skills_root.name, create=create) as descriptor:
        yield descriptor


def read_text_at(directory_fd: int, relative_path: str) -> str | None:
    """Return one bounded UTF-8 regular file without following links, or None when it is absent."""
    chunks: list[bytes] = []
    size = 0
    try:
        with open_regular_file_within_root(directory_fd, relative_path) as file_fd:
            while chunk := os.read(file_fd, 65536):
                size += len(chunk)
                if size > MAX_SKILL_FILE_BYTES:
                    msg = f"{relative_path} exceeds {MAX_SKILL_FILE_BYTES} bytes"
                    raise ValueError(msg)
                chunks.append(chunk)
    except FileNotFoundError:
        return None
    return b"".join(chunks).decode("utf-8")


def list_entries(directory_fd: int, *, directories: bool) -> list[str]:
    """Return sorted visible real directories or regular files, never links."""
    wanted = stat.S_ISDIR if directories else stat.S_ISREG
    with os.scandir(directory_fd) as entries:
        return sorted(
            entry.name
            for entry in entries
            if not entry.name.startswith(".") and wanted(entry.stat(follow_symlinks=False).st_mode)
        )


def list_support_files(skill_fd: int, directory: str) -> list[str]:
    """Return visible regular files directly inside one support directory; a linked directory has none."""
    try:
        with open_directory_within_root(skill_fd, directory) as support_fd:
            return list_entries(support_fd, directories=False)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Ignoring unsafe workspace skill support directory", directory=directory, error=str(exc))
        return []


def parse_skill_markdown(content: str) -> tuple[dict[str, Any], str]:
    """Split SKILL.md into its frontmatter mapping and instruction body."""
    match = FRONTMATTER_PATTERN.match(content)
    if match is None:
        return {}, content
    frontmatter = yaml_io.safe_load(match.group(1)) or {}
    if not isinstance(frontmatter, dict):
        msg = "Skill frontmatter must be a mapping"
        raise TypeError(msg)
    return frontmatter, match.group(2).strip()


def parse_skill_metadata(raw: object, *, path: str) -> dict[str, Any] | None:
    """Return frontmatter metadata as a mapping, accepting OpenClaw JSON5 strings."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if isinstance(raw, dict):
        return cast("dict[str, Any]", raw)
    if isinstance(raw, str):
        try:
            parsed = json5.loads(raw)
        except Exception as exc:
            logger.warning("Failed to parse skill metadata JSON5", path=path, error=str(exc))
            return None
        if isinstance(parsed, dict):
            return parsed
        logger.warning("Skill metadata JSON5 must be an object", path=path)
        return None

    logger.warning("Skill metadata must be a mapping or JSON5 string", path=path)
    return None


# AGNO_COMPAT: LocalSkills reads skill files by pathname and follows links.
# Reason: Agno's local loader opens SKILL.md, scripts/ and references/ with pathname reads, so a link planted in a
# worker-shared workspace would make the primary read files outside it. The loader has no reader or descriptor
# extension point, so workspace roots build the same Skill fields from descriptor-bound reads.
# Upstream issue: tracking gap; no Agno issue or PR proposes caller-owned file access for LocalSkills.
# Upstream PR: none identified; https://github.com/agno-agi/agno/pull/9194 adds a database loader, not confined files.
# Remove when: LocalSkills accepts a caller-supplied no-follow reader for skill files and support-file discovery.
# Coverage: tests/test_skills.py::test_workspace_loader_skips_links_and_special_files and
# tests/test_skills.py::test_workspace_support_reads_refuse_swapped_links.
def load_workspace_skills(skills_root: Path) -> list[Skill]:
    """Build Agno skills from one workspace skill root, skipping unsafe or unreadable entries."""
    if not skills_root.is_dir():
        return []
    skills: list[Skill] = []
    try:
        with open_skills_root(skills_root) as root_fd:
            for directory in list_entries(root_fd, directories=True):
                try:
                    skill = _load_workspace_skill(root_fd, skills_root, directory)
                except (OSError, ValueError, TypeError, YAMLError) as exc:
                    logger.warning(
                        "Skipping unreadable workspace skill",
                        path=str(skills_root / directory),
                        error=str(exc),
                    )
                    continue
                if skill is not None:
                    skills.append(skill)
    except OSError as exc:
        logger.warning("Workspace skill root is unavailable", path=str(skills_root), error=str(exc))
        return []
    return skills


def _load_workspace_skill(root_fd: int, skills_root: Path, directory: str) -> Skill | None:
    with open_directory_within_root(root_fd, directory) as skill_fd:
        content = read_text_at(skill_fd, SKILL_FILENAME)
        if content is None:
            return None
        frontmatter, instructions = parse_skill_markdown(content)
        return Skill(
            name=frontmatter.get("name", directory),
            description=frontmatter.get("description", ""),
            instructions=instructions,
            source_path=str(skills_root / directory),
            scripts=list_support_files(skill_fd, "scripts"),
            references=list_support_files(skill_fd, "references"),
            metadata=frontmatter.get("metadata"),
            license=frontmatter.get("license"),
            compatibility=frontmatter.get("compatibility"),
            allowed_tools=frontmatter.get("allowed-tools"),
        )


def read_support_file(skill_path: Path, directory: str, filename: str) -> str:
    """Read one listed support file of a workspace skill without following links."""
    if "/" in filename or filename in {"", ".", ".."}:
        msg = f"Invalid support file name: {filename!r}"
        raise ValueError(msg)
    with (
        open_skills_root(skill_path.parent) as root_fd,
        open_directory_within_root(root_fd, skill_path.name) as skill_fd,
    ):
        content = read_text_at(skill_fd, f"{directory}/{filename}")
    if content is None:
        msg = f"{directory}/{filename} does not exist"
        raise FileNotFoundError(msg)
    return content


def _usage_records(root_fd: int) -> dict[str, object]:
    """Return the raw usage records; an unreadable file or one that is not a JSON object reads as empty."""
    try:
        payload = read_text_at(root_fd, _USAGE_FILENAME)
        records = json.loads(payload) if payload else {}
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable skill usage telemetry", error=str(exc))
        return {}
    return records if isinstance(records, dict) else {}


def _parse_usage(record: object) -> SkillUsage | None:
    try:
        return SkillUsage.model_validate(record)
    except ValidationError:
        return None


def _write_usage_records(root_fd: int, records: dict[str, object]) -> None:
    atomic_write_bytes_at(root_fd, _USAGE_FILENAME, json.dumps(records, separators=(",", ":")).encode())


def load_skill_usage(root_fd: int) -> dict[str, SkillUsage]:
    """Return usage keyed by skill directory; a malformed record reads as absent without hiding the others."""
    usage = {name: _parse_usage(record) for name, record in _usage_records(root_fd).items()}
    return {name: record for name, record in usage.items() if record is not None}


def update_skill_usage(root_fd: int, directory: str, update: Callable[[SkillUsage], SkillUsage]) -> None:
    """Replace one skill's usage record atomically, leaving every other record as written.

    The lock is process-local on purpose: any lock inside the worker-shared workspace could be held by worker
    code to stall the primary, so concurrent primaries sharing one storage root may occasionally drop a count.
    """
    with _USAGE_LOCK:
        records = _usage_records(root_fd)
        current = _parse_usage(records.get(directory)) or SkillUsage()
        records[directory] = update(current).model_dump(mode="json", exclude_defaults=True)
        _write_usage_records(root_fd, records)


def forget_missing_skill_usage(root_fd: int) -> None:
    """Drop records of skill directories that are gone, so a restored or reused name starts as a new skill."""
    with _USAGE_LOCK:
        records = _usage_records(root_fd)
        present = set(list_entries(root_fd, directories=True))
        kept = {name: record for name, record in records.items() if name in present}
        if len(kept) < len(records):
            _write_usage_records(root_fd, kept)


def record_skill_use(skill_path: Path) -> None:
    """Count one agent load of a workspace skill; telemetry failures never fail the load."""
    now = datetime.now(UTC)
    try:
        with open_skills_root(skill_path.parent) as root_fd:
            update_skill_usage(
                root_fd,
                skill_path.name,
                lambda usage: usage.model_copy(update={"use_count": usage.use_count + 1, "last_used_at": now}),
            )
    except OSError as exc:
        logger.warning("Could not record workspace skill use", path=str(skill_path), error=str(exc))
