"""Plugin loader for Mindroom tools and skills."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from importlib import util
from pathlib import Path
from typing import TYPE_CHECKING

from .constants import resolve_config_relative_path
from .logging_config import get_logger
from .skills import set_plugin_skill_roots

if TYPE_CHECKING:
    from .config.main import Config

logger = get_logger(__name__)

_PLUGIN_MANIFEST = "mindroom.plugin.json"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _PluginManifest:
    """Validated plugin manifest data."""

    name: str
    tools_module: str | None
    skills: list[str]


@dataclass(frozen=True)
class _Plugin:
    """Loaded plugin details."""

    name: str
    root: Path
    manifest_path: Path
    tools_module_path: Path | None
    skill_dirs: list[Path]


@dataclass
class _PluginCacheEntry:
    manifest_mtime: float
    plugin: _Plugin


_PLUGIN_CACHE: dict[Path, _PluginCacheEntry] = {}
_TOOL_MODULE_CACHE: dict[Path, float] = {}


def load_plugins(config: Config, *, config_path: Path | None = None) -> list[_Plugin]:
    """Load plugins from config and register their tools and skills."""
    plugin_paths = getattr(config, "plugins", None)
    if not plugin_paths:
        set_plugin_skill_roots([])
        return []

    plugins: list[_Plugin] = []
    skill_roots: list[Path] = []

    for plugin_path in plugin_paths:
        root = _resolve_plugin_root(plugin_path, config_path)
        plugin = _load_plugin(root)
        if plugin is None:
            continue
        plugins.append(plugin)
        skill_roots.extend(plugin.skill_dirs)

    if plugins:
        logger.info("Loaded plugins", plugins=[plugin.name for plugin in plugins])

    set_plugin_skill_roots(skill_roots)
    return plugins


def _resolve_plugin_root(plugin_path: str, config_path: Path | None) -> Path:
    relative = resolve_config_relative_path(plugin_path, config_path=config_path)
    if relative.exists():
        return relative

    module_root = _resolve_python_plugin_root(plugin_path)
    if module_root is not None:
        return module_root

    return relative


def _resolve_python_plugin_root(plugin_path: str) -> Path | None:
    parsed = _parse_python_plugin_spec(plugin_path)
    if parsed is None:
        return None

    module_name, subpath, explicit = parsed
    spec = util.find_spec(module_name)
    if spec is None:
        if explicit:
            logger.warning("Plugin module not found", module=module_name, spec=plugin_path)
        return None

    if spec.submodule_search_locations:
        root = Path(next(iter(spec.submodule_search_locations)))
    elif spec.origin:
        root = Path(spec.origin).parent
    else:
        if explicit:
            logger.warning("Plugin module has no filesystem location", module=module_name)
        return None

    resolved_root = (root / subpath).resolve() if subpath else root.resolve()
    if not resolved_root.exists() or not resolved_root.is_dir():
        if explicit:
            logger.warning("Plugin module path is not a directory", module=module_name, path=str(resolved_root))
        return None

    return resolved_root


def _parse_python_plugin_spec(plugin_path: str) -> tuple[str, str | None, bool] | None:
    prefixes = ("python:", "pkg:", "module:")
    for prefix in prefixes:
        if plugin_path.startswith(prefix):
            spec = plugin_path[len(prefix) :]
            explicit = True
            break
    else:
        spec = plugin_path
        explicit = False
        if "/" in spec or spec.startswith("."):
            return None

    parts = spec.split(":", 1)
    module_name = parts[0].strip()
    if not module_name:
        return None
    subpath = parts[1].strip() if len(parts) > 1 else None
    if subpath == "":
        subpath = None
    return module_name, subpath, explicit


def _load_plugin(root: Path) -> _Plugin | None:
    if not root.exists() or not root.is_dir():
        logger.warning("Plugin path does not exist", path=str(root))
        return None

    manifest_path = root / _PLUGIN_MANIFEST
    if not manifest_path.exists():
        logger.warning("Plugin manifest missing", path=str(manifest_path))
        return None

    if not root.is_relative_to(_REPO_ROOT):
        logger.warning("Loading non-bundled plugin", path=str(root))

    try:
        manifest_mtime = manifest_path.stat().st_mtime
    except OSError as exc:
        logger.warning("Failed to stat plugin manifest", path=str(manifest_path), error=str(exc))
        return None

    cached = _PLUGIN_CACHE.get(manifest_path)
    if cached and cached.manifest_mtime == manifest_mtime:
        _load_tools_module(cached.plugin)
        return cached.plugin

    manifest = _parse_manifest(manifest_path)
    if manifest is None:
        return None

    tools_module_path = _resolve_tools_module(root, manifest.tools_module)
    skill_dirs = _resolve_skill_dirs(root, manifest.skills)

    plugin = _Plugin(
        name=manifest.name,
        root=root,
        manifest_path=manifest_path,
        tools_module_path=tools_module_path,
        skill_dirs=skill_dirs,
    )

    _PLUGIN_CACHE[manifest_path] = _PluginCacheEntry(manifest_mtime=manifest_mtime, plugin=plugin)

    _load_tools_module(plugin)
    return plugin


def _parse_manifest(path: Path) -> _PluginManifest | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse plugin manifest", path=str(path), error=str(exc))
        return None

    if not isinstance(data, dict):
        logger.warning("Plugin manifest must be a JSON object", path=str(path))
        return None

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning("Plugin manifest missing name", path=str(path))
        return None

    tools_module = data.get("tools_module")
    if tools_module is not None and not isinstance(tools_module, str):
        logger.warning("Plugin tools_module must be a string", path=str(path))
        return None

    raw_skills = data.get("skills", [])
    if raw_skills is None:
        raw_skills = []
    if not isinstance(raw_skills, list) or any(not isinstance(item, str) for item in raw_skills):
        logger.warning("Plugin skills must be a list of strings", path=str(path))
        return None

    return _PluginManifest(name=name.strip(), tools_module=tools_module, skills=raw_skills)


def _resolve_tools_module(root: Path, tools_module: str | None) -> Path | None:
    if not tools_module:
        return None
    module_path = (root / tools_module).resolve()
    if not module_path.exists():
        logger.warning("Plugin tools module not found", path=str(module_path))
        return None
    return module_path


def _resolve_skill_dirs(root: Path, skills: list[str]) -> list[Path]:
    skill_dirs: list[Path] = []
    for relative_path in skills:
        path = (root / relative_path).resolve()
        if not path.exists() or not path.is_dir():
            logger.warning("Plugin skill path is not a directory", path=str(path))
            continue
        skill_dirs.append(path)
    return skill_dirs


def _load_tools_module(plugin: _Plugin) -> None:
    if plugin.tools_module_path is None:
        return

    module_path = plugin.tools_module_path
    try:
        mtime = module_path.stat().st_mtime
    except OSError as exc:
        logger.warning("Failed to stat plugin tools module", path=str(module_path), error=str(exc))
        return

    cached_mtime = _TOOL_MODULE_CACHE.get(module_path)
    if cached_mtime == mtime:
        return

    module_name = _module_name(plugin.name, module_path)
    spec = util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        logger.warning("Failed to load plugin tools module", path=str(module_path))
        return

    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        logger.warning("Plugin tools module execution failed", path=str(module_path), error=str(exc))
        return

    _TOOL_MODULE_CACHE[module_path] = mtime


def _module_name(plugin_name: str, module_path: Path) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", plugin_name).strip("_") or "plugin"
    digest = abs(hash(str(module_path)))
    return f"mindroom_plugin_{slug}_{digest}"
