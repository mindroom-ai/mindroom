"""Skill integration built on Agno skills with OpenClaw-compatible metadata."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import json5
from agno.skills import LocalSkills, Skills
from agno.skills.errors import SkillValidationError
from agno.skills.loaders import SkillLoader
from agno.skills.skill import Skill

from mindroom import yaml_io
from mindroom.background_tasks import create_background_task
from mindroom.constants import runtime_env_values
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root
from mindroom.tool_system.output_files import ToolOutputFilePolicy, wrap_function_for_output_files
from mindroom.tool_system.skill_usage import record_skill_use
from mindroom.tool_system.worker_routing import agent_workspace_root_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools.function import Function

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

SKILL_FILENAME = "SKILL.md"
_WORKSPACE_SKILLS_DIRNAME = "skills"
_MAX_WORKSPACE_SKILLS = 256
MAX_WORKSPACE_SKILL_FILE_BYTES = 1 << 20
_MAX_WORKSPACE_SKILLS_BYTES = 8 << 20
# Names, descriptions, and file listings reach every system prompt, not only the skills a model opens.
MAX_WORKSPACE_SKILL_NAME_CHARS = 64
MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS = 1024
_MAX_WORKSPACE_SKILL_LISTING_ENTRIES = 256
# Worker code can plant any number of entries, so a workspace skill listing examines only this many.
_MAX_WORKSPACE_SKILL_SCANNED_ENTRIES = 1024
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

_OS_ALIASES = {
    "darwin": {"darwin", "macos", "mac", "osx"},
    "linux": {"linux"},
    "windows": {"windows", "win", "win32"},
}

_PLUGIN_SKILL_ROOTS: list[Path] = []
_SkillSnapshot = tuple[tuple[str, int, int], ...]
_SKILL_CACHE: dict[Path, tuple[_SkillSnapshot, list[Skill]]] = {}
_THIS_DIR = Path(__file__).resolve().parent
_BUNDLED_SKILLS_DEV_DIR = _THIS_DIR.parents[2] / "skills"
_BUNDLED_SKILLS_PACKAGE_DIR = _THIS_DIR.parent / "_bundled_skills"


@dataclass
class _MindroomSkillsLoader(SkillLoader):
    """Load skills via Agno with OpenClaw compatibility filtering."""

    roots: Sequence[Path]
    config: Config
    runtime_paths: RuntimePaths
    allowlist: Sequence[str] | None = None
    env_vars: Mapping[str, str] | None = None
    credential_keys: set[str] | None = None

    def load(self) -> list[Skill]:
        """Return the eligible skills for the configured roots and allowlist."""
        env_vars = runtime_env_values(self.runtime_paths) if self.env_vars is None else self.env_vars
        credential_keys = (
            self.credential_keys
            if self.credential_keys is not None
            else _collect_credential_keys(
                self.config,
                self.runtime_paths,
            )
        )
        config_data = self.config.model_dump()
        allowlist_set = set(self.allowlist or [])

        skills_by_name: dict[str, Skill] = {}
        for skill in self._candidate_skills():
            normalized = _normalize_skill(skill)
            if normalized is None:
                continue
            if self.allowlist and normalized.name not in allowlist_set:
                continue
            if not _is_skill_eligible(
                normalized,
                config_data,
                env_vars=env_vars,
                credential_keys=credential_keys,
            ):
                continue
            skills_by_name[normalized.name] = normalized

        if self.allowlist:
            return [skills_by_name[name] for name in self.allowlist if name in skills_by_name]
        return list(skills_by_name.values())

    def _candidate_skills(self) -> list[Skill]:
        return [skill for root in _unique_paths(self.roots) for skill in _load_root_skills(root)]


@dataclass(kw_only=True)
class _WorkspaceSkillsLoader(_MindroomSkillsLoader):
    """Load one workspace's ``skills`` directory through no-follow descriptors."""

    workspace_root: Path

    def _candidate_skills(self) -> list[Skill]:
        return _load_workspace_skills(self.workspace_root)


