"""Workspace skill files read through no-follow descriptors, plus their usage telemetry.

Worker code shares agent workspaces, so the primary process never opens a workspace skill by pathname.
Hidden entries under ``skills/`` (usage, history, archive) are never discovered as skills.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import stat
import threading
from collections import OrderedDict
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
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
MAX_WORKSPACE_SKILL_READ_BYTES = 16 << 20
# Loads repeat for every agent build and skill edit, so parses of unchanged frontmatter and metadata are reused, and
# the cache keeps a bounded number of bytes whatever worker code writes.
_PARSE_CACHE_BYTES = 8 << 20
_PARSE_CACHE_ENTRY_OVERHEAD = 256
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


def _file_size(directory_fd: int, filename: str) -> int | None:
    try:
        return os.stat(filename, dir_fd=directory_fd, follow_symlinks=False).st_size
    except FileNotFoundError:
        return None


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
            sizes = {name: _file_size(support_fd, name) for name in filenames[:MAX_WORKSPACE_SKILL_LISTING_ENTRIES]}
            # A file too large to read is not offered; worker code can plant many, so the directory warns once.
            oversized = [name for name, size in sizes.items() if size is not None and size > MAX_SKILL_FILE_BYTES]
            if oversized:
                logger.warning(
                    "Not listing workspace skill files over the size limit",
                    path=str(skill_path / directory),
                    limit=MAX_SKILL_FILE_BYTES,
                    refused=len(oversized),
                    first=oversized[0],
                )
            return [name for name, size in sizes.items() if size is not None and size <= MAX_SKILL_FILE_BYTES]
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
    kind = "trusted-yaml" if trusted else "untrusted-yaml"
    parsed = _PARSE_CACHE.parse(kind, text, yaml_io.safe_load if trusted else yaml_io.safe_load_untrusted)
    if isinstance(parsed, _ParseError):
        raise YAMLError(parsed.message)
    return parsed or {}


@dataclass(frozen=True)
class _ParseError:
    message: str


class _ParseCache:
    """Pickled parses of skill text, keyed by digest and bounded by the bytes they keep, least recently used first."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._entries: OrderedDict[tuple[str, bytes], bytes | _ParseError] = OrderedDict()
        self._retained = 0
        self._lock = threading.Lock()

    def parse(self, kind: str, text: str, parse: Callable[[str], object]) -> Any:  # noqa: ANN401
        """Return a fresh copy of the parse of ``text``, or the error that refused it, parsing only on a miss."""
        # YAML escapes can put lone surrogates in a metadata string, which the digest must still read.
        key = (kind, hashlib.blake2b(text.encode(errors="surrogatepass"), digest_size=16).digest())
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
        if entry is None:
            try:
                entry = pickle.dumps(parse(text), protocol=pickle.HIGHEST_PROTOCOL)
            except Exception as exc:
                # PyYAML refuses values such as 2026-02-30, `!!int ""`, `!!bool maybe`, or deep nesting with ValueError,
                # IndexError, KeyError, AttributeError, or RecursionError; like LocalSkills, any error makes it invalid.
                entry = _ParseError(str(exc))
            self._store(key, entry)
        # Only this cache pickled the entry, from parser output of built-in types; unpickling returns a fresh copy.
        return entry if isinstance(entry, _ParseError) else pickle.loads(entry)  # noqa: S301

    def _store(self, key: tuple[str, bytes], entry: bytes | _ParseError) -> None:
        size = _cached_entry_bytes(entry)
        with self._lock:
            if key in self._entries or size > self._max_bytes:
                return
            self._entries[key] = entry
            self._retained += size
            while self._retained > self._max_bytes:
                _key, evicted = self._entries.popitem(last=False)
                self._retained -= _cached_entry_bytes(evicted)


def _cached_entry_bytes(entry: bytes | _ParseError) -> int:
    # An error message can quote a lone surrogate from the text it refused.
    size = len(entry) if isinstance(entry, bytes) else len(entry.message.encode(errors="surrogatepass"))
    return size + _PARSE_CACHE_ENTRY_OVERHEAD


