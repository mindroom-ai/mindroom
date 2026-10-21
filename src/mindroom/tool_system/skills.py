"""OpenClaw-compatible skills: loading from skill roots and agent workspaces, and the toolkit that offers them."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import json5
from agno.exceptions import PathSecurityError
from agno.skills import Skills
from agno.skills.skill import Skill as AgnoSkill
from agno.tools import Toolkit
from agno.tools.function import Function
from agno.utils.path_safety import safe_join_relative_path

from mindroom import yaml_io
from mindroom.background_tasks import create_background_task
from mindroom.constants import runtime_env_values
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root
from mindroom.tool_system.output_files import ToolOutputFilePolicy, wrap_function_for_output_files
from mindroom.tool_system.skill_usage import record_skill_use
from mindroom.tool_system.worker_routing import agent_workspace_root_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

SKILL_FILENAME = "SKILL.md"
# The functions of an agent's skills toolkit; none has an owning toolkit.
# All three only read a skill; Dynamic Workflow participants get get_skill_script only through an operator rule.
SKILL_READ_FUNCTION_NAMES = frozenset({"get_skill_instructions", "get_skill_reference"})
SKILL_FUNCTION_NAMES = SKILL_READ_FUNCTION_NAMES | {"get_skill_script"}
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
_GET_SKILL_INSTRUCTIONS_DESCRIPTION = (
    "Load the full instructions for a skill. Use this when you need to follow a skill's guidance."
)
_GET_SKILL_REFERENCE_DESCRIPTION = (
    "Load a reference document from a skill's references. Use this to access detailed documentation."
)
_GET_SKILL_SCRIPT_DESCRIPTION = (
    "Read a script from a skill. This never runs the script; run it with your shell tool if you have one."
)
# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
# Agno's skills prompt, with get_skill_script offered only for reading.
_PROMPT_HEADER = (
    "<skills_system>",
    "",
    "## What are Skills?",
    "Skills are packages of domain expertise that extend your capabilities. Each skill contains:",
    "- **Instructions**: Detailed guidance on when and how to apply the skill",
    "- **Scripts**: Executable code templates you can use or adapt",
    "- **References**: Supporting documentation (guides, cheatsheets, examples)",
    "",
    "## IMPORTANT: How to Use Skills",
    "**Skill names are NOT callable functions.** You cannot call a skill directly by its name.",
    "Instead, you MUST use the provided skill access tools:",
    "",
    "1. `get_skill_instructions(skill_name)` - Load the full instructions for a skill",
    "2. `get_skill_reference(skill_name, reference_path)` - Access specific documentation",
    "3. `get_skill_script(skill_name, script_path)` - Read scripts",
    "",
    "## Progressive Discovery Workflow",
    "1. **Browse**: Review the skill summaries below to understand what's available",
    "2. **Load**: When a task matches a skill, call `get_skill_instructions(skill_name)` first",
    "3. **Reference**: Use `get_skill_reference` to access specific documentation as needed",
    "4. **Scripts**: Use `get_skill_script` to read scripts from a skill; it never runs them",
    "",
    "**IMPORTANT**: References are documentation files (NOT executable). Only use `get_skill_script` when "
    "`<scripts>` lists actual script files. If `<scripts>none</scripts>`, do NOT call `get_skill_script`.",
    "",
    "This approach ensures you only load detailed instructions when actually needed.",
    "",
    "## Available Skills",
)
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


@dataclass(frozen=True, slots=True)
class Skill:
    """One skill: its ``SKILL.md`` instructions and the script and reference file names beside it.

    ``metadata`` is the frontmatter ``metadata`` as a mapping once loading has validated the skill.
    """

    name: str
    description: str
    instructions: str
    source_path: Path
    scripts: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    metadata: Mapping[str, Any] | None = None


@dataclass
class _SkillsLoader:
    """Load the eligible skills of the configured roots, in allowlist order when an allowlist is set."""

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


@dataclass(frozen=True)
class RefusedWorkspaceSkill:
    """A workspace ``SKILL.md`` that did not load, by its path in the workspace, and why."""

    path: str
    reason: str


@dataclass(kw_only=True)
class _WorkspaceSkillsLoader(_SkillsLoader):
    """Load one workspace's ``skills`` directory through no-follow descriptors."""

    workspace_root: Path
    # The workspace skills the last load refused.
    refused: list[RefusedWorkspaceSkill] = field(default_factory=list)

    def _candidate_skills(self) -> list[Skill]:
        self.refused = []
        return _load_workspace_skills(self.workspace_root, self.refused)