class _MindroomSkills(Skills):
    """MindRoom-specific Skills wrapper for workspace skill access policy."""

    def __init__(
        self,
        *,
        loaders: list[SkillLoader],
        output_file_policy: ToolOutputFilePolicy | None = None,
    ) -> None:
        self._workspace_roots_by_skill: dict[str, Path] = {}
        self._output_file_policy = output_file_policy
        super().__init__(loaders=loaders)

    def get_tools(self) -> list[Function]:
        """Return skill access tools with MindRoom's reserved output-path argument."""
        tools = super().get_tools()
        if self._output_file_policy is None:
            return tools
        return [wrap_function_for_output_files(tool, self._output_file_policy) for tool in tools]

    def _load_skills(self) -> None:
        """Load skills while tracking which final skills came from workspace loaders."""
        self._workspace_roots_by_skill.clear()
        for loader in self.loaders:
            try:
                skills = loader.load()
                workspace_root = loader.workspace_root if isinstance(loader, _WorkspaceSkillsLoader) else None
                for skill in skills:
                    if skill.name in self._skills:
                        logger.warning("Duplicate skill name; overwriting with newer version", skill=skill.name)
                    self._skills[skill.name] = skill
                    if workspace_root is not None:
                        self._workspace_roots_by_skill[skill.name] = workspace_root
                    else:
                        self._workspace_roots_by_skill.pop(skill.name, None)
            except SkillValidationError:
                raise
            except Exception as exc:
                logger.warning("Error loading skills", loader=repr(loader), error=str(exc))

        logger.debug("Loaded skills", count=len(self._skills))

    # AGNO_COMPAT: Skills offers no hook for when an agent loads a skill.
    # Reason: Agno 3.0.9 Skills.get_tools binds the private _get_skill_instructions directly, so counting workspace
    #   skill loads for the skill learner's inactivity clock requires overriding it.
    # Upstream issue: Tracking gap; no issue for a skill-load callback has been identified.
    # Upstream PR: None identified.
    # Remove when: Skills calls a hook when a skill's instructions or files are loaded.
    # Coverage: tests/test_skills.py::test_workspace_skill_loads_record_usage_but_configured_skills_do_not.
    def _get_skill_instructions(self, skill_name: str) -> str:
        self._record_use(skill_name)
        return super()._get_skill_instructions(skill_name)

    def _record_use(self, skill_name: str) -> None:
        """Record one agent load of a workspace skill directory; configured skills are not recorded.

        Usage records live beside skill directories, so a workspace whose ``skills/`` is itself one skill keeps none.
        Agno calls the skill tools on the event loop, so there the usage write runs in a thread.
        """
        workspace_root = self._workspace_roots_by_skill.get(skill_name)
        skill = self.get_skill(skill_name)
        if (
            workspace_root is None
            or skill is None
            or Path(skill.source_path).parent != workspace_root / _WORKSPACE_SKILLS_DIRNAME
        ):
            return
        record = partial(record_skill_use, Path(skill.source_path))
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            record()
            return
        create_background_task(asyncio.to_thread(record), name="record_skill_use")

    # AGNO_COMPAT: Skills reads skill references and scripts by path, following links.
    # Reason: Agno 3.0.9 Skills._get_skill_reference and _get_skill_script open files below a
    #   skill's source_path by path, so a workspace skill file swapped for a link or FIFO would
    #   be followed or block; workspace skills are read through no-follow descriptors instead.
    # Upstream issue: Tracking gap; no issue for descriptor-safe skill file reads has been identified.
    # Upstream PR: None identified.
    # Remove when: Agno lets a loader supply skill file contents or refuses links and non-regular files.
    # Coverage: tests/test_skills.py::test_workspace_skill_references_are_read_without_following_links.
    def _get_skill_reference(self, skill_name: str, reference_path: str | None = None) -> str:
        content = self._read_workspace_skill_file(skill_name, "references", reference_path)
        if content is None:
            return super()._get_skill_reference(skill_name, reference_path)
        return json.dumps({"skill_name": skill_name, "reference_path": reference_path, **content})

    def _get_skill_script(
        self,
        skill_name: str,
        script_path: str | None = None,
        execute: bool = False,
        args: list[str] | None = None,
        timeout: int = 30,
    ) -> str:
        if execute and skill_name in self._workspace_roots_by_skill:
            return json.dumps(
                {
                    "error": "Workspace skill scripts cannot be executed through get_skill_script",
                    "skill_name": skill_name,
                    "script_path": script_path,
                },
            )
        content = None if execute else self._read_workspace_skill_file(skill_name, "scripts", script_path)
        if content is not None:
            return json.dumps({"skill_name": skill_name, "script_path": script_path, **content})
        return super()._get_skill_script(
            skill_name=skill_name,
            script_path=script_path,
            execute=execute,
            args=args,
            timeout=timeout,
        )

    def _read_workspace_skill_file(
        self,
        skill_name: str,
        kind: str,
        filename: str | None,
    ) -> dict[str, str] | None:
        """Read one listed workspace skill file, or return ``None`` for names Agno validates itself."""
        workspace_root = self._workspace_roots_by_skill.get(skill_name)
        skill = self.get_skill(skill_name)
        if workspace_root is None or skill is None or not filename:
            return None
        if filename not in (skill.references if kind == "references" else skill.scripts):
            return None
        relative_path = Path(skill.source_path).relative_to(workspace_root) / kind / filename
        try:
            payload = read_regular_file_within_root(
                workspace_root,
                relative_path,
                max_bytes=MAX_WORKSPACE_SKILL_FILE_BYTES,
            )
            content = payload.decode("utf-8")
        except (OSError, ValueError) as exc:
            logger.warning("Refused a workspace skill file", path=str(workspace_root / relative_path), error=str(exc))
            return {"error": f"Error reading workspace skill file {filename}: {type(exc).__name__}"}
        self._record_use(skill_name)
        return {"content": content}


