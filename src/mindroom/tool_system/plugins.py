"""Plugin loader for Mindroom tools and skills."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from importlib import util
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

from mindroom.config.plugin import PluginEntryConfig  # noqa: TC001
from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.hooks.decorators import iter_module_hooks
from mindroom.logging_config import get_logger
from mindroom.tool_system.plugin_identity import validate_plugin_name
from mindroom.tool_system.skills import set_plugin_skill_roots

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.hooks.types import HookCallback
    from mindroom.tool_system.metadata import ToolMetadata

logger = get_logger(__name__)

_PLUGIN_MANIFEST = "mindroom.plugin.json"
_REPO_ROOT = Path(__file__).resolve().parents[3]


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


@dataclass(frozen=True)
class _Plugin:
    """Loaded plugin details for the active config snapshot."""

    name: str
    root: Path
    manifest_path: Path
    entry_config: PluginEntryConfig
    plugin_order: int
    tools_module_path: Path | None
    hooks_module_path: Path | None
    skill_dirs: list[Path]
    discovered_hooks: tuple[HookCallback, ...]


@dataclass
class _ModuleCacheEntry:
    mtime: float
    module_name: str
    module: ModuleType


_PLUGIN_CACHE: dict[Path, _PluginCacheEntry] = {}
_MODULE_IMPORT_CACHE: dict[Path, _ModuleCacheEntry] = {}
_WARNED_PLUGIN_MESSAGES: set[tuple[str, Path]] = set()


def _hook_display_name(callback: HookCallback) -> str:
    return cast("Any", callback).__name__


def _warn_once(message: str, *, path: Path) -> None:
    """Emit one plugin-path warning once per process for the same message/path pair."""
    warning_key = (message, path)
    if warning_key in _WARNED_PLUGIN_MESSAGES:
        return
    _WARNED_PLUGIN_MESSAGES.add(warning_key)
    logger.warning(message, path=str(path))


def _sync_loaded_plugin_tools(plugins: list[_Plugin]) -> None:
    """Remove plugin tool registrations for plugins no longer present in config."""
    from mindroom.tool_system.metadata import synchronize_plugin_tools  # noqa: PLC0415

    active_tool_modules = [
        (plugin.name, _module_name(plugin.name, plugin.root, plugin.tools_module_path))
        for plugin in plugins
        if plugin.tools_module_path is not None
    ]
    synchronize_plugin_tools(active_tool_modules)


def load_plugins(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    set_skill_roots: bool = True,
) -> list[_Plugin]:
    """Load plugins from config and register their tools and skills."""
    import mindroom.tools  # noqa: F401, PLC0415
    from mindroom.tool_system.metadata import (  # noqa: PLC0415
        _capture_tool_registry_snapshot,
        _restore_tool_registry_snapshot,
        locked_tool_registry_state,
    )

    with locked_tool_registry_state():
        plugin_entries = config.plugins
        if not plugin_entries:
            _sync_loaded_plugin_tools([])
            if set_skill_roots:
                set_plugin_skill_roots([])
            return []
        plugins: list[_Plugin] = []
        skill_roots: list[Path] = []
        plugin_bases = _collect_plugin_bases(
            plugin_entries,
            runtime_paths,
            skip_broken_plugins=True,
        )
        snapshot = _capture_tool_registry_snapshot()
        try:
            _reject_duplicate_plugin_manifest_names(plugin_bases)

            for plugin_base, plugin_entry, plugin_order in plugin_bases:
                plugin_snapshot = _capture_tool_registry_snapshot()
                try:
                    plugin = _materialize_plugin(plugin_base, plugin_entry, plugin_order)
                except Exception as exc:
                    _restore_tool_registry_snapshot(plugin_snapshot)
                    _log_skipped_plugin_entry(plugin_entry.path, plugin_base.root, exc)
                    continue
                plugins.append(plugin)
                skill_roots.extend(plugin.skill_dirs)

            if plugins:
                logger.info("Loaded plugins", plugins=[plugin.name for plugin in plugins])

            _sync_loaded_plugin_tools(plugins)

            if set_skill_roots:
                set_plugin_skill_roots(skill_roots)
        except Exception:
            _restore_tool_registry_snapshot(snapshot)
            raise

        return plugins


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

    log_kwargs: dict[str, object] = {"plugin_path": plugin_path}
    if root is not None:
        log_kwargs["path"] = str(root)
    logger.exception("Failed to load plugin, skipping", **log_kwargs)


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


def _materialize_plugin(
    plugin: _PluginBase,
    entry_config: PluginEntryConfig,
    plugin_order: int,
) -> _Plugin:
    tools_module = _load_plugin_module(plugin.name, plugin.root, plugin.tools_module_path, kind="tools")
    hooks_module_path = plugin.hooks_module_path or plugin.tools_module_path
    hooks_module = (
        _load_plugin_module(plugin.name, plugin.root, hooks_module_path, kind="hooks") if hooks_module_path else None
    )
    if hooks_module is None and plugin.hooks_module_path is None:
        hooks_module = tools_module
    discovered_hooks = tuple(iter_module_hooks(hooks_module)) if hooks_module is not None else ()
    if discovered_hooks:
        logger.info(
            "Discovered plugin hooks",
            plugin_name=plugin.name,
            hook_names=[_hook_display_name(hook) for hook in discovered_hooks],
        )
    return _Plugin(
        name=plugin.name,
        root=plugin.root,
        manifest_path=plugin.manifest_path,
        entry_config=entry_config,
        plugin_order=plugin_order,
        tools_module_path=plugin.tools_module_path,
        hooks_module_path=plugin.hooks_module_path,
        skill_dirs=plugin.skill_dirs,
        discovered_hooks=discovered_hooks,
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


def _prepare_plugin_tool_module_reload(
    module_name: str,
    cached: _ModuleCacheEntry | None,
) -> dict[str, dict[str, ToolMetadata]]:
    """Snapshot one tool module's cached registrations before reload."""
    from mindroom.tool_system.metadata import (  # noqa: PLC0415
        clear_plugin_tool_registrations,
        snapshot_plugin_tool_registrations,
    )

    previous_registrations_by_module_name: dict[str, dict[str, ToolMetadata]] = {}
    for candidate_module_name in {module_name, cached.module_name if cached is not None else None}:
        if candidate_module_name is None:
            continue
        previous_registrations_by_module_name[candidate_module_name] = snapshot_plugin_tool_registrations(
            candidate_module_name,
        )
        clear_plugin_tool_registrations(candidate_module_name)
    return previous_registrations_by_module_name


