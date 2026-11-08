"""Plugin loading: the one plugin generation a process publishes, and each config's view of it.

A process publishes one ``PluginGeneration``: the module instances, tools, hooks, automations, skill directories, OAuth
callbacks, and HTTP routes of the plugins it loaded. An operation captures ``config_plugins(config, runtime_paths)``
once and reads every plugin fact it needs from that value, so a publication during the operation never mixes two
generations.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import threading
import tokenize
import weakref
from contextlib import contextmanager, suppress
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
    from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
    from types import ModuleType

    from starlette.requests import HTTPConnection
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from mindroom.automations.registry import CheckFn
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookCallback
    from mindroom.mcp.config import MCPServerConfig
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.plugin_lifecycle import PluginLifespan
    from mindroom.tool_system.declarations import ToolMetadata

logger = get_logger(__name__)

_CONFIGURED_PLUGIN_ROOT_CACHE_MAX_SIZE = 128
# Guards the canonical plugin modules in sys.modules, the module cache, the root packages, and the published generation.
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
    """One plugin a generation loaded: what its modules registered and discovered."""

    name: str
    # The plugin API version it needs, as its manifest states it.
    mindroom_plugin_api: str | None
    root: Path
    skill_dirs: tuple[Path, ...]
    # The content-key and event-type prefixes its manifest reserves from agent tools.
    reserved_namespaces: tuple[str, ...]
    # The tools, hooks, and OAuth modules' paths and mtimes as the load began, so validation and worker refreshes know
    # when the source changed; candidate validation never imports a routes or lifecycle module, so it leaves them out.
    source: tuple[tuple[Path, float], ...]
    tools: Mapping[str, ToolMetadata]
    discovered_hooks: tuple[HookCallback, ...]
    discovered_automations: tuple[CheckFn, ...]
    oauth_callback: Callable[..., object] | None
    # The plugin's router as an app of its own, built by staged loads (the primary's and the CLI checks'); worker loads
    # never import a routes module.
    routes_app: PluginRoutesApp | None
    # Why a staged load serves no routes from the plugin's routes module, which leaves its tools and hooks loaded.
    routes_error: plugin_imports.PluginValidationError | None
    # The lifecycle module's ``lifespan``, imported by staged loads like the routes module; only the primary runs it.
    lifespan: PluginLifespan | None
    # Why a staged load found no ``lifespan`` in the plugin's lifecycle module, which leaves the rest of it loaded.
    lifespan_error: plugin_imports.PluginValidationError | None


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

# Each plugin's ``PluginLifecycle.process_state`` by plugin name, kept for the process, so it outlives reloads.
_PROCESS_STATE: dict[str, dict[str, Any]] = {}
# The package name a plugin load imported the plugin's root under, such as ``demo_pkg`` for ``demo_pkg.helpers``, with
# that root. The modules under it, whenever imported, are the plugin's modules too, so its next load imports them again.
_ROOT_PACKAGES: dict[str, Path] = {}

# The scope key naming the plugin whose routes serve a request: ``mindroom.api.plugin_routes`` sets it, and
# ``mindroom.plugin_http.plugin_context`` reads it.
PLUGIN_NAME_SCOPE_KEY = "mindroom.plugin_name"


class PluginInactiveError(RuntimeError):
    """A plugin's context was used while the current config does not load the plugin; plugin routes answer 503."""

    def __init__(self, plugin_name: str) -> None:
        super().__init__(f"Plugin {plugin_name!r} is not active: the current config does not load it.")


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
            _mcp_tool_entries(self.mcp_servers),
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
    def reserved_namespaces(self) -> tuple[str, ...]:
        """Return the content-key and event-type prefixes reserved by the configured plugins the generation loaded."""
        return tuple(namespace for plugin in self.loaded_plugins for namespace in plugin.reserved_namespaces)

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

    def plugin_entry(self, plugin_name: str) -> PluginEntryConfig | None:
        """Return the config entry of the loaded plugin ``plugin_name``, or None when this config does not load it."""
        for plugin in self._configured:
            if plugin.loaded is not None and plugin.loaded.name == plugin_name:
                return plugin.entry_config
        return None


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
    """Return the canonical plugin modules in sys.modules: those under the canonical prefix and the root packages."""
    return _modules_named(tuple(_ROOT_PACKAGES), PLUGIN_MODULE_PREFIX)


