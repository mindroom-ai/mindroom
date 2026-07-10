"""Plugin loader for Mindroom tools and skills."""

from __future__ import annotations

import asyncio
import json
import sys
import tokenize
from dataclasses import dataclass, replace
from types import MappingProxyType
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
    restore_plugin_tool_registrations,
    restore_tool_registry_snapshot,
    scoped_plugin_registration_owner,
    snapshot_plugin_tool_registrations,
    synchronize_plugin_tools,
)
from mindroom.tool_system.skills import get_plugin_skill_roots, set_plugin_skill_roots

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from types import ModuleType

    from agno.tools import Toolkit

    from mindroom.config.main import Config, RuntimeToolAvailabilitySnapshot
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookCallback
    from mindroom.tool_system.declarations import ToolMetadata
    from mindroom.tool_system.registry_state import ToolRegistrySnapshot

logger = get_logger(__name__)

_PluginValidationError = plugin_imports.PluginValidationError
_CONFIGURED_PLUGIN_ROOT_CACHE_MAX_SIZE = 128


class _ConfiguredPluginRootCacheKey(NamedTuple):
    plugin_entries: tuple[tuple[str, bool], ...]
    config_path: str
    config_dir: str
    storage_root: str
    sys_path: tuple[str, ...]


class _ActivePluginRuntimeSignature(NamedTuple):
    plugin_entries: tuple[str, ...]
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
    runtime_paths: RuntimePaths
    tool_registry_snapshot: ToolRegistrySnapshot
    plugin_skill_roots: tuple[Path, ...]
    runtime_tool_availability: RuntimeToolAvailabilitySnapshot


@dataclass(frozen=True, slots=True)
class _ActivePluginRuntime:
    """Authoritative plugin runtime published for one exact config context."""

    signature: _ActivePluginRuntimeSignature
    hook_registry: HookRegistry


_ACTIVE_PLUGIN_RUNTIME: _ActivePluginRuntime | None = None


class _PluginRuntimeMismatchError(RuntimeError):
    """Raised when an implicit caller targets a stale plugin runtime generation."""


@dataclass(frozen=True, slots=True)
class PluginRuntimeSnapshot:
    """Immutable process-global plugin surfaces captured from one generation."""

    hook_registry: HookRegistry
    runtime_config: Config
    tool_registry: Mapping[str, Callable[[], type[Toolkit]]]
    tool_metadata: Mapping[str, ToolMetadata]
    plugin_skill_roots: tuple[Path, ...]


def publish_active_plugin_runtime(
    config: Config,
    runtime_paths: RuntimePaths,
    hook_registry: HookRegistry,
) -> None:
    """Publish the exact plugin snapshot that implicit runtime consumers must reuse."""
    global _ACTIVE_PLUGIN_RUNTIME
    with locked_tool_registry_state():
        _ACTIVE_PLUGIN_RUNTIME = _ActivePluginRuntime(
            signature=_active_plugin_runtime_signature(config, runtime_paths),
            hook_registry=hook_registry,
        )


def active_plugin_hook_registry(
    config: Config,
    runtime_paths: RuntimePaths,
) -> HookRegistry | None:
    """Return the authoritative active hooks for this exact config context, if published."""
    with locked_tool_registry_state():
        active_runtime = _ACTIVE_PLUGIN_RUNTIME
        if active_runtime is None:
            return None
        signature = _active_plugin_runtime_signature(config, runtime_paths)
        if active_runtime.signature != signature:
            msg = "Plugin runtime differs from the active generation; use an explicit plugin reload to switch it."
            raise _PluginRuntimeMismatchError(msg)
        return active_runtime.hook_registry


