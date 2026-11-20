"""Immutable hook registry compilation and lookup helpers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from mindroom.automations.registry import AutomationCatalog, compile_automations
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger

from .decorators import get_hook_metadata
from .types import EVENT_BOT_READY, EVENT_CONFIG_RELOADED, HookCallback, RegisteredHook

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from mindroom.automations.registry import CheckFn
    from mindroom.config.plugin import PluginEntryConfig

    from .types import HookScope

logger = get_logger(__name__)


class HookRegistryPlugin(Protocol):
    """Structural plugin shape consumed by hook-registry compilation."""

    name: str
    entry_config: PluginEntryConfig
    plugin_order: int
    discovered_hooks: tuple[HookCallback, ...]
    discovered_automations: tuple[CheckFn, ...]


def _callback_source_lineno(callback: HookCallback) -> int:
    return cast("Any", callback).__code__.co_firstlineno


@dataclass(frozen=True, slots=True)
class HookRegistry:
    """Compiled immutable snapshot of what loaded plugins provide: hooks by event, and automations by name."""

    _hooks_by_event: dict[str, tuple[RegisteredHook, ...]]
    automations: AutomationCatalog = field(default_factory=AutomationCatalog)

    @classmethod
    def empty(cls) -> HookRegistry:
        """Return an empty hook registry."""
        return cls(_hooks_by_event={})

    @classmethod
    def from_plugins(cls, plugins: Iterable[HookRegistryPlugin]) -> HookRegistry:
        """Compile one immutable snapshot from loaded plugins."""
        plugins = list(plugins)
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
                scope: HookScope = metadata.scope
                agents = metadata.agents
                # LEGACY_COMPAT: the `agents=(ROUTER_AGENT_NAME,)` hook filter that meant once per room or process.
                # Legacy format: a plugin hook whose `agents` filter is exactly the router's name.
                # Last legacy release: every release before the router-free change ships, v2026.11.459 the newest when
                # written, runs such hooks on the router bot; replacement: unreleased, no bot is in every room, and
                # hooks say `scope="room"` or `scope="service"`.
                # Handling: registered with one warning as service scope on bot:ready and as room scope elsewhere, with
                # the filter cleared; on config:reloaded the filter stays and, as before, never matches.
                # Coverage: tests/test_hook_registry.py::test_router_alias_on_bot_ready_becomes_service_scope and the
                # `test_router_*` tests beside it
                if agents == (ROUTER_AGENT_NAME,) and metadata.event_name != EVENT_CONFIG_RELOADED:
                    scope = "service" if metadata.event_name == EVENT_BOT_READY else "room"
                    agents = None
                    logger.warning(
                        "Hook filter agents=(ROUTER_AGENT_NAME,) is deprecated; register the hook with this scope",
                        plugin_name=plugin.name,
                        hook_name=metadata.hook_name,
                        scope=scope,
                    )
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
                        agents=agents,
                        rooms=metadata.rooms,
                        required=metadata.required,
                        scope=scope,
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
            automations=compile_automations(plugins),
        )

    def hooks_for(self, event_name: str) -> tuple[RegisteredHook, ...]:
        """Return compiled hooks for one event name."""
        return self._hooks_by_event.get(event_name, ())

    def has_hooks(self, event_name: str) -> bool:
        """Return whether any hooks are registered for one event."""
        return bool(self.hooks_for(event_name))


class HookRegistrySource(Protocol):
    """Where hooks read the registry snapshot that is active now: a runtime's holder, or one lifetime's view of it."""

    @property
    def registry(self) -> HookRegistry:
        """Return the active registry snapshot."""
        ...


@dataclass(slots=True, eq=False)
class HookRegistryState:
    """Mutable holder for the currently active hook registry snapshot.

    Hook contexts bound through a holder stay active while it still holds the snapshot they were bound to.
    """

    registry: HookRegistry
    # Set by the runtime: whether a bot runs room-scoped hooks, given ``(agent_name, room_id, event_name, event_id)``.
    room_hook_owner: Callable[[str, str, str, str | None], Awaitable[bool]] | None = None

    def lifetime(self, emitter: str | None = None) -> HookRegistryLifetime:
        """Return a view of this holder for one lifetime that can end on its own: a bot generation's names that bot."""
        return HookRegistryLifetime(self, emitter)


class HookRegistryLifetime:
    """One lifetime's read-only view of its runtime's hook registry: the runtime's current snapshot until it ends.

    A plugin reload publishes to every live lifetime at once through the runtime's holder. Once a lifetime ends, its
    view holds no hooks, so hooks it emitted report inactive and a stopped bot generation emits nothing more.
    """

    __slots__ = ("_emitter", "_source")

    def __init__(self, runtime: HookRegistryState, emitter: str | None = None) -> None:
        self._source = runtime
        self._emitter = emitter

    @property
    def registry(self) -> HookRegistry:
        """Return the runtime's current snapshot, or this lifetime's own empty registry once it ended."""
        return self._source.registry

    async def runs_room_hooks(self, room_ids: Iterable[str], event_name: str, event_id: str | None) -> bool:
        """Return whether this lifetime's bot runs an emission's room-scoped hooks; a lifetime of no bot always does."""
        owner = self._source.room_hook_owner
        if self._emitter is None or owner is None:
            return True
        return any([await owner(self._emitter, room_id, event_name, event_id) for room_id in room_ids])

    def end(self) -> None:
        """End this lifetime; its view holds no hooks from now on."""
        self._source = HookRegistryState(HookRegistry.empty())
