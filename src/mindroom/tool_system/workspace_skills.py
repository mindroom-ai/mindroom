"""Workspace skill files read through no-follow descriptors, plus their usage telemetry.

Worker code shares agent workspaces, so the primary process never opens a workspace skill by pathname.
Hidden entries under ``skills/`` (usage, history, archive) are never discovered as skills.
"""

from __future__ import annotations

import json
import os
import pickle
import re
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import json5
from agno.skills.skill import Skill
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from yaml import YAMLError

from mindroom import yaml_io
from mindroom.atomic_file import atomic_write_bytes_at, existing_file_mode
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

logger = get_logger(__name__)

SKILL_FILENAME = "SKILL.md"
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
MAX_SKILL_FILE_BYTES = 1_048_576
WORKSPACE_SKILLS_DIRNAME = "skills"
MAX_WORKSPACE_SKILLS = 256
MAX_WORKSPACE_SKILLS_BYTES = 8 << 20
# Names, descriptions, and file listings reach every system prompt, not only the skills a model opens.
MAX_WORKSPACE_SKILL_NAME_CHARS = 64
MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS = 1024
MAX_WORKSPACE_SKILL_LISTING_ENTRIES = 256
# The primary parses worker-writable frontmatter with pure-Python YAML and JSON5, so both its size per skill and its
# total per workspace stay bounded; skill bodies are never parsed.
_MAX_WORKSPACE_SKILL_FRONTMATTER_BYTES = 8 << 10
MAX_WORKSPACE_FRONTMATTER_BYTES = 128 << 10
# JSON5 metadata parses about three times slower per byte than YAML, so it counts three times toward that budget.
_JSON5_PARSE_WEIGHT = 3
# Refused files count too, so planted ones cannot make a pass read without bound.
_MAX_WORKSPACE_SKILL_READ_BYTES = 16 << 20
# Loads repeat for every agent build and skill edit, so parses of unchanged frontmatter and metadata are reused.
_PARSE_CACHE_ENTRIES = 1024
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
    try:
        return read_regular_file_within_root(directory_fd, relative_path, max_bytes=MAX_SKILL_FILE_BYTES).decode(
            "utf-8",
        )
    except FileNotFoundError:
        return None


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


def _readable_size(directory_fd: int, path: Path) -> bool:
    try:
        size = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False).st_size
    except FileNotFoundError:
        return False
    if size > MAX_SKILL_FILE_BYTES:
        logger.warning("Refused a workspace skill file", path=str(path), size=size)
        return False
    return True


def list_support_files(skill_fd: int, skill_path: Path, directory: str) -> list[str]:
    """Return the readable regular files directly inside one support directory; a linked directory has none."""
    try:
        with open_directory_within_root(skill_fd, directory) as support_fd:
            filenames = list_entries(support_fd, directories=False)
            if len(filenames) > MAX_WORKSPACE_SKILL_LISTING_ENTRIES:
                logger.warning(
                    "Listing only the first workspace skill files",
                    path=str(skill_path / directory),
                    limit=MAX_WORKSPACE_SKILL_LISTING_ENTRIES,
                    found=len(filenames),
                )
            # A file too large to read is not offered.
            return [
                filename
                for filename in filenames[:MAX_WORKSPACE_SKILL_LISTING_ENTRIES]
                if _readable_size(support_fd, skill_path / directory / filename)
            ]
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning(
            "Ignoring unsafe workspace skill support directory",
            path=str(skill_path / directory),
            error=str(exc),
        )
        return []


def _simple_frontmatter(text: str) -> dict[str, Any]:
    """Parse ``key: value`` lines like Agno's ``LocalSkills`` fallback for frontmatter that is not strict YAML."""
    fields: dict[str, Any] = {}
    for line in text.strip().split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def match_frontmatter(content: str) -> re.Match[str] | None:
    """Match a SKILL.md's frontmatter and body.

    The pattern needs a closing ``---`` at the start of a line; without one it would backtrack quadratically through
    whitespace that worker code planted after the opening ``---``, so such content never reaches it and has no
    frontmatter either way.
    """
    return _FRONTMATTER_PATTERN.match(content) if "\n---" in content else None


def _strict_frontmatter(text: str, *, trusted: bool) -> Any:  # noqa: ANN401
    """Parse frontmatter with the loader its writer's trust calls for.

    Workspace frontmatter, which worker code can write, gets PyYAML's pure-Python loader like Agno's LocalSkills, with
    the refusals of ``yaml_io.safe_load_untrusted``; operator-owned skill roots keep the fast safe loader.
    """
    pickled, error = _cached_frontmatter(text, trusted=trusted)
    if error is not None:
        raise YAMLError(error)
    return _unpickled(pickled)


