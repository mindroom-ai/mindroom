"""Skill discovery, parsing, and prompt injection utilities."""

from __future__ import annotations

import os
import platform
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import json5
import yaml

from .credentials import get_credentials_manager
from .logging_config import get_logger

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)

SKILL_FILENAME = "SKILL.md"

_OS_ALIASES = {
    "darwin": {"darwin", "macos", "mac", "osx"},
    "linux": {"linux"},
    "windows": {"windows", "win", "win32"},
}


@dataclass(frozen=True)
class Skill:
    """Parsed skill metadata from a SKILL.md file."""

    name: str
    description: str
    location: Path
    metadata: dict[str, Any]
    source: str


@dataclass
class _SkillCacheEntry:
    mtime: float
    skill: Skill | None


_SKILL_CACHE: dict[Path, _SkillCacheEntry] = {}
_PLUGIN_SKILL_ROOTS: list[Path] = []


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
    return _unique_paths([get_user_skills_dir(), *_PLUGIN_SKILL_ROOTS, get_bundled_skills_dir()])


def discover_skill_files(root: Path) -> list[Path]:
    """Discover SKILL.md files under a root directory."""
    if not root.exists() or not root.is_dir():
        return []
    return sorted(root.rglob(SKILL_FILENAME))


def load_skills(skill_roots: Sequence[Path] | None = None) -> list[Skill]:
    """Load skills from search roots with user precedence."""
    roots = list(skill_roots or get_default_skill_roots())
    skills_by_name: dict[str, Skill] = {}

    for root in roots:
        for path in discover_skill_files(root):
            skill = _load_skill_cached(path, source=str(root))
            if skill is None:
                continue
            if skill.name in skills_by_name:
                continue
            skills_by_name[skill.name] = skill

    return list(skills_by_name.values())


def get_agent_skills(
    agent_name: str,
    config: Config,
    *,
    skill_roots: Sequence[Path] | None = None,
    env_vars: Mapping[str, str] | None = None,
    credential_keys: set[str] | None = None,
) -> list[Skill]:
    """Return eligible skills for a specific agent."""
    agent_config = config.get_agent(agent_name)
    if not agent_config.skills:
        return []

    all_skills = load_skills(skill_roots)
    skills_by_name = {skill.name: skill for skill in all_skills}
    selected = [skills_by_name[name] for name in agent_config.skills if name in skills_by_name]
    return filter_skills_by_eligibility(
        selected,
        config,
        env_vars=env_vars,
        credential_keys=credential_keys,
    )


def filter_skills_by_eligibility(
    skills: Iterable[Skill],
    config: Config,
    *,
    env_vars: Mapping[str, str] | None = None,
    credential_keys: set[str] | None = None,
) -> list[Skill]:
    """Filter skills based on OpenClaw eligibility rules."""
    config_data = config.model_dump()
    env_vars = os.environ if env_vars is None else env_vars
    if credential_keys is None:
        credential_keys = _collect_credential_keys()

    return [
        skill
        for skill in skills
        if _is_skill_eligible(
            skill,
            config_data,
            env_vars=env_vars,
            credential_keys=credential_keys,
        )
    ]


def build_skills_prompt(skills: Sequence[Skill]) -> str:
    """Build a compact skills prompt section."""
    lines = [
        "If a skill is clearly applicable, read its SKILL.md with the file tool before acting.",
        "Read at most one skill up front.",
        "<available_skills>",
    ]
    for skill in skills:
        lines.extend(
            [
                "  <skill>",
                f"    <name>{skill.name}</name>",
                f"    <description>{skill.description}</description>",
                f"    <location>{skill.location}</location>",
                "  </skill>",
            ],
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def parse_skill_file(path: Path, *, source: str) -> Skill | None:
    """Parse a SKILL.md file into a Skill object."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read skill file", path=str(path), error=str(exc))
        return None

    frontmatter = parse_frontmatter(content)
    if frontmatter is None:
        logger.warning("Skill missing or invalid frontmatter", path=str(path))
        return None

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not name.strip():
        logger.warning("Skill missing name", path=str(path))
        return None
    if not isinstance(description, str) or not description.strip():
        logger.warning("Skill missing description", path=str(path))
        return None

    metadata = _parse_metadata(frontmatter.get("metadata"), path)
    if metadata is None:
        return None

    return Skill(
        name=name.strip(),
        description=description.strip(),
        location=path.resolve(),
        metadata=metadata,
        source=source,
    )


def parse_frontmatter(content: str) -> dict[str, Any] | None:
    """Parse YAML frontmatter from a markdown file."""
    lines = content.splitlines()
    if not lines:
        return None
    if lines[0].strip() != "---":
        return None

    end_index = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            end_index = idx
            break
    if end_index is None:
        return None

    frontmatter_text = "\n".join(lines[1:end_index])
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict):
        return None
    return data


def _load_skill_cached(path: Path, *, source: str) -> Skill | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _SKILL_CACHE.pop(path, None)
        return None

    entry = _SKILL_CACHE.get(path)
    if entry and entry.mtime == mtime:
        return entry.skill

    skill = parse_skill_file(path, source=source)
    _SKILL_CACHE[path] = _SkillCacheEntry(mtime=mtime, skill=skill)
    return skill


def _parse_metadata(raw: object, path: Path) -> dict[str, Any] | None:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        logger.warning("Skill metadata must be a JSON5 string", path=str(path))
        return None
    try:
        parsed = json5.loads(raw)
    except Exception as exc:
        logger.warning("Failed to parse skill metadata JSON5", path=str(path), error=str(exc))
        return None
    if not isinstance(parsed, dict):
        logger.warning("Skill metadata JSON5 must be an object", path=str(path))
        return None
    return parsed


def _is_skill_eligible(
    skill: Skill,
    config_data: Mapping[str, Any],
    *,
    env_vars: Mapping[str, str],
    credential_keys: set[str],
) -> bool:
    openclaw = skill.metadata.get("openclaw")
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
