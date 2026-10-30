"""Plugin loading: the one plugin generation a process publishes, and each config's view of it.

A process publishes one ``PluginGeneration``: the module instances, tools, hooks, automations, skill directories, and
OAuth callbacks of the plugins it loaded. An operation captures ``config_plugins(config, runtime_paths)`` once and reads
every plugin fact it needs from that value, so a publication during the operation never mixes two generations.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import threading
import tokenize
import weakref
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from mindroom.automations.registry import iter_module_automations
from mindroom.config.plugin import PluginEntryConfig  # noqa: TC001
from mindroom.egress_broker.env import egress_tool_unavailable
from mindroom.hooks import HookRegistry, iter_module_hooks
from mindroom.logging_config import get_logger
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.plugin_imports import SkippedPlugin
from mindroom.tool_system.registry import (
    CANDIDATE_PLUGIN_MODULE_PREFIX,
    PLUGIN_MODULE_PREFIX,
    ToolMetadataValidationError,
    ToolRegistry,
    compose_tool_registry,
    plugin_module_registrations,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from types import ModuleType

    from mindroom.automations.registry import CheckFn
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookCallback
    from mindroom.mcp.config import MCPServerConfig
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.declarations import ToolMetadata

logger = get_logger(__name__)

_CONFIGURED_PLUGIN_ROOT_CACHE_MAX_SIZE = 128
# Guards the canonical plugin modules in sys.modules, the module cache, and the published generation.
_PLUGIN_LOCK = threading.RLock()
_CANDIDATE_NAMESPACES = itertools.count()


class _ConfiguredPluginRootCacheKey(NamedTuple):
    plugin_entries: tuple[tuple[str, bool], ...]
    config_path: str
    config_dir: str
    storage_root: str
    sys_path: tuple[str, ...]


_CONFIGURED_PLUGIN_ROOT_CACHE: dict[_ConfiguredPluginRootCacheKey, tuple[Path, ...]] = {}


@dataclass(frozen=True, eq=False)
class _LoadedPlugin:
    """One plugin a generation loaded: what its modules registered and discovered, and the modules themselves."""

    name: str
    root: Path
    skill_dirs: tuple[Path, ...]
    # The modules' paths and mtimes when they executed, so validation knows when the source changed.
    source: tuple[tuple[Path, float], ...]
    tools: Mapping[str, ToolMetadata]
    discovered_hooks: tuple[HookCallback, ...]
    discovered_automations: tuple[CheckFn, ...]
    oauth_callback: Callable[..., object] | None
    modules: tuple[ModuleType, ...]


@dataclass(frozen=True, eq=False)
class PluginGeneration:
    """The plugins one load produced: each one's registrations and module instances, and the plugins it skipped."""

    plugins: Mapping[Path, _LoadedPlugin] = field(default_factory=lambda: MappingProxyType({}))
    skipped: tuple[SkippedPlugin, ...] = ()
    # Every canonical plugin module the load left in sys.modules, and the module cache entries it built.
    modules: Mapping[str, ModuleType] = field(default_factory=lambda: MappingProxyType({}))
    module_cache: Mapping[str, plugin_imports._ModuleCacheEntry] = field(default_factory=lambda: MappingProxyType({}))
    _hooks: dict[tuple[str, str], HookRegistry] = field(default_factory=dict, repr=False)
    _hooks_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def hooks(self, configured: Sequence[_ConfiguredPlugin], key: tuple[str, str]) -> HookRegistry:
        """Return the hooks of the configured plugins this generation loaded, one registry per equal plugin entries.

        Hook contexts tell whether their registry is still live by identity, so configs whose plugin entries are equal
        share one registry.
        """
        with self._hooks_lock:
            registry = self._hooks.get(key)
            if registry is None:
                registry = HookRegistry.from_plugins(plugin for plugin in configured if plugin.loaded is not None)
                self._hooks[key] = registry
            return registry


@dataclass(frozen=True, slots=True)
class PluginReloadResult:
    """What one plugin publication made live."""

    active_plugin_names: tuple[str, ...]
    cancelled_task_count: int
    skipped: tuple[SkippedPlugin, ...] = ()