def build_agent_skills(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    skill_roots: Sequence[Path] | None = None,
    workspace_root: Path | None = None,
    env_vars: Mapping[str, str] | None = None,
    credential_keys: set[str] | None = None,
    output_file_policy: ToolOutputFilePolicy | None = None,
) -> Skills | None:
    """Build an Agno Skills object for a specific agent."""
    agent_config = config.get_agent(agent_name)
    resolved_credential_keys = (
        credential_keys if credential_keys is not None else _collect_credential_keys(config, runtime_paths)
    )

    configured_loader = None
    if agent_config.skills:
        configured_loader = _MindroomSkillsLoader(
            roots=_resolve_configured_skill_roots(skill_roots),
            config=config,
            runtime_paths=runtime_paths,
            allowlist=agent_config.skills,
            env_vars=env_vars,
            credential_keys=resolved_credential_keys,
        )

    workspace_loader = _WorkspaceSkillsLoader(
        roots=(),
        config=config,
        runtime_paths=runtime_paths,
        env_vars=env_vars,
        credential_keys=resolved_credential_keys,
        workspace_root=(
            workspace_root
            if workspace_root is not None
            else agent_workspace_root_path(runtime_paths.storage_root, agent_name)
        ),
    )

    loaders: list[SkillLoader]
    if skill_roots is None:
        loaders = [loader for loader in (configured_loader, workspace_loader) if loader is not None]
    else:
        loaders = [loader for loader in (workspace_loader, configured_loader) if loader is not None]

    skills = _MindroomSkills(loaders=loaders, output_file_policy=output_file_policy)
    if agent_config.skills or skills.get_skill_names():
        return skills
    return None


@dataclass(frozen=True)
class _SkillListing:
    """Summary information for a discoverable skill."""

    name: str
    description: str
    path: Path
    origin: str


@dataclass(frozen=True)
class _ResolvedSkillFrontmatter:
    """Normalized skill metadata parsed from SKILL.md."""

    name: str
    description: str
    frontmatter: Mapping[str, Any]