_PARSE_CACHE = _ParseCache(_PARSE_CACHE_BYTES)


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
        parsed = _PARSE_CACHE.parse("json5", raw, json5.loads)
        if isinstance(parsed, _ParseError):
            logger.warning("Failed to parse skill metadata JSON5", path=path, error=parsed.message)
            return None
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
    stand_in: tuple[str, Callable[[], Result | None]] | None = None,
) -> Iterator[Result]:
    """Read visible workspace skill directories as the caller consumes them, skipping unreadable entries.

    Worker code can plant entries in a shared workspace, so one never hides the others or fails the caller, and an
    unavailable root yields nothing. ``stand_in`` reads one directory, present or not, in place of what is on disk.
    """
    try:
        with open_skills_root(skills_root) as root_fd:
            with suppress(FileNotFoundError):
                os.stat(SKILL_FILENAME, dir_fd=root_fd, follow_symlinks=False)
                # LocalSkills loaded such a file as the only skill of the root, hiding every skill directory beside it.
                logger.warning("Ignoring SKILL.md directly in the workspace skills directory", path=str(skills_root))
            directories = list_entries(root_fd, directories=True)
            if stand_in is not None and stand_in[0] not in directories:
                directories = sorted([*directories, stand_in[0]])
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
                    result = _read_skill_directory(root_fd, directory, read, stand_in)
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


def _read_skill_directory[Result](
    root_fd: int,
    directory: str,
    read: Callable[[int, str], Result | None],
    stand_in: tuple[str, Callable[[], Result | None]] | None,
) -> Result | None:
    if stand_in is not None and stand_in[0] == directory:
        return stand_in[1]()
    with open_directory_within_root(root_fd, directory) as skill_fd:
        return read(skill_fd, directory)


# AGNO_COMPAT: LocalSkills reads skill files by pathname and follows links.
# Reason: Agno's local loader opens SKILL.md, scripts/ and references/ with pathname reads, so a link planted in a
# worker-shared workspace would make the primary read files outside it. The loader has no reader or descriptor
# extension point, so workspace roots build the same Skill fields from descriptor-bound reads.
# Upstream issue: tracking gap; no Agno issue or PR proposes caller-owned file access for LocalSkills.
# Upstream PR: none identified; https://github.com/agno-agi/agno/pull/9194 adds a database loader, not confined files.
# Remove when: LocalSkills accepts a caller-supplied no-follow reader for skill files and support-file discovery, and a
# caller-supplied frontmatter parser, since its own regex and yaml.safe_load are unbounded for worker-writable text; the
# workspace count, budget, name, description, listing, and file-size limits, the per-skill frontmatter cap, the per-pass
# read and parse budgets, the untrusted YAML loader, the fence check that keeps matching linear, and the parse cache
# remain MindRoom policy.
# Coverage: tests/test_skills.py::test_a_linked_support_directory_lists_nothing,
# tests/test_skills.py::test_workspace_skill_references_are_read_without_following_links,
# tests/test_skills.py::test_workspace_skill_with_loose_frontmatter_loads_like_agno,
# tests/test_skills.py::test_workspace_skills_above_the_count_cap_are_skipped_with_a_warning,
# tests/test_skills.py::test_workspace_skills_stay_within_a_file_cap_and_a_total_budget,
# tests/test_skills.py::test_workspace_skill_names_and_listings_cannot_bloat_the_prompt,
# tests/test_skills.py::test_workspace_frontmatter_stays_within_its_parse_caps,
# tests/test_skills.py::test_refused_skills_spend_the_frontmatter_budget,
# tests/test_skills.py::test_planted_skill_files_stay_within_the_read_budget,
# tests/test_skills.py::test_frontmatter_aliases_neither_stall_loading_nor_hide_other_skills,
# tests/test_skills.py::test_planted_frontmatter_never_stalls_loading_or_hides_other_skills,
# tests/test_skills.py::test_oversized_support_files_are_unlisted_with_one_warning_per_directory, and
# tests/test_yaml_io.py::test_untrusted_loads_refuse_what_grows_beyond_the_input.
def load_workspace_skills(skills_root: Path) -> list[Skill]:
    """Build Agno skills from one workspace skill root, skipping unsafe or unreadable entries.

    Every loaded skill reaches the system prompt, so the skills share a count cap and a total budget.
    """
    return list(workspace_skill_load(skills_root).skills.values())


BudgetStop = Literal["prompt", "read", "parse"]


@dataclass(frozen=True)
class _BudgetSpent:
    """A pass stopped because one of its budgets ran out."""

    stop: BudgetStop
    warning: str


