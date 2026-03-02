"""Skill integration built on Agno skills with OpenClaw-compatible metadata."""

from __future__ import annotations

import os
import platform
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import json5
import yaml
from agno.skills import LocalSkills, Skills
from agno.skills.loaders import SkillLoader
from agno.skills.skill import Skill

from mindroom.credentials import get_credentials_manager
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config

logger = get_logger(__name__)

_SKILL_FILENAME = "SKILL.md"
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
    allowlist: Sequence[str] | None = None
    env_vars: Mapping[str, str] | None = None
    credential_keys: set[str] | None = None

    def load(self) -> list[Skill]:
        """Return the eligible skills for the configured roots and allowlist."""
        env_vars = os.environ if self.env_vars is None else self.env_vars
        credential_keys = self.credential_keys if self.credential_keys is not None else _collect_credential_keys()
        config_data = self.config.model_dump()
        allowlist_set = set(self.allowlist or [])

        skills_by_name: dict[str, Skill] = {}
        for root in _unique_paths(self.roots):
            for skill in _load_root_skills(root):
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


def build_agent_skills(
    agent_name: str,
    config: Config,
    *,
    skill_roots: Sequence[Path] | None = None,
    env_vars: Mapping[str, str] | None = None,
    credential_keys: set[str] | None = None,
) -> Skills | None:
    """Build an Agno Skills object for a specific agent."""
    agent_config = config.get_agent(agent_name)
    if not agent_config.skills:
        return None

    roots = list(skill_roots or _get_default_skill_roots())
    loader = _MindroomSkillsLoader(
        roots=roots,
        config=config,
        allowlist=agent_config.skills,
        env_vars=env_vars,
        credential_keys=credential_keys,
    )
    return Skills(loaders=[loader])


@dataclass(frozen=True)
class _SkillCommandDispatch:
    """Dispatch configuration for a skill command."""

    tool_name: str
    arg_mode: str = "raw"
    kind: str = "tool"


@dataclass(frozen=True)
class _SkillCommandSpec:
    """Resolved skill command metadata from SKILL.md."""

    name: str
    description: str
    source_path: Path
    user_invocable: bool
    disable_model_invocation: bool
    dispatch: _SkillCommandDispatch | None = None


@dataclass(frozen=True)
class _SkillListing:
    """Summary information for a discoverable skill."""

    name: str
    description: str
    path: Path
    origin: str


def resolve_skill_command_spec(  # noqa: C901
    skill_name: str,
    config: Config,
    agent_name: str,
    *,
    skill_roots: Sequence[Path] | None = None,
    env_vars: Mapping[str, str] | None = None,
    credential_keys: set[str] | None = None,
) -> _SkillCommandSpec | None:
    """Resolve command dispatch metadata for a skill, if enabled for the agent."""
    agent_config = config.get_agent(agent_name)
    requested_name = skill_name.strip()
    if not requested_name:
        return None

    allowlist = {name.strip().lower() for name in agent_config.skills}
    if not allowlist or requested_name.lower() not in allowlist:
        return None

    env_vars = os.environ if env_vars is None else env_vars
    credential_keys = credential_keys if credential_keys is not None else _collect_credential_keys()
    config_data = config.model_dump()

    resolved: _SkillCommandSpec | None = None
    roots = list(skill_roots or _get_default_skill_roots())
    for root in _unique_paths(roots):
        for skill_dir in _iter_skill_dirs(root):
            frontmatter = _read_skill_frontmatter(skill_dir / _SKILL_FILENAME)
            if frontmatter is None:
                continue

            name = frontmatter.get("name", skill_dir.name)
            description = frontmatter.get("description", "")
            if not isinstance(name, str) or not name.strip():
                logger.warning("Skill missing name", path=str(skill_dir))
                continue
            if not isinstance(description, str) or not description.strip():
                logger.warning("Skill missing description", name=name, path=str(skill_dir))
                continue

            if name.strip().lower() != requested_name.lower():
                continue

            metadata = _parse_metadata(frontmatter.get("metadata"), path=str(skill_dir))
            if metadata is None:
                continue

            skill = Skill(
                name=name.strip(),
                description=description.strip(),
                instructions="",
                source_path=str(skill_dir),
                metadata=metadata,
            )
            if not _is_skill_eligible(
                skill,
                config_data,
                env_vars=env_vars,
                credential_keys=credential_keys,
            ):
                continue

            user_invocable = _parse_frontmatter_bool(
                _get_frontmatter_value(frontmatter, "user-invocable", "user_invocable"),
                default=True,
            )
            disable_model_invocation = _parse_frontmatter_bool(
                _get_frontmatter_value(frontmatter, "disable-model-invocation", "disable_model_invocation"),
                default=False,
            )
            dispatch = _parse_command_dispatch(frontmatter, name.strip(), skill_dir)
            resolved = _SkillCommandSpec(
                name=name.strip(),
                description=description.strip(),
                source_path=skill_dir,
                user_invocable=user_invocable,
                disable_model_invocation=disable_model_invocation,
                dispatch=dispatch,
            )

    return resolved