def set_plugin_skill_roots(roots: Sequence[Path]) -> None:
    """Replace the plugin-provided skill roots, invalidating cached skills only on change.

    Unchanged roots keep the snapshot-validated skill cache warm; per-root
    mtime/size snapshots in ``_load_root_skills`` still catch file edits.
    """
    global _PLUGIN_SKILL_ROOTS
    unique_roots = _unique_paths(roots)
    if unique_roots == _PLUGIN_SKILL_ROOTS:
        return
    _PLUGIN_SKILL_ROOTS = unique_roots
    clear_skill_cache()


def get_plugin_skill_roots() -> list[Path]:
    """Return the current plugin-provided skill roots."""
    return list(_PLUGIN_SKILL_ROOTS)


def _get_plugin_skill_roots() -> list[Path]:
    """Return the current plugin-provided skill roots."""
    return get_plugin_skill_roots()


def get_user_skills_dir() -> Path:
    """Return the user-managed skills directory."""
    return Path.home() / ".mindroom" / "skills"


def _get_bundled_skills_dir() -> Path:
    """Return the bundled skills directory from repo checkout or installed package."""
    if _BUNDLED_SKILLS_DEV_DIR.exists():
        return _BUNDLED_SKILLS_DEV_DIR
    if _BUNDLED_SKILLS_PACKAGE_DIR.exists():
        return _BUNDLED_SKILLS_PACKAGE_DIR
    return _BUNDLED_SKILLS_DEV_DIR


def _get_default_skill_roots() -> list[Path]:
    """Return the default skill search roots in precedence order."""
    return _unique_paths([_get_bundled_skills_dir(), *_PLUGIN_SKILL_ROOTS, get_user_skills_dir()])


def agent_workspace_skills_root(runtime_paths: RuntimePaths, agent_name: str, *, workspace_root: Path | None) -> Path:
    """Return the ``skills`` directory of a resolved workspace, or of the agent's canonical shared workspace."""
    root = (
        workspace_root
        if workspace_root is not None
        else agent_workspace_root_path(runtime_paths.storage_root, agent_name)
    )
    return root / _WORKSPACE_SKILLS_DIRNAME


def _resolve_configured_skill_roots(skill_roots: Sequence[Path] | None = None) -> list[Path]:
    """Return configured global skill roots without the agent workspace root."""
    return _unique_paths(list(skill_roots) if skill_roots is not None else _get_default_skill_roots())


def list_skill_listings(roots: Sequence[Path] | None = None) -> list[_SkillListing]:
    """Return skill listings with precedence rules applied."""
    roots = list(roots or _get_default_skill_roots())
    bundled_root = _get_bundled_skills_dir().expanduser().resolve()
    user_root = get_user_skills_dir().expanduser().resolve()
    plugin_roots = {root.expanduser().resolve() for root in _get_plugin_skill_roots()}

    skills_by_name: dict[str, _SkillListing] = {}
    for root in _unique_paths(roots):
        origin = _root_origin(root, bundled_root, user_root, plugin_roots)
        for skill_dir in _iter_skill_dirs(root):
            resolved_frontmatter = _resolve_skill_frontmatter(
                skill_dir,
                allow_missing_frontmatter=True,
            )
            if resolved_frontmatter is None:
                continue

            listing = _SkillListing(
                name=resolved_frontmatter.name,
                description=resolved_frontmatter.description,
                path=skill_dir / SKILL_FILENAME,
                origin=origin,
            )
            skills_by_name[listing.name] = listing

    return sorted(skills_by_name.values(), key=lambda item: item.name.lower())


def resolve_skill_listing(skill_name: str, roots: Sequence[Path] | None = None) -> _SkillListing | None:
    """Resolve a skill listing by name, honoring precedence rules."""
    normalized = skill_name.strip().lower()
    if not normalized:
        return None
    for listing in list_skill_listings(roots):
        if listing.name.lower() == normalized:
            return listing
    return None


