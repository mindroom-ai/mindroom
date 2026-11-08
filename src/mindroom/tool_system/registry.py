"""The tools one config can build, and the built-in tools every config shares."""

from __future__ import annotations

import inspect
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from mindroom.logging_config import get_logger
from mindroom.tool_system.declarations import ToolMetadata, ToolValidationInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from mindroom.tool_system.agno_toolkits import AnyToolkit

logger = get_logger(__name__)

# Canonical plugin modules, and the modules config validation executes for a candidate config.
PLUGIN_MODULE_PREFIX = "mindroom_plugin_"

_BUILTIN_TOOLS: dict[str, ToolMetadata] = {}
_REGISTRATION_SCOPE = threading.local()
# The plugin directories the configs of this process named.
_PLUGIN_ROOTS: set[Path] = set()


class ToolMetadataValidationError(ValueError):
    """Raised when runtime tool metadata derived from authored config is invalid."""


def record_plugin_root(root: Path) -> None:
    """Remember that a config names a plugin at ``root``: a module there registers tools only inside a plugin load."""
    _PLUGIN_ROOTS.add(root)


def plugin_load_active(caller: str) -> bool:
    """Return whether a plugin module is loading, for ``caller``, which registers plugin tools only then."""
    if getattr(_REGISTRATION_SCOPE, "registrations", None) is not None:
        return True
    logger.warning("Ignoring plugin tools registered outside a plugin load", caller=caller)
    return False


def _plugin_root(module_name: str) -> Path | None:
    """Return the recorded plugin directory holding module ``module_name``'s file, if any."""
    module_file = getattr(sys.modules.get(module_name), "__file__", None)
    if not isinstance(module_file, str):
        return None
    return next((parent for parent in Path(module_file).resolve().parents if parent in _PLUGIN_ROOTS), None)


def register_tool(metadata: ToolMetadata) -> None:
    """Record one tool: a plugin module's while that module loads, else a built-in, which no plugin's module adds."""
    registrations = getattr(_REGISTRATION_SCOPE, "registrations", None)
    if registrations is not None:
        if metadata.name in _BUILTIN_TOOLS:
            msg = f"Plugin tool '{metadata.name}' conflicts with built-in tool '{metadata.name}'."
            raise ToolMetadataValidationError(msg)
        if metadata.name in registrations:
            module_name = _REGISTRATION_SCOPE.module_name
            msg = f"Plugin tool '{metadata.name}' is registered multiple times in plugin module '{module_name}'."
            raise ToolMetadataValidationError(msg)
        registrations[metadata.name] = metadata
        return
    module_name = metadata.factory.__module__ if metadata.factory is not None else ""
    if module_name.startswith(PLUGIN_MODULE_PREFIX):
        msg = f"Plugin tool '{metadata.name}' must register while its plugin module loads, not later."
        raise ToolMetadataValidationError(msg)
    plugin_root = _plugin_root(module_name)
    if plugin_root is not None:
        # Its plugin's load registers it; another import of the module only executes it again.
        logger.warning(
            "Ignoring a plugin tool registered outside its plugin's load",
            tool=metadata.name,
            plugin_root=str(plugin_root),
            module=module_name,
        )
        return
    _BUILTIN_TOOLS[metadata.name] = metadata


@contextmanager
def plugin_module_registrations(module_name: str) -> Iterator[dict[str, ToolMetadata]]:
    """Collect the tools one plugin module registers while it executes in this thread."""
    previous = getattr(_REGISTRATION_SCOPE, "registrations", None), getattr(_REGISTRATION_SCOPE, "module_name", None)
    registrations: dict[str, ToolMetadata] = {}
    _REGISTRATION_SCOPE.registrations, _REGISTRATION_SCOPE.module_name = registrations, module_name
    try:
        yield registrations
    finally:
        _REGISTRATION_SCOPE.registrations, _REGISTRATION_SCOPE.module_name = previous