@dataclass(frozen=True, eq=False)
class _ConfiguredPlugin:
    """One enabled plugin entry of a config, matched with what a generation loaded for its root."""

    entry_config: PluginEntryConfig
    plugin_order: int
    root: Path | None
    loaded: _LoadedPlugin | None

    @property
    def name(self) -> str:
        assert self.loaded is not None
        return self.loaded.name

    @property
    def discovered_hooks(self) -> tuple[HookCallback, ...]:
        return self.loaded.discovered_hooks if self.loaded is not None else ()

    @property
    def discovered_automations(self) -> tuple[CheckFn, ...]:
        return self.loaded.discovered_automations if self.loaded is not None else ()


_PUBLISHED = PluginGeneration()


@dataclass(frozen=True, eq=False)
class ConfigPlugins:
    """One config's view of one plugin generation: everything an operation needs from plugins, from that generation.

    A configured plugin the generation did not load contributes only unavailable tools, which it declares in its
    source; composing never executes plugin code.
    """

    generation: PluginGeneration
    runtime_paths: RuntimePaths
    plugin_entries: tuple[PluginEntryConfig, ...]
    mcp_servers: Mapping[str, MCPServerConfig]

    @classmethod
    def of(cls, generation: PluginGeneration, config: Config, runtime_paths: RuntimePaths) -> ConfigPlugins:
        """Return ``config``'s view of ``generation``."""
        return cls(generation, runtime_paths, tuple(config.plugins), MappingProxyType(dict(config.mcp_servers)))

    @cached_property
    def _configured(self) -> tuple[_ConfiguredPlugin, ...]:
        configured: list[_ConfiguredPlugin] = []
        seen_roots: set[Path] = set()
        for plugin_order, entry in enumerate(self.plugin_entries):
            if not entry.enabled:
                continue
            try:
                root = plugin_imports._resolve_plugin_root(entry.path, self.runtime_paths)
            except plugin_imports.PluginValidationError:
                configured.append(_ConfiguredPlugin(entry, plugin_order, None, None))
                continue
            if root in seen_roots:
                continue
            seen_roots.add(root)
            configured.append(_ConfiguredPlugin(entry, plugin_order, root, self.generation.plugins.get(root)))
        return tuple(configured)

    @cached_property
    def tools(self) -> ToolRegistry:
        """Return the built-ins, the tools of the configured plugins the generation loaded, and the MCP tools."""
        # Deferred: the MCP registry imports this module through the tool runtime context its toolkit reads.
        from mindroom.mcp.registry import mcp_tool_entries  # noqa: PLC0415

        plugin_tools: dict[str, ToolMetadata] = {}
        unavailable: dict[str, ToolMetadata] = {}
        unresolved: set[str] = set()
        for plugin in self._configured:
            if plugin.loaded is not None:
                plugin_tools.update(plugin.loaded.tools)
                continue
            declared, unresolved_source = _declared_plugin_tools(plugin)
            unavailable.update(declared)
            if unresolved_source is not None:
                unresolved.add(unresolved_source)
        registry = compose_tool_registry(
            plugin_tools,
            mcp_tool_entries(self.mcp_servers),
            unavailable=unavailable,
            unresolved_plugin_sources=frozenset(unresolved),
        )
        return replace(
            registry,
            runtime_unavailable=frozenset(
                name for name in registry if egress_tool_unavailable(name, self.runtime_paths)
            ),
        )

    @cached_property
    def hooks(self) -> HookRegistry:
        """Return the hooks of the configured plugins, with the config's plugin settings and hook overrides."""
        key = (
            str(self.runtime_paths.config_dir),
            json.dumps([entry.model_dump(mode="json") for entry in self.plugin_entries], sort_keys=True),
        )
        return self.generation.hooks(self._configured, key)

    @cached_property
    def skill_roots(self) -> tuple[Path, ...]:
        """Return the skill directories of the configured plugins the generation loaded."""
        return tuple(
            dict.fromkeys(
                skill_dir
                for plugin in self._configured
                if plugin.loaded is not None
                for skill_dir in plugin.loaded.skill_dirs
            ),
        )

    @cached_property
    def oauth_providers(self) -> Mapping[str, OAuthProvider]:
        """Return the built-in, MCP, and configured plugins' OAuth providers; a plugin whose callback fails is skipped."""
        # Deferred: the OAuth registry imports every provider family, which the plugin loader must not load.
        from mindroom.oauth.registry import oauth_provider_registry  # noqa: PLC0415

        return MappingProxyType(oauth_provider_registry(self))

    def plugin_oauth_callbacks(self) -> tuple[tuple[Path, PluginEntryConfig, Callable[..., object]], ...]:
        """Return the OAuth callback of each configured plugin the generation loaded, with its root and entry."""
        return tuple(
            (plugin.loaded.root, plugin.entry_config, plugin.loaded.oauth_callback)
            for plugin in self._configured
            if plugin.loaded is not None and plugin.loaded.oauth_callback is not None
        )

    @property
    def loaded_plugins(self) -> tuple[_LoadedPlugin, ...]:
        """Return the configured plugins the generation loaded, in config order."""
        return tuple(plugin.loaded for plugin in self._configured if plugin.loaded is not None)


