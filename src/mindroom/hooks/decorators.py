"""Decorator helpers for MindRoom hooks."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .types import HookCallback, validate_event_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from types import ModuleType


_HOOK_METADATA_ATTR = "__mindroom_hook_metadata__"


@dataclass(frozen=True, slots=True)
class _HookMetadata:
    """Static decorator metadata attached to a hook callback."""

    event_name: str
    hook_name: str
    priority: int
    timeout_ms: int | None
    agents: tuple[str, ...] | None
    rooms: tuple[str, ...] | None


def hook(
    event: str,
    *,
    name: str | None = None,
    priority: int = 100,
    timeout_ms: int | None = None,
    agents: Iterable[str] | None = None,
    rooms: Iterable[str] | None = None,
) -> Callable[[HookCallback], HookCallback]:
    """Annotate an async function as a MindRoom hook."""
    event_name = validate_event_name(event)
    normalized_agents = tuple(agent.strip() for agent in agents or () if agent.strip()) or None
    normalized_rooms = tuple(room.strip() for room in rooms or () if room.strip()) or None

    def decorator(callback: HookCallback) -> HookCallback:
        if not inspect.iscoroutinefunction(callback):
            msg = f"Hook callback {callback!r} must be async"
            raise TypeError(msg)

        metadata = _HookMetadata(
            event_name=event_name,
            hook_name=name or cast("Any", callback).__name__,
            priority=priority,
            timeout_ms=timeout_ms,
            agents=normalized_agents,
            rooms=normalized_rooms,
        )
        setattr(callback, _HOOK_METADATA_ATTR, metadata)
        return callback

    return decorator


def get_hook_metadata(callback: object) -> _HookMetadata | None:
    """Return decorator metadata for a hook callback when present."""
    metadata = getattr(callback, _HOOK_METADATA_ATTR, None)
    if isinstance(metadata, _HookMetadata):
        return metadata
    return None


def iter_module_hooks(module: ModuleType) -> list[HookCallback]:
    """Return all decorated hook callbacks defined on one imported module."""
    callbacks: list[HookCallback] = []
    seen_ids: set[int] = set()
    for value in vars(module).values():
        if id(value) in seen_ids:
            continue
        seen_ids.add(id(value))
        metadata = get_hook_metadata(value)
        if metadata is None:
            continue
        callbacks.append(cast("HookCallback", value))
    return callbacks
