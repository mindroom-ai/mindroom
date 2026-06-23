"""Typed runtime shutdown intent shared by sync, bot, and response drains."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from mindroom.cancellation import TaskCancelSource

__all__ = [
    "ENTITY_REMOVED_SHUTDOWN",
    "GENERIC_SHUTDOWN",
    "SYNC_RESTART_SHUTDOWN",
    "RuntimeShutdownIntent",
    "StopReason",
    "shutdown_intent_for_entity",
]

StopReason = Literal["restart", "entity_removed"]


@dataclass(frozen=True)
class RuntimeShutdownIntent:
    """One lifecycle shutdown decision made at the runtime boundary."""

    stop_reason: StopReason | None
    cancel_source: TaskCancelSource | None = None


GENERIC_SHUTDOWN = RuntimeShutdownIntent(stop_reason=None, cancel_source=None)
ENTITY_REMOVED_SHUTDOWN = RuntimeShutdownIntent(stop_reason="entity_removed", cancel_source=None)
SYNC_RESTART_SHUTDOWN = RuntimeShutdownIntent(stop_reason="restart", cancel_source="sync_restart")


def shutdown_intent_for_entity(
    entity_name: str,
    *,
    restart_entities: set[str],
) -> RuntimeShutdownIntent:
    """Return shutdown intent for one stopped entity."""
    if entity_name in restart_entities:
        return SYNC_RESTART_SHUTDOWN
    return ENTITY_REMOVED_SHUTDOWN