class _CachedConfigPlugins(NamedTuple):
    config: weakref.ref[Config]
    plugins: ConfigPlugins


# The latest view per live config object; older views live as long as the operations that captured them.
_CONFIG_PLUGINS: dict[int, _CachedConfigPlugins] = {}
# Reentrant: the cyclic collector can free a config, and so run its finalizer, inside an allocation on the thread
# that holds the lock; the finalizer drops only its own config's entry.
_CONFIG_PLUGINS_LOCK = threading.RLock()


def _forget_config_plugins(config_id: int, config_ref: weakref.ref[Config]) -> None:
    with _CONFIG_PLUGINS_LOCK:
        cached = _CONFIG_PLUGINS.get(config_id)
        if cached is not None and cached.config is config_ref:
            del _CONFIG_PLUGINS[config_id]


def config_plugins(config: Config, runtime_paths: RuntimePaths) -> ConfigPlugins:
    """Capture the published plugin generation once and return ``config``'s view of it."""
    generation = _PUBLISHED
    config_id = id(config)
    with _CONFIG_PLUGINS_LOCK:
        cached = _CONFIG_PLUGINS.get(config_id)
        if cached is not None and cached.config() is not config:
            # The object this id named is gone; its finalizer has not run yet.
            cached = None
        if (
            cached is not None
            and cached.plugins.generation is generation
            and cached.plugins.runtime_paths == runtime_paths
        ):
            return cached.plugins
        plugins = ConfigPlugins.of(generation, config, runtime_paths)
        if cached is None:
            config_ref = weakref.ref(config)
            weakref.finalize(config, _forget_config_plugins, config_id, config_ref)
        else:
            config_ref = cached.config
        _CONFIG_PLUGINS[config_id] = _CachedConfigPlugins(config_ref, plugins)
        return plugins


def _declared_plugin_tools(plugin: _ConfiguredPlugin) -> tuple[dict[str, ToolMetadata], str | None]:
    """Return the tools an unloaded plugin's source declares, and the source whose tool names may be incomplete."""
    if plugin.root is None:
        return {}, plugin.entry_config.path
    try:
        base = plugin_imports._load_plugin_base(plugin.root)
    except plugin_imports.PluginValidationError:
        return {}, plugin.entry_config.path
    return _declared_tools(base), base.name if base.tools_module_path is not None else None


def _declared_tools(plugin: plugin_imports._PluginBase) -> dict[str, ToolMetadata]:
    declared: dict[str, ToolMetadata] = {}
    for module_path in (plugin.tools_module_path, plugin.hooks_module_path):
        if module_path is not None:
            declared.update(plugin_imports.declared_tool_metadata(module_path))
    return declared


def _raise_if_host_control_exception(exc: BaseException) -> None:
    if not isinstance(exc, (Exception, SystemExit)):
        raise exc