def _plugin_modules(plugin: plugin_imports._PluginBase) -> dict[str, ModuleType]:
    """Return one plugin's canonical modules: those under its package and under the package names of its root."""
    return _modules_named(
        (
            plugin_imports._plugin_package_name(plugin.name, plugin.root),
            *(package for package, root in tuple(_ROOT_PACKAGES.items()) if root == plugin.root),
        ),
    )


def _modules_named(packages: tuple[str, ...], *prefixes: str) -> dict[str, ModuleType]:
    """Return the modules in sys.modules that are or are under ``packages``, or whose names start with ``prefixes``."""
    starts = (*prefixes, *(f"{package}." for package in packages))
    return {
        module_name: module
        for module_name, module in tuple(sys.modules.items())
        if (module_name in packages or module_name.startswith(starts)) and module is not None
    }


def _evict_plugin_modules(
    plugin: plugin_imports._PluginBase,
) -> tuple[dict[str, ModuleType], dict[str, plugin_imports._ModuleCacheEntry]]:
    """Remove one plugin's canonical modules and their module cache entries, and return both."""
    prefix = f"{plugin_imports._plugin_package_name(plugin.name, plugin.root)}."
    modules = _plugin_modules(plugin)
    cache = {name: entry for name, entry in plugin_imports._MODULE_IMPORT_CACHE.items() if name.startswith(prefix)}
    for module_name in modules:
        sys.modules.pop(module_name, None)
    for module_name in cache:
        del plugin_imports._MODULE_IMPORT_CACHE[module_name]
    return modules, cache


def _imported_from_root(root: Path, loaded_before: set[str]) -> dict[str, ModuleType]:
    """Return the modules imported from ``root`` since ``loaded_before`` under a name outside the canonical prefix."""
    return {
        module_name: module
        for module_name, module in tuple(sys.modules.items())
        if module is not None
        and module_name not in loaded_before
        and not module_name.startswith(PLUGIN_MODULE_PREFIX)
        and _module_within(module, root)
    }


def _root_packages(root: Path, loaded_before: set[str]) -> set[str]:
    """Return the package names the modules imported since ``loaded_before`` reached ``root`` under."""
    packages: set[str] = set()
    for module_name, module in _imported_from_root(root, loaded_before).items():
        # A candidate check running meanwhile imports under its own namespace.
        if module_name.startswith(CANDIDATE_PLUGIN_MODULE_PREFIX):
            continue
        relative = Path(cast("str", module.__file__)).resolve().relative_to(root).with_suffix("").parts
        if relative[-1:] == ("__init__",):
            relative = relative[:-1]
        parts = tuple(module_name.split("."))
        depth = len(parts) - len(relative)
        # A vendored package maps to no root package.
        if depth > 0 and parts[depth:] == relative:
            packages.add(".".join(parts[:depth]))
    return packages


def _install_canonical_modules(
    modules: Mapping[str, ModuleType],
    module_cache: Mapping[str, plugin_imports._ModuleCacheEntry],
) -> None:
    """Make ``modules`` the canonical plugin modules in sys.modules, and ``module_cache`` the module cache."""
    for module_name in _canonical_plugin_modules():
        sys.modules.pop(module_name, None)
    sys.modules.update(modules)
    plugin_imports._MODULE_IMPORT_CACHE.clear()
    plugin_imports._MODULE_IMPORT_CACHE.update(module_cache)


def _installed_generation(
    plugins: Mapping[Path, _LoadedPlugin],
    skipped: tuple[SkippedPlugin, ...] = (),
) -> PluginGeneration:
    """Return a generation of ``plugins`` holding the canonical plugin modules and module cache installed now."""
    return PluginGeneration(
        plugins=MappingProxyType(dict(plugins)),
        skipped=skipped,
        modules=MappingProxyType(_canonical_plugin_modules()),
        module_cache=MappingProxyType(dict(plugin_imports._MODULE_IMPORT_CACHE)),
    )