def skill_can_edit(skill_path: Path) -> bool:
    """Return True if a skill file is editable by users."""
    user_root = get_user_skills_dir().expanduser().resolve()
    try:
        resolved = skill_path.expanduser().resolve()
    except OSError:
        return False
    if resolved != user_root and user_root not in resolved.parents:
        return False
    return os.access(resolved, os.W_OK)


def clear_skill_cache() -> None:
    """Clear cached skill loads."""
    _SKILL_CACHE.clear()


def get_skill_snapshot(roots: Sequence[Path] | None = None) -> _SkillSnapshot:
    """Return a snapshot of SKILL.md files under the provided roots."""
    roots = list(roots or _get_default_skill_roots())
    entries: list[tuple[str, int, int]] = []
    for root in _unique_paths(roots):
        entries.extend(_snapshot_skill_files(root))
    entries.sort()
    return tuple(entries)


def _snapshot_skill_files(root: Path) -> list[tuple[str, int, int]]:
    if not root.exists() or not root.is_dir():
        return []

    entries: list[tuple[str, int, int]] = []
    for skill_file in root.rglob(SKILL_FILENAME):
        try:
            stat = skill_file.stat()
        except OSError:
            continue
        entries.append((str(skill_file), stat.st_mtime_ns, stat.st_size))
    entries.sort()
    return entries


def _iter_skill_dirs(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []

    if (root / SKILL_FILENAME).exists():
        return [root]

    skill_dirs = [
        path
        for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / SKILL_FILENAME).exists()
    ]
    return sorted(skill_dirs)


class SkillMarkdownError(ValueError):
    """A ``SKILL.md`` whose frontmatter skill loading cannot read."""


def _match_skill_frontmatter(content: str) -> re.Match[str] | None:
    # With no closing fence the pattern backtracks quadratically before failing, so skip it when it cannot match.
    return _FRONTMATTER_PATTERN.match(content) if "\n---" in content else None


def parse_skill_markdown(
    content: str,
    *,
    load_yaml: Callable[[str], Any] = yaml_io.safe_load_without_aliases,
) -> tuple[dict[str, Any], str]:
    """Split one ``SKILL.md`` into its frontmatter mapping and instructions, as skill loading reads them.

    The default loader refuses frontmatter that worker code could write to exhaust the primary.
    """
    match = _match_skill_frontmatter(content)
    if not match:
        msg = "Skill missing frontmatter"
        raise SkillMarkdownError(msg)
    try:
        frontmatter = load_yaml(match.group(1)) or {}
    except Exception as exc:
        msg = f"Failed to parse skill frontmatter: {exc}"
        raise SkillMarkdownError(msg) from exc
    if not isinstance(frontmatter, dict):
        msg = "Skill frontmatter must be a mapping"
        raise SkillMarkdownError(msg)
    return cast("dict[str, Any]", frontmatter), match.group(2).strip()


def _parse_skill_frontmatter(
    content: str,
    *,
    path: str,
    allow_missing: bool,
    load_yaml: Callable[[str], Any] = yaml_io.safe_load_without_aliases,
) -> tuple[dict[str, Any], str] | None:
    """Split one ``SKILL.md`` into its frontmatter mapping and instructions, or warn and return None."""
    if allow_missing and not _match_skill_frontmatter(content):
        return {}, content
    try:
        return parse_skill_markdown(content, load_yaml=load_yaml)
    except SkillMarkdownError as exc:
        logger.warning("Refused skill frontmatter", path=path, error=str(exc))
        return None


def _read_skill_frontmatter(
    skill_path: Path,
    *,
    allow_missing: bool = False,
) -> dict[str, Any] | None:
    try:
        content = skill_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to read skill file", path=str(skill_path), error=str(exc))
        return None
    # Only operator skill roots are listed here, and agents load them with Agno's plain YAML loader, so parse alike.
    parsed = _parse_skill_frontmatter(
        content,
        path=str(skill_path),
        allow_missing=allow_missing,
        load_yaml=yaml_io.safe_load,
    )
    return None if parsed is None else parsed[0]


