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
from mindroom.tool_schema_cache import clear_tool_schema_cache
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.registry_state import (
    capture_tool_registry_snapshot,
    clear_plugin_tool_registrations,
    locked_tool_registry_state,
    registered_plugin_tool_names,
    resolved_tool_state,
    restore_plugin_tool_registrations,
    restore_tool_registry_snapshot,
    scoped_plugin_registration_owner,
    snapshot_plugin_tool_registrations,
    synchronize_plugin_tools,
)
from mindroom.tool_system.skills import get_plugin_skill_roots, set_plugin_skill_roots

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from types import ModuleType

    from mindroom.config.main import RuntimeConfig
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookCallback
    from mindroom.tool_system.catalog import ResolvedToolRuntimeState
    from mindroom.tool_system.declarations import ToolMetadata

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
    resolved_tool_state: ResolvedToolRuntimeState
    plugin_skill_roots: tuple[Path, ...]
    plugin_cache: dict[Path, Any]
    module_import_cache: dict[Path, Any]
    synthetic_modules: dict[str, ModuleType]
    previous_package_roots: frozenset[str]
    candidate_package_roots: frozenset[str]


def _hook_display_name(callback: HookCallback) -> str:
    return cast("Any", callback).__name__


def _raise_if_host_control_exception(exc: BaseException) -> None:
    if not isinstance(exc, (Exception, SystemExit)):
        raise exc


def _active_plugin_tool_modules(plugins: list[_Plugin]) -> list[tuple[str, str]]:
    """Return the registry owners for active plugin tool modules."""
    return [
        (plugin.name, plugin_imports._module_name(plugin.name, plugin.root, plugin.tools_module_path))
        for plugin in plugins
        if plugin.tools_module_path is not None
    ]


def _sync_loaded_plugin_tools(
    plugins: list[_Plugin],
    previous_plugin_tool_names: set[str],
) -> None:
    """Remove plugin tool registrations for plugins no longer present in config."""
    synchronize_plugin_tools(
        _active_plugin_tool_modules(plugins),
        previous_plugin_tool_names=previous_plugin_tool_names,
    )


def load_plugins(
    config: RuntimeConfig,
    runtime_paths: RuntimePaths,
    *,
    set_skill_roots: bool = True,
    skip_broken_plugins: bool = True,
) -> list[_Plugin]:
    """Load plugins from config and register their tools and skills."""
    import mindroom.tools  # noqa: F401, PLC0415

    with locked_tool_registry_state():
        previous_plugin_tool_names = registered_plugin_tool_names()
        plugin_entries = config.plugins
        if not plugin_entries:
            _sync_loaded_plugin_tools([], previous_plugin_tool_names)
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
                except (Exception, SystemExit) as exc:
                    restore_tool_registry_snapshot(plugin_snapshot)
                    if not skip_broken_plugins:
                        if isinstance(exc, SystemExit):
                            msg = f"Plugin materialization failed for {plugin_base.root}: {exc}"
                            raise _PluginValidationError(msg) from exc
                        raise
                    plugin_imports._log_skipped_plugin_entry(plugin_entry.path, plugin_base.root, exc)
                    continue
                plugins.append(plugin)
                skill_roots.extend(plugin.skill_dirs)

            if plugins:
                logger.info("Loaded plugins", plugins=[plugin.name for plugin in plugins])

            _sync_loaded_plugin_tools(plugins, previous_plugin_tool_names)

            if set_skill_roots:
                set_plugin_skill_roots(skill_roots)
        except BaseException:
            restore_tool_registry_snapshot(snapshot)
            raise

        return plugins


