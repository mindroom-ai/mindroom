"""Skill integration built on Agno skills with OpenClaw-compatible metadata."""

from __future__ import annotations

import os
import platform
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import json5
import yaml
from agno.skills import LocalSkills, Skills
from agno.skills.loaders import SkillLoader
from agno.skills.skill import Skill

from .credentials import get_credentials_manager
from .logging_config import get_logger

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)

SKILL_FILENAME = "SKILL.md"
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

_OS_ALIASES = {
    "darwin": {"darwin", "macos", "mac", "osx"},
    "linux": {"linux"},
    "windows": {"windows", "win", "win32"},
}

_PLUGIN_SKILL_ROOTS: list[Path] = []


@dataclass
class MindroomSkillsLoader(SkillLoader):
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

    roots = list(skill_roots or get_default_skill_roots())
    loader = MindroomSkillsLoader(
        roots=roots,
        config=config,
        allowlist=agent_config.skills,
        env_vars=env_vars,
        credential_keys=credential_keys,
    )
    return Skills(loaders=[loader])


@dataclass(frozen=True)
class SkillCommandDispatch:
    """Dispatch configuration for a skill command."""

    tool_name: str
    arg_mode: str = "raw"
    kind: str = "tool"


@dataclass(frozen=True)
class SkillCommandSpec:
    """Resolved skill command metadata from SKILL.md."""

    name: str
    description: str
    source_path: Path
    user_invocable: bool
    disable_model_invocation: bool
    dispatch: SkillCommandDispatch | None = None


def resolve_skill_command_spec(  # noqa: C901
    skill_name: str,
    config: Config,
    agent_name: str,
    *,
    skill_roots: Sequence[Path] | None = None,
    env_vars: Mapping[str, str] | None = None,
    credential_keys: set[str] | None = None,
) -> SkillCommandSpec | None:
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

    resolved: SkillCommandSpec | None = None
    roots = list(skill_roots or get_default_skill_roots())
    for root in _unique_paths(roots):
        for skill_dir in _iter_skill_dirs(root):
            frontmatter = _read_skill_frontmatter(skill_dir / SKILL_FILENAME)
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
            resolved = SkillCommandSpec(
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


def get_plugin_skill_roots() -> list[Path]:
    """Return the current plugin-provided skill roots."""
    return list(_PLUGIN_SKILL_ROOTS)


def get_user_skills_dir() -> Path:
    """Return the user-managed skills directory."""
    return Path.home() / ".mindroom" / "skills"


def get_bundled_skills_dir() -> Path:
    """Return the bundled skills directory from the repo root."""
    return Path(__file__).resolve().parents[2] / "skills"


def get_default_skill_roots() -> list[Path]:
    """Return the default skill search roots in precedence order."""
    return _unique_paths([get_bundled_skills_dir(), *_PLUGIN_SKILL_ROOTS, get_user_skills_dir()])


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
) -> SkillCommandDispatch | None:
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
    return SkillCommandDispatch(tool_name=tool_name.strip(), arg_mode=arg_mode)


def _load_root_skills(root: Path) -> list[Skill]:
    if not root.exists() or not root.is_dir():
        return []

    loader = LocalSkills(str(root), validate=False)
    try:
        return loader.load()
    except Exception as exc:
        logger.warning("Failed to load skills", path=str(root), error=str(exc))
        return []


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
        return raw
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
    if isinstance(requires, dict):
        env_requirements = _normalize_str_list(requires.get("env"))
        if env_requirements and not _env_requirements_met(env_requirements, env_vars, credential_keys):
            return False

        config_requirements = _normalize_str_list(requires.get("config"))
        if config_requirements and not _config_requirements_met(config_requirements, config_data):
            return False

    return True


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


def _config_requirements_met(requirements: Sequence[str], config_data: Mapping[str, Any]) -> bool:
    return all(_config_path_truthy(config_data, requirement) for requirement in requirements)


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