def _normalize_skill_identity(
    name: object,
    description: object,
    *,
    path: str,
) -> tuple[str, str] | None:
    if not isinstance(name, str) or not name.strip():
        logger.warning("Skill missing name", path=path)
        return None

    normalized_name = name.strip()
    if not isinstance(description, str) or not description.strip():
        return normalized_name, normalized_name
    return normalized_name, description.strip()


def _resolve_skill_frontmatter(
    skill_dir: Path,
    *,
    allow_missing_frontmatter: bool = False,
) -> _ResolvedSkillFrontmatter | None:
    frontmatter = _read_skill_frontmatter(
        skill_dir / SKILL_FILENAME,
        allow_missing=allow_missing_frontmatter,
    )
    if frontmatter is None:
        return None

    normalized = _normalize_skill_identity(
        frontmatter.get("name", skill_dir.name),
        frontmatter.get("description", ""),
        path=str(skill_dir),
    )
    if normalized is None:
        return None

    name, description = normalized
    return _ResolvedSkillFrontmatter(
        name=name,
        description=description,
        frontmatter=frontmatter,
    )


def _load_root_skills(root: Path) -> list[Skill]:
    if not root.exists() or not root.is_dir():
        return []

    resolved_root = root.expanduser().resolve()
    snapshot = tuple(_snapshot_skill_files(resolved_root))
    cached = _SKILL_CACHE.get(resolved_root)
    if cached and cached[0] == snapshot:
        return cached[1]

    loader = LocalSkills(str(resolved_root), validate=False)
    try:
        skills = loader.load()
    except Exception as exc:
        logger.warning("Failed to load skills", path=str(resolved_root), error=str(exc))
        if cached:
            return cached[1]
        return []

    _SKILL_CACHE[resolved_root] = (snapshot, skills)
    return skills


@dataclass(frozen=True)
class _WorkspaceEntryNames:
    """Sorted visible real directories or regular files from one bounded scan of a workspace directory."""

    names: list[str]
    # False when the scan stopped at its limit, so the directory may hold entries it did not see.
    complete: bool


def workspace_entry_names(directory_fd: int, *, directories: bool) -> _WorkspaceEntryNames:
    """Scan only the first entries of a directory worker code can fill, never following links."""
    with os.scandir(directory_fd) as entries:
        scanned = list(islice(entries, _MAX_WORKSPACE_SKILL_SCANNED_ENTRIES))
        names = sorted(
            entry.name
            for entry in scanned
            if not entry.name.startswith(".")
            and (entry.is_dir(follow_symlinks=False) if directories else entry.is_file(follow_symlinks=False))
        )
    return _WorkspaceEntryNames(names=names, complete=len(scanned) < _MAX_WORKSPACE_SKILL_SCANNED_ENTRIES)


def workspace_skill_file_names(skill_fd: int, dirname: str) -> list[str]:
    """Return the regular files one workspace skill lists in ``dirname``, never following links."""
    try:
        with open_directory_within_root(skill_fd, dirname) as listing_fd:
            names = workspace_entry_names(listing_fd, directories=False).names
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Refused a linked workspace skill directory", dirname=dirname, error=type(exc).__name__)
        return []
    if len(names) > _MAX_WORKSPACE_SKILL_LISTING_ENTRIES:
        logger.warning("Listing only the first workspace skill files", dirname=dirname, found=len(names))
    return names[:_MAX_WORKSPACE_SKILL_LISTING_ENTRIES]


def _read_workspace_skill_markdown(skill_fd: int, source_path: Path) -> str | None:
    """Return one workspace ``SKILL.md`` read below its pinned directory, or None when it is absent or refused."""
    try:
        return read_regular_file_within_root(
            skill_fd,
            SKILL_FILENAME,
            max_bytes=MAX_WORKSPACE_SKILL_FILE_BYTES,
        ).decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Refused a workspace skill file", path=str(source_path / SKILL_FILENAME), error=str(exc))
        return None


