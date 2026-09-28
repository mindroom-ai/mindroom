"""Workspace skill files read through no-follow descriptors, plus their usage telemetry.

Worker code shares agent workspaces, so the primary process never opens a workspace skill by pathname.
Hidden entries under ``skills/`` (usage, history, archive) are never discovered as skills.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import json5
from agno.skills.skill import Skill
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from yaml import YAMLError

from mindroom import yaml_io
from mindroom.atomic_file import atomic_write_bytes_at, existing_file_mode
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root, open_regular_file_within_root

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

logger = get_logger(__name__)

SKILL_FILENAME = "SKILL.md"
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
MAX_SKILL_FILE_BYTES = 1_048_576
WORKSPACE_SKILLS_DIRNAME = "skills"
_MAX_WORKSPACE_SKILLS = 256
_MAX_WORKSPACE_SKILLS_BYTES = 8 << 20
# Names, descriptions, and file listings reach every system prompt, not only the skills a model opens.
_MAX_WORKSPACE_SKILL_NAME_CHARS = 64
_MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS = 1024
_MAX_WORKSPACE_SKILL_LISTING_ENTRIES = 256
_MAX_COUNT = 2**53
_USAGE_FILENAME = ".usage.json"
_USAGE_LOCK = threading.Lock()


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# Hand-written or worker-written telemetry may omit the offset, which would break comparisons with aware times.
_UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class SkillUsage(BaseModel):
    """Provenance and activity for one workspace skill directory; worker-writable, so it never grants access."""

    # Fields a person or another tool added survive rewrites of the record.
    model_config = ConfigDict(extra="allow")

    created_by: Literal["learner"] | None = None
    created_at: _UtcDatetime | None = None
    # A count out of range, which only a hand edit makes, starts over instead of growing past what JSON can write.
    use_count: int = Field(default=0, ge=0, le=_MAX_COUNT)
    last_used_at: _UtcDatetime | None = None
    patch_count: int = Field(default=0, ge=0, le=_MAX_COUNT)
    last_patched_at: _UtcDatetime | None = None

    def last_activity_at(self) -> datetime | None:
        """Return the newest creation, use, or ``skill_manage`` edit."""
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
    """Return sorted visible real directories or regular files, never links.

    An entry removed meanwhile never raises here; callers skip it when opening it fails.
    """
    with os.scandir(directory_fd) as entries:
        return sorted(
            entry.name
            for entry in entries
            if not entry.name.startswith(".")
            and (entry.is_dir(follow_symlinks=False) if directories else entry.is_file(follow_symlinks=False))
        )


def _readable_size(directory_fd: int, filename: str) -> bool:
    try:
        return os.stat(filename, dir_fd=directory_fd, follow_symlinks=False).st_size <= MAX_SKILL_FILE_BYTES
    except FileNotFoundError:
        return False


def list_support_files(skill_fd: int, directory: str) -> list[str]:
    """Return the readable regular files directly inside one support directory; a linked directory has none."""
    try:
        with open_directory_within_root(skill_fd, directory) as support_fd:
            filenames = list_entries(support_fd, directories=False)
            if len(filenames) > _MAX_WORKSPACE_SKILL_LISTING_ENTRIES:
                logger.warning(
                    "Listing only the first workspace skill files",
                    directory=directory,
                    found=len(filenames),
                )
            # A file too large to read is not offered.
            return [
                filename
                for filename in filenames[:_MAX_WORKSPACE_SKILL_LISTING_ENTRIES]
                if _readable_size(support_fd, filename)
            ]
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Ignoring unsafe workspace skill support directory", directory=directory, error=str(exc))
        return []


def _simple_frontmatter(text: str) -> dict[str, Any]:
    """Parse ``key: value`` lines like Agno's ``LocalSkills`` fallback for frontmatter that is not strict YAML."""
    fields: dict[str, Any] = {}
    for line in text.strip().split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def _strict_frontmatter(text: str) -> Any:  # noqa: ANN401
    """Parse frontmatter that worker code can write with PyYAML's pure-Python loader, like Agno's LocalSkills."""
    try:
        return yaml_io.safe_load_untrusted(text) or {}
    except YAMLError:
        raise
    except Exception as exc:
        # PyYAML refuses values such as 2026-02-30, `!!int ""`, `!!bool maybe`, or deep nesting with ValueError,
        # IndexError, KeyError, AttributeError, or RecursionError; like LocalSkills, any of them makes the YAML invalid.
        raise YAMLError(str(exc)) from exc


