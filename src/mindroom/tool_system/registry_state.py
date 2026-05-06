"""Private mutable tool-registry state and helpers."""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.tool_system import plugin_imports

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from types import ModuleType

    from agno.tools import Toolkit

    from mindroom.tool_system.metadata import ToolMetadata

TOOL_METADATA: dict[str, ToolMetadata] = {}
TOOL_REGISTRY: dict[str, Callable[[], type[Toolkit]]] = {}
BUILTIN_TOOL_REGISTRY: dict[str, Callable[[], type[Toolkit]]] = {}
_PLUGIN_TOOL_METADATA_BY_MODULE: dict[str, dict[str, ToolMetadata]] = {}
BUILTIN_TOOL_METADATA: dict[str, ToolMetadata] = {}
PLUGIN_MODULE_PREFIX = "mindroom_plugin_"
_TOOL_REGISTRY_STATE_LOCK = threading.RLock()
PLUGIN_REGISTRATION_SCOPE = threading.local()


class ToolMetadataValidationError(ValueError):
    """Raised when runtime tool metadata derived from authored config is invalid."""


@dataclass(frozen=True)
class _ToolRegistrySnapshot:
    registry: dict[str, Callable[[], type[Toolkit]]]
    metadata: dict[str, ToolMetadata]
    builtin_registry: dict[str, Callable[[], type[Toolkit]]]
    builtin_metadata: dict[str, ToolMetadata]
    module_import_cache: dict[Path, plugin_imports._ModuleCacheEntry]
    plugin_tool_metadata_by_module: dict[str, dict[str, ToolMetadata]]
    plugin_modules: dict[str, ModuleType]


def clear_plugin_tool_registrations(module_name: str) -> None:
    """Forget cached tool registrations for one plugin module before it is re-executed."""
    _PLUGIN_TOOL_METADATA_BY_MODULE.pop(module_name, None)


def snapshot_plugin_tool_registrations(module_name: str) -> dict[str, ToolMetadata]:
    """Return a copy of one plugin module's cached tool registrations."""
    return _PLUGIN_TOOL_METADATA_BY_MODULE.get(module_name, {}).copy()


def restore_plugin_tool_registrations(
    module_name: str,
    registrations: dict[str, ToolMetadata],
) -> None:
    """Restore one plugin module's cached tool registrations after a failed reload."""
    if registrations:
        _PLUGIN_TOOL_METADATA_BY_MODULE[module_name] = registrations.copy()
    else:
        _PLUGIN_TOOL_METADATA_BY_MODULE.pop(module_name, None)


@contextmanager
def scoped_plugin_registration_store(
    registrations_by_module: dict[str, dict[str, ToolMetadata]],
) -> Iterator[None]:
    """Route plugin registration decorators into one temporary module->metadata store."""
    sentinel = object()
    previous = getattr(PLUGIN_REGISTRATION_SCOPE, "registrations_by_module", sentinel)
    PLUGIN_REGISTRATION_SCOPE.registrations_by_module = registrations_by_module
    try:
        yield
    finally:
        if previous is sentinel:
            delattr(PLUGIN_REGISTRATION_SCOPE, "registrations_by_module")
        else:
            PLUGIN_REGISTRATION_SCOPE.registrations_by_module = previous


@contextmanager
def scoped_plugin_registration_owner(module_name: str) -> Iterator[None]:
    """Attribute scoped validation registrations to one synthetic plugin module."""
    sentinel = object()
    previous = getattr(PLUGIN_REGISTRATION_SCOPE, "owner_module_name", sentinel)
    PLUGIN_REGISTRATION_SCOPE.owner_module_name = module_name
    try:
        yield
    finally:
        if previous is sentinel:
            delattr(PLUGIN_REGISTRATION_SCOPE, "owner_module_name")
        else:
            PLUGIN_REGISTRATION_SCOPE.owner_module_name = previous


def _plugin_registration_store() -> dict[str, dict[str, ToolMetadata]]:
    """Return the active plugin registration sink for this thread."""
    registrations = getattr(PLUGIN_REGISTRATION_SCOPE, "registrations_by_module", None)
    if registrations is None:
        return _PLUGIN_TOOL_METADATA_BY_MODULE
    return registrations


@contextmanager
def locked_tool_registry_state() -> Iterator[None]:
    """Serialize mutations of the process-global tool and plugin registries."""
    with _TOOL_REGISTRY_STATE_LOCK:
        yield