def _load_workspace_skill(skill_fd: int, source_path: Path, content: str) -> Skill | None:
    """Build one workspace skill from its ``SKILL.md`` text and descriptor reads below its pinned directory."""
    parsed = _parse_skill_frontmatter(content, path=str(source_path), allow_missing=True)
    if parsed is None:
        return None
    frontmatter, instructions = parsed
    name = frontmatter.get("name", source_path.name)
    if isinstance(name, str) and len(name) > MAX_WORKSPACE_SKILL_NAME_CHARS:
        logger.warning("Refused a workspace skill whose name is too long", path=str(source_path / SKILL_FILENAME))
        return None
    description = frontmatter.get("description", "")
    if isinstance(description, str) and len(description) > MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS:
        logger.warning("Truncated a workspace skill description", path=str(source_path / SKILL_FILENAME))
        description = description[:MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS]
    return Skill(
        name=name,
        description=description,
        instructions=instructions,
        source_path=str(source_path),
        scripts=workspace_skill_file_names(skill_fd, "scripts"),
        references=workspace_skill_file_names(skill_fd, "references"),
        metadata=frontmatter.get("metadata"),
        license=frontmatter.get("license"),
        compatibility=frontmatter.get("compatibility"),
        allowed_tools=frontmatter.get("allowed-tools"),
    )


def _load_workspace_skills(workspace_root: Path) -> list[Skill]:
    """Read one workspace's skills through no-follow descriptors; files are reread the same way on use."""
    skills_root = workspace_root / _WORKSPACE_SKILLS_DIRNAME
    try:
        with open_directory_within_root(workspace_root, _WORKSPACE_SKILLS_DIRNAME) as skills_fd:
            try:
                os.stat(SKILL_FILENAME, dir_fd=skills_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                content = _read_workspace_skill_markdown(skills_fd, skills_root)
                skill = None if content is None else _load_workspace_skill(skills_fd, skills_root, content)
                return [] if skill is None else [skill]
            skill_names = workspace_entry_names(skills_fd, directories=True).names
            if len(skill_names) > _MAX_WORKSPACE_SKILLS:
                logger.warning(
                    "Loading only the first workspace skills",
                    path=str(skills_root),
                    limit=_MAX_WORKSPACE_SKILLS,
                    found=len(skill_names),
                )
                skill_names = skill_names[:_MAX_WORKSPACE_SKILLS]
            return _load_workspace_skill_directories(skills_fd, skills_root, skill_names)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Refused workspace skills", path=str(skills_root), error=type(exc).__name__)
        return []


def _load_workspace_skill_directories(skills_fd: int, skills_root: Path, skill_names: list[str]) -> list[Skill]:
    """Load the named skill directories in order until their ``SKILL.md`` files and listings exceed the budget."""
    skills: list[Skill] = []
    loaded_bytes = 0
    for skill_name in skill_names:
        try:
            with open_directory_within_root(skills_fd, skill_name) as skill_fd:
                content = _read_workspace_skill_markdown(skill_fd, skills_root / skill_name)
                if content is None:
                    continue
                # Each file is charged before it is parsed, so skills that fail to load spend the budget too.
                loaded_bytes += len(content.encode())
                if loaded_bytes > _MAX_WORKSPACE_SKILLS_BYTES:
                    break
                skill = _load_workspace_skill(skill_fd, skills_root / skill_name, content)
        except OSError as exc:
            logger.warning("Refused a workspace skill", path=str(skills_root / skill_name), error=type(exc).__name__)
            continue
        if skill is None:
            continue
        loaded_bytes += len("".join((*skill.scripts, *skill.references)).encode())
        if loaded_bytes > _MAX_WORKSPACE_SKILLS_BYTES:
            break
        skills.append(skill)
    else:
        return skills
    logger.warning("Workspace skills exceed their budget; skipping the rest", path=str(skills_root))
    return skills


def _normalize_skill(skill: Skill) -> Skill | None:
    normalized = _normalize_skill_identity(
        skill.name,
        skill.description,
        path=str(skill.source_path),
    )
    if normalized is None:
        return None

    skill.name, skill.description = normalized

    metadata = parse_skill_metadata(skill.metadata, path=skill.source_path)
    if metadata is None:
        return None
    skill.metadata = metadata
    return skill


def parse_skill_metadata(raw: object, *, path: str) -> dict[str, Any] | None:
    """Return skill metadata as a mapping, accepting OpenClaw JSON5 strings, or warn and return None."""
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


def _is_skill_eligible(
    skill: Skill,
    config_data: Mapping[str, Any],
    *,
    env_vars: Mapping[str, str],
    credential_keys: set[str],
) -> bool:
    metadata = skill.metadata or {}
    openclaw = metadata.get("openclaw")
    if not isinstance(openclaw, dict):
        return True

    os_requirements = _normalize_str_list(openclaw.get("os"))
    if os_requirements and not _matches_current_os(os_requirements):
        return False

    if openclaw.get("always") is True:
        return True

    requires = openclaw.get("requires")
    return _requirements_met(requires, config_data, env_vars, credential_keys, skill.name)


def _normalize_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if isinstance(item, str)]
    return []


