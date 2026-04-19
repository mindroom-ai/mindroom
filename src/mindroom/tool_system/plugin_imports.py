"""Low-level plugin manifest and import helpers for the tool system."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from importlib import util
from pathlib import Path
from types import ModuleType

from mindroom.config.plugin import PluginEntryConfig  # noqa: TC001
from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.logging_config import get_logger
from mindroom.tool_system.plugin_identity import validate_plugin_name

logger = get_logger(__name__)

_PLUGIN_MANIFEST = "mindroom.plugin.json"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_WARNED_PLUGIN_MESSAGES: set[tuple[str, Path]] = set()


class PluginValidationError(ValueError):
    """Raised when plugin config or plugin runtime validation fails for authored config."""


@dataclass(frozen=True)
class _PluginManifest:
    """Validated plugin manifest data."""

    name: str
    tools_module: str | None
    hooks_module: str | None
    skills: list[str]


@dataclass(frozen=True)
class _PluginBase:
    """Loaded plugin details that depend only on the manifest."""

    name: str
    root: Path
    manifest_path: Path
    tools_module_path: Path | None
    hooks_module_path: Path | None
    skill_dirs: list[Path]


@dataclass
class _PluginCacheEntry:
    manifest_mtime: float
    manifest: _PluginManifest


@dataclass
class _ModuleCacheEntry:
    mtime: float
    module_name: str
    module: ModuleType


_PLUGIN_CACHE: dict[Path, _PluginCacheEntry] = {}
_MODULE_IMPORT_CACHE: dict[Path, _ModuleCacheEntry] = {}


def _warn_once(message: str, *, path: Path) -> None:
    """Emit one plugin-path warning once per process for the same message/path pair."""
    warning_key = (message, path)
    if warning_key in _WARNED_PLUGIN_MESSAGES:
        return
    _WARNED_PLUGIN_MESSAGES.add(warning_key)
    logger.warning(message, path=str(path))


def _collect_plugin_bases(
    plugin_entries: list[PluginEntryConfig],
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool,
) -> list[tuple[_PluginBase, PluginEntryConfig, int]]:
    """Resolve plugin roots/manifests for one config snapshot."""
    plugin_bases: list[tuple[_PluginBase, PluginEntryConfig, int]] = []
    for plugin_order, plugin_entry in enumerate(plugin_entries):
        if not plugin_entry.enabled:
            continue

        root: Path | None = None
        try:
            root = _resolve_plugin_root(plugin_entry.path, runtime_paths)
            plugin_base = _load_plugin_base(root)
        except Exception as exc:
            if not skip_broken_plugins:
                raise
            _log_skipped_plugin_entry(plugin_entry.path, root, exc)
            continue

        plugin_bases.append((plugin_base, plugin_entry, plugin_order))
    return plugin_bases


def _log_skipped_plugin_entry(
    plugin_path: str,
    root: Path | None,
    exc: Exception,
) -> None:
    """Log one broken plugin entry without aborting the rest of startup."""
    if root is not None and (not root.exists() or not root.is_dir()):
        _warn_once("Plugin path does not exist, skipping", path=root)
        return

    if isinstance(exc, PluginValidationError) and str(exc).startswith(
        "Configured plugin module could not be resolved:",
    ):
        logger.warning("Plugin module could not be resolved, skipping", spec=plugin_path)
        return

    log_kwargs: dict[str, object] = {"plugin_path": plugin_path, "error": str(exc)}
    if root is not None:
        log_kwargs["path"] = str(root)
    logger.warning("Failed to load plugin, skipping", **log_kwargs)


def _reject_duplicate_plugin_manifest_names(
    plugin_bases: list[tuple[_PluginBase, PluginEntryConfig, int]],
) -> None:
    """Fail plugin loading when configured manifests reuse the same plugin name."""
    manifest_paths_by_name: dict[str, list[Path]] = {}
    for plugin_base, _, _ in plugin_bases:
        manifest_paths_by_name.setdefault(plugin_base.name, []).append(plugin_base.manifest_path)

    duplicates = {name: paths for name, paths in manifest_paths_by_name.items() if len(paths) > 1}
    if not duplicates:
        return

    duplicate_descriptions = ", ".join(
        f"{name}: {', '.join(str(path) for path in paths)}" for name, paths in sorted(duplicates.items())
    )
    logger.error("Duplicate plugin manifest names configured", duplicates=duplicates)
    msg = f"Duplicate plugin manifest names configured: {duplicate_descriptions}"
    raise PluginValidationError(msg)


def _resolve_plugin_root(plugin_path: str, runtime_paths: RuntimePaths) -> Path:
    parsed_python_spec = _parse_python_plugin_spec(plugin_path)
    if parsed_python_spec is not None and parsed_python_spec[2]:
        module_root = _resolve_python_plugin_root(plugin_path)
        if module_root is not None:
            return module_root
        msg = f"Configured plugin module could not be resolved: {plugin_path}"
        raise PluginValidationError(msg)

    relative = resolve_config_relative_path(plugin_path, runtime_paths)
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

    module_name, subpath, _explicit = parsed
    spec = util.find_spec(module_name)
    if spec is None:
        return None

    if spec.submodule_search_locations:
        root = Path(next(iter(spec.submodule_search_locations)))
    elif spec.origin:
        root = Path(spec.origin).parent
    else:
        return None

    resolved_root = (root / subpath).resolve() if subpath else root.resolve()
    if not resolved_root.exists() or not resolved_root.is_dir():
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


def _load_plugin_base(root: Path) -> _PluginBase:
    if not root.exists() or not root.is_dir():
        msg = f"Configured plugin path does not exist: {root}"
        raise PluginValidationError(msg)

    manifest_path = root / _PLUGIN_MANIFEST
    if not manifest_path.exists():
        msg = f"Plugin manifest missing: {manifest_path}"
        logger.error("Plugin manifest missing", path=str(manifest_path))
        raise PluginValidationError(msg)

    if not root.is_relative_to(_REPO_ROOT):
        _warn_once("Loading non-bundled plugin", path=root)

    try:
        manifest_mtime = manifest_path.stat().st_mtime
    except OSError as exc:
        msg = f"Failed to stat plugin manifest {manifest_path}: {exc}"
        logger.exception("Failed to stat plugin manifest", path=str(manifest_path), error=str(exc))
        raise PluginValidationError(msg) from exc

    cached = _PLUGIN_CACHE.get(manifest_path)
    if cached and cached.manifest_mtime == manifest_mtime:
        manifest = cached.manifest
    else:
        manifest = _parse_manifest(manifest_path)
        _PLUGIN_CACHE[manifest_path] = _PluginCacheEntry(manifest_mtime=manifest_mtime, manifest=manifest)

    tools_module_path = _resolve_module_path(root, manifest.tools_module, kind="tools")
    hooks_module_path = _resolve_module_path(root, manifest.hooks_module, kind="hooks")
    skill_dirs = _resolve_skill_dirs(root, manifest.skills)

    return _PluginBase(
        name=manifest.name,
        root=root,
        manifest_path=manifest_path,
        tools_module_path=tools_module_path,
        hooks_module_path=hooks_module_path,
        skill_dirs=skill_dirs,
    )


def _parse_manifest(path: Path) -> _PluginManifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Failed to parse plugin manifest {path}: {exc}"
        logger.exception("Failed to parse plugin manifest", path=str(path), error=str(exc))
        raise PluginValidationError(msg) from exc

    if not isinstance(data, dict):
        msg = f"Plugin manifest must be a JSON object: {path}"
        logger.error("Plugin manifest must be a JSON object", path=str(path))
        raise PluginValidationError(msg)

    name = data.get("name")
    if not isinstance(name, str):
        msg = f"Plugin manifest missing valid string name ({path})"
        logger.error("Plugin manifest missing valid string name", path=str(path))
        raise PluginValidationError(msg)
    try:
        normalized_name = validate_plugin_name(name)
    except ValueError as exc:
        logger.error("Invalid plugin manifest name", path=str(path), plugin_name=name, error=str(exc))  # noqa: TRY400
        msg = f"{exc} ({path})"
        raise PluginValidationError(msg) from exc

    tools_module = data.get("tools_module")
    if tools_module is not None and not isinstance(tools_module, str):
        msg = f"Plugin tools_module must be a string: {path}"
        logger.error("Plugin tools_module must be a string", path=str(path))
        raise PluginValidationError(msg)

    hooks_module = data.get("hooks_module")
    if hooks_module is not None and not isinstance(hooks_module, str):
        msg = f"Plugin hooks_module must be a string: {path}"
        logger.error("Plugin hooks_module must be a string", path=str(path))
        raise PluginValidationError(msg)

    raw_skills = data.get("skills", [])
    if raw_skills is None:
        raw_skills = []
    if not isinstance(raw_skills, list) or any(not isinstance(item, str) for item in raw_skills):
        msg = f"Plugin skills must be a list of strings: {path}"
        logger.error("Plugin skills must be a list of strings", path=str(path))
        raise PluginValidationError(msg)

    return _PluginManifest(
        name=normalized_name,
        tools_module=tools_module,
        hooks_module=hooks_module,
        skills=raw_skills,
    )


def _resolve_module_path(root: Path, module_path: str | None, *, kind: str) -> Path | None:
    if not module_path:
        return None
    resolved_path = (root / module_path).resolve()
    if not resolved_path.exists() or not resolved_path.is_file():
        msg = f"Plugin {kind} module not found: {resolved_path}"
        logger.error("Plugin module not found", kind=kind, path=str(resolved_path))
        raise PluginValidationError(msg)
    return resolved_path


def _resolve_skill_dirs(root: Path, skills: list[str]) -> list[Path]:
    skill_dirs: list[Path] = []
    for relative_path in skills:
        path = (root / relative_path).resolve()
        if not path.exists() or not path.is_dir():
            msg = f"Plugin skill path is not a directory: {path}"
            logger.error("Plugin skill path is not a directory", path=str(path))
            raise PluginValidationError(msg)
        skill_dirs.append(path)
    return skill_dirs


def _plugin_slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_") or "plugin"


def _plugin_package_name(plugin_name: str, plugin_root: Path) -> str:
    digest = abs(hash(str(plugin_root)))
    return f"mindroom_plugin_{_plugin_slug(plugin_name)}_{digest}"


def _relative_module_name(plugin_root: Path, module_path: Path) -> str:
    relative_path = module_path.relative_to(plugin_root).with_suffix("")
    return ".".join(_plugin_slug(part) for part in relative_path.parts)


def _module_name(plugin_name: str, plugin_root: Path, module_path: Path) -> str:
    return f"{_plugin_package_name(plugin_name, plugin_root)}.{_relative_module_name(plugin_root, module_path)}"


def _package_chain_names(plugin_name: str, plugin_root: Path, module_path: Path) -> list[tuple[str, Path]]:
    package_name = _plugin_package_name(plugin_name, plugin_root)
    chain = [(package_name, plugin_root)]
    package_root = plugin_root
    relative_parent = module_path.relative_to(plugin_root).parent
    parent_package_name = package_name
    for part in relative_parent.parts:
        package_root /= part
        parent_package_name = f"{parent_package_name}.{_plugin_slug(part)}"
        chain.append((parent_package_name, package_root))
    return chain


def _snapshot_plugin_package_chain(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path,
) -> dict[str, ModuleType | None]:
    return {
        package_name: sys.modules.get(package_name)
        for package_name, _ in _package_chain_names(plugin_name, plugin_root, module_path)
    }


def _install_plugin_package_chain(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path,
) -> None:
    for package_name, package_root in _package_chain_names(plugin_name, plugin_root, module_path):
        if package_name in sys.modules:
            continue
        package = ModuleType(package_name)
        package.__file__ = str(package_root / "__init__.py")
        package.__package__ = package_name
        package.__path__ = [str(package_root)]
        sys.modules[package_name] = package


def _restore_plugin_package_chain(previous_packages: dict[str, ModuleType | None]) -> None:
    for package_name, previous_module in previous_packages.items():
        if previous_module is None:
            sys.modules.pop(package_name, None)
        else:
            sys.modules[package_name] = previous_module