def _mcp_tool_entries(mcp_servers: Mapping[str, MCPServerConfig]) -> dict[str, ToolMetadata]:
    # Deferred: the MCP registry imports this module through the tool runtime context its toolkit reads.
    from mindroom.mcp.registry import mcp_tool_entries  # noqa: PLC0415

    return mcp_tool_entries(mcp_servers)


def prepare_plugins(config: Config, runtime_paths: RuntimePaths) -> PluginGeneration:
    """Load ``config``'s plugins fresh into an unpublished generation, leaving the installed modules in place.

    A plugin that fails to load is skipped, unless its entry is required. A required plugin that fails, or a structural
    error, such as two configured plugins with one manifest name or two plugins registering one tool, raises, and
    nothing this preparation started keeps running.
    """
    with staged_plugins(config, runtime_paths) as generation:
        return generation


@contextmanager
def staged_plugins(config: Config, runtime_paths: RuntimePaths) -> Iterator[PluginGeneration]:
    """Load ``config``'s plugins fresh, as ``prepare_plugins`` does, with their modules importable during the block.

    The installed modules return when the block ends, so code the block runs, such as a plugin callback's lazy relative
    import, finds the staged modules; another thread's import meanwhile can see them too.
    """
    import mindroom.tools  # noqa: F401, PLC0415 - built-ins register before any plugin may collide with them.

    with _PLUGIN_LOCK:
        installed_modules = _canonical_plugin_modules()
        installed_cache = dict(plugin_imports._MODULE_IMPORT_CACHE)
        installed_manifests = dict(plugin_imports._PLUGIN_CACHE)
        try:
            _install_canonical_modules({}, {})
            plugin_imports._PLUGIN_CACHE.clear()
            plugins, skipped = _load_plugins(config, runtime_paths, previous=None, primary_modules=True)
            generation = _installed_generation(plugins, skipped)
            _raise_if_required_plugin_skipped(config, generation)
            _log_loaded_plugins(generation)
            yield generation
        finally:
            _install_canonical_modules(installed_modules, installed_cache)
            plugin_imports._PLUGIN_CACHE.clear()
            plugin_imports._PLUGIN_CACHE.update(installed_manifests)


def _raise_if_required_plugin_skipped(config: Config, generation: PluginGeneration) -> None:
    """Raise with each skipped plugin whose entry is required, ending the tasks ``generation`` started."""
    required = {entry.path for entry in config.plugins if entry.enabled and entry.required}
    failures = [
        f"Required plugin {plugin.path} failed to load: {plugin.error}"
        for plugin in generation.skipped
        if plugin.path in required
    ]
    if failures:
        dispose_plugins(generation)
        raise plugin_imports.PluginValidationError("; ".join(failures))


def publish_plugins(generation: PluginGeneration) -> PluginReloadResult:
    """Make ``generation`` this process's published plugins, cancelling the tasks of the modules it replaces."""
    global _PUBLISHED
    with _PLUGIN_LOCK:
        kept = {id(module) for module in generation.modules.values()}
        replaced = [*_canonical_plugin_modules().values(), *_PUBLISHED.modules.values()]
        cancelled_task_count = _cancel_module_tasks(module for module in replaced if id(module) not in kept)
        _install_canonical_modules(generation.modules, generation.module_cache)
        _clear_configured_plugin_roots_cache()
        _PUBLISHED = generation
    return PluginReloadResult(
        active_plugin_names=tuple(plugin.name for plugin in generation.plugins.values()),
        cancelled_task_count=cancelled_task_count,
        skipped=generation.skipped,
    )


def plugin_process_state(plugin_name: str) -> dict[str, Any]:
    """Return the state ``plugin_name``'s lifespans share for this process; reloads cancel no task held in it."""
    return _PROCESS_STATE.setdefault(plugin_name, {})


def dispose_plugins(generation: PluginGeneration) -> None:
    """Cancel the tasks of a generation that will not be published."""
    with _PLUGIN_LOCK:
        published = {id(module) for module in _PUBLISHED.modules.values()}
        _cancel_module_tasks(module for module in generation.modules.values() if id(module) not in published)