def _matches_current_os(requirements: Sequence[str]) -> bool:
    current_os = platform.system().lower()
    aliases = _OS_ALIASES.get(current_os, {current_os})
    return any(requirement.lower() in aliases for requirement in requirements)


def _env_requirements_met(
    requirements: Sequence[str],
    env_vars: Mapping[str, str],
    credential_keys: set[str],
) -> bool:
    for requirement in requirements:
        if env_vars.get(requirement):
            continue
        if requirement in credential_keys:
            continue
        return False
    return True


def _missing_bins(requirements: Sequence[str]) -> list[str]:
    return [requirement for requirement in requirements if shutil.which(requirement) is None]


def _any_bins_requirements_met(requirements: Sequence[str]) -> bool:
    return any(shutil.which(requirement) for requirement in requirements)


def _config_requirements_met(requirements: Sequence[str], config_data: Mapping[str, Any]) -> bool:
    return all(_config_path_truthy(config_data, requirement) for requirement in requirements)


def _requirements_met(
    requires: object,
    config_data: Mapping[str, Any],
    env_vars: Mapping[str, str],
    credential_keys: set[str],
    skill_name: str,
) -> bool:
    if not isinstance(requires, dict):
        return True
    reqs = cast("dict[str, Any]", requires)

    env_requirements = _normalize_str_list(reqs.get("env"))
    if env_requirements and not _env_requirements_met(env_requirements, env_vars, credential_keys):
        return False

    config_requirements = _normalize_str_list(reqs.get("config"))
    if config_requirements and not _config_requirements_met(config_requirements, config_data):
        return False

    bin_requirements = _normalize_str_list(reqs.get("bins"))
    if bin_requirements:
        missing_bins = _missing_bins(bin_requirements)
        if missing_bins:
            logger.debug("Skill missing required binaries", skill=skill_name, bins=missing_bins)
            return False

    any_bins_requirements = _normalize_str_list(reqs.get("anyBins"))
    if any_bins_requirements and not _any_bins_requirements_met(any_bins_requirements):
        logger.debug("Skill missing any required binaries", skill=skill_name, bins=any_bins_requirements)
        return False

    return True


def _config_path_truthy(config_data: Mapping[str, Any], path: str) -> bool:
    current: Any = config_data
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return False
    return bool(current)


def _collect_credential_keys(_config: Config, runtime_paths: RuntimePaths) -> set[str]:
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    keys: set[str] = set()
    for service in credentials_manager.list_services():
        credentials = credentials_manager.load_credentials(service) or {}
        for key, value in credentials.items():
            if value:
                keys.add(key)
    return keys


def _unique_paths(paths: Sequence[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique_paths: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_paths.append(resolved)
    return unique_paths


def _root_origin(root: Path, bundled_root: Path, user_root: Path, plugin_roots: set[Path]) -> str:
    if root == bundled_root:
        return "bundled"
    if root == user_root:
        return "user"
    if root in plugin_roots:
        return "plugin"
    return "custom"
