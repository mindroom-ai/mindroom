"""Plugin loader for Mindroom tools and skills."""

from __future__ import annotations

import asyncio
import sys
import tokenize
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from mindroom.config.plugin import PluginEntryConfig  # noqa: TC001
from mindroom.hooks import HookRegistry, iter_module_hooks
from mindroom.logging_config import get_logger
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.registry_state import (
    capture_tool_registry_snapshot,
    clear_plugin_tool_registrations,
    locked_tool_registry_state,
    restore_plugin_tool_registrations,
    restore_tool_registry_snapshot,
    scoped_plugin_registration_owner,
    snapshot_plugin_tool_registrations,
    synchronize_plugin_tools,
)
from mindroom.tool_system.skills import get_plugin_skill_roots, set_plugin_skill_roots

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookCallback
    from mindroom.tool_system.metadata import ToolMetadata

logger = get_logger(__name__)

_PluginValidationError = plugin_imports.PluginValidationError
_CONFIGURED_PLUGIN_ROOT_CACHE_MAX_SIZE = 128


class _ConfiguredPluginRootCacheKey(NamedTuple):
    plugin_entries: tuple[tuple[str, bool], ...]
    config_path: str
    config_dir: str
    storage_root: str
    sys_path: tuple[str, ...]


_CONFIGURED_PLUGIN_ROOT_CACHE: dict[_ConfiguredPluginRootCacheKey, tuple[Path, ...]] = {}


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


@dataclass(frozen=True, slots=True)
class PluginReloadResult:
    """Fresh plugin snapshot built from the current config."""

    hook_registry: HookRegistry
    active_plugin_names: tuple[str, ...]
    cancelled_task_count: int


@dataclass(frozen=True, slots=True)
class _PreparedPluginReload:
    """Prepared plugin runtime state that is safe to apply later."""

    hook_registry: HookRegistry
    active_plugin_names: tuple[str, ...]
    tool_registry_snapshot: Any
    plugin_skill_roots: tuple[Path, ...]


def _hook_display_name(callback: HookCallback) -> str:
    return cast("Any", callback).__name__


def _sync_loaded_plugin_tools(plugins: list[_Plugin]) -> None:
    """Remove plugin tool registrations for plugins no longer present in config."""
    active_tool_modules = [
        (plugin.name, plugin_imports._module_name(plugin.name, plugin.root, plugin.tools_module_path))
        for plugin in plugins
        if plugin.tools_module_path is not None
    ]
    synchronize_plugin_tools(active_tool_modules)


def deactivate_plugins() -> PluginReloadResult:
    """Clear live plugin-derived tools, skills, and hooks."""
    with locked_tool_registry_state():
        _sync_loaded_plugin_tools([])
        set_plugin_skill_roots([])
        _clear_oauth_provider_cache_after_plugin_change()
    return PluginReloadResult(
        hook_registry=HookRegistry.empty(),
        active_plugin_names=(),
        cancelled_task_count=0,
    )


def load_plugins(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    set_skill_roots: bool = True,
    skip_broken_plugins: bool = True,
) -> list[_Plugin]:
    """Load plugins from config and register their tools and skills."""
    import mindroom.tools  # noqa: F401, PLC0415

    with locked_tool_registry_state():
        plugin_entries = config.plugins
        if not plugin_entries:
            _sync_loaded_plugin_tools([])
            if set_skill_roots:
                set_plugin_skill_roots([])
            return []
        plugins: list[_Plugin] = []
        skill_roots: list[Path] = []
        plugin_bases = plugin_imports._collect_plugin_bases(
            plugin_entries,
            runtime_paths,
            skip_broken_plugins=skip_broken_plugins,
        )
        snapshot = capture_tool_registry_snapshot()
        try:
            plugin_imports._reject_duplicate_plugin_manifest_names(plugin_bases)

            for plugin_base, plugin_entry, plugin_order in plugin_bases:
                plugin_snapshot = capture_tool_registry_snapshot()
                try:
                    plugin = _materialize_plugin(plugin_base, plugin_entry, plugin_order)
                except Exception as exc:
                    restore_tool_registry_snapshot(plugin_snapshot)
                    if not skip_broken_plugins:
                        raise
                    plugin_imports._log_skipped_plugin_entry(plugin_entry.path, plugin_base.root, exc)
                    continue
                plugins.append(plugin)
                skill_roots.extend(plugin.skill_dirs)

            if plugins:
                logger.info("Loaded plugins", plugins=[plugin.name for plugin in plugins])

            _sync_loaded_plugin_tools(plugins)

            if set_skill_roots:
                set_plugin_skill_roots(skill_roots)
        except Exception:
            restore_tool_registry_snapshot(snapshot)
            raise

        return plugins