class SkillsToolkit(Toolkit):
    """One agent's skills: three functions that only read them, and the skills section of the system prompt.

    Workspace skills, which worker code can write, are read through no-follow descriptors, and loading one records
    its use for the skill learner's inactivity clock; configured skills are read by path and record nothing.
    """

    def __init__(
        self,
        skills: Mapping[str, Skill],
        *,
        workspace_roots: Mapping[str, Path],
        output_file_policy: ToolOutputFilePolicy | None = None,
    ) -> None:
        super().__init__(name="skills")
        self._skills = dict(skills)
        # The workspace root of each skill that came from an agent workspace.
        self._workspace_roots = dict(workspace_roots)
        self._output_file_policy = output_file_policy
        for function in self._new_functions():
            self.functions[function.name] = function

    def _new_functions(self) -> list[Function]:
        functions = [
            Function(
                name="get_skill_instructions",
                description=_GET_SKILL_INSTRUCTIONS_DESCRIPTION,
                entrypoint=self.get_skill_instructions,
            ),
            Function(
                name="get_skill_reference",
                description=_GET_SKILL_REFERENCE_DESCRIPTION,
                entrypoint=self.get_skill_reference,
            ),
            Function(
                name="get_skill_script",
                description=_GET_SKILL_SCRIPT_DESCRIPTION,
                entrypoint=self.get_skill_script,
            ),
        ]
        if self._output_file_policy is None:
            return functions
        return [wrap_function_for_output_files(function, self._output_file_policy) for function in functions]

    @property
    def skills(self) -> tuple[Skill, ...]:
        """The loaded skills, in the order the prompt lists them."""
        return tuple(self._skills.values())

    def get_skill(self, name: str) -> Skill | None:
        """Return the loaded skill named ``name``."""
        return self._skills.get(name)

    def prompt_block(self) -> str:
        """Return the skills section of the system prompt, or ``""`` when no skill loaded."""
        if not self._skills:
            return ""
        lines = list(_PROMPT_HEADER)
        for skill in self._skills.values():
            lines.append("<skill>")
            lines.append(f"  <name>{skill.name}</name>")
            lines.append(f"  <description>{skill.description}</description>")
            lines.append(
                f"  <scripts>{', '.join(skill.scripts)}</scripts>" if skill.scripts else "  <scripts>none</scripts>",
            )
            if skill.references:
                lines.append(f"  <references>{', '.join(skill.references)}</references>")
            lines.append("</skill>")
        lines.extend(("", "</skills_system>"))
        return "\n".join(lines)

    def get_skill_instructions(self, skill_name: str) -> str:
        """Load the full instructions for a skill.

        Args:
            skill_name: The name of the skill to get instructions for.

        Returns:
            A JSON string with the skill's instructions and metadata.

        """
        self._record_use(skill_name)
        skill = self._skills.get(skill_name)
        if skill is None:
            return self._not_found(skill_name)
        return json.dumps(
            {
                "skill_name": skill.name,
                "description": skill.description,
                "instructions": skill.instructions,
                "available_scripts": skill.scripts,
                "available_references": skill.references,
            },
        )

    def get_skill_reference(self, skill_name: str, reference_path: str | None = None) -> str:
        """Load a reference document from a skill.

        Args:
            skill_name: The name of the skill.
            reference_path: The filename of the reference document.

        Returns:
            A JSON string with the reference content.

        """
        return self._read_skill_file(skill_name, _REFERENCES, reference_path)

    def get_skill_script(self, skill_name: str, script_path: str | None = None) -> str:
        """Read a script from a skill without running it.

        Args:
            skill_name: The name of the skill.
            script_path: The filename of the script.

        Returns:
            A JSON string with the script content.

        """
        return self._read_skill_file(skill_name, _SCRIPTS, script_path)

    def _not_found(self, skill_name: str) -> str:
        return json.dumps({"error": f"Skill '{skill_name}' not found", "available_skills": ", ".join(self._skills)})

    def _read_skill_file(self, skill_name: str, kind: _SkillFileKind, filename: str | None) -> str:
        """Read one listed reference or script of a skill as the JSON a skill function returns."""
        skill = self._skills.get(skill_name)
        if skill is None:
            return self._not_found(skill_name)
        listed = kind.listed(skill)
        if not filename or filename not in listed:
            return json.dumps(_unlisted_file_error(skill_name, kind, filename, listed))
        workspace_root = self._workspace_roots.get(skill_name)
        if workspace_root is not None:
            content = self._read_workspace_skill_file(workspace_root, skill, kind.dirname, filename)
            return json.dumps({"skill_name": skill_name, kind.path_key: filename, **content})
        try:
            path = safe_join_relative_path(skill.source_path / kind.dirname, filename)
        except PathSecurityError:
            return json.dumps({"error": f"Invalid {kind.noun} path: '{filename}'", "skill_name": skill_name})
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            return json.dumps(
                {"error": f"Error reading {kind.noun} file: {exc}", "skill_name": skill_name, kind.path_key: filename},
            )
        return json.dumps({"skill_name": skill_name, kind.path_key: filename, "content": content})

    def _read_workspace_skill_file(
        self,
        workspace_root: Path,
        skill: Skill,
        kind: str,
        filename: str,
    ) -> dict[str, str]:
        """Read one listed workspace skill file below its pinned directory, never following links."""
        relative_path = skill.source_path.relative_to(workspace_root) / kind / filename
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
        self._record_use(skill.name)
        return {"content": content}

    def _record_use(self, skill_name: str) -> None:
        """Record one agent load of a workspace skill directory; configured skills are not recorded.

        Usage records live beside skill directories, so a workspace whose ``skills/`` is itself one skill keeps none.
        Skill functions can run on the event loop, so there the usage write runs in a thread.
        """
        workspace_root = self._workspace_roots.get(skill_name)
        skill = self._skills.get(skill_name)
        if (
            workspace_root is None
            or skill is None
            or skill.source_path.parent != workspace_root / _WORKSPACE_SKILLS_DIRNAME
        ):
            return
        record = partial(record_skill_use, skill.source_path)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            record()
            return
        create_background_task(asyncio.to_thread(record), name="record_skill_use")