def refresh_plugins(config: Config, runtime_paths: RuntimePaths) -> ConfigPlugins:
    """Load ``config``'s plugins into this process's generation and return ``config``'s view; the worker's publisher.

    Unchanged plugins stay as installed, and each module of a plugin whose tools, hooks, or OAuth module changed
    executes again, on every worker request, ending the tasks of the modules they replace. The generation only grows
    or refreshes, so another request's config keeps the plugins it loaded; a plugin that fails to load leaves it until
    it loads again. Workers serve no HTTP routes and run no lifespans, so they never load routes or lifecycle modules
    themselves.
    """
    import mindroom.tools  # noqa: F401, PLC0415 - built-ins register before any plugin may collide with them.

    with _PLUGIN_LOCK:
        published = _PUBLISHED
        plugins, skipped = _load_plugins(config, runtime_paths, previous=published, primary_modules=False)
        failed_roots = {plugin.root for plugin in skipped} & set(published.plugins)
        changed = {root: plugin for root, plugin in plugins.items() if published.plugins.get(root) is not plugin}
        if changed or failed_roots:
            generation = _installed_generation(
                {
                    **{root: plugin for root, plugin in published.plugins.items() if root not in failed_roots},
                    **plugins,
                },
            )
            if changed:
                _log_loaded_plugins(generation)
            publish_plugins(generation)
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

    plugin_bases, skipped = plugin_imports._collect_plugin_bases(config.plugins, runtime_paths)
    published = _PUBLISHED
    unresolved = {plugin.path for plugin in skipped}
    plugin_tools: dict[str, tuple[str, ToolMetadata]] = {}
    unavailable: dict[str, ToolMetadata] = {}
    for plugin_base, plugin_entry in plugin_bases:
        loaded = published.plugins.get(plugin_base.root)
        if loaded is not None and _loaded_from_current_source(loaded, plugin_base):
            tools: Mapping[str, ToolMetadata] = loaded.tools
        else:
            registrations: dict[str, ToolMetadata] = {}
            completed: list[str] = []
            try:
                _execute_candidate_plugin(plugin_base, registrations, completed)
            except (Exception, SystemExit) as exc:
                skipped.append(_skipped_plugin(plugin_entry, plugin_base, exc))
                if plugin_base.tools_module_path is not None and "tools" not in completed:
                    unresolved.add(plugin_base.name)
                unavailable.update(_declared_tools(plugin_base))
                unavailable.update(registrations)
                continue
            tools = registrations
        _add_plugin_tools(plugin_tools, plugin_base.name, tools)
    registry = compose_tool_registry(
        {name: metadata for name, (_, metadata) in plugin_tools.items()},
        _mcp_tool_entries(config.mcp_servers),
        unavailable=unavailable,
        unresolved_plugin_sources=frozenset(unresolved),
    )
    return registry, tuple(skipped)


def _execute_candidate_plugin(
    plugin: plugin_imports._PluginBase,
    registrations: dict[str, ToolMetadata],
    completed: list[str],
) -> None:
    """Execute one plugin's modules under a fresh namespace as loading it does, collecting its tools module's tools.

    Afterwards the namespace is removed, with any module from the plugin's root they imported by absolute name, so
    the plugin's next load imports and registers those again. Modules under the canonical prefix that another thread
    imports meanwhile stay.
    """
    prefix = f"{CANDIDATE_PLUGIN_MODULE_PREFIX}{next(_CANDIDATE_NAMESPACES)}_"
    loaded_before = set(sys.modules)
    try:
        # A file that serves several kinds executes once, and each kind it serves is checked on that module.
        executed: dict[Path, plugin_imports._ModuleCacheEntry] = {}
        for kind, module_path in (
            ("tools", plugin.tools_module_path),
            ("hooks", plugin.hooks_module_path),
            ("oauth", plugin.oauth_module_path),
        ):
            if module_path is None:
                continue
            entry = executed.get(module_path)
            if entry is None:
                entry = _load_plugin_module(plugin.name, plugin.root, module_path, kind=kind, prefix=prefix)
                assert entry is not None
                executed[module_path] = entry
            if kind == "tools":
                registrations.update(entry.registrations)
            elif kind == "oauth":
                _oauth_provider_callback(entry.module)
            completed.append(kind)
    finally:
        candidate_modules = {**_modules_named((), prefix), **_imported_from_root(plugin.root, loaded_before)}
        for module_name in candidate_modules:
            sys.modules.pop(module_name, None)
        _cancel_module_tasks(candidate_modules.values())


