"""Skill integration built on Agno skills with OpenClaw-compatible metadata."""

from __future__ import annotations

import json
import os
import platform
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agno.skills import LocalSkills, Skills
from agno.skills.errors import SkillValidationError
from agno.skills.loaders import SkillLoader

from mindroom.constants import runtime_env_values
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.tool_system.output_files import ToolOutputFilePolicy, wrap_function_for_output_files
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from mindroom.tool_system.workspace_skills import (
    FRONTMATTER_PATTERN,
    SKILL_FILENAME,
    load_workspace_skills,
    parse_skill_markdown,
    parse_skill_metadata,
    read_support_file,
    record_skill_use,
)

if TYPE_CHECKING:
    from agno.skills.skill import Skill
    from agno.tools.function import Function

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

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
    """Load skills via Agno, or confined reads for a workspace root, with OpenClaw compatibility filtering."""

    roots: Sequence[Path]
    config: Config
    runtime_paths: RuntimePaths
    allowlist: Sequence[str] | None = None
    env_vars: Mapping[str, str] | None = None
    credential_keys: set[str] | None = None
    workspace: bool = False

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
        for root in _unique_paths(self.roots):
            for skill in load_workspace_skills(root) if self.workspace else _load_root_skills(root):
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


class MindroomSkills(Skills):
    """MindRoom-specific Skills wrapper for confined workspace reads, script policy, and usage."""

    def __init__(
        self,
        *,
        loaders: list[SkillLoader],
        output_file_policy: ToolOutputFilePolicy | None = None,
    ) -> None:
        self._workspace_skill_names: set[str] = set()
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
        self._workspace_skill_names.clear()
        for loader in self.loaders:
            try:
                skills = loader.load()
                workspace = isinstance(loader, _MindroomSkillsLoader) and loader.workspace
                for skill in skills:
                    if skill.name in self._skills:
                        logger.warning("Duplicate skill name; overwriting with newer version", skill=skill.name)
                    self._skills[skill.name] = skill
                    if workspace:
                        self._workspace_skill_names.add(skill.name)
                    else:
                        self._workspace_skill_names.discard(skill.name)
            except SkillValidationError:
                raise
            except Exception as exc:
                logger.warning("Error loading skills", loader=repr(loader), error=str(exc))

        logger.debug("Loaded skills", count=len(self._skills))

    # AGNO_COMPAT: Skills tool entrypoints are private methods without a load hook or file reader.
    # Reason: get_tools binds _get_skill_instructions, _get_skill_reference and _get_skill_script directly, and the
    # latter two read support files by pathname. Workspace skills need no-follow reads, a usage record for the
    # learner's inactivity clock, and blocked script execution, so the private entrypoints are overridden.
    # Upstream issue: tracking gap; no Agno issue or PR proposes a skill-load callback or caller-owned file reads.
    # Upstream PR: none identified.
    # Remove when: Skills exposes a load callback and reads support files through the loader's reader; the
    # execution block for workspace scripts remains MindRoom policy.
    # Coverage: tests/test_skills.py::test_workspace_support_reads_refuse_swapped_links,
    # tests/test_skills.py::test_workspace_skill_loads_record_usage_but_configured_skills_do_not, and
    # tests/test_skills.py::test_workspace_skill_script_read_allowed_but_execute_blocked.
    def _get_skill_instructions(self, skill_name: str) -> str:
        self.record_use(skill_name)
        return super()._get_skill_instructions(skill_name)

    def _get_skill_reference(self, skill_name: str, reference_path: str | None = None) -> str:
        skill = self._workspace_skill(skill_name)
        if skill is None or reference_path is None or reference_path not in skill.references:
            return super()._get_skill_reference(skill_name, reference_path)
        return self._read_workspace_support(skill, "references", reference_path, "reference_path")

    def _get_skill_script(
        self,
        skill_name: str,
        script_path: str | None = None,
        execute: bool = False,
        args: list[str] | None = None,
        timeout: int = 30,
    ) -> str:
        skill = self._workspace_skill(skill_name)
        if skill is not None and execute:
            return json.dumps(
                {
                    "error": "Workspace skill scripts cannot be executed through get_skill_script",
                    "skill_name": skill_name,
                    "script_path": script_path,
                },
            )
        if skill is None or script_path is None or script_path not in skill.scripts:
            return super()._get_skill_script(
                skill_name=skill_name,
                script_path=script_path,
                execute=execute,
                args=args,
                timeout=timeout,
            )
        return self._read_workspace_support(skill, "scripts", script_path, "script_path")

    def _workspace_skill(self, skill_name: str) -> Skill | None:
        return self.get_skill(skill_name) if skill_name in self._workspace_skill_names else None

    def record_use(self, skill_name: str) -> None:
        """Count one agent load of a workspace skill; configured skills carry no usage telemetry."""
        skill = self._workspace_skill(skill_name)
        if skill is not None:
            record_skill_use(Path(skill.source_path))

    def _read_workspace_support(self, skill: Skill, directory: str, filename: str, key: str) -> str:
        """Read a listed support file through the confined workspace reader."""
        try:
            content = read_support_file(Path(skill.source_path), directory, filename)
        except (OSError, ValueError) as exc:
            return json.dumps(
                {"error": f"Error reading {directory} file: {exc}", "skill_name": skill.name, key: filename},
            )
        self.record_use(skill.name)
        return json.dumps({"skill_name": skill.name, key: filename, "content": content})


def build_agent_skills(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    skill_roots: Sequence[Path] | None = None,
    workspace_skills_root: Path | None = None,
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

    workspace_skill_root = (
        workspace_skills_root
        if workspace_skills_root is not None
        else agent_workspace_skills_root(runtime_paths, agent_name, workspace_root=None)
    )
    workspace_loader = _MindroomSkillsLoader(
        roots=[workspace_skill_root],
        config=config,
        runtime_paths=runtime_paths,
        env_vars=env_vars,
        credential_keys=resolved_credential_keys,
        workspace=True,
    )

    loaders: list[SkillLoader]
    if skill_roots is None:
        loaders = [loader for loader in (configured_loader, workspace_loader) if loader is not None]
    else:
        loaders = [loader for loader in (workspace_loader, configured_loader) if loader is not None]

    skills = MindroomSkills(loaders=loaders, output_file_policy=output_file_policy)
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
    """Return a resolved workspace's skill root, or the agent's canonical shared workspace skill root."""
    root = (
        workspace_root
        if workspace_root is not None
        else agent_workspace_root_path(runtime_paths.storage_root, agent_name)
    )
    return root / "skills"


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

    if not FRONTMATTER_PATTERN.match(content):
        if allow_missing:
            return {}
        logger.warning("Skill missing frontmatter", path=str(skill_path))
        return None

    try:
        return parse_skill_markdown(content)[0]
    except Exception as exc:
        logger.warning("Failed to parse skill frontmatter", path=str(skill_path), error=str(exc))
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