def normalized_newlines(text: str) -> str:
    """Return text with the line endings Agno's LocalSkills reads, which ``Path.read_text`` normalizes."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_skill_markdown(content: str, *, loose: bool = False) -> tuple[dict[str, Any], str]:
    """Split SKILL.md into its frontmatter mapping and instruction body.

    Ownership and edit checks need strict YAML; ``loose`` loads a skill for the agent the way Agno does.
    """
    content = normalized_newlines(content)
    match = FRONTMATTER_PATTERN.match(content)
    if match is None:
        return {}, content
    try:
        frontmatter = _strict_frontmatter(match.group(1))
    except YAMLError:
        if not loose:
            raise
        # Skills Agno loads, such as "description: Use when: deploying", must keep loading from workspaces.
        frontmatter = _simple_frontmatter(match.group(1))
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


def _each_skill_directory[Result](
    skills_root: Path,
    read: Callable[[int, str], Result | None],
    *,
    limit: int | None = None,
) -> Iterator[Result]:
    """Read visible workspace skill directories as the caller consumes them, skipping unreadable entries.

    Worker code can plant entries in a shared workspace, so one never hides the others or fails the caller, and an
    unavailable root yields nothing.
    """
    if not skills_root.is_dir():
        return
    try:
        with open_skills_root(skills_root) as root_fd:
            with suppress(FileNotFoundError):
                os.stat(SKILL_FILENAME, dir_fd=root_fd, follow_symlinks=False)
                # LocalSkills loaded such a file as the only skill of the root, hiding every skill directory beside it.
                logger.warning("Ignoring SKILL.md directly in the workspace skills directory", path=str(skills_root))
            directories = list_entries(root_fd, directories=True)
            if limit is not None and len(directories) > limit:
                logger.warning(
                    "Loading only the first workspace skills",
                    path=str(skills_root),
                    limit=limit,
                    found=len(directories),
                )
                directories = directories[:limit]
            for directory in directories:
                try:
                    with open_directory_within_root(root_fd, directory) as skill_fd:
                        result = read(skill_fd, directory)
                except (OSError, ValueError, TypeError) as exc:
                    logger.warning(
                        "Skipping unreadable workspace skill",
                        path=str(skills_root / directory),
                        error=str(exc),
                    )
                    continue
                if result is not None:
                    yield result
    except OSError as exc:
        logger.warning("Workspace skill root is unavailable", path=str(skills_root), error=str(exc))


# AGNO_COMPAT: LocalSkills reads skill files by pathname and follows links.
# Reason: Agno's local loader opens SKILL.md, scripts/ and references/ with pathname reads, so a link planted in a
# worker-shared workspace would make the primary read files outside it. The loader has no reader or descriptor
# extension point, so workspace roots build the same Skill fields from descriptor-bound reads.
# Upstream issue: tracking gap; no Agno issue or PR proposes caller-owned file access for LocalSkills.
# Upstream PR: none identified; https://github.com/agno-agi/agno/pull/9194 adds a database loader, not confined files.
# Remove when: LocalSkills accepts a caller-supplied no-follow reader for skill files and support-file discovery.
# Coverage: tests/test_skills.py::test_workspace_loader_skips_links_and_special_files,
# tests/test_skills.py::test_workspace_support_reads_refuse_swapped_links, and
# tests/test_skills.py::test_workspace_skill_with_loose_frontmatter_loads_like_agno.
def load_workspace_skills(skills_root: Path) -> list[Skill]:
    """Build Agno skills from one workspace skill root, skipping unsafe or unreadable entries.

    Every loaded skill reaches the system prompt, so the skills share a count cap and a total budget.
    """
    skills: list[Skill] = []
    loaded_bytes = 0
    for skill in _each_skill_directory(
        skills_root,
        lambda skill_fd, directory: _load_workspace_skill(skill_fd, skills_root, directory),
        limit=_MAX_WORKSPACE_SKILLS,
    ):
        prompt_parts = (skill.name, skill.description, skill.instructions, skill.metadata or "")
        loaded_bytes += len("".join(map(str, (*prompt_parts, *skill.scripts, *skill.references))).encode())
        if loaded_bytes > _MAX_WORKSPACE_SKILLS_BYTES:
            logger.warning("Workspace skills exceed their budget; skipping the rest", path=str(skills_root))
            break
        skills.append(skill)
    return skills


def workspace_skill_directories(skills_root: Path) -> list[str]:
    """Return the visible workspace directories that hold a SKILL.md, without reading it."""
    return list(
        _each_skill_directory(
            skills_root,
            lambda skill_fd, directory: (
                directory if SKILL_FILENAME in list_entries(skill_fd, directories=False) else None
            ),
        ),
    )


def _frontmatter_name(frontmatter: dict[str, Any], directory: str) -> str | None:
    """Return the stripped name a skill loads under, its directory's when it names none, or None when it is unusable."""
    name = frontmatter.get("name", directory)
    return name.strip() if isinstance(name, str) and name.strip() else None