def _module_within(module: ModuleType, root: Path) -> bool:
    module_file = getattr(module, "__file__", None)
    return isinstance(module_file, str) and Path(module_file).resolve().is_relative_to(root)


def _skipped_plugin(
    entry: PluginEntryConfig,
    plugin: plugin_imports._PluginBase,
    exc: BaseException,
) -> SkippedPlugin:
    """Log one plugin that failed to load and return the record of its skip."""
    plugin_imports._log_failed_plugin_entry(entry, plugin.root, exc)
    if isinstance(exc, Exception):
        return SkippedPlugin(entry.path, exc, plugin.root)
    msg = f"Plugin materialization failed for {plugin.root}: {exc}"
    return SkippedPlugin(entry.path, plugin_imports.PluginValidationError(msg), plugin.root)


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


def _loaded_from_current_source(loaded: _LoadedPlugin, plugin: plugin_imports._PluginBase) -> bool:
    """Return whether ``loaded`` executed ``plugin``'s current modules under its current name."""
    return loaded.name == plugin.name and loaded.source == _plugin_source(plugin)


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
    primary_modules: bool,
) -> tuple[dict[Path, _LoadedPlugin], tuple[SkippedPlugin, ...]]:
    """Load ``config``'s enabled plugins through the canonical module cache, skipping each one that fails.

    Their routes and lifecycle modules are imported only with ``primary_modules``. A structural error raises with the
    canonical modules as they were before the load and stops what it executed.
    """
    installed_modules = _canonical_plugin_modules()
    installed_cache = dict(plugin_imports._MODULE_IMPORT_CACHE)
    try:
        return _load_each_plugin(config, runtime_paths, previous=previous, primary_modules=primary_modules)
    except BaseException:
        installed = {id(module) for module in installed_modules.values()}
        _cancel_module_tasks(module for module in _canonical_plugin_modules().values() if id(module) not in installed)
        _install_canonical_modules(installed_modules, installed_cache)
        raise


def _load_each_plugin(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    previous: PluginGeneration | None,
    primary_modules: bool,
) -> tuple[dict[Path, _LoadedPlugin], tuple[SkippedPlugin, ...]]:
    plugin_bases, skipped = plugin_imports._collect_plugin_bases(config.plugins, runtime_paths)
    plugins: dict[Path, _LoadedPlugin] = {}
    for plugin_base, plugin_entry in plugin_bases:
        loaded = previous.plugins.get(plugin_base.root) if previous is not None else None
        if loaded is not None and _loaded_from_current_source(loaded, plugin_base):
            plugins[plugin_base.root] = _with_current_manifest(loaded, plugin_base)
            continue
        # The plugin loads whole: each of its modules executes once, and every module they import from its root executes
        # again with them, so they share one instance of each other and their tools register again.
        installed_modules, installed_cache = _evict_plugin_modules(plugin_base)
        try:
            plugin = _materialize_plugin(plugin_base, primary_modules=primary_modules)
        except BaseException as exc:
            _dispose_failed_plugin(plugin_base, installed_modules, installed_cache)
            _raise_if_host_control_exception(exc)
            skipped.append(_skipped_plugin(plugin_entry, plugin_base, exc))
            continue
        plugins[plugin_base.root] = plugin
    plugin_tools: dict[str, tuple[str, ToolMetadata]] = {}
    for plugin in plugins.values():
        _add_plugin_tools(plugin_tools, plugin.name, plugin.tools)
    # The config's MCP servers name tools too, so a plugin tool taking one of those names fails the load as a whole.
    compose_tool_registry(
        {name: metadata for name, (_, metadata) in plugin_tools.items()},
        _mcp_tool_entries(config.mcp_servers),
    )
    return plugins, tuple(skipped)


