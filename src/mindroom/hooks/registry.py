"""Immutable hook registry compilation and lookup helpers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from mindroom.logging_config import get_logger

from .decorators import get_hook_metadata
from .types import HookCallback, RegisteredHook

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.config.plugin import PluginEntryConfig

logger = get_logger(__name__)


class HookRegistryPlugin(Protocol):
    """Structural plugin shape consumed by hook-registry compilation."""

    name: str
    entry_config: PluginEntryConfig
    plugin_order: int
    discovered_hooks: tuple[HookCallback, ...]


def _callback_source_lineno(callback: HookCallback) -> int:
    return cast("Any", callback).__code__.co_firstlineno


@dataclass(frozen=True, slots=True)
class HookRegistry:
    """Compiled immutable event -> hooks mapping."""

    _hooks_by_event: dict[str, tuple[RegisteredHook, ...]]

    @classmethod
    def empty(cls) -> HookRegistry:
        """Return an empty hook registry."""
        return cls(_hooks_by_event={})

    @classmethod
    def from_plugins(cls, plugins: Iterable[HookRegistryPlugin]) -> HookRegistry:
        """Compile one immutable snapshot from loaded plugins."""
        hooks_by_event: defaultdict[str, list[RegisteredHook]] = defaultdict(list)
        seen_hook_names: set[tuple[str, str]] = set()

        for plugin in plugins:
            discovered_names: set[str] = set()
            for callback in plugin.discovered_hooks:
                metadata = get_hook_metadata(callback)
                if metadata is None:
                    continue

                discovered_names.add(metadata.hook_name)
                qualified_name = (plugin.name, metadata.hook_name)
                if qualified_name in seen_hook_names:
                    logger.warning(
                        "Skipping duplicate hook registration",
                        plugin_name=plugin.name,
                        hook_name=metadata.hook_name,
                    )
                    continue

                override = plugin.entry_config.hooks.get(metadata.hook_name)
                if override is not None and not override.enabled:
                    continue

                seen_hook_names.add(qualified_name)
                hooks_by_event[metadata.event_name].append(
                    RegisteredHook(
                        plugin_name=plugin.name,
                        hook_name=metadata.hook_name,
                        event_name=metadata.event_name,
                        priority=override.priority if override and override.priority is not None else metadata.priority,
                        timeout_ms=(
                            override.timeout_ms if override and override.timeout_ms is not None else metadata.timeout_ms
                        ),
                        callback=callback,
                        settings=dict(plugin.entry_config.settings),
                        plugin_order=plugin.plugin_order,
                        source_lineno=_callback_source_lineno(callback),
                        agents=metadata.agents,
                        rooms=metadata.rooms,
                    ),
                )

            unknown_overrides = sorted(set(plugin.entry_config.hooks) - discovered_names)
            for hook_name in unknown_overrides:
                logger.warning(
                    "Plugin hook override did not match any discovered hook",
                    plugin_name=plugin.name,
                    hook_name=hook_name,
                )

        return cls(
            _hooks_by_event={
                event_name: tuple(
                    sorted(
                        registered_hooks,
                        key=lambda hook: (hook.priority, hook.plugin_order, hook.source_lineno),
                    ),
                )
                for event_name, registered_hooks in hooks_by_event.items()
            },
        )

    def hooks_for(self, event_name: str) -> tuple[RegisteredHook, ...]:
        """Return compiled hooks for one event name."""
        return self._hooks_by_event.get(event_name, ())

    def has_hooks(self, event_name: str) -> bool:
        """Return whether any hooks are registered for one event."""
        return bool(self.hooks_for(event_name))


@dataclass(slots=True)
class HookRegistryState:
    """Mutable holder for the currently active hook registry snapshot."""

    registry: HookRegistry