def set_plugin_skill_roots(roots: Sequence[Path]) -> None:
    """Replace the plugin-provided skill roots."""
    global _PLUGIN_SKILL_ROOTS
    _PLUGIN_SKILL_ROOTS = _unique_paths(roots)
    clear_skill_cache()


def _get_plugin_skill_roots() -> list[Path]:
    """Return the current plugin-provided skill roots."""
    return list(_PLUGIN_SKILL_ROOTS)


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
            frontmatter = _read_skill_frontmatter(skill_dir / _SKILL_FILENAME)
            if frontmatter is None:
                continue

            name = frontmatter.get("name", skill_dir.name)
            description = frontmatter.get("description", "")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(description, str) or not description.strip():
                continue

            listing = _SkillListing(
                name=name.strip(),
                description=description.strip(),
                path=skill_dir / _SKILL_FILENAME,
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
    for skill_file in root.rglob(_SKILL_FILENAME):
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

    if (root / _SKILL_FILENAME).exists():
        return [root]

    skill_dirs = [
        path
        for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / _SKILL_FILENAME).exists()
    ]
    return sorted(skill_dirs)


def _read_skill_frontmatter(skill_path: Path) -> dict[str, Any] | None:
    try:
        content = skill_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to read skill file", path=str(skill_path), error=str(exc))
        return None

    match = _FRONTMATTER_PATTERN.match(content)
    if not match:
        logger.warning("Skill missing frontmatter", path=str(skill_path))
        return None

    frontmatter_text = match.group(1)
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except Exception as exc:
        logger.warning("Failed to parse skill frontmatter", path=str(skill_path), error=str(exc))
        return None

    if not isinstance(frontmatter, dict):
        logger.warning("Skill frontmatter must be a mapping", path=str(skill_path))
        return None

    return frontmatter


def _get_frontmatter_value(frontmatter: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in frontmatter:
            return frontmatter[key]
    return None


def _parse_frontmatter_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _parse_command_dispatch(
    frontmatter: Mapping[str, Any],
    skill_name: str,
    skill_dir: Path,
) -> _SkillCommandDispatch | None:
    dispatch_raw = _get_frontmatter_value(frontmatter, "command-dispatch", "command_dispatch")
    if not isinstance(dispatch_raw, str) or dispatch_raw.strip().lower() != "tool":
        return None

    tool_name = _get_frontmatter_value(frontmatter, "command-tool", "command_tool")
    if not isinstance(tool_name, str) or not tool_name.strip():
        logger.warning(
            "Skill command dispatch missing command-tool",
            skill=skill_name,
            path=str(skill_dir),
        )
        return None

    arg_mode_raw = _get_frontmatter_value(frontmatter, "command-arg-mode", "command_arg_mode")
    arg_mode = "raw"
    if isinstance(arg_mode_raw, str) and arg_mode_raw.strip():
        normalized = arg_mode_raw.strip().lower()
        if normalized != "raw":
            logger.warning(
                "Unknown skill command arg mode; defaulting to raw",
                skill=skill_name,
                arg_mode=arg_mode_raw,
                path=str(skill_dir),
            )
    return _SkillCommandDispatch(tool_name=tool_name.strip(), arg_mode=arg_mode)


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
    if not isinstance(skill.name, str) or not skill.name.strip():
        logger.warning("Skill missing name", path=str(skill.source_path))
        return None
    if not isinstance(skill.description, str) or not skill.description.strip():
        logger.warning("Skill missing description", name=skill.name, path=str(skill.source_path))
        return None

    skill.name = skill.name.strip()
    skill.description = skill.description.strip()

    metadata = _parse_metadata(skill.metadata, path=skill.source_path)
    if metadata is None:
        return None
    skill.metadata = metadata
    return skill


def _parse_metadata(raw: object, *, path: str) -> dict[str, Any] | None:
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

    if openclaw.get("always") is True:
        return True

    os_requirements = _normalize_str_list(openclaw.get("os"))
    if os_requirements and not _matches_current_os(os_requirements):
        return False

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


def _collect_credential_keys() -> set[str]:
    credentials_manager = get_credentials_manager()
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
