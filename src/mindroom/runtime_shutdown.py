"""Typed runtime shutdown intent shared by sync, bot, and response drains."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from mindroom.cancellation import TaskCancelSource

__all__ = [
    "ENTITY_REMOVED_SHUTDOWN",
    "GENERIC_SHUTDOWN",
    "ORDERLY_SHUTDOWN",
    "RESPONSE_FINALIZATION_TIMEOUT_SECONDS",
    "SYNC_RESTART_SHUTDOWN",
    "SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS",
    "ResponseShutdownTimeoutError",
    "RestartReasonCategory",
    "RuntimeLifecycleAction",
    "RuntimeShutdownIntent",
    "ShutdownBudget",
    "StopReason",
    "gather_shutdown_phase",
    "restart_reason_category_for",
    "shutdown_intent_for_entity",
]

StopReason = Literal["restart", "entity_removed", "shutdown"]

SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS = 5.0
RESPONSE_FINALIZATION_TIMEOUT_SECONDS = 15.0


async def gather_shutdown_phase(
    *awaitables: Awaitable[object],
) -> tuple[list[object], asyncio.CancelledError | None]:
    """Gather and finish one ownership-release phase if its caller is cancelled."""
    phase = asyncio.gather(*awaitables, return_exceptions=True)
    try:
        return list(await asyncio.shield(phase)), None
    except asyncio.CancelledError as cancellation:
        return list(await phase), cancellation


class ResponseShutdownTimeoutError(RuntimeError):
    """Raised when a response still owns runtime resources after bounded cleanup."""


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


@dataclass(frozen=True)
class ShutdownBudget:
    """One monotonic deadline shared by sequential shutdown stages."""

    deadline_monotonic: float

    @classmethod
    def start(cls, timeout_seconds: float) -> ShutdownBudget:
        """Start a budget that expires after ``timeout_seconds``."""
        return cls(deadline_monotonic=time.monotonic() + max(0.0, timeout_seconds))

    def remaining_seconds(self) -> float:
        """Return the non-negative time remaining before the deadline."""
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def per_window_seconds(self, *, windows: int) -> float:
        """Divide the remaining budget across a fixed number of wait windows."""
        if windows <= 0:
            message = "shutdown budget windows must be positive"
            raise ValueError(message)
        return self.remaining_seconds() / windows


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