def _restore_failed_plugin_tool_module_reload(
    module_path: Path,
    module_name: str,
    cached: _ModuleCacheEntry | None,
    previous_registrations_by_module_name: dict[str, dict[str, ToolMetadata]],
) -> None:
    """Restore cached tool registrations and module imports after one failed reload."""
    from mindroom.tool_system.metadata import restore_plugin_tool_registrations  # noqa: PLC0415

    sys.modules.pop(module_name, None)
    for restored_module_name, registrations in previous_registrations_by_module_name.items():
        restore_plugin_tool_registrations(restored_module_name, registrations)
    if cached is not None:
        _MODULE_IMPORT_CACHE[module_path] = cached
        sys.modules[cached.module_name] = cached.module
    else:
        _MODULE_IMPORT_CACHE.pop(module_path, None)


def _load_plugin_module(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path | None,
    *,
    kind: str,
) -> ModuleType | None:
    if module_path is None:
        return None
    try:
        mtime = module_path.stat().st_mtime
    except OSError as exc:
        msg = f"Failed to stat plugin {kind} module {module_path}: {exc}"
        logger.exception("Failed to stat plugin module", path=str(module_path), kind=kind, error=str(exc))
        raise PluginValidationError(msg) from exc

    module_name = _module_name(plugin_name, plugin_root, module_path)
    cached = _MODULE_IMPORT_CACHE.get(module_path)
    if cached is not None and cached.mtime == mtime and cached.module_name == module_name:
        return cached.module

    previous_registrations_by_module_name = (
        _prepare_plugin_tool_module_reload(module_name, cached) if kind == "tools" else {}
    )

    if cached is not None and cached.module_name != module_name:
        sys.modules.pop(cached.module_name, None)

    previous_packages = _snapshot_plugin_package_chain(plugin_name, plugin_root, module_path)
    _install_plugin_package_chain(plugin_name, plugin_root, module_path)
    spec = util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        _restore_plugin_package_chain(previous_packages)
        msg = f"Failed to load plugin {kind} module: {module_path}"
        logger.error("Failed to load plugin module", path=str(module_path), kind=kind)
        raise PluginValidationError(msg)

    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        if kind == "tools":
            from mindroom.tool_system.metadata import _scoped_plugin_registration_owner  # noqa: PLC0415

            with _scoped_plugin_registration_owner(module_name):
                spec.loader.exec_module(module)
        else:
            spec.loader.exec_module(module)
    except Exception as exc:
        if kind == "tools":
            _restore_failed_plugin_tool_module_reload(
                module_path,
                module_name,
                cached,
                previous_registrations_by_module_name,
            )
        else:
            sys.modules.pop(module_name, None)
            if cached is not None:
                _MODULE_IMPORT_CACHE[module_path] = cached
                sys.modules[cached.module_name] = cached.module
            else:
                _MODULE_IMPORT_CACHE.pop(module_path, None)
        _restore_plugin_package_chain(previous_packages)
        msg = f"Plugin {kind} module execution failed for {module_path}: {exc}"
        logger.exception("Plugin module execution failed", path=str(module_path), kind=kind, error=str(exc))
        raise PluginValidationError(msg) from exc

    _MODULE_IMPORT_CACHE[module_path] = _ModuleCacheEntry(mtime=mtime, module_name=module_name, module=module)
    return module


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