def _dispose_failed_plugin(
    plugin: plugin_imports._PluginBase,
    installed_modules: dict[str, ModuleType],
    installed_cache: dict[str, plugin_imports._ModuleCacheEntry],
) -> None:
    """Cancel the tasks of a failed plugin's newly executed modules and reinstate its installed ones."""
    installed = {id(module) for module in installed_modules.values()}
    executed, _ = _evict_plugin_modules(plugin)
    _cancel_module_tasks(module for module in executed.values() if id(module) not in installed)
    sys.modules.update(installed_modules)
    plugin_imports._MODULE_IMPORT_CACHE.update(installed_cache)


def _with_current_manifest(loaded: _LoadedPlugin, plugin: plugin_imports._PluginBase) -> _LoadedPlugin:
    """Return the unchanged modules ``loaded`` holds with what ``plugin``'s manifest declares now; itself if equal."""
    if (
        loaded.mindroom_plugin_api == plugin.mindroom_plugin_api
        and loaded.skill_dirs == tuple(plugin.skill_dirs)
        and loaded.reserved_namespaces == plugin.reserved_namespaces
    ):
        return loaded
    return replace(
        loaded,
        mindroom_plugin_api=plugin.mindroom_plugin_api,
        skill_dirs=tuple(plugin.skill_dirs),
        reserved_namespaces=plugin.reserved_namespaces,
    )


def _materialize_plugin(plugin: plugin_imports._PluginBase, *, primary_modules: bool) -> _LoadedPlugin:
    # Read before any module executes, so a module edited while the load runs counts as changed at the next refresh.
    source = _plugin_source(plugin)
    tools_entry = _load_plugin_module(plugin.name, plugin.root, plugin.tools_module_path, kind="tools")
    # Without its own hooks module, a plugin's hooks live in its tools module.
    hooks_entry = (
        tools_entry
        if plugin.hooks_module_path is None
        else _load_plugin_module(plugin.name, plugin.root, plugin.hooks_module_path, kind="hooks")
    )
    oauth_entry = _load_plugin_module(plugin.name, plugin.root, plugin.oauth_module_path, kind="oauth")
    routes_app: PluginRoutesApp | None = None
    routes_error: plugin_imports.PluginValidationError | None = None
    if primary_modules and plugin.routes_module_path is not None:
        # A broken routes module costs only the routes: a reload must not take the plugin's tools from agents.
        try:
            routes_entry = _load_plugin_module(plugin.name, plugin.root, plugin.routes_module_path, kind="routes")
            assert routes_entry is not None
            routes_app = _plugin_routes_app(routes_entry.module)
        except plugin_imports.PluginValidationError as error:
            logger.warning("Plugin routes failed to load; serving none", plugin_name=plugin.name, error=str(error))
            routes_error = error
    lifespan: PluginLifespan | None = None
    lifespan_error: plugin_imports.PluginValidationError | None = None
    if primary_modules and plugin.lifecycle_module_path is not None:
        # Likewise, a broken lifecycle module costs only the lifespan. It loads last, so a helper module it shares with
        # the tools module registers its tools while the tools module loads.
        try:
            lifecycle_entry = _load_plugin_module(
                plugin.name,
                plugin.root,
                plugin.lifecycle_module_path,
                kind="lifecycle",
            )
            assert lifecycle_entry is not None
            lifespan = _plugin_lifespan(lifecycle_entry.module)
        except plugin_imports.PluginValidationError as error:
            logger.warning(
                "Plugin lifecycle module failed to load; running no lifespan",
                plugin_name=plugin.name,
                error=str(error),
            )
            lifespan_error = error
    hooks_module = hooks_entry.module if hooks_entry is not None else None
    return _LoadedPlugin(
        name=plugin.name,
        mindroom_plugin_api=plugin.mindroom_plugin_api,
        root=plugin.root,
        skill_dirs=tuple(plugin.skill_dirs),
        reserved_namespaces=plugin.reserved_namespaces,
        # A source that could not be read never matches, so the next refresh loads the plugin again.
        source=source or (),
        # A plugin's tools are the ones its tools module registers.
        tools=tools_entry.registrations if tools_entry is not None else MappingProxyType({}),
        discovered_hooks=tuple(iter_module_hooks(hooks_module)) if hooks_module is not None else (),
        discovered_automations=tuple(iter_module_automations(hooks_module)) if hooks_module is not None else (),
        oauth_callback=_oauth_provider_callback(oauth_entry.module) if oauth_entry is not None else None,
        routes_app=routes_app,
        routes_error=routes_error,
        lifespan=lifespan,
        lifespan_error=lifespan_error,
    )


