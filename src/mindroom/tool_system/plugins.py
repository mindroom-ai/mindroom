"""Plugin loader for Mindroom tools and skills."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from importlib import util
from typing import TYPE_CHECKING, Any, cast

from mindroom.config.plugin import PluginEntryConfig  # noqa: TC001
from mindroom.hooks.decorators import iter_module_hooks
from mindroom.hooks.registry import HookRegistry
from mindroom.logging_config import get_logger
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.registry_state import (
    _capture_tool_registry_snapshot,
    _clear_plugin_tool_registrations,
    _locked_tool_registry_state,
    _restore_plugin_tool_registrations,
    _restore_tool_registry_snapshot,
    _scoped_plugin_registration_owner,
    _snapshot_plugin_tool_registrations,
    _synchronize_plugin_tools,
)
from mindroom.tool_system.skills import set_plugin_skill_roots

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks.types import HookCallback
    from mindroom.tool_system.metadata import ToolMetadata

logger = get_logger(__name__)

PluginValidationError = plugin_imports.PluginValidationError


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


def _hook_display_name(callback: HookCallback) -> str:
    return cast("Any", callback).__name__


def _sync_loaded_plugin_tools(plugins: list[_Plugin]) -> None:
    """Remove plugin tool registrations for plugins no longer present in config."""
    active_tool_modules = [
        (plugin.name, plugin_imports._module_name(plugin.name, plugin.root, plugin.tools_module_path))
        for plugin in plugins
        if plugin.tools_module_path is not None
    ]
    _synchronize_plugin_tools(active_tool_modules)


def load_plugins(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    set_skill_roots: bool = True,
) -> list[_Plugin]:
    """Load plugins from config and register their tools and skills."""
    import mindroom.tools  # noqa: F401, PLC0415

    with _locked_tool_registry_state():
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
            skip_broken_plugins=True,
        )
        snapshot = _capture_tool_registry_snapshot()
        try:
            plugin_imports._reject_duplicate_plugin_manifest_names(plugin_bases)

            for plugin_base, plugin_entry, plugin_order in plugin_bases:
                plugin_snapshot = _capture_tool_registry_snapshot()
                try:
                    plugin = _materialize_plugin(plugin_base, plugin_entry, plugin_order)
                except Exception as exc:
                    _restore_tool_registry_snapshot(plugin_snapshot)
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
            _restore_tool_registry_snapshot(snapshot)
            raise

        return plugins


def get_configured_plugin_roots(
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[Path, ...]:
    """Resolve the enabled plugin roots for one config snapshot."""
    roots: list[Path] = []
    for plugin_entry in config.plugins:
        if not plugin_entry.enabled:
            continue
        try:
            roots.append(plugin_imports._resolve_plugin_root(plugin_entry.path, runtime_paths))
        except PluginValidationError:
            continue
    return tuple(dict.fromkeys(roots))


def reload_plugins(
    config: Config,
    runtime_paths: RuntimePaths,
) -> PluginReloadResult:
    """Evict cached plugin imports and rebuild the live hook snapshot."""
    roots = get_configured_plugin_roots(config, runtime_paths)
    package_roots = {
        cached.module_name.split(".", 1)[0]
        for module_path, cached in plugin_imports._MODULE_IMPORT_CACHE.items()
        if any(module_path.is_relative_to(root) for root in roots)
    }
    cancelled_task_count = _cancel_plugin_module_tasks(package_roots)
    _clear_plugin_reload_caches(roots)
    _evict_synthetic_plugin_subtrees(package_roots)
    plugins = load_plugins(config, runtime_paths)
    return PluginReloadResult(
        hook_registry=HookRegistry.from_plugins(plugins),
        active_plugin_names=tuple(plugin.name for plugin in plugins),
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


def _clear_plugin_reload_caches(roots: tuple[Path, ...]) -> None:
    """Drop manifest and module cache entries under the configured plugin roots."""
    for manifest_path in tuple(plugin_imports._PLUGIN_CACHE):
        if any(manifest_path.parent.is_relative_to(root) for root in roots):
            plugin_imports._PLUGIN_CACHE.pop(manifest_path, None)
    for module_path in tuple(plugin_imports._MODULE_IMPORT_CACHE):
        if any(module_path.is_relative_to(root) for root in roots):
            plugin_imports._MODULE_IMPORT_CACHE.pop(module_path, None)


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


def _prepare_plugin_tool_module_reload(
    module_name: str,
    cached: plugin_imports._ModuleCacheEntry | None,
) -> dict[str, dict[str, ToolMetadata]]:
    """Snapshot one tool module's cached registrations before reload."""
    previous_registrations_by_module_name: dict[str, dict[str, ToolMetadata]] = {}
    for candidate_module_name in {module_name, cached.module_name if cached is not None else None}:
        if candidate_module_name is None:
            continue
        previous_registrations_by_module_name[candidate_module_name] = _snapshot_plugin_tool_registrations(
            candidate_module_name,
        )
        _clear_plugin_tool_registrations(candidate_module_name)
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
        _restore_plugin_tool_registrations(restored_module_name, registrations)
    if cached is not None:
        plugin_imports._MODULE_IMPORT_CACHE[module_path] = cached
        sys.modules[cached.module_name] = cached.module
    else:
        plugin_imports._MODULE_IMPORT_CACHE.pop(module_path, None)


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

    module_name = plugin_imports._module_name(plugin_name, plugin_root, module_path)
    cached = plugin_imports._MODULE_IMPORT_CACHE.get(module_path)
    if cached is not None and cached.mtime == mtime and cached.module_name == module_name:
        return cached.module

    previous_registrations_by_module_name = (
        _prepare_plugin_tool_module_reload(module_name, cached) if kind == "tools" else {}
    )

    if cached is not None and cached.module_name != module_name:
        sys.modules.pop(cached.module_name, None)

    previous_packages = plugin_imports._snapshot_plugin_package_chain(plugin_name, plugin_root, module_path)
    plugin_imports._install_plugin_package_chain(plugin_name, plugin_root, module_path)
    spec = util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        plugin_imports._restore_plugin_package_chain(previous_packages)
        msg = f"Failed to load plugin {kind} module: {module_path}"
        logger.error("Failed to load plugin module", path=str(module_path), kind=kind)
        raise PluginValidationError(msg)

    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        if kind == "tools":
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
                plugin_imports._MODULE_IMPORT_CACHE[module_path] = cached
                sys.modules[cached.module_name] = cached.module
            else:
                plugin_imports._MODULE_IMPORT_CACHE.pop(module_path, None)
        plugin_imports._restore_plugin_package_chain(previous_packages)
        msg = f"Plugin {kind} module execution failed for {module_path}: {exc}"
        logger.exception("Plugin module execution failed", path=str(module_path), kind=kind, error=str(exc))
        raise PluginValidationError(msg) from exc

    plugin_imports._MODULE_IMPORT_CACHE[module_path] = plugin_imports._ModuleCacheEntry(
        mtime=mtime,
        module_name=module_name,
        module=module,
    )
    return module