def resolved_tool_state(
    active_plugins: list[tuple[str, str]],
    plugin_metadata_by_module: dict[str, dict[str, ToolMetadata]],
) -> tuple[dict[str, Callable[[], type[Toolkit]]], dict[str, ToolMetadata]]:
    """Build one complete tool registry state from built-ins plus active plugin overlays."""
    desired_metadata = BUILTIN_TOOL_METADATA.copy()
    desired_registry = BUILTIN_TOOL_REGISTRY.copy()
    plugin_owner_by_tool_name: dict[str, str] = {}

    for plugin_name, module_name in active_plugins:
        for tool_name, plugin_metadata in plugin_metadata_by_module.get(module_name, {}).items():
            existing_owner = plugin_owner_by_tool_name.get(tool_name)
            if existing_owner is not None and existing_owner != plugin_name:
                msg = f"Plugin tool '{tool_name}' conflicts between plugins '{existing_owner}' and '{plugin_name}'."
                raise ToolMetadataValidationError(msg)
            plugin_owner_by_tool_name[tool_name] = plugin_name
            desired_metadata[tool_name] = plugin_metadata
            factory = cast("Callable[[], type[Toolkit]] | None", getattr(plugin_metadata, "factory", None))
            if factory is None:
                desired_registry.pop(tool_name, None)
            else:
                desired_registry[tool_name] = factory

    return desired_registry, desired_metadata


def synchronize_plugin_tools(active_plugins: list[tuple[str, str]]) -> None:
    """Rebuild the active plugin tool overlay from cached per-module registrations."""
    desired_registry, desired_metadata = resolved_tool_state(
        active_plugins,
        _PLUGIN_TOOL_METADATA_BY_MODULE,
    )
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(desired_registry)
    TOOL_METADATA.clear()
    TOOL_METADATA.update(desired_metadata)


def _reject_plugin_builtin_tool_collision(tool_name: str) -> None:
    """Fail plugin registration when it reuses a built-in tool name."""
    if tool_name in BUILTIN_TOOL_METADATA:
        msg = f"Plugin tool '{tool_name}' conflicts with built-in tool '{tool_name}'."
        raise ToolMetadataValidationError(msg)


def register_builtin_tool_metadata(metadata: ToolMetadata) -> None:
    """Store one built-in tool or metadata-only built-in entry in the durable registry."""
    factory = cast("Callable[[], type[Toolkit]] | None", getattr(metadata, "factory", None))
    BUILTIN_TOOL_METADATA[metadata.name] = metadata
    TOOL_METADATA[metadata.name] = metadata
    if factory is None:
        BUILTIN_TOOL_REGISTRY.pop(metadata.name, None)
        TOOL_REGISTRY.pop(metadata.name, None)
    else:
        BUILTIN_TOOL_REGISTRY[metadata.name] = factory
        TOOL_REGISTRY[metadata.name] = factory


def register_plugin_tool_metadata(module_name: str, metadata: ToolMetadata) -> None:
    """Store one plugin tool in the per-module overlay cache."""
    _reject_plugin_builtin_tool_collision(metadata.name)
    module_registrations = _plugin_registration_store().setdefault(module_name, {})
    if metadata.name in module_registrations:
        msg = f"Plugin tool '{metadata.name}' is registered multiple times in plugin module '{module_name}'."
        raise ToolMetadataValidationError(msg)
    module_registrations[metadata.name] = metadata


def capture_tool_registry_snapshot() -> _ToolRegistrySnapshot:
    """Capture the mutable tool/plugin registry state for transactional restoration."""
    loaded_modules = sys.modules.copy()
    return _ToolRegistrySnapshot(
        registry=TOOL_REGISTRY.copy(),
        metadata=TOOL_METADATA.copy(),
        builtin_registry=BUILTIN_TOOL_REGISTRY.copy(),
        builtin_metadata=BUILTIN_TOOL_METADATA.copy(),
        module_import_cache=plugin_imports._MODULE_IMPORT_CACHE.copy(),
        plugin_tool_metadata_by_module={
            module_name: registrations.copy() for module_name, registrations in _PLUGIN_TOOL_METADATA_BY_MODULE.items()
        },
        plugin_modules={
            module_name: module
            for module_name, module in loaded_modules.items()
            if module_name.startswith(PLUGIN_MODULE_PREFIX)
        },
    )


def restore_tool_registry_snapshot(snapshot: _ToolRegistrySnapshot) -> None:
    """Restore one previously captured tool/plugin registry snapshot."""
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(snapshot.registry)
    TOOL_METADATA.clear()
    TOOL_METADATA.update(snapshot.metadata)
    BUILTIN_TOOL_REGISTRY.clear()
    BUILTIN_TOOL_REGISTRY.update(snapshot.builtin_registry)
    BUILTIN_TOOL_METADATA.clear()
    BUILTIN_TOOL_METADATA.update(snapshot.builtin_metadata)
    plugin_imports._MODULE_IMPORT_CACHE.clear()
    plugin_imports._MODULE_IMPORT_CACHE.update(snapshot.module_import_cache)
    _PLUGIN_TOOL_METADATA_BY_MODULE.clear()
    _PLUGIN_TOOL_METADATA_BY_MODULE.update(
        {
            module_name: registrations.copy()
            for module_name, registrations in snapshot.plugin_tool_metadata_by_module.items()
        },
    )
    for module_name in tuple(sys.modules.copy()):
        if module_name.startswith(PLUGIN_MODULE_PREFIX) and module_name not in snapshot.plugin_modules:
            sys.modules.pop(module_name, None)
    sys.modules.update(snapshot.plugin_modules)