def _canonical_plugin_modules() -> dict[str, ModuleType]:
    return {
        module_name: module
        for module_name, module in tuple(sys.modules.items())
        if module_name.startswith(PLUGIN_MODULE_PREFIX) and module is not None
    }


def _modules_under(prefix: str) -> dict[str, ModuleType]:
    return {
        module_name: module
        for module_name, module in tuple(sys.modules.items())
        if (module_name == prefix or module_name.startswith(f"{prefix}.")) and module is not None
    }


def prepare_plugins(config: Config, runtime_paths: RuntimePaths) -> PluginGeneration:
    """Load ``config``'s plugins fresh into an unpublished generation, leaving the installed modules in place.

    A plugin that fails to load is skipped. A structural error, such as two configured plugins with one manifest name
    or two plugins registering one tool, raises, and nothing this preparation started keeps running.
    """
    import mindroom.tools  # noqa: F401, PLC0415 - built-ins register before any plugin may collide with them.

    with _PLUGIN_LOCK:
        installed_modules = _canonical_plugin_modules()
        installed_cache = dict(plugin_imports._MODULE_IMPORT_CACHE)
        installed_manifests = dict(plugin_imports._PLUGIN_CACHE)
        try:
            plugin_imports._MODULE_IMPORT_CACHE.clear()
            plugin_imports._PLUGIN_CACHE.clear()
            for module_name in installed_modules:
                sys.modules.pop(module_name, None)
            try:
                plugins, skipped = _load_plugins(config, runtime_paths, previous=None)
            except BaseException:
                _cancel_module_tasks(_canonical_plugin_modules().values())
                raise
            generation = PluginGeneration(
                plugins=MappingProxyType(plugins),
                skipped=skipped,
                modules=MappingProxyType(_canonical_plugin_modules()),
                module_cache=MappingProxyType(dict(plugin_imports._MODULE_IMPORT_CACHE)),
            )
            _log_loaded_plugins(generation)
            return generation
        finally:
            for module_name in _canonical_plugin_modules():
                sys.modules.pop(module_name, None)
            sys.modules.update(installed_modules)
            plugin_imports._MODULE_IMPORT_CACHE.clear()
            plugin_imports._MODULE_IMPORT_CACHE.update(installed_cache)
            plugin_imports._PLUGIN_CACHE.clear()
            plugin_imports._PLUGIN_CACHE.update(installed_manifests)


def publish_plugins(generation: PluginGeneration) -> PluginReloadResult:
    """Make ``generation`` this process's published plugins, cancelling the tasks of the modules it replaces."""
    global _PUBLISHED
    with _PLUGIN_LOCK:
        kept = {id(module) for module in generation.modules.values()}
        replaced = [*_canonical_plugin_modules().values(), *_PUBLISHED.modules.values()]
        cancelled_task_count = _cancel_module_tasks(module for module in replaced if id(module) not in kept)
        for module_name in _canonical_plugin_modules():
            sys.modules.pop(module_name, None)
        sys.modules.update(generation.modules)
        plugin_imports._MODULE_IMPORT_CACHE.clear()
        plugin_imports._MODULE_IMPORT_CACHE.update(generation.module_cache)
        _clear_configured_plugin_roots_cache()
        _PUBLISHED = generation
    return PluginReloadResult(
        active_plugin_names=tuple(plugin.name for plugin in generation.plugins.values()),
        cancelled_task_count=cancelled_task_count,
        skipped=generation.skipped,
    )


def dispose_plugins(generation: PluginGeneration) -> None:
    """Cancel the tasks of a generation that will not be published."""
    with _PLUGIN_LOCK:
        published = {id(module) for module in _PUBLISHED.modules.values()}
        _cancel_module_tasks(module for module in generation.modules.values() if id(module) not in published)