@dataclass(frozen=True, slots=True)
class _SkillFileKind:
    """One kind of file a skill lists, with the error texts and keys Agno's skill functions used."""

    dirname: str
    noun: str
    path_key: str
    listing_key: str

    def listed(self, skill: Skill) -> tuple[str, ...]:
        """Return the file names ``skill`` lists of this kind."""
        return skill.references if self.dirname == "references" else skill.scripts


_REFERENCES = _SkillFileKind("references", "reference", "reference_path", "available_references")
_SCRIPTS = _SkillFileKind("scripts", "script", "script_path", "available_scripts")


def _unlisted_file_error(
    skill_name: str,
    kind: _SkillFileKind,
    filename: str | None,
    listed: tuple[str, ...],
) -> dict[str, object]:
    """Return the error a skill function answers when ``filename`` is missing or not a listed file of the skill."""
    if not filename:
        return {
            "error": f"A {kind.path_key} is required to read a {kind.noun} from skill '{skill_name}'",
            "skill_name": skill_name,
            kind.listing_key: listed,
        }
    return {
        "error": f"{kind.noun.capitalize()} '{filename}' not found in skill '{skill_name}'",
        kind.listing_key: listed,
    }


class _AgnoSkills(Skills):
    """The Agno ``Skills`` object agents built on Agno take, offering one owned ``SkillsToolkit``.

    Like Agno's own, it builds fresh skill functions on every ``get_tools`` call, because Agno's callers prepare and
    mutate the functions they receive.
    """

    def __init__(self, toolkit: SkillsToolkit) -> None:
        self.toolkit = toolkit
        super().__init__(loaders=[])

    def get_skill(self, name: str) -> AgnoSkill | None:
        """Return the loaded skill named ``name``."""
        skill = self.toolkit.get_skill(name)
        return None if skill is None else _agno_skill(skill)

    def get_all_skills(self) -> list[AgnoSkill]:
        """Return every loaded skill."""
        return [_agno_skill(skill) for skill in self.toolkit.skills]

    def get_skill_names(self) -> list[str]:
        """Return the loaded skill names."""
        return [skill.name for skill in self.toolkit.skills]

    def get_system_prompt_snippet(self) -> str:
        """Return the skills section of the system prompt."""
        return self.toolkit.prompt_block()

    def get_tools(self) -> list[Function]:
        """Return fresh skill functions."""
        return self.toolkit._new_functions()


def _agno_skill(skill: Skill) -> AgnoSkill:
    return AgnoSkill(
        name=skill.name,
        description=skill.description,
        instructions=skill.instructions,
        source_path=str(skill.source_path),
        scripts=list(skill.scripts),
        references=list(skill.references),
        metadata=None if skill.metadata is None else dict(skill.metadata),
    )