def _oauth_provider_callback(module: ModuleType) -> Callable[..., object]:
    callback = vars(module).get("register_oauth_providers")
    if not callable(callback):
        msg = "OAuth plugin module must define callable register_oauth_providers(settings, runtime_paths)"
        raise plugin_imports.PluginValidationError(msg)
    return callback


def _plugin_lifespan(module: ModuleType) -> PluginLifespan:
    lifespan = vars(module).get("lifespan")
    if not callable(lifespan):
        msg = "Plugin lifecycle module must define lifespan(lifecycle), returning an async context manager"
        raise plugin_imports.PluginValidationError(msg)
    return lifespan


def _plugin_routes_app(module: ModuleType) -> PluginRoutesApp:
    """Build the app that serves the module's ``router``, once, so a router FastAPI cannot serve fails as it loads."""
    # Deferred: workers and slim entry points load plugins too, and only a routes module needs FastAPI.
    from fastapi import APIRouter, FastAPI, WebSocket, status  # noqa: PLC0415
    from fastapi.exceptions import RequestValidationError  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    from mindroom.api.validation_errors import validation_error_response  # noqa: PLC0415

    router = vars(module).get("router")
    if not isinstance(router, APIRouter):
        msg = "Plugin routes module must define router: APIRouter"
        raise plugin_imports.PluginValidationError(msg)

    async def plugin_inactive(connection: HTTPConnection, error: Exception) -> JSONResponse | None:
        """Answer a route that used its context while its plugin is not active with 503, or close its websocket."""
        if isinstance(connection, WebSocket):
            await connection.close(status.WS_1013_TRY_AGAIN_LATER, str(error))
            return None
        return JSONResponse({"detail": str(error)}, status_code=503)

    # Errors are answered as the main API answers them, so a rejected body is not echoed back. FastAPI types these
    # handlers for requests only, but Starlette calls them for websockets too.
    exception_handlers: dict[Any, Any] = {
        RequestValidationError: validation_error_response,
        PluginInactiveError: plugin_inactive,
    }
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, exception_handlers=exception_handlers)
    try:
        app.include_router(router)
    except Exception as error:  # FastAPI refuses some routers only here, such as one with a route at "".
        msg = f"Plugin router cannot be served: {error}"
        raise plugin_imports.PluginValidationError(msg) from error
    # The app's error handling without the app itself, which would make itself ``request.app``: plugin routes keep
    # the main app there, and with it the client sessions and room membership that ``mindroom.plugin_http`` reads.
    return PluginRoutesApp(app.build_middleware_stack())