def capture_plugin_runtime_snapshot(
    config: Config,
    runtime_paths: RuntimePaths,
) -> PluginRuntimeSnapshot:
    """Capture hooks, tools, metadata, and skill roots from one committed generation."""
    with locked_tool_registry_state():
        hook_registry = active_plugin_hook_registry(config, runtime_paths)
        if hook_registry is None:
            hook_registry = reload_plugins(
                config,
                runtime_paths,
                skip_broken_plugins=True,
            ).hook_registry
        runtime_tool_availability = prepare_active_plugin_tool_state_for_config(config)
        resolved_state = runtime_tool_availability.resolved_tool_state
        return PluginRuntimeSnapshot(
            hook_registry=hook_registry,
            runtime_config=config.copy_with_runtime_tool_availability(runtime_tool_availability),
            tool_registry=MappingProxyType(dict(resolved_state.tool_registry)),
            tool_metadata=MappingProxyType(dict(resolved_state.tool_metadata)),
            plugin_skill_roots=tuple(get_plugin_skill_roots()),
        )


def resolve_snapshot_hook_registry(
    snapshot: PluginRuntimeSnapshot,
    explicit_hook_registry: HookRegistry | None,
) -> HookRegistry:
    """Return an intentional injected registry or the captured plugin hooks."""
    return explicit_hook_registry if explicit_hook_registry is not None else snapshot.hook_registry


def _with_resolved_runtime_tools(
    snapshot: ToolRegistrySnapshot,
    runtime_tool_availability: RuntimeToolAvailabilitySnapshot,
) -> ToolRegistrySnapshot:
    """Carry the exact validated plugin and MCP tool state into the committed snapshot."""
    resolved_state = runtime_tool_availability.resolved_tool_state
    return replace(
        snapshot,
        registry=dict(resolved_state.tool_registry),
        metadata=dict(resolved_state.tool_metadata),
    )


def _hook_display_name(callback: HookCallback) -> str:
    return cast("Any", callback).__name__


def _raise_if_host_control_exception(exc: BaseException) -> None:
    if not isinstance(exc, (Exception, SystemExit)):
        raise exc


def _sync_loaded_plugin_tools(plugins: list[_Plugin]) -> None:
    """Remove plugin tool registrations for plugins no longer present in config."""
    active_tool_modules = [
        (plugin.name, plugin_imports._module_name(plugin.name, plugin.root, plugin.tools_module_path))
        for plugin in plugins
        if plugin.tools_module_path is not None
    ]
    synchronize_plugin_tools(active_tool_modules)


def _reject_import_time_tasks(
    plugin_name: str,
    existing_tasks: frozenset[asyncio.Task[object]],
) -> None:
    """Reject plugin imports that start tasks before their runtime is committed."""
    plugin_imports.reject_asyncio_tasks_created_since(
        existing_tasks,
        message=(
            f"Plugin '{plugin_name}' created background tasks during import; "
            "start tasks from runtime hooks after activation instead."
        ),
    )


def deactivate_plugins(config: Config, runtime_paths: RuntimePaths) -> PluginReloadResult:
    """Prepare and atomically apply an empty plugin runtime."""
    with locked_tool_registry_state():
        package_roots = {cached.module_name.split(".", 1)[0] for cached in plugin_imports._MODULE_IMPORT_CACHE.values()}
        previous_snapshot = capture_tool_registry_snapshot()
        previous_plugin_skill_roots = tuple(get_plugin_skill_roots())
        try:
            _clear_plugin_reload_caches()
            _evict_synthetic_plugin_subtrees(package_roots)
            _sync_loaded_plugin_tools([])
            set_plugin_skill_roots([])
            tool_registry_snapshot = capture_tool_registry_snapshot()
        finally:
            restore_tool_registry_snapshot(previous_snapshot)
            set_plugin_skill_roots(previous_plugin_skill_roots)

    runtime_tool_availability = config.prepare_runtime_tool_availability(
        tool_registry_snapshot.registry,
        tool_registry_snapshot.metadata,
        tolerate_plugin_load_errors=True,
    )
    tool_registry_snapshot = _with_resolved_runtime_tools(
        tool_registry_snapshot,
        runtime_tool_availability,
    )
    prepared_reload = _PreparedPluginReload(
        hook_registry=HookRegistry.empty(),
        active_plugin_names=(),
        runtime_paths=runtime_paths,
        tool_registry_snapshot=tool_registry_snapshot,
        plugin_skill_roots=(),
        runtime_tool_availability=runtime_tool_availability,
    )
    return apply_prepared_plugin_reload(
        prepared_reload,
        config,
        cancel_existing_tasks=True,
    )