def build_skills_toolkit(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    skill_roots: Sequence[Path] | None = None,
    workspace_root: Path | None = None,
    env_vars: Mapping[str, str] | None = None,
    credential_keys: set[str] | None = None,
    output_file_policy: ToolOutputFilePolicy | None = None,
    only: Collection[str] | None = None,
    refused: list[RefusedWorkspaceSkill] | None = None,
) -> SkillsToolkit | None:
    """Build one agent's skills toolkit, narrowed to the skill names in ``only`` when set.

    Workspace skills win over the default roots on a name conflict; explicit ``skill_roots`` win over the workspace.
    ``refused`` collects the workspace skills that did not load, also when no skill loads.
    """
    if only is not None and not only:
        return None
    agent_config = config.get_agent(agent_name)
    resolved_credential_keys = (
        credential_keys if credential_keys is not None else _collect_credential_keys(config, runtime_paths)
    )

    configured_loader = None
    if agent_config.skills:
        configured_loader = _SkillsLoader(
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

    loaders: list[_SkillsLoader]
    if skill_roots is None:
        loaders = [loader for loader in (configured_loader, workspace_loader) if loader is not None]
    else:
        loaders = [loader for loader in (workspace_loader, configured_loader) if loader is not None]

    skills, workspace_roots = _load_skills(loaders, None if only is None else frozenset(only))
    if refused is not None:
        refused.extend(workspace_loader.refused)
    # A narrowed set with no surviving skill offers nothing, even beside configured skills.
    if not skills and not (agent_config.skills and only is None):
        return None
    return SkillsToolkit(skills, workspace_roots=workspace_roots, output_file_policy=output_file_policy)


def _load_skills(
    loaders: Sequence[_SkillsLoader],
    only: frozenset[str] | None,
) -> tuple[dict[str, Skill], dict[str, Path]]:
    """Load every loader's skills in order, a later loader winning a name, and track which came from a workspace."""
    skills: dict[str, Skill] = {}
    workspace_roots: dict[str, Path] = {}
    for loader in loaders:
        try:
            loaded = loader.load()
        except Exception as exc:
            logger.warning("Error loading skills", loader=type(loader).__name__, error=str(exc))
            continue
        workspace_root = loader.workspace_root if isinstance(loader, _WorkspaceSkillsLoader) else None
        for skill in loaded:
            if only is not None and skill.name not in only:
                continue
            if skill.name in skills:
                logger.warning("Duplicate skill name; overwriting with newer version", skill=skill.name)
            skills[skill.name] = skill
            if workspace_root is not None:
                workspace_roots[skill.name] = workspace_root
            else:
                workspace_roots.pop(skill.name, None)
    logger.debug("Loaded skills", count=len(skills))
    return skills, workspace_roots


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
    only: Collection[str] | None = None,
    refused: list[RefusedWorkspaceSkill] | None = None,
) -> Skills | None:
    """Return ``build_skills_toolkit``'s toolkit as the Agno ``Skills`` object agents built on Agno take."""
    toolkit = build_skills_toolkit(
        agent_name,
        config,
        runtime_paths,
        skill_roots=skill_roots,
        workspace_root=workspace_root,
        env_vars=env_vars,
        credential_keys=credential_keys,
        output_file_policy=output_file_policy,
        only=only,
        refused=refused,
    )
    return None if toolkit is None else _AgnoSkills(toolkit)


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
        # Leading line breaks keep the lines YAML errors name at their lines in SKILL.md.
        frontmatter = load_yaml("\n" * content.count("\n", 0, match.start(1)) + match.group(1)) or {}
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
    allow_missing: bool,
    load_yaml: Callable[[str], Any] = yaml_io.safe_load_without_aliases,
) -> tuple[dict[str, Any], str]:
    """Split one ``SKILL.md`` like ``parse_skill_markdown``, reading one without frontmatter as all instructions."""
    if allow_missing and not _match_skill_frontmatter(content):
        return {}, content
    return parse_skill_markdown(content, load_yaml=load_yaml)


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
    try:
        return _parse_skill_frontmatter(content, allow_missing=allow_missing, load_yaml=yaml_io.safe_load)[0]
    except SkillMarkdownError as exc:
        logger.warning("Refused skill frontmatter", path=str(skill_path), error=str(exc))
        return None


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

    try:
        skills = _read_skill_root(resolved_root)
    except Exception as exc:
        logger.warning("Failed to load skills", path=str(resolved_root), error=str(exc))
        if cached:
            return cached[1]
        return []

    _SKILL_CACHE[resolved_root] = (snapshot, skills)
    return skills


# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
# Operator skill roots load as Agno's LocalSkills(validate=False) loaded them, so their skills keep loading the same.
def _read_skill_root(root: Path) -> list[Skill]:
    """Read one skill folder, or every skill folder directly below ``root``, in directory order."""
    if (root / SKILL_FILENAME).exists():
        skill = _read_skill_folder(root)
        return [] if skill is None else [skill]
    skills: list[Skill] = []
    for item in root.iterdir():
        if item.is_dir() and not item.name.startswith(".") and (item / SKILL_FILENAME).exists():
            skill = _read_skill_folder(item)
            if skill is not None:
                skills.append(skill)
    return skills