def get_configured_plugin_roots(
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[Path, ...]:
    """Resolve the enabled plugin roots for one config snapshot."""
    cache_key = _configured_plugin_root_cache_key(config, runtime_paths)
    cached = _CONFIGURED_PLUGIN_ROOT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    roots: list[Path] = []
    for plugin_entry in config.plugins:
        if not plugin_entry.enabled:
            continue
        try:
            roots.append(plugin_imports._resolve_plugin_root(plugin_entry.path, runtime_paths))
        except _PluginValidationError:
            continue
    configured_roots = tuple(dict.fromkeys(roots))
    if len(_CONFIGURED_PLUGIN_ROOT_CACHE) >= _CONFIGURED_PLUGIN_ROOT_CACHE_MAX_SIZE:
        _CONFIGURED_PLUGIN_ROOT_CACHE.clear()
    _CONFIGURED_PLUGIN_ROOT_CACHE[cache_key] = configured_roots
    return configured_roots


def _log_skipped_plugin_entry(
    plugin_path: str,
    root: Path | None,
    exc: Exception,
) -> None:
    """Log one broken plugin entry without aborting the rest of startup."""
    if root is not None and (not root.exists() or not root.is_dir()):
        _warn_once("Plugin path does not exist, skipping", path=root)
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


def _clear_configured_plugin_roots_cache() -> None:
    """Drop cached configured plugin roots after plugin runtime invalidation."""
    _CONFIGURED_PLUGIN_ROOT_CACHE.clear()


def _clear_oauth_provider_cache_after_plugin_change() -> None:
    """Drop cached OAuth providers without creating an import cycle at module load."""
    from mindroom.oauth.registry import clear_oauth_provider_cache  # noqa: PLC0415

    clear_oauth_provider_cache()


def prepare_plugin_reload(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool = False,
) -> _PreparedPluginReload:
    """Build one fresh plugin snapshot without mutating the live runtime."""
    with locked_tool_registry_state():
        package_roots = {cached.module_name.split(".", 1)[0] for cached in plugin_imports._MODULE_IMPORT_CACHE.values()}
        previous_snapshot = capture_tool_registry_snapshot()
        previous_plugin_skill_roots = tuple(get_plugin_skill_roots())
        try:
            _clear_plugin_reload_caches()
            _evict_synthetic_plugin_subtrees(package_roots)
            plugins = load_plugins(config, runtime_paths, skip_broken_plugins=skip_broken_plugins)
            return _PreparedPluginReload(
                hook_registry=HookRegistry.from_plugins(plugins),
                active_plugin_names=tuple(plugin.name for plugin in plugins),
                tool_registry_snapshot=capture_tool_registry_snapshot(),
                plugin_skill_roots=tuple(get_plugin_skill_roots()),
            )
        finally:
            restore_tool_registry_snapshot(previous_snapshot)
            set_plugin_skill_roots(previous_plugin_skill_roots)


def _load_plugin_base(root: Path) -> _PluginBase:
    if not root.exists() or not root.is_dir():
        msg = f"Configured plugin path does not exist: {root}"
        raise PluginValidationError(msg)

    manifest_path = root / _PLUGIN_MANIFEST
    if not manifest_path.exists():
        msg = f"Plugin manifest missing: {manifest_path}"
        raise PluginValidationError(msg)

    if not root.is_relative_to(_REPO_ROOT):
        _warn_once("Loading non-bundled plugin", path=root)

    try:
        manifest_mtime = manifest_path.stat().st_mtime
    except OSError as exc:
        msg = f"Failed to stat plugin manifest {manifest_path}: {exc}"
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


def reload_plugins(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool = False,
) -> PluginReloadResult:
    """Tear down the live plugin runtime and rebuild it from the current config."""
    package_roots = {cached.module_name.split(".", 1)[0] for cached in plugin_imports._MODULE_IMPORT_CACHE.values()}
    cancelled_task_count = _cancel_plugin_module_tasks(package_roots)
    prepared_reload = prepare_plugin_reload(
        config,
        runtime_paths,
        skip_broken_plugins=skip_broken_plugins,
    )
    return apply_prepared_plugin_reload(
        prepared_reload,
        cancelled_task_count=cancelled_task_count,
    )


def _cancel_plugin_module_tasks(package_roots: set[str]) -> int:
    """Best-effort cancel module-global tasks owned by one synthetic plugin subtree."""
    if not package_roots:
        return 0

    cancelled_task_ids: set[int] = set()
    for module_name, module in tuple(sys.modules.items()):
        if module is None or not any(
            module_name == root or module_name.startswith(f"{root}.") for root in package_roots
        ):
            continue
        for value in vars(module).values():
            for task in _iter_module_tasks(value):
                if task.done() or id(task) in cancelled_task_ids:
                    continue
                task.cancel()
                cancelled_task_ids.add(id(task))
    return len(cancelled_task_ids)


def _iter_module_tasks(value: object) -> tuple[asyncio.Task[Any], ...]:
    """Return task globals or one-level container-held tasks from one module value."""
    if isinstance(value, asyncio.Task):
        return (value,)
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, tuple | list | set):
        values = value
    else:
        return ()
    return tuple(item for item in values if isinstance(item, asyncio.Task))