def refresh_plugins(config: Config, runtime_paths: RuntimePaths) -> ConfigPlugins:
    """Load ``config``'s plugins into this process's generation and return ``config``'s view; the worker's publisher.

    Unchanged modules stay as installed and edited ones execute again, on every worker request. The generation
    only grows or refreshes, so another request's config keeps the plugins it loaded; a plugin that fails to load
    leaves it until it loads again.
    """
    global _PUBLISHED
    import mindroom.tools  # noqa: F401, PLC0415 - built-ins register before any plugin may collide with them.

    with _PLUGIN_LOCK:
        published = _PUBLISHED
        plugins, skipped = _load_plugins(config, runtime_paths, previous=published)
        failed_roots = {plugin.root for plugin in skipped} & set(published.plugins)
        changed = {root: plugin for root, plugin in plugins.items() if published.plugins.get(root) is not plugin}
        if changed or failed_roots:
            generation = PluginGeneration(
                plugins=MappingProxyType(
                    {
                        **{root: plugin for root, plugin in published.plugins.items() if root not in failed_roots},
                        **plugins,
                    },
                ),
                modules=MappingProxyType(_canonical_plugin_modules()),
                module_cache=MappingProxyType(dict(plugin_imports._MODULE_IMPORT_CACHE)),
            )
            if changed:
                _log_loaded_plugins(generation)
            _PUBLISHED = generation
    return config_plugins(config, runtime_paths)