def _read_skill_folder(folder: Path) -> Skill | None:
    """Read one operator skill folder; its frontmatter is validated when an agent loads the skill."""
    try:
        frontmatter, instructions = _parse_operator_skill_markdown(
            (folder / SKILL_FILENAME).read_text(encoding="utf-8"),
        )
        return Skill(
            name=frontmatter.get("name", folder.name),
            description=frontmatter.get("description", ""),
            instructions=instructions,
            source_path=folder,
            scripts=_listed_file_names(folder / "scripts"),
            references=_listed_file_names(folder / "references"),
            metadata=frontmatter.get("metadata"),
        )
    except Exception as exc:
        logger.warning("Error loading skill", path=str(folder), error=str(exc))
        return None


def _parse_operator_skill_markdown(content: str) -> tuple[Any, str]:
    """Split an operator ``SKILL.md``; frontmatter that is not valid YAML is read as ``key: value`` lines."""
    match = _match_skill_frontmatter(content)
    if match is None:
        return {}, content
    try:
        frontmatter = yaml_io.safe_load(match.group(1)) or {}
    except Exception as exc:
        logger.warning("Error parsing skill frontmatter as YAML", error=str(exc))
        frontmatter = _simple_frontmatter(match.group(1))
    return frontmatter, match.group(2).strip()


def _simple_frontmatter(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.strip().split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _listed_file_names(directory: Path) -> tuple[str, ...]:
    if not directory.exists() or not directory.is_dir():
        return ()
    return tuple(sorted(item.name for item in directory.iterdir() if item.is_file() and not item.name.startswith(".")))


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


def _load_workspace_skill(skill_fd: int, source_path: Path, content: str) -> Skill | str:
    """Build one workspace skill from its ``SKILL.md`` text and descriptor reads below its pinned directory.

    Returns why the skill does not load instead when its frontmatter is unreadable or its name too long.
    """
    try:
        frontmatter, instructions = _parse_skill_frontmatter(content, allow_missing=True)
    except SkillMarkdownError as exc:
        logger.warning("Refused skill frontmatter", path=str(source_path), error=str(exc))
        # The reason reaches the agent's prompt, and a parse error can quote planted frontmatter.
        return " ".join(str(exc).split())[:MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS]
    name = frontmatter.get("name", source_path.name)
    if isinstance(name, str) and len(name) > MAX_WORKSPACE_SKILL_NAME_CHARS:
        logger.warning("Refused a workspace skill whose name is too long", path=str(source_path / SKILL_FILENAME))
        return f"Skill name exceeds {MAX_WORKSPACE_SKILL_NAME_CHARS} characters"
    description = frontmatter.get("description", "")
    if isinstance(description, str) and len(description) > MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS:
        logger.warning("Truncated a workspace skill description", path=str(source_path / SKILL_FILENAME))
        description = description[:MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS]
    return Skill(
        name=name,
        description=description,
        instructions=instructions,
        source_path=source_path,
        scripts=tuple(workspace_skill_file_names(skill_fd, "scripts")),
        references=tuple(workspace_skill_file_names(skill_fd, "references")),
        metadata=frontmatter.get("metadata"),
    )


def _load_workspace_skills(workspace_root: Path, refused: list[RefusedWorkspaceSkill]) -> list[Skill]:
    """Read one workspace's skills through no-follow descriptors, adding those that do not load to ``refused``.

    Skill files are reread the same way on use.
    """
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
                if isinstance(skill, str):
                    refused.append(RefusedWorkspaceSkill(f"{_WORKSPACE_SKILLS_DIRNAME}/{SKILL_FILENAME}", skill))
                    return []
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
            return _load_workspace_skill_directories(skills_fd, skills_root, skill_names, refused)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Refused workspace skills", path=str(skills_root), error=type(exc).__name__)
        return []


def _load_workspace_skill_directories(
    skills_fd: int,
    skills_root: Path,
    skill_names: list[str],
    refused: list[RefusedWorkspaceSkill],
) -> list[Skill]:
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
        if isinstance(skill, str):
            refused.append(RefusedWorkspaceSkill(f"{_WORKSPACE_SKILLS_DIRNAME}/{skill_name}/{SKILL_FILENAME}", skill))
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
    metadata = parse_skill_metadata(skill.metadata, path=str(skill.source_path))
    if metadata is None:
        return None
    name, description = normalized
    return replace(skill, name=name, description=description, metadata=metadata)


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