_PROMPT_BUDGET_SPENT = _BudgetSpent("prompt", "Workspace skills exceed their budget; skipping the rest")
_FRONTMATTER_BUDGET_SPENT = _BudgetSpent(
    "parse",
    "Workspace skill frontmatter exceeds its parse budget; skipping the rest",
)
_READ_BUDGET_SPENT = _BudgetSpent("read", "Workspace skill files exceed their read budget; skipping the rest")


@dataclass(frozen=True)
class _WorkspaceSkillLoad:
    """The skills one loading pass loads, by directory, and the budget that stopped it before the rest, if any."""

    skills: dict[str, Skill]
    stop: BudgetStop | None


@dataclass(frozen=True)
class ProposedSkill:
    """One skill directory as a change would leave it, which a loading pass can read before the change is written."""

    directory: str
    markdown: str
    scripts: list[str]
    references: list[str]


def workspace_skill_load(skills_root: Path, proposed: ProposedSkill | None = None) -> _WorkspaceSkillLoad:
    """Load one workspace's skills within its count, prompt, read, and parse budgets, stopping at the first one spent.

    Frontmatter is parsed only while the pass's parse budget lasts, and each parse is charged before it runs, so planted
    skill files, refused or not, cannot make the primary parse more than that per load. With ``proposed``, the pass
    reads that directory as a change would leave it, so a check sees what loading would do after the change; such a
    pass logs no budget warning, since nothing it describes has happened.
    """
    budget = SkillPassBudget()

    def measured(skill_fd: int, directory: str) -> tuple[str, Skill] | _BudgetSpent | None:
        skill_path = skills_root / directory
        content = _read_skill_markdown(skill_fd, skill_path / SKILL_FILENAME, budget)
        return _charged_skill(budget, skills_root, directory, content, lambda: _support_listings(skill_fd, skill_path))

    stand_in = None
    if proposed is not None:
        stand_in = (
            proposed.directory,
            lambda: _charged_skill(
                budget,
                skills_root,
                proposed.directory,
                _charged_text(budget, proposed.markdown.encode()),
                lambda: (proposed.scripts, proposed.references),
            ),
        )
    skills: dict[str, Skill] = {}
    prompt_bytes = 0
    for result in _each_skill_directory(skills_root, measured, limit=MAX_WORKSPACE_SKILLS, stand_in=stand_in):
        if isinstance(result, _BudgetSpent):
            spent = result
        else:
            directory, skill = result
            prompt_bytes += _skill_prompt_bytes(skill)
            if prompt_bytes <= MAX_WORKSPACE_SKILLS_BYTES:
                skills[directory] = skill
                continue
            spent = _PROMPT_BUDGET_SPENT
        if proposed is None:
            logger.warning(spent.warning, path=str(skills_root))
        return _WorkspaceSkillLoad(skills, spent.stop)
    return _WorkspaceSkillLoad(skills, None)


def _charged_skill(
    budget: SkillPassBudget,
    skills_root: Path,
    directory: str,
    content: str | None,
    listings: Callable[[], tuple[list[str], list[str]]],
) -> tuple[str, Skill] | _BudgetSpent | None:
    """Build the skill loading reads from one SKILL.md it read, parsing only within the pass's budget."""
    if budget.read_spent:
        return _READ_BUDGET_SPENT
    size = None if content is None else _checked_frontmatter_bytes(content, skills_root / directory / SKILL_FILENAME)
    if content is None or size is None:
        return None
    # Spent even when the parse raises or skill loading refuses the skill it parsed.
    if not budget.spend_parse(size):
        return _FRONTMATTER_BUDGET_SPENT
    scripts, references = listings()
    skill = _workspace_skill(content, skills_root, directory, scripts=scripts, references=references)
    if skill is None:
        return None
    # Only a loaded skill's JSON5 metadata is parsed later, so only it adds that weight.
    if not budget.spend_parse(metadata_surcharge(skill.metadata)):
        return _FRONTMATTER_BUDGET_SPENT
    return directory, skill


def _support_listings(skill_fd: int, skill_path: Path) -> tuple[list[str], list[str]]:
    return list_support_files(skill_fd, skill_path, "scripts"), list_support_files(skill_fd, skill_path, "references")


