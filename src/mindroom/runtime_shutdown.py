"""Typed runtime shutdown intent shared by sync, bot, and response drains."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from mindroom.cancellation import TaskCancelSource

__all__ = [
    "ENTITY_REMOVED_SHUTDOWN",
    "GENERIC_SHUTDOWN",
    "ORDERLY_SHUTDOWN",
    "SYNC_RESTART_SHUTDOWN",
    "RestartReasonCategory",
    "RuntimeLifecycleAction",
    "RuntimeShutdownIntent",
    "StopReason",
    "restart_reason_category_for",
    "shutdown_intent_for_entity",
]

StopReason = Literal["restart", "entity_removed", "shutdown"]

# One taxonomy for the `restart_reason_category` and `resulting_action` fields that
# the sync supervisor and the bot response runtime both log, so the two emitters
# cannot drift into describing the same lifecycle event differently.
RestartReasonCategory = Literal[
    "first_sync_timeout",
    "sync_activity_timeout",
    "cache_write_grace_exhausted",
    "watchdog_stall",
    "sync_failure",
    "unexpected_sync_return",
    "config_reload",
    "agent_shutdown",
    "process_shutdown",
]
RuntimeLifecycleAction = Literal[
    "cancel_receive_loop",
    "restart_receive_loop",
    "preserve_response_runtime",
    "drain_then_cancel_response_runtime",
]

_STOP_REASON_CATEGORIES: dict[StopReason | None, RestartReasonCategory] = {
    "restart": "config_reload",
    "entity_removed": "agent_shutdown",
    "shutdown": "process_shutdown",
    None: "agent_shutdown",
}


@dataclass(frozen=True)
class RuntimeShutdownIntent:
    """One lifecycle shutdown decision made at the runtime boundary."""

    stop_reason: StopReason | None
    cancel_source: TaskCancelSource | None = None


GENERIC_SHUTDOWN = RuntimeShutdownIntent(stop_reason=None, cancel_source=None)
ORDERLY_SHUTDOWN = RuntimeShutdownIntent(stop_reason="shutdown", cancel_source=None)
ENTITY_REMOVED_SHUTDOWN = RuntimeShutdownIntent(stop_reason="entity_removed", cancel_source=None)
SYNC_RESTART_SHUTDOWN = RuntimeShutdownIntent(stop_reason="restart", cancel_source="sync_restart")


def restart_reason_category_for(shutdown_intent: RuntimeShutdownIntent) -> RestartReasonCategory:
    """Return the log category describing why one response runtime is shutting down."""
    return _STOP_REASON_CATEGORIES[shutdown_intent.stop_reason]


def shutdown_intent_for_entity(
    entity_name: str,
    *,
    restart_entities: set[str],
) -> RuntimeShutdownIntent:
    """Return shutdown intent for one stopped entity."""
    if entity_name in restart_entities:
        return SYNC_RESTART_SHUTDOWN
    return ENTITY_REMOVED_SHUTDOWN