def workspace_skill_name(content: str, directory: str) -> str | None:
    """Return the name a workspace SKILL.md loads under, read loosely like skill loading, or None when it has none."""
    try:
        frontmatter, _instructions = parse_skill_markdown(content, loose=True)
    except TypeError:
        return None
    return _frontmatter_name(frontmatter, directory)


def _load_workspace_skill(skill_fd: int, skills_root: Path, directory: str) -> Skill | None:
    path = skills_root / directory / SKILL_FILENAME
    try:
        content = read_text_at(skill_fd, SKILL_FILENAME)
    except (OSError, ValueError) as exc:
        logger.warning("Refused a workspace skill file", path=str(path), error=str(exc))
        return None
    if content is None:
        return None
    frontmatter, instructions = parse_skill_markdown(content, loose=True)
    # Skill normalization drops a skill without a usable name.
    name = _frontmatter_name(frontmatter, directory) or ""
    if len(name) > _MAX_WORKSPACE_SKILL_NAME_CHARS:
        logger.warning("Refused a workspace skill whose name is too long", path=str(path))
        return None
    description = frontmatter.get("description", "")
    if isinstance(description, str) and len(description) > _MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS:
        logger.warning("Truncated a workspace skill description", path=str(path))
        description = description[:_MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS]
    return Skill(
        name=name,
        description=description,
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
    return normalized_newlines(content)


def _usage_records(root_fd: int) -> dict[str, object] | None:
    """Return the raw usage records, empty without a file, or None for a file that is not a readable JSON object."""
    try:
        payload = read_text_at(root_fd, _USAGE_FILENAME)
        records = json.loads(payload) if payload else {}
    except (OSError, ValueError, RecursionError) as exc:
        logger.warning("Ignoring unreadable skill usage telemetry", error=str(exc))
        return None
    if not isinstance(records, dict):
        logger.warning("Ignoring skill usage telemetry that is not a JSON object")
        return None
    return records


def _parse_usage(record: object) -> SkillUsage | None:
    """Parse one record, dropping only fields that do not validate, so ownership and the rest stay."""
    if not isinstance(record, dict):
        return None
    try:
        usage = SkillUsage.model_validate(record)
    except ValidationError as exc:
        errors = exc.errors()
        if not all(error["loc"] for error in errors):
            # An error outside every field, such as a key that is not valid Unicode, makes the whole record malformed.
            return None
        invalid = {error["loc"][0] for error in errors}
        usage = SkillUsage.model_validate({name: value for name, value in record.items() if name not in invalid})
    try:
        usage.model_dump(mode="json")
    except ValueError:
        # Hand-added fields that cannot all be written back, such as one nested too deeply, are dropped together,
        # keeping ownership and the counts.
        usage = SkillUsage.model_validate(usage.model_dump(include=set(SkillUsage.model_fields)))
    return usage


def _write_usage_records(root_fd: int, records: dict[str, object]) -> None:
    atomic_write_bytes_at(
        root_fd,
        _USAGE_FILENAME,
        json.dumps(records, separators=(",", ":")).encode(),
        file_mode=existing_file_mode(root_fd, _USAGE_FILENAME),
    )


def load_skill_usage(root_fd: int) -> dict[str, SkillUsage]:
    """Return usage keyed by skill directory; a malformed record reads as absent without hiding the others."""
    usage = {name: _parse_usage(record) for name, record in (_usage_records(root_fd) or {}).items()}
    return {name: record for name, record in usage.items() if record is not None}


def update_skill_usage(root_fd: int, directory: str, update: Callable[[SkillUsage], SkillUsage]) -> None:
    """Replace one skill's usage record atomically, leaving every other record as written.

    Telemetry never fails its caller: records are cleaned when read, and a write that fails is logged.
    The lock is process-local on purpose: any lock inside the worker-shared workspace could be held by worker
    code to stall the primary, so concurrent primaries sharing one storage root may occasionally drop a count.
    """
    with _USAGE_LOCK:
        records = _usage_records(root_fd)
        if records is None:
            # Rewriting an unreadable file would drop every record in it; a person can still repair it.
            return
        current = _parse_usage(records.get(directory)) or SkillUsage()
        records[directory] = update(current).model_dump(mode="json", exclude_defaults=True)
        try:
            _write_usage_records(root_fd, records)
        except OSError as exc:
            # The change this records has already landed, so a failed write, such as on a full disk, is only logged.
            logger.warning("Could not update skill usage telemetry", directory=directory, error=str(exc))


def forget_missing_skill_usage(root_fd: int) -> None:
    """Drop records of skill directories that are gone, so a name restored or reused afterwards starts as a new skill."""
    with _USAGE_LOCK:
        records = _usage_records(root_fd)
        if records is None:
            return
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