@lru_cache(maxsize=_PARSE_CACHE_ENTRIES)
def _cached_frontmatter(text: str, *, trusted: bool) -> tuple[bytes, str | None]:
    try:
        return _pickled((yaml_io.safe_load(text) if trusted else yaml_io.safe_load_untrusted(text)) or {}), None
    except Exception as exc:
        # PyYAML refuses values such as 2026-02-30, `!!int ""`, `!!bool maybe`, or deep nesting with ValueError,
        # IndexError, KeyError, AttributeError, or RecursionError; like LocalSkills, any of them makes the YAML invalid.
        return b"", str(exc)


@lru_cache(maxsize=_PARSE_CACHE_ENTRIES)
def _cached_json5(text: str) -> tuple[bytes, str | None]:
    try:
        return _pickled(json5.loads(text)), None
    except Exception as exc:
        return b"", str(exc)


def _pickled(value: object) -> bytes:
    """Keep a cached parse as bytes, which stay near the size of its text where live parsed objects take far more."""
    return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)


def _unpickled(data: bytes) -> Any:  # noqa: ANN401
    """Return a fresh copy of a cached parse, which only ``_pickled`` wrote from parser output of built-in types."""
    return pickle.loads(data)  # noqa: S301 - this module pickled it


def normalized_newlines(text: str) -> str:
    """Return text with the line endings Agno's LocalSkills reads, which ``Path.read_text`` normalizes."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class SkillFrontmatterTooLargeError(ValueError):
    """Workspace frontmatter over the size the primary parses."""


def parse_skill_markdown(content: str, *, loose: bool = False, trusted: bool = False) -> tuple[dict[str, Any], str]:
    """Split SKILL.md into its frontmatter mapping and instruction body.

    Ownership and edit checks need strict YAML; ``loose`` loads a skill for the agent the way Agno does, and
    ``trusted`` marks content from an operator-owned skill root rather than a workspace.
    """
    content = normalized_newlines(content)
    match = match_frontmatter(content)
    if match is None:
        return {}, content
    if not trusted and len(match.group(1).encode()) > _MAX_WORKSPACE_SKILL_FRONTMATTER_BYTES:
        msg = f"SKILL.md frontmatter exceeds {_MAX_WORKSPACE_SKILL_FRONTMATTER_BYTES >> 10} KiB"
        raise SkillFrontmatterTooLargeError(msg)
    try:
        frontmatter = _strict_frontmatter(match.group(1), trusted=trusted)
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
        pickled, error = _cached_json5(raw)
        if error is not None:
            logger.warning("Failed to parse skill metadata JSON5", path=path, error=error)
            return None
        parsed = _unpickled(pickled)
        if isinstance(parsed, dict):
            return cast("dict[str, Any]", parsed)
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
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Workspace skill root is unavailable", path=str(skills_root), error=str(exc))


# AGNO_COMPAT: LocalSkills reads skill files by pathname and follows links.
# Reason: Agno's local loader opens SKILL.md, scripts/ and references/ with pathname reads, so a link planted in a
# worker-shared workspace would make the primary read files outside it. The loader has no reader or descriptor
# extension point, so workspace roots build the same Skill fields from descriptor-bound reads.
# Upstream issue: tracking gap; no Agno issue or PR proposes caller-owned file access for LocalSkills.
# Upstream PR: none identified; https://github.com/agno-agi/agno/pull/9194 adds a database loader, not confined files.
# Remove when: LocalSkills accepts a caller-supplied no-follow reader for skill files and support-file discovery; the
# workspace count, budget, name, description, listing, and file-size limits remain MindRoom policy.
# Coverage: tests/test_skills.py::test_a_linked_support_directory_lists_nothing,
# tests/test_skills.py::test_workspace_skill_references_are_read_without_following_links,
# tests/test_skills.py::test_workspace_skill_with_loose_frontmatter_loads_like_agno,
# tests/test_skills.py::test_workspace_skills_above_the_count_cap_are_skipped_with_a_warning,
# tests/test_skills.py::test_workspace_skills_stay_within_a_file_cap_and_a_total_budget, and
# tests/test_skills.py::test_workspace_skill_names_and_listings_cannot_bloat_the_prompt.
def load_workspace_skills(skills_root: Path) -> list[Skill]:
    """Build Agno skills from one workspace skill root, skipping unsafe or unreadable entries.

    Every loaded skill reaches the system prompt, so the skills share a count cap and a total budget.
    """
    skills: list[Skill] = []
    loaded_bytes = 0
    for _directory, skill, prompt_bytes in _measured_skills(skills_root, charges={}):
        loaded_bytes += prompt_bytes
        if loaded_bytes > MAX_WORKSPACE_SKILLS_BYTES:
            logger.warning("Workspace skills exceed their budget; skipping the rest", path=str(skills_root))
            break
        skills.append(skill)
    return skills


@dataclass(frozen=True)
class _BudgetSpent:
    """A pass stopped because one of its budgets ran out."""

    warning: str


_FRONTMATTER_BUDGET_SPENT = _BudgetSpent("Workspace skill frontmatter exceeds its parse budget; skipping the rest")
_READ_BUDGET_SPENT = _BudgetSpent("Workspace skill files exceed their read budget; skipping the rest")


def _measured_skills(skills_root: Path, *, charges: dict[str, int]) -> Iterator[tuple[str, Skill, int]]:
    """Yield the skills loading reads with their prompt bytes, measured inside the guard that skips one skill.

    Frontmatter is parsed only while the workspace's frontmatter budget lasts, and each parse is charged before it runs,
    so planted skill files, refused or not, cannot make the primary parse more than that per load. ``charges`` receives
    what each parsed directory cost.
    """
    parsed_bytes = 0
    read_bytes = 0

    def measured(skill_fd: int, directory: str) -> tuple[str, Skill, int] | _BudgetSpent | None:
        nonlocal parsed_bytes, read_bytes
        content = _read_skill_markdown(skill_fd, skills_root, directory)
        if content is None:
            return None
        read_bytes += len(content)
        if read_bytes > _MAX_WORKSPACE_SKILL_READ_BYTES:
            return _READ_BUDGET_SPENT
        size = _checked_frontmatter_bytes(content, skills_root / directory / SKILL_FILENAME)
        if size is None:
            return None
        if parsed_bytes + size > MAX_WORKSPACE_FRONTMATTER_BYTES:
            return _FRONTMATTER_BUDGET_SPENT
        # Spent even when the parse raises or skill loading refuses the skill it parsed.
        parsed_bytes += size
        charges[directory] = size
        skill = _read_skill(skill_fd, content, skills_root, directory)
        # Only a loaded skill's JSON5 metadata is parsed later, so only it adds that weight.
        surcharge = skill_parse_cost(size, skill) - size if skill is not None else 0
        if parsed_bytes + surcharge > MAX_WORKSPACE_FRONTMATTER_BYTES:
            return _FRONTMATTER_BUDGET_SPENT
        parsed_bytes += surcharge
        charges[directory] += surcharge
        return None if skill is None else (directory, skill, skill_prompt_bytes(skill))

    for result in _each_skill_directory(skills_root, measured, limit=MAX_WORKSPACE_SKILLS):
        if isinstance(result, _BudgetSpent):
            logger.warning(result.warning, path=str(skills_root))
            return
        yield result


def _read_skill(skill_fd: int, content: str, skills_root: Path, directory: str) -> Skill | None:
    return workspace_skill(
        content,
        skills_root,
        directory,
        scripts=list_support_files(skill_fd, skills_root / directory, "scripts"),
        references=list_support_files(skill_fd, skills_root / directory, "references"),
    )


def skill_parse_cost(frontmatter_size: int, skill: Skill) -> int:
    """Return what one skill's frontmatter costs to parse, in YAML bytes, counting JSON5 metadata at its weight."""
    metadata = skill.metadata
    json5_bytes = len(metadata.encode()) if isinstance(metadata, str) else 0
    return frontmatter_size + (_JSON5_PARSE_WEIGHT - 1) * json5_bytes