def prepare_active_plugin_tool_state_for_config(
    config: Config,
) -> RuntimeToolAvailabilitySnapshot:
    """Prepare exact active plugin tools for a new non-plugin config snapshot."""
    import mindroom.tools  # noqa: F401, PLC0415
    from mindroom.mcp.registry import registered_mcp_tool_names  # noqa: PLC0415

    with locked_tool_registry_state():
        excluded_tool_names = registered_mcp_tool_names()
        active_snapshot = capture_tool_registry_snapshot()
        return config.prepare_runtime_tool_availability(
            {
                tool_name: factory
                for tool_name, factory in active_snapshot.registry.items()
                if tool_name not in excluded_tool_names
            },
            {
                tool_name: metadata
                for tool_name, metadata in active_snapshot.metadata.items()
                if tool_name not in excluded_tool_names
            },
            tolerate_plugin_load_errors=True,
        )


def _load_plugins(
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
        plugin_bases, _skipped_plugin_sources = plugin_imports._collect_plugin_bases(
            plugin_entries,
            runtime_paths,
            skip_broken_plugins=skip_broken_plugins,
        )
        snapshot = capture_tool_registry_snapshot()
        try:
            for plugin_base, plugin_entry, plugin_order in plugin_bases:
                plugin_snapshot = capture_tool_registry_snapshot()
                existing_plugin_tasks = plugin_imports.running_asyncio_tasks()
                try:
                    plugin = _materialize_plugin(plugin_base, plugin_entry, plugin_order)
                    _reject_import_time_tasks(plugin_base.name, existing_plugin_tasks)
                except (Exception, SystemExit) as exc:
                    restore_tool_registry_snapshot(plugin_snapshot)
                    if not skip_broken_plugins:
                        if isinstance(exc, SystemExit):
                            msg = f"Plugin materialization failed for {plugin_base.root}: {exc}"
                            raise _PluginValidationError(msg) from exc
                        raise
                    plugin_imports.cancel_asyncio_tasks_created_since(existing_plugin_tasks)
                    plugin_imports._log_skipped_plugin_entry(plugin_entry.path, plugin_base.root, exc)
                    continue
                plugins.append(plugin)
                skill_roots.extend(plugin.skill_dirs)

            if plugins:
                logger.info("Loaded plugins", plugins=[plugin.name for plugin in plugins])

            _sync_loaded_plugin_tools(plugins)

            if set_skill_roots:
                set_plugin_skill_roots(skill_roots)
        except BaseException:
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
            roots.append(plugin_imports.resolve_plugin_root(plugin_entry.path, runtime_paths))
        except _PluginValidationError:
            continue
    configured_roots = tuple(dict.fromkeys(roots))
    if len(_CONFIGURED_PLUGIN_ROOT_CACHE) >= _CONFIGURED_PLUGIN_ROOT_CACHE_MAX_SIZE:
        _CONFIGURED_PLUGIN_ROOT_CACHE.clear()
    _CONFIGURED_PLUGIN_ROOT_CACHE[cache_key] = configured_roots
    return configured_roots


def _configured_plugin_root_cache_key(
    config: Config,
    runtime_paths: RuntimePaths,
) -> _ConfiguredPluginRootCacheKey:
    return _ConfiguredPluginRootCacheKey(
        plugin_entries=tuple((plugin_entry.path, plugin_entry.enabled) for plugin_entry in config.plugins),
        config_path=str(runtime_paths.config_path),
        config_dir=str(runtime_paths.config_dir),
        storage_root=str(runtime_paths.storage_root),
        sys_path=tuple(str(path) for path in sys.path),
    )


def _active_plugin_runtime_signature(
    config: Config,
    runtime_paths: RuntimePaths,
) -> _ActivePluginRuntimeSignature:
    """Return the full plugin semantics and resolution identity for one runtime."""
    enabled_plugin_entries = [plugin_entry for plugin_entry in config.plugins if plugin_entry.enabled]
    if not enabled_plugin_entries:
        return _ActivePluginRuntimeSignature(
            plugin_entries=(),
            config_path="",
            config_dir="",
            storage_root="",
            sys_path=(),
        )
    return _ActivePluginRuntimeSignature(
        plugin_entries=tuple(
            json.dumps(plugin_entry.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            for plugin_entry in enabled_plugin_entries
        ),
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
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool = False,
) -> _PreparedPluginReload:
    """Build one fresh plugin snapshot without mutating the live runtime."""
    existing_tasks = plugin_imports.running_asyncio_tasks()
    try:
        with locked_tool_registry_state():
            package_roots = {
                cached.module_name.split(".", 1)[0] for cached in plugin_imports._MODULE_IMPORT_CACHE.values()
            }
            previous_snapshot = capture_tool_registry_snapshot()
            previous_plugin_skill_roots = tuple(get_plugin_skill_roots())
            try:
                _clear_plugin_reload_caches()
                _evict_synthetic_plugin_subtrees(package_roots)
                plugins = _load_plugins(config, runtime_paths, skip_broken_plugins=skip_broken_plugins)
                hook_registry = HookRegistry.from_plugins(plugins)
                active_plugin_names = tuple(plugin.name for plugin in plugins)
                tool_registry_snapshot = capture_tool_registry_snapshot()
                plugin_skill_roots = tuple(get_plugin_skill_roots())
            finally:
                restore_tool_registry_snapshot(previous_snapshot)
                set_plugin_skill_roots(previous_plugin_skill_roots)

        runtime_tool_availability = config.prepare_runtime_tool_availability(
            tool_registry_snapshot.registry,
            tool_registry_snapshot.metadata,
            tolerate_plugin_load_errors=skip_broken_plugins,
        )
        tool_registry_snapshot = _with_resolved_runtime_tools(
            tool_registry_snapshot,
            runtime_tool_availability,
        )
        return _PreparedPluginReload(
            hook_registry=hook_registry,
            active_plugin_names=active_plugin_names,
            runtime_paths=runtime_paths,
            tool_registry_snapshot=tool_registry_snapshot,
            plugin_skill_roots=plugin_skill_roots,
            runtime_tool_availability=runtime_tool_availability,
        )
    except BaseException:
        plugin_imports.cancel_asyncio_tasks_created_since(existing_tasks)
        raise


def apply_prepared_plugin_reload(
    prepared_reload: _PreparedPluginReload,
    config: Config,
    *,
    cancelled_task_count: int = 0,
    cancel_existing_tasks: bool = False,
) -> PluginReloadResult:
    """Commit one previously prepared plugin runtime snapshot."""
    from mindroom.mcp.registry import publish_registered_mcp_tool_names  # noqa: PLC0415

    with locked_tool_registry_state():
        if cancel_existing_tasks:
            package_roots = {
                cached.module_name.split(".", 1)[0] for cached in plugin_imports._MODULE_IMPORT_CACHE.values()
            }
            cancelled_task_count = _cancel_plugin_module_tasks(package_roots)
        restore_tool_registry_snapshot(prepared_reload.tool_registry_snapshot)
        _clear_oauth_provider_cache_after_plugin_change()
        publish_registered_mcp_tool_names(
            prepared_reload.runtime_tool_availability.resolved_tool_state.mcp_tool_names,
        )
        set_plugin_skill_roots(prepared_reload.plugin_skill_roots)
        config.apply_runtime_tool_availability(
            prepared_reload.runtime_paths,
            prepared_reload.runtime_tool_availability,
        )
        publish_active_plugin_runtime(
            config,
            prepared_reload.runtime_paths,
            prepared_reload.hook_registry,
        )
    return PluginReloadResult(
        hook_registry=prepared_reload.hook_registry,
        active_plugin_names=prepared_reload.active_plugin_names,
        cancelled_task_count=cancelled_task_count,
    )


def reload_plugins(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool = False,
) -> PluginReloadResult:
    """Tear down the live plugin runtime and rebuild it from the current config."""
    prepared_reload = prepare_plugin_reload(
        config,
        runtime_paths,
        skip_broken_plugins=skip_broken_plugins,
    )
    return apply_prepared_plugin_reload(
        prepared_reload,
        config,
        cancel_existing_tasks=True,
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
    clear_tool_schema_cache()
    _clear_configured_plugin_roots_cache()
    plugin_imports._PLUGIN_CACHE.clear()
    plugin_imports._MODULE_IMPORT_CACHE.clear()


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


def _restore_failed_plugin_module_reload(
    *,
    kind: str,
    module_path: Path,
    module_name: str,
    cached: plugin_imports._ModuleCacheEntry | None,
    previous_registrations_by_module_name: dict[str, dict[str, ToolMetadata]],
    loaded_modules: plugin_imports.ModuleImportState,
    tracked_imports: plugin_imports.TrackedModuleImports,
) -> None:
    """Restore registry, module-cache, and package state after one failed module import."""
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
    plugin_imports.restore_module_import_state(loaded_modules, tracked_imports)


def load_plugin_module(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path | None,
    *,
    kind: str,
) -> ModuleType | None:
    """Load one plugin module under the transactional module-import lock."""
    with plugin_imports.tracked_module_imports() as tracked_imports:
        return _load_plugin_module_locked(
            plugin_name,
            plugin_root,
            module_path,
            kind=kind,
            tracked_imports=tracked_imports,
        )


def _load_plugin_module_locked(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path | None,
    *,
    kind: str,
    tracked_imports: plugin_imports.TrackedModuleImports,
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

    existing_tasks = plugin_imports.running_asyncio_tasks()
    loaded_modules = plugin_imports.snapshot_module_import_state()
    previous_registrations_by_module_name = (
        _prepare_plugin_tool_module_reload(module_name, cached) if kind == "tools" else {}
    )

    if cached is not None and cached.module_name != module_name:
        sys.modules.pop(cached.module_name, None)

    try:
        prepared_module = plugin_imports._prepare_module(plugin_name, plugin_root, module_path, module_name)
    except BaseException:
        plugin_imports.cancel_asyncio_tasks_created_since(existing_tasks)
        plugin_imports.restore_module_import_state(loaded_modules, tracked_imports)
        raise
    if prepared_module is None:
        plugin_imports.cancel_asyncio_tasks_created_since(existing_tasks)
        plugin_imports.restore_module_import_state(loaded_modules, tracked_imports)
        msg = f"Failed to load plugin {kind} module: {module_path}"
        logger.error("Failed to load plugin module", path=str(module_path), kind=kind)
        raise _PluginValidationError(msg)
    module, _, _previous_packages = prepared_module

    try:
        if kind == "tools":
            with scoped_plugin_registration_owner(module_name):
                _exec_plugin_source(module_path, module)
        else:
            _exec_plugin_source(module_path, module)
        _reject_import_time_tasks(plugin_name, existing_tasks)
    except BaseException as exc:
        plugin_imports.cancel_asyncio_tasks_created_since(existing_tasks)
        _restore_failed_plugin_module_reload(
            kind=kind,
            module_path=module_path,
            module_name=module_name,
            cached=cached,
            previous_registrations_by_module_name=previous_registrations_by_module_name,
            loaded_modules=loaded_modules,
            tracked_imports=tracked_imports,
        )
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


def _exec_plugin_source(module_path: Path, module: ModuleType) -> None:
    with tokenize.open(str(module_path)) as source_file:
        source = source_file.read()
    code = compile(source, str(module_path), "exec", dont_inherit=True)
    exec(code, module.__dict__)  # noqa: S102 - configured plugin modules are intentionally executable code.