def _clear_plugin_reload_caches() -> None:
    """Drop cached plugin manifests and imported plugin modules before one rebuild."""
    _clear_configured_plugin_roots_cache()
    plugin_imports._PLUGIN_CACHE.clear()
    plugin_imports._MODULE_IMPORT_CACHE.clear()
    _clear_oauth_provider_cache_after_plugin_change()


def _evict_synthetic_plugin_subtrees(package_roots: set[str]) -> None:
    """Remove all imported synthetic plugin modules for the targeted roots."""
    for module_name in tuple(sys.modules):
        if any(module_name == root or module_name.startswith(f"{root}.") for root in package_roots):
            sys.modules.pop(module_name, None)


def _materialize_plugin(
    plugin: plugin_imports._PluginBase,
    entry_config: PluginEntryConfig,
    plugin_order: int,
) -> _Plugin:
    tools_module = load_plugin_module(plugin.name, plugin.root, plugin.tools_module_path, kind="tools")
    hooks_module_path = plugin.hooks_module_path or plugin.tools_module_path
    hooks_module = (
        load_plugin_module(plugin.name, plugin.root, hooks_module_path, kind="hooks") if hooks_module_path else None
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
        raise PluginValidationError(msg) from exc

    if not isinstance(data, dict):
        msg = f"Plugin manifest must be a JSON object: {path}"
        raise PluginValidationError(msg)

    name = data.get("name")
    if not isinstance(name, str):
        msg = f"Plugin manifest missing valid string name ({path})"
        raise PluginValidationError(msg)
    try:
        normalized_name = validate_plugin_name(name)
    except ValueError as exc:
        msg = f"{exc} ({path})"
        raise PluginValidationError(msg) from exc

    tools_module = data.get("tools_module")
    if tools_module is not None and not isinstance(tools_module, str):
        msg = f"Plugin tools_module must be a string: {path}"
        raise PluginValidationError(msg)

    hooks_module = data.get("hooks_module")
    if hooks_module is not None and not isinstance(hooks_module, str):
        msg = f"Plugin hooks_module must be a string: {path}"
        raise PluginValidationError(msg)

    raw_skills = data.get("skills", [])
    if raw_skills is None:
        raw_skills = []
    if not isinstance(raw_skills, list) or any(not isinstance(item, str) for item in raw_skills):
        msg = f"Plugin skills must be a list of strings: {path}"
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
        raise PluginValidationError(msg)
    return resolved_path


def _resolve_skill_dirs(root: Path, skills: list[str]) -> list[Path]:
    skill_dirs: list[Path] = []
    for relative_path in skills:
        path = (root / relative_path).resolve()
        if not path.exists() or not path.is_dir():
            msg = f"Plugin skill path is not a directory: {path}"
            raise PluginValidationError(msg)
        skill_dirs.append(path)
    return skill_dirs


def _prepare_plugin_tool_module_reload(
    module_name: str,
    cached: plugin_imports._ModuleCacheEntry | None,
) -> dict[str, dict[str, ToolMetadata]]:
    """Snapshot one tool module's cached registrations before reload."""
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
    cached: plugin_imports._ModuleCacheEntry | None,
    previous_registrations_by_module_name: dict[str, dict[str, ToolMetadata]],
) -> None:
    """Restore cached tool registrations and module imports after one failed reload."""
    sys.modules.pop(module_name, None)
    for restored_module_name, registrations in previous_registrations_by_module_name.items():
        restore_plugin_tool_registrations(restored_module_name, registrations)
    if cached is not None:
        plugin_imports._MODULE_IMPORT_CACHE[module_path] = cached
        sys.modules[cached.module_name] = cached.module
    else:
        plugin_imports._MODULE_IMPORT_CACHE.pop(module_path, None)