def frontmatter_bytes(content: str) -> int:
    """Return the size of a SKILL.md's frontmatter, the part the primary parses; a body is never parsed."""
    match = match_frontmatter(normalized_newlines(content))
    return len(match.group(1).encode()) if match is not None else 0


def _checked_frontmatter_bytes(content: str, path: Path) -> int | None:
    size = frontmatter_bytes(content)
    if size > _MAX_WORKSPACE_SKILL_FRONTMATTER_BYTES:
        logger.warning("Refused a workspace skill whose frontmatter is too large", path=str(path), size=size)
        return None
    return size


def skill_prompt_bytes(skill: Skill) -> int:
    """Return one skill's share of the workspace prompt budget: loaded skill content, not only its prompt listing."""
    prompt_parts = (skill.name, skill.description, skill.instructions, skill.metadata or "")
    return len("".join(map(str, (*prompt_parts, *skill.scripts, *skill.references))).encode())


@dataclass(frozen=True)
class _WorkspaceSkillBudget:
    """What one loading pass spends: each loaded skill's prompt bytes, and the parse charge of every directory it parsed."""

    prompt_bytes: dict[str, int]
    parse_charges: dict[str, int]


def workspace_skill_budget(skills_root: Path) -> _WorkspaceSkillBudget:
    """Return what a loading pass spends on this workspace, as skill loading measures it."""
    charges: dict[str, int] = {}
    prompt_bytes = {directory: size for directory, _skill, size in _measured_skills(skills_root, charges=charges)}
    return _WorkspaceSkillBudget(prompt_bytes, charges)