@dataclass(frozen=True, eq=False)
class ToolRegistry:
    """Every tool one config can build: built-ins, its loaded plugins' tools, and its enabled MCP servers.

    ``unavailable`` are the tools plugins that failed to load declare, ``unresolved_plugin_sources`` the plugin entries
    whose tool names could not be read at all, and ``runtime_unavailable`` the registered tools this runtime cannot
    run, such as the egress credentials tool while the egress broker is off.
    """

    entries: Mapping[str, ToolMetadata]
    unavailable: Mapping[str, ToolMetadata] = field(default_factory=lambda: MappingProxyType({}))
    unresolved_plugin_sources: frozenset[str] = frozenset()
    runtime_unavailable: frozenset[str] = frozenset()

    def get(self, name: str) -> ToolMetadata | None:
        """Return the metadata of one registered tool."""
        return self.entries.get(name)

    def __contains__(self, name: object) -> bool:
        """Return whether ``name`` is a registered tool."""
        return name in self.entries

    def __iter__(self) -> Iterator[str]:
        """Iterate over the registered tool names."""
        return iter(self.entries)

    def available(self, name: str) -> bool:
        """Return whether ``name`` is a registered tool this runtime can run."""
        return name in self.entries and name not in self.runtime_unavailable

    def listable(self) -> dict[str, ToolMetadata]:
        """Return the tools a tool listing offers: the registered ones this runtime can run."""
        return {name: metadata for name, metadata in self.entries.items() if name not in self.runtime_unavailable}

    def factory(self, name: str) -> Callable[[], type[AnyToolkit]]:
        """Return the factory of one tool built from the registry; a tool without one is unknown here."""
        metadata = self.entries.get(name)
        if metadata is None or metadata.factory is None:
            available = ", ".join(sorted(name for name, entry in self.entries.items() if entry.factory is not None))
            msg = f"Unknown tool: {name}. Available tools: {available}"
            raise ToolMetadataValidationError(msg)
        return cast("Callable[[], type[AnyToolkit]]", metadata.factory)

    def origin(self, name: str) -> list[str] | None:
        """Return one tool factory's stable provenance without importing or constructing its tool."""
        metadata = self.entries.get(name)
        factory = metadata.factory if metadata is not None else None
        return [factory.__module__, factory.__qualname__] if inspect.isfunction(factory) else None

    def mcp_server_id(self, name: str) -> str | None:
        """Return the MCP server one tool name stands for, or None when it names no configured MCP server."""
        metadata = self.entries.get(name)
        return metadata.mcp_server_id if metadata is not None else None

    @cached_property
    def validation_snapshot(self) -> dict[str, ToolValidationInfo]:
        """Return the validation-only view of every registered and unavailable tool."""
        return {
            tool_name: ToolValidationInfo(
                name=tool_name,
                config_fields=tuple(metadata.config_fields or ()),
                agent_override_fields=tuple(metadata.agent_override_fields or ()),
                authored_override_validator=metadata.authored_override_validator,
                file_access=metadata.file_access,
                supports_toolkit_filters=metadata.supports_toolkit_filters,
                requires_room_context=metadata.requires_room_context,
                requires_primary_runtime=metadata.requires_primary_runtime,
                runtime_loadable=tool_name in self.entries and metadata.factory is not None,
                unavailable_due_to_plugin_load_error=tool_name in self.unavailable,
            )
            for tool_name, metadata in {**self.entries, **self.unavailable}.items()
        }


_BUILTIN_TOOL_REGISTRY = ToolRegistry(MappingProxyType(_BUILTIN_TOOLS))


def builtin_tool_registry() -> ToolRegistry:
    """Return the built-in tools, for callers that act for no config."""
    import mindroom.tools  # noqa: F401, PLC0415 - the built-in manifest registers itself on first import.

    return _BUILTIN_TOOL_REGISTRY


def compose_tool_registry(
    plugin_tools: Mapping[str, ToolMetadata],
    mcp_tools: Mapping[str, ToolMetadata],
    *,
    unavailable: Mapping[str, ToolMetadata] = MappingProxyType({}),
    unresolved_plugin_sources: frozenset[str] = frozenset(),
) -> ToolRegistry:
    """Return the built-ins plus one config's plugin and MCP tools; an MCP tool may not reuse a registered name."""
    entries = {**builtin_tool_registry().entries, **plugin_tools}
    collisions = sorted(set(mcp_tools) & set(entries))
    if collisions:
        msg = f"MCP tool '{collisions[0]}' conflicts with an existing registered tool"
        raise ToolMetadataValidationError(msg)
    entries.update(mcp_tools)
    return ToolRegistry(
        entries=MappingProxyType(entries),
        # A failed plugin's declaration that another source registers is dropped.
        unavailable=MappingProxyType({name: metadata for name, metadata in unavailable.items() if name not in entries}),
        unresolved_plugin_sources=unresolved_plugin_sources,
    )