def candidate_tool_registry(
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[ToolRegistry, tuple[SkippedPlugin, ...]]:
    """Return the tools a candidate config can build and the plugins that failed to load, changing nothing live.

    A plugin the published generation loaded from unchanged source is not executed again. A new or changed one
    executes under a separate module namespace, which is removed afterwards with any task it started.
    """
    import mindroom.tools  # noqa: F401, PLC0415 - built-ins register before any plugin may collide with them.

    # Deferred: the MCP registry imports this module through the tool runtime context its toolkit reads.
    from mindroom.mcp.registry import mcp_tool_entries  # noqa: PLC0415

    plugin_bases, skipped = plugin_imports._collect_plugin_bases(config.plugins, runtime_paths)
    plugin_imports._reject_duplicate_plugin_manifest_names(plugin_bases)
    published = _PUBLISHED
    unresolved = {plugin.path for plugin in skipped}
    plugin_tools: dict[str, tuple[str, ToolMetadata]] = {}
    unavailable: dict[str, ToolMetadata] = {}
    for plugin_base, plugin_entry in plugin_bases:
        loaded = published.plugins.get(plugin_base.root)
        if loaded is not None and loaded.name == plugin_base.name and loaded.source == _plugin_source(plugin_base):
            tools: Mapping[str, ToolMetadata] = loaded.tools
        else:
            registrations: dict[str, ToolMetadata] = {}
            completed: list[str] = []
            try:
                _execute_candidate_plugin(plugin_base, registrations, completed)
            except (Exception, SystemExit) as exc:
                plugin_imports._log_skipped_plugin_entry(plugin_entry.path, plugin_base.root, exc)
                skipped.append(SkippedPlugin(plugin_entry.path, _plugin_error(plugin_base, exc), plugin_base.root))
                if plugin_base.tools_module_path is not None and "tools" not in completed:
                    unresolved.add(plugin_base.name)
                unavailable.update(_declared_tools(plugin_base))
                unavailable.update(registrations)
                continue
            tools = registrations
        _add_plugin_tools(plugin_tools, plugin_base.name, tools)
    registry = compose_tool_registry(
        {name: metadata for name, (_, metadata) in plugin_tools.items()},
        mcp_tool_entries(config.mcp_servers),
        unavailable=unavailable,
        unresolved_plugin_sources=frozenset(unresolved),
    )
    return registry, tuple(skipped)


def _execute_candidate_plugin(
    plugin: plugin_imports._PluginBase,
    registrations: dict[str, ToolMetadata],
    completed: list[str],
) -> None:
    """Execute one plugin's tools and hooks modules under a fresh namespace, collecting its tools module's tools.

    Afterwards the namespace is removed, with any module from the plugin's root they imported by absolute name, so
    the plugin's next load imports and registers those again. Live plugin modules another thread imports meanwhile
    stay.
    """
    prefix = f"{CANDIDATE_PLUGIN_MODULE_PREFIX}{next(_CANDIDATE_NAMESPACES)}_"
    loaded_before = set(sys.modules)
    try:
        for kind, module_path in (("tools", plugin.tools_module_path), ("hooks", plugin.hooks_module_path)):
            if module_path is None or (kind == "hooks" and module_path == plugin.tools_module_path):
                continue
            entry = _load_plugin_module(plugin.name, plugin.root, module_path, kind=kind, prefix=prefix)
            assert entry is not None
            if kind == "tools":
                registrations.update(entry.registrations)
            completed.append(kind)
    finally:
        candidate_modules = {
            module_name: module
            for module_name, module in tuple(sys.modules.items())
            if module is not None
            and (
                module_name.startswith(prefix)
                or (
                    module_name not in loaded_before
                    and not module_name.startswith(PLUGIN_MODULE_PREFIX)
                    and _module_within(module, plugin.root)
                )
            )
        }
        for module_name in candidate_modules:
            sys.modules.pop(module_name, None)
        _cancel_module_tasks(candidate_modules.values())


def _module_within(module: ModuleType, root: Path) -> bool:
    module_file = getattr(module, "__file__", None)
    return isinstance(module_file, str) and Path(module_file).resolve().is_relative_to(root)


def _plugin_error(plugin: plugin_imports._PluginBase, exc: BaseException) -> Exception:
    if isinstance(exc, Exception):
        return exc
    msg = f"Plugin materialization failed for {plugin.root}: {exc}"
    return plugin_imports.PluginValidationError(msg)


def _add_plugin_tools(
    plugin_tools: dict[str, tuple[str, ToolMetadata]],
    plugin_name: str,
    tools: Mapping[str, ToolMetadata],
) -> None:
    for tool_name, metadata in tools.items():
        owner = plugin_tools.get(tool_name)
        if owner is not None and owner[0] != plugin_name:
            msg = f"Plugin tool '{tool_name}' conflicts between plugins '{owner[0]}' and '{plugin_name}'."
            raise ToolMetadataValidationError(msg)
        plugin_tools[tool_name] = (plugin_name, metadata)


def _plugin_source(plugin: plugin_imports._PluginBase) -> tuple[tuple[Path, float], ...] | None:
    """Return the paths and mtimes of one plugin's modules, or None when one cannot be read."""
    paths = dict.fromkeys(
        path for path in (plugin.tools_module_path, plugin.hooks_module_path, plugin.oauth_module_path) if path
    )
    try:
        return tuple((path, path.stat().st_mtime) for path in paths)
    except OSError:
        return None


def _load_plugins(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    previous: PluginGeneration | None,
) -> tuple[dict[Path, _LoadedPlugin], tuple[SkippedPlugin, ...]]:
    """Load ``config``'s enabled plugins through the canonical module cache, skipping each one that fails."""
    plugin_bases, skipped = plugin_imports._collect_plugin_bases(config.plugins, runtime_paths)
    plugin_imports._reject_duplicate_plugin_manifest_names(plugin_bases)
    plugins: dict[Path, _LoadedPlugin] = {}
    for plugin_base, plugin_entry in plugin_bases:
        package_name = plugin_imports._plugin_package_name(plugin_base.name, plugin_base.root)
        installed_modules = _modules_under(package_name)
        installed_cache = {
            name: entry
            for name, entry in plugin_imports._MODULE_IMPORT_CACHE.items()
            if name.startswith(f"{package_name}.")
        }
        try:
            plugin = _materialize_plugin(
                plugin_base,
                previous.plugins.get(plugin_base.root) if previous is not None else None,
            )
        except (Exception, SystemExit) as exc:
            _dispose_failed_plugin(package_name, installed_modules, installed_cache)
            plugin_imports._log_skipped_plugin_entry(plugin_entry.path, plugin_base.root, exc)
            skipped.append(SkippedPlugin(plugin_entry.path, _plugin_error(plugin_base, exc), plugin_base.root))
            continue
        except BaseException:
            _dispose_failed_plugin(package_name, installed_modules, installed_cache)
            raise
        plugins[plugin_base.root] = plugin
    plugin_tools: dict[str, tuple[str, ToolMetadata]] = {}
    for plugin in plugins.values():
        _add_plugin_tools(plugin_tools, plugin.name, plugin.tools)
    return plugins, tuple(skipped)


def _dispose_failed_plugin(
    package_name: str,
    installed_modules: dict[str, ModuleType],
    installed_cache: dict[str, plugin_imports._ModuleCacheEntry],
) -> None:
    """Cancel the tasks of a failed plugin's newly executed modules and reinstate its installed ones."""
    installed = {id(module) for module in installed_modules.values()}
    executed = _modules_under(package_name)
    _cancel_module_tasks(module for module in executed.values() if id(module) not in installed)
    for module_name in executed:
        sys.modules.pop(module_name, None)
    sys.modules.update(installed_modules)
    for module_name in tuple(plugin_imports._MODULE_IMPORT_CACHE):
        if module_name.startswith(f"{package_name}."):
            del plugin_imports._MODULE_IMPORT_CACHE[module_name]
    plugin_imports._MODULE_IMPORT_CACHE.update(installed_cache)


def _materialize_plugin(plugin: plugin_imports._PluginBase, previous: _LoadedPlugin | None) -> _LoadedPlugin:
    tools_entry = _load_plugin_module(plugin.name, plugin.root, plugin.tools_module_path, kind="tools")
    # Without its own hooks module, a plugin's hooks live in its tools module.
    hooks_entry = (
        tools_entry
        if plugin.hooks_module_path is None
        else _load_plugin_module(plugin.name, plugin.root, plugin.hooks_module_path, kind="hooks")
    )
    oauth_entry = _load_plugin_module(plugin.name, plugin.root, plugin.oauth_module_path, kind="oauth")
    entries = tuple(dict.fromkeys(entry for entry in (tools_entry, hooks_entry, oauth_entry) if entry is not None))
    modules = tuple(entry.module for entry in entries)
    module_paths = (
        (plugin.tools_module_path, tools_entry),
        (plugin.hooks_module_path, hooks_entry),
        (plugin.oauth_module_path, oauth_entry),
    )
    source = {path: entry.mtime for path, entry in module_paths if path is not None and entry is not None}
    if (
        previous is not None
        and previous.name == plugin.name
        and previous.modules == modules
        and previous.skill_dirs == tuple(plugin.skill_dirs)
    ):
        return previous
    hooks_module = hooks_entry.module if hooks_entry is not None else None
    return _LoadedPlugin(
        name=plugin.name,
        root=plugin.root,
        skill_dirs=tuple(plugin.skill_dirs),
        source=tuple(source.items()),
        # A plugin's tools are the ones its tools module registers.
        tools=tools_entry.registrations if tools_entry is not None else MappingProxyType({}),
        discovered_hooks=tuple(iter_module_hooks(hooks_module)) if hooks_module is not None else (),
        discovered_automations=tuple(iter_module_automations(hooks_module)) if hooks_module is not None else (),
        oauth_callback=_oauth_provider_callback(oauth_entry.module) if oauth_entry is not None else None,
        modules=modules,
    )


def _oauth_provider_callback(module: ModuleType) -> Callable[..., object]:
    callback = vars(module).get("register_oauth_providers")
    if not callable(callback):
        msg = "OAuth plugin module must define callable register_oauth_providers(settings, runtime_paths)"
        raise plugin_imports.PluginValidationError(msg)
    return callback


def _log_loaded_plugins(generation: PluginGeneration) -> None:
    if not generation.plugins:
        return
    logger.info("Loaded plugins", plugins=[plugin.name for plugin in generation.plugins.values()])
    for plugin in generation.plugins.values():
        if plugin.discovered_hooks:
            logger.info(
                "Discovered plugin hooks",
                plugin_name=plugin.name,
                hook_names=[_hook_display_name(hook) for hook in plugin.discovered_hooks],
            )


def _hook_display_name(callback: HookCallback) -> str:
    return cast("Any", callback).__name__


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
        except plugin_imports.PluginValidationError:
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


def _clear_configured_plugin_roots_cache() -> None:
    """Drop cached configured plugin roots after a publication."""
    _CONFIGURED_PLUGIN_ROOT_CACHE.clear()


def _cancel_module_tasks(modules: Iterable[ModuleType]) -> int:
    """Best-effort cancel the unfinished tasks the given plugin modules hold, each task once."""
    cancelled: dict[int, asyncio.Task[Any]] = {}
    for module in modules:
        for task in _module_tasks(module):
            if task.done() or id(task) in cancelled:
                continue
            cancelled[id(task)] = task
            _cancel_task(task)
    return len(cancelled)


def _cancel_task(task: asyncio.Task[Any]) -> None:
    loop = task.get_loop()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop or loop.is_closed():
        task.cancel()
    else:
        loop.call_soon_threadsafe(task.cancel)


def _module_tasks(module: ModuleType) -> tuple[asyncio.Task[Any], ...]:
    """Return the tasks one plugin module holds in its globals or one level inside them."""
    # Loads read these globals from worker threads while the event loop changes them, so this and _iter_module_tasks copy each container before iterating.
    return tuple(task for value in tuple(vars(module).values()) for task in _iter_module_tasks(value))


def _iter_module_tasks(value: object) -> tuple[asyncio.Task[Any], ...]:
    """Return task globals or one-level container-held tasks from one module value."""
    if isinstance(value, asyncio.Task):
        return (value,)
    if isinstance(value, dict):
        values = tuple(value.values())
    elif isinstance(value, tuple | list | set):
        values = tuple(value)
    else:
        return ()
    return tuple(item for item in values if isinstance(item, asyncio.Task))


def _load_plugin_module(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path | None,
    *,
    kind: str,
    prefix: str = PLUGIN_MODULE_PREFIX,
) -> plugin_imports._ModuleCacheEntry | None:
    """Execute one plugin module, or return its installed instance while its file is unchanged.

    The canonical ``prefix`` installs the module and caches it by mtime; another prefix executes it under a separate
    namespace, with its relative imports, and caches nothing.
    """
    if module_path is None:
        return None
    try:
        mtime = module_path.stat().st_mtime
    except OSError as exc:
        msg = f"Failed to stat plugin {kind} module {module_path}: {exc}"
        logger.exception("Failed to stat plugin module", path=str(module_path), kind=kind, error=str(exc))
        raise plugin_imports.PluginValidationError(msg) from exc

    canonical = prefix == PLUGIN_MODULE_PREFIX
    module_name = plugin_imports._module_name(plugin_name, plugin_root, module_path, prefix)
    cached = plugin_imports._MODULE_IMPORT_CACHE.get(module_name) if canonical else None
    if cached is not None and cached.mtime == mtime:
        return cached

    previous_module = sys.modules.get(module_name)
    prepared_module = plugin_imports._prepare_module(plugin_name, plugin_root, module_path, module_name, prefix)
    if prepared_module is None:
        msg = f"Failed to load plugin {kind} module: {module_path}"
        logger.error("Failed to load plugin module", path=str(module_path), kind=kind)
        raise plugin_imports.PluginValidationError(msg)
    module, _, previous_packages = prepared_module

    try:
        # A module may provide several capabilities; whatever it registers belongs to this module.
        with plugin_module_registrations(module_name) as registrations:
            _exec_plugin_source(module_path, module)
    except BaseException as exc:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
        plugin_imports._restore_plugin_package_chain(previous_packages)
        _raise_if_host_control_exception(exc)
        msg = f"Plugin {kind} module execution failed for {module_path}: {exc}"
        logger.exception("Plugin module execution failed", path=str(module_path), kind=kind, error=str(exc))
        raise plugin_imports.PluginValidationError(msg) from exc

    entry = plugin_imports._ModuleCacheEntry(mtime=mtime, module=module, registrations=MappingProxyType(registrations))
    if canonical:
        plugin_imports._MODULE_IMPORT_CACHE[module_name] = entry
    return entry


def _exec_plugin_source(module_path: Path, module: ModuleType) -> None:
    with tokenize.open(str(module_path)) as source_file:
        source = source_file.read()
    code = compile(source, str(module_path), "exec", dont_inherit=True)
    exec(code, module.__dict__)  # noqa: S102 - configured plugin modules are intentionally executable code.