def support_entry_count(skill_fd: int, directory: str) -> int:
    """Return how many visible regular files one support directory holds, the entries its listing cap counts."""
    try:
        with open_directory_within_root(skill_fd, directory) as support_fd:
            return len(list_entries(support_fd, directories=False))
    except FileNotFoundError:
        return 0


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


def frontmatter_name(frontmatter: dict[str, Any], directory: str) -> str | None:
    """Return the stripped name a skill loads under, its directory's when it names none, or None when it is unusable."""
    name = frontmatter.get("name", directory)
    return name.strip() if isinstance(name, str) and name.strip() else None


def workspace_skill_name(content: str, directory: str) -> str | None:
    """Return the name a workspace SKILL.md loads under, read loosely like skill loading, or None when it has none."""
    try:
        frontmatter, _instructions = parse_skill_markdown(content, loose=True)
    except (TypeError, SkillFrontmatterTooLargeError):
        return None
    return frontmatter_name(frontmatter, directory)


def _read_skill_markdown(skill_fd: int, skills_root: Path, directory: str) -> str | None:
    path = skills_root / directory / SKILL_FILENAME
    try:
        return read_text_at(skill_fd, SKILL_FILENAME)
    except (OSError, ValueError) as exc:
        logger.warning("Refused a workspace skill file", path=str(path), error=str(exc))
        return None


def workspace_skill(
    content: str,
    skills_root: Path,
    directory: str,
    *,
    scripts: list[str],
    references: list[str],
) -> Skill | None:
    """Build the skill that loading reads from one SKILL.md and its listings, or None when loading refuses it."""
    path = skills_root / directory / SKILL_FILENAME
    frontmatter, instructions = parse_skill_markdown(content, loose=True)
    # Skill normalization drops a skill without a usable name.
    name = frontmatter_name(frontmatter, directory) or ""
    if len(name) > MAX_WORKSPACE_SKILL_NAME_CHARS:
        logger.warning("Refused a workspace skill whose name is too long", path=str(path))
        return None
    description = frontmatter.get("description", "")
    if isinstance(description, str) and len(description) > MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS:
        logger.warning("Truncated a workspace skill description", path=str(path))
        description = description[:MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS]
    return Skill(
        name=name,
        description=description,
        instructions=instructions,
        source_path=str(skills_root / directory),
        scripts=scripts,
        references=references,
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
    """Replace one skill's usage record atomically, leaving every other record as written."""
    update_skill_usages(root_fd, {directory: update})


def update_skill_usages(root_fd: int, updates: Mapping[str, Callable[[SkillUsage], SkillUsage]]) -> None:
    """Replace several skills' usage records in one atomic write, leaving every other record as written.

    Telemetry never fails its caller: records are cleaned when read, and a write that fails is logged.
    The lock is process-local on purpose: any lock inside the worker-shared workspace could be held by worker
    code to stall the primary, so concurrent primaries sharing one storage root may occasionally drop a count.
    """
    if not updates:
        return
    with _USAGE_LOCK:
        records = _usage_records(root_fd)
        if records is None:
            # Rewriting an unreadable file would drop every record in it; a person can still repair it.
            return
        for directory, update in updates.items():
            current = _parse_usage(records.get(directory)) or SkillUsage()
            records[directory] = update(current).model_dump(mode="json", exclude_defaults=True)
        try:
            _write_usage_records(root_fd, records)
        except OSError as exc:
            # The changes this records have already landed, so a failed write, such as on a full disk, is only logged.
            logger.warning("Could not update skill usage telemetry", directories=sorted(updates), error=str(exc))


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