def get_configured_plugin_roots(
    config: RuntimeConfig,
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


def _configured_plugin_root_cache_key(
    config: RuntimeConfig,
    runtime_paths: RuntimePaths,
) -> _ConfiguredPluginRootCacheKey:
    return _ConfiguredPluginRootCacheKey(
        plugin_entries=tuple((plugin_entry.path, plugin_entry.enabled) for plugin_entry in config.plugins),
        config_path=str(runtime_paths.config_path),
        config_dir=str(runtime_paths.config_dir),
        storage_root=str(runtime_paths.storage_root),
        sys_path=tuple(str(path) for path in sys.path),
    )


def _clear_configured_plugin_roots_cache() -> None:
    """Drop cached configured plugin roots after plugin runtime invalidation."""
    _CONFIGURED_PLUGIN_ROOT_CACHE.clear()


def _clear_oauth_provider_cache_after_plugin_change() -> None:
    """Drop cached OAuth providers without creating an import cycle at module load."""
    from mindroom.oauth.registry import clear_oauth_provider_cache  # noqa: PLC0415

    clear_oauth_provider_cache()


def prepare_plugin_reload(
    config: RuntimeConfig,
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool = False,
) -> _PreparedPluginReload:
    """Build one fresh plugin snapshot without mutating the live runtime."""
    with locked_tool_registry_state():
        previous_plugin_cache = plugin_imports._PLUGIN_CACHE.copy()
        previous_module_import_cache = plugin_imports._MODULE_IMPORT_CACHE.copy()
        previous_package_roots = _package_roots(previous_module_import_cache)
        previous_synthetic_modules = _synthetic_plugin_modules(previous_package_roots)
        previous_snapshot = capture_tool_registry_snapshot()
        previous_plugin_skill_roots = tuple(get_plugin_skill_roots())
        prepared_succeeded = False
        try:
            _clear_plugin_import_caches()
            _evict_synthetic_plugin_subtrees(previous_package_roots)
            plugins = load_plugins(config, runtime_paths, skip_broken_plugins=skip_broken_plugins)
            candidate_tool_registry_snapshot = capture_tool_registry_snapshot()
            from mindroom.mcp.registry import reconcile_mcp_tool_registry  # noqa: PLC0415
            from mindroom.tool_system.catalog import resolved_tool_runtime_state_from_registry  # noqa: PLC0415

            reconcile_mcp_tool_registry(
                candidate_tool_registry_snapshot.registry,
                candidate_tool_registry_snapshot.metadata,
                config,
            )
            candidate_hook_registry = HookRegistry.from_plugins(plugins)
            candidate_plugin_registry, candidate_plugin_metadata = resolved_tool_state(
                _active_plugin_tool_modules(plugins),
                candidate_tool_registry_snapshot.plugin_tool_metadata_by_module,
            )
            resolved_runtime_tool_state = resolved_tool_runtime_state_from_registry(
                runtime_paths,
                config,
                candidate_plugin_registry,
                candidate_plugin_metadata,
                hook_registry=candidate_hook_registry,
                unavailable_tool_names=config.unavailable_plugin_tool_names,
            )
            candidate_plugin_cache = plugin_imports._PLUGIN_CACHE.copy()
            candidate_module_import_cache = plugin_imports._MODULE_IMPORT_CACHE.copy()
            candidate_package_roots = _package_roots(candidate_module_import_cache)
            prepared_reload = _PreparedPluginReload(
                hook_registry=candidate_hook_registry,
                active_plugin_names=tuple(plugin.name for plugin in plugins),
                tool_registry_snapshot=candidate_tool_registry_snapshot,
                resolved_tool_state=resolved_runtime_tool_state,
                plugin_skill_roots=tuple(get_plugin_skill_roots()),
                plugin_cache=candidate_plugin_cache,
                module_import_cache=candidate_module_import_cache,
                synthetic_modules=_synthetic_plugin_modules(candidate_package_roots),
                previous_package_roots=frozenset(previous_package_roots),
                candidate_package_roots=frozenset(candidate_package_roots),
            )
            prepared_succeeded = True
            return prepared_reload
        finally:
            candidate_package_roots = _package_roots(plugin_imports._MODULE_IMPORT_CACHE)
            if not prepared_succeeded:
                _cancel_plugin_module_tasks(candidate_package_roots)
            _evict_synthetic_plugin_subtrees(previous_package_roots | candidate_package_roots)
            sys.modules.update(previous_synthetic_modules)
            plugin_imports._PLUGIN_CACHE.clear()
            plugin_imports._PLUGIN_CACHE.update(previous_plugin_cache)
            plugin_imports._MODULE_IMPORT_CACHE.clear()
            plugin_imports._MODULE_IMPORT_CACHE.update(previous_module_import_cache)
            restore_tool_registry_snapshot(previous_snapshot)
            set_plugin_skill_roots(previous_plugin_skill_roots)


def apply_prepared_plugin_reload(
    prepared_reload: _PreparedPluginReload,
    *,
    cancelled_task_count: int = 0,
    cancel_existing_tasks: bool = False,
) -> PluginReloadResult:
    """Commit one previously prepared plugin runtime snapshot."""
    with locked_tool_registry_state():
        if cancel_existing_tasks:
            cancelled_task_count = _cancel_plugin_module_tasks(set(prepared_reload.previous_package_roots))
        _evict_synthetic_plugin_subtrees(
            set(prepared_reload.previous_package_roots | prepared_reload.candidate_package_roots),
        )
        sys.modules.update(prepared_reload.synthetic_modules)
        plugin_imports._PLUGIN_CACHE.clear()
        plugin_imports._PLUGIN_CACHE.update(prepared_reload.plugin_cache)
        plugin_imports._MODULE_IMPORT_CACHE.clear()
        plugin_imports._MODULE_IMPORT_CACHE.update(prepared_reload.module_import_cache)
        restore_tool_registry_snapshot(prepared_reload.tool_registry_snapshot)
        set_plugin_skill_roots(prepared_reload.plugin_skill_roots)
        clear_tool_schema_cache()
        _clear_configured_plugin_roots_cache()
        _clear_oauth_provider_cache_after_plugin_change()
    return PluginReloadResult(
        hook_registry=prepared_reload.hook_registry,
        active_plugin_names=prepared_reload.active_plugin_names,
        cancelled_task_count=cancelled_task_count,
    )


def _cancel_plugin_module_tasks(package_roots: set[str]) -> int:
    """Best-effort cancel module-global tasks owned by one synthetic plugin subtree."""
    if not package_roots:
        return 0

    modules = (
        module
        for module_name, module in tuple(sys.modules.items())
        if module is not None
        and any(module_name == root or module_name.startswith(f"{root}.") for root in package_roots)
    )
    return _cancel_tasks_in_modules(modules)


def _cancel_tasks_in_modules(modules: Iterable[ModuleType]) -> int:
    """Best-effort cancel module-global tasks in the supplied plugin modules."""
    return _cancel_tasks(
        task for module in modules for value in vars(module).values() for task in _iter_module_tasks(value)
    )


def _cancel_tasks(tasks: Iterable[asyncio.Task[Any]]) -> int:
    """Best-effort cancel each distinct pending task."""
    cancelled_task_ids: set[int] = set()
    for task in tasks:
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


def _clear_plugin_import_caches() -> None:
    """Drop staged plugin manifests and imported modules before one rebuild."""
    plugin_imports._PLUGIN_CACHE.clear()
    plugin_imports._MODULE_IMPORT_CACHE.clear()


def _package_roots(module_import_cache: dict[Path, Any]) -> set[str]:
    """Return synthetic package roots referenced by one plugin module cache."""
    return {cached.module_name.split(".", 1)[0] for cached in module_import_cache.values()}


def _synthetic_plugin_modules(package_roots: set[str]) -> dict[str, ModuleType]:
    """Snapshot imported synthetic modules for the targeted plugin packages."""
    return {
        module_name: module
        for module_name, module in sys.modules.items()
        if module is not None
        and any(module_name == root or module_name.startswith(f"{root}.") for root in package_roots)
    }


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
        logger.exception("Failed to stat plugin module", path=str(module_path), kind=kind, error=str(exc))
        raise _PluginValidationError(msg) from exc

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
        logger.error("Failed to load plugin module", path=str(module_path), kind=kind)
        raise _PluginValidationError(msg)
    module, _, previous_packages = prepared_module
    try:
        _exec_plugin_module_source(module_path, module, module_name=module_name, kind=kind)
    except BaseException as exc:
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
        _raise_if_host_control_exception(exc)
        msg = f"Plugin {kind} module execution failed for {module_path}: {exc}"
        logger.exception("Plugin module execution failed", path=str(module_path), kind=kind, error=str(exc))
        raise _PluginValidationError(msg) from exc

    plugin_imports._MODULE_IMPORT_CACHE[module_path] = plugin_imports._ModuleCacheEntry(
        mtime=mtime,
        module_name=module_name,
        module=module,
    )
    return module


def _exec_plugin_module_source(
    module_path: Path,
    module: ModuleType,
    *,
    module_name: str,
    kind: str,
) -> None:
    """Execute a plugin module while rejecting import-time background tasks."""
    tasks_before_import = plugin_imports._running_tasks()
    try:
        if kind == "tools":
            with scoped_plugin_registration_owner(module_name):
                _exec_plugin_source(module_path, module)
        else:
            _exec_plugin_source(module_path, module)
    except BaseException:
        plugin_imports._cancel_tasks_created_since(tasks_before_import)
        raise
    if plugin_imports._cancel_tasks_created_since(tasks_before_import):
        msg = f"Plugin {kind} module created async tasks during import: {module_path}"
        raise _PluginValidationError(msg)


def _exec_plugin_source(module_path: Path, module: ModuleType) -> None:
    with tokenize.open(str(module_path)) as source_file:
        source = source_file.read()
    code = compile(source, str(module_path), "exec", dont_inherit=True)
    exec(code, module.__dict__)  # noqa: S102 - configured plugin modules are intentionally executable code.