def load_plugin_module(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path | None,
    *,
    kind: str,
) -> ModuleType | None:
    """Load a plugin module from a configured plugin root."""
    if module_path is None:
        return None
    try:
        mtime = module_path.stat().st_mtime
    except OSError as exc:
        msg = f"Failed to stat plugin {kind} module {module_path}: {exc}"
        raise PluginValidationError(msg) from exc

    module_name = plugin_imports._module_name(plugin_name, plugin_root, module_path)
    cached = plugin_imports._MODULE_IMPORT_CACHE.get(module_path)
    if cached is not None and cached.mtime == mtime and cached.module_name == module_name:
        return cached.module

    previous_registrations_by_module_name = (
        _prepare_plugin_tool_module_reload(module_name, cached) if kind == "tools" else {}
    )

    if cached is not None and cached.module_name != module_name:
        sys.modules.pop(cached.module_name, None)

    prepared_module = plugin_imports._prepare_module(plugin_name, plugin_root, module_path, module_name)
    if prepared_module is None:
        msg = f"Failed to load plugin {kind} module: {module_path}"
        raise PluginValidationError(msg)

    try:
        if kind == "tools":
            with scoped_plugin_registration_owner(module_name):
                _exec_plugin_source(module_path, module)
        else:
            _exec_plugin_source(module_path, module)
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
                plugin_imports._MODULE_IMPORT_CACHE[module_path] = cached
                sys.modules[cached.module_name] = cached.module
            else:
                plugin_imports._MODULE_IMPORT_CACHE.pop(module_path, None)
        plugin_imports._restore_plugin_package_chain(previous_packages)
        msg = f"Plugin {kind} module execution failed for {module_path}: {exc}"
        raise PluginValidationError(msg) from exc

    plugin_imports._MODULE_IMPORT_CACHE[module_path] = plugin_imports._ModuleCacheEntry(
        mtime=mtime,
        module_name=module_name,
        module=module,
    )
    return module


def _exec_plugin_source(module_path: Path, module: ModuleType) -> None:
    with tokenize.open(str(module_path)) as source_file:
        source = source_file.read()
    code = compile(source, str(module_path), "exec", dont_inherit=True)
    exec(code, module.__dict__)  # noqa: S102 - configured plugin modules are intentionally executable code.