@dataclass
class SkillPassBudget:
    """What one pass over a workspace may still read of its SKILL.md files and parse of their frontmatter.

    Worker code can write both, so a read is charged before it is decoded or matched, and a parse before it runs.
    """

    read_remaining: int = MAX_WORKSPACE_SKILL_READ_BYTES
    parse_remaining: int = MAX_WORKSPACE_FRONTMATTER_BYTES

    @property
    def read_spent(self) -> bool:
        """Return whether the pass has read past its limit, after which it reads nothing more."""
        return self.read_remaining < 0

    def read_markdown(self, skill_fd: int) -> str | None:
        """Return a skill's SKILL.md, or None when it is absent or the pass has read past its limit.

        The file that crosses the limit is read and charged but never decoded, and a file that is not UTF-8 is charged
        before its decoding fails.
        """
        if self.read_spent:
            return None
        try:
            data = read_regular_file_within_root(skill_fd, SKILL_FILENAME, max_bytes=MAX_SKILL_FILE_BYTES)
        except FileNotFoundError:
            return None
        return _charged_text(self, data)

    def spend_parse(self, cost: int) -> bool:
        """Charge a parse of ``cost`` when it fits, and return whether it did."""
        if cost > self.parse_remaining:
            return False
        self.parse_remaining -= cost
        return True


def _charged_text(budget: SkillPassBudget, data: bytes) -> str | None:
    """Charge one SKILL.md's bytes to the pass's read limit, and return its text while the pass is within it."""
    budget.read_remaining -= len(data)
    return None if budget.read_spent else data.decode("utf-8")


def metadata_surcharge(metadata: object) -> int:
    """Return what parsing JSON5 metadata costs beyond its share of the frontmatter, in YAML bytes at its weight."""
    return (_JSON5_PARSE_WEIGHT - 1) * len(metadata.encode(errors="surrogatepass")) if isinstance(metadata, str) else 0


def frontmatter_charge(content: str) -> int:
    """Return what parsing a SKILL.md charges its pass: its frontmatter, or nothing over the cap that refuses it unparsed."""
    size = _frontmatter_bytes(content)
    return 0 if size > _MAX_WORKSPACE_SKILL_FRONTMATTER_BYTES else size


def _frontmatter_bytes(content: str) -> int:
    """Return the size of a SKILL.md's frontmatter, the part the primary parses; a body is never parsed."""
    match = match_frontmatter(normalized_newlines(content))
    return len(match.group(1).encode()) if match is not None else 0


def _checked_frontmatter_bytes(content: str, path: Path) -> int | None:
    size = _frontmatter_bytes(content)
    if size > _MAX_WORKSPACE_SKILL_FRONTMATTER_BYTES:
        logger.warning("Refused a workspace skill whose frontmatter is too large", path=str(path), size=size)
        return None
    return size


def _skill_prompt_bytes(skill: Skill) -> int:
    """Return one skill's share of the workspace prompt budget: loaded skill content, not only its prompt listing."""
    prompt_parts = (skill.name, skill.description, skill.instructions, skill.metadata or "")
    return len("".join(map(str, (*prompt_parts, *skill.scripts, *skill.references))).encode())


def support_entry_count(skill_fd: int, directory: str) -> int:
    """Return how many visible regular files one support directory holds, the entries its listing cap counts."""
    try:
        with open_directory_within_root(skill_fd, directory) as support_fd:
            return len(list_entries(support_fd, directories=False))
    except FileNotFoundError:
        return 0


def workspace_skill_directories(skills_root: Path) -> list[str]:
    """Return the visible workspace directories skill loading reads that hold a SKILL.md, without reading it."""
    return list(
        _each_skill_directory(
            skills_root,
            lambda skill_fd, directory: directory if _holds_skill_file(skill_fd) else None,
            limit=MAX_WORKSPACE_SKILLS,
        ),
    )


def _holds_skill_file(skill_fd: int) -> bool:
    try:
        return stat.S_ISREG(os.stat(SKILL_FILENAME, dir_fd=skill_fd, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


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


def _read_skill_markdown(skill_fd: int, path: Path, budget: SkillPassBudget) -> str | None:
    try:
        return budget.read_markdown(skill_fd)
    except (OSError, ValueError) as exc:
        logger.warning("Refused a workspace skill file", path=str(path), error=str(exc))
        return None


def _workspace_skill(
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
            update_skill_usages(
                root_fd,
                {
                    skill_path.name: lambda usage: usage.model_copy(
                        update={"use_count": usage.use_count + 1, "last_used_at": now},
                    ),
                },
            )
    except OSError as exc:
        logger.warning("Could not record workspace skill use", path=str(skill_path), error=str(exc))