@dataclass(eq=False)
class PluginRoutesApp:
    """One plugin's routes app, which tracks its open websockets so a reload that replaces it can close them."""

    app: ASGIApp
    # Closes one open websocket each.
    _closers: set[Callable[[], Awaitable[None]]] = field(default_factory=set, init=False)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one request or websocket, tracking a websocket until it ends."""
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return
        closed = False

        async def close() -> None:
            nonlocal closed
            closed = True
            # The route or its client may have ended the websocket already, which makes the server refuse the close.
            with suppress(OSError, RuntimeError):
                await send({"type": "websocket.close", "code": 1012})

        async def tracked_send(message: Message) -> None:
            if closed:
                # As when its client left: the route ends rather than send on a closed websocket. Deferred, as
                # FastAPI is above: workers and slim entry points load this module too.
                from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

                raise WebSocketDisconnect(1012)
            await send(message)

        self._closers.add(close)
        try:
            await self.app(scope, receive, tracked_send)
        finally:
            self._closers.discard(close)

    async def close_websockets(self) -> None:
        """Close each open websocket with 1012 (service restart), so its client reconnects to the plugin's new code."""
        await asyncio.gather(*(close() for close in tuple(self._closers)))


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
    """Best-effort cancel the unfinished tasks the given plugin modules hold, each task once.

    A task a plugin keeps in its process state, or one level inside it as module globals are scanned, outlives reloads,
    whichever module global also holds it or its container.
    """
    kept = {
        id(task)
        for state in tuple(_PROCESS_STATE.values())
        for value in tuple(state.values())
        for task in _iter_module_tasks(value)
    }
    cancelled: dict[int, asyncio.Task[Any]] = {}
    for module in modules:
        for task in _module_tasks(module):
            if task.done() or id(task) in cancelled or id(task) in kept:
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
    """Execute one plugin module, or return the instance this load already has.

    The canonical ``prefix`` installs the module and caches it, once per load: another of the plugin's modules may have
    imported it already, and executing it again would leave that importer a copy of its own. Another prefix executes it
    under a separate namespace, with its relative imports, and caches nothing.
    """
    if module_path is None:
        return None
    try:
        module_path.stat()
    except OSError as exc:
        msg = f"Failed to stat plugin {kind} module {module_path}: {exc}"
        logger.exception("Failed to stat plugin module", path=str(module_path), kind=kind, error=str(exc))
        raise plugin_imports.PluginValidationError(msg) from exc

    canonical = prefix == PLUGIN_MODULE_PREFIX
    module_name = plugin_imports._module_name(plugin_name, plugin_root, module_path, prefix)
    if canonical and (loaded := _loaded_module(module_name)) is not None:
        return loaded

    previous_module = sys.modules.get(module_name)
    prepared_module = plugin_imports._prepare_module(plugin_name, plugin_root, module_path, module_name, prefix)
    if prepared_module is None:
        msg = f"Failed to load plugin {kind} module: {module_path}"
        logger.error("Failed to load plugin module", path=str(module_path), kind=kind)
        raise plugin_imports.PluginValidationError(msg)
    module, previous_packages = prepared_module

    loaded_before = set(sys.modules) if canonical else set()
    try:
        # A module may provide several capabilities; whatever it registers belongs to this module.
        with plugin_module_registrations(module_name) as registrations:
            _exec_plugin_source(module_path, module)
    except BaseException as exc:
        # Its tasks go with it: once it leaves sys.modules, the failed plugin's cleanup no longer finds them.
        _cancel_module_tasks((module,))
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
        plugin_imports._restore_plugin_package_chain(previous_packages)
        _raise_if_host_control_exception(exc)
        msg = f"Plugin {kind} module execution failed for {module_path}: {exc}"
        logger.exception("Plugin module execution failed", path=str(module_path), kind=kind, error=str(exc))
        raise plugin_imports.PluginValidationError(msg) from exc
    finally:
        if canonical:
            _ROOT_PACKAGES.update(dict.fromkeys(_root_packages(plugin_root, loaded_before), plugin_root))

    entry = plugin_imports._ModuleCacheEntry(module=module, registrations=MappingProxyType(registrations))
    if canonical:
        plugin_imports._MODULE_IMPORT_CACHE[module_name] = entry
    return entry


def _loaded_module(module_name: str) -> plugin_imports._ModuleCacheEntry | None:
    """Return the canonical module this load has already, whether the loader or another plugin module imported it."""
    cached = plugin_imports._MODULE_IMPORT_CACHE.get(module_name)
    if cached is not None:
        return cached
    imported = sys.modules.get(module_name)
    if imported is None:
        return None
    # What it registered as it executed belongs to the module that imported it.
    entry = plugin_imports._ModuleCacheEntry(module=imported, registrations=MappingProxyType({}))
    plugin_imports._MODULE_IMPORT_CACHE[module_name] = entry
    return entry


def _exec_plugin_source(module_path: Path, module: ModuleType) -> None:
    with tokenize.open(str(module_path)) as source_file:
        source = source_file.read()
    code = compile(source, str(module_path), "exec", dont_inherit=True)
    exec(code, module.__dict__)  # noqa: S102 - configured plugin modules are intentionally executable code.
