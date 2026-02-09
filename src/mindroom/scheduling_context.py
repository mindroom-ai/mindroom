"""Runtime context for the scheduler tool."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    import nio

    from .config import Config


@dataclass(frozen=True)
class SchedulingToolContext:
    """Runtime context needed for tool-driven scheduling."""

    client: nio.AsyncClient
    room: nio.MatrixRoom
    room_id: str
    thread_id: str | None
    requester_id: str
    config: Config


_SCHEDULING_TOOL_CONTEXT: ContextVar[SchedulingToolContext | None] = ContextVar(
    "scheduling_tool_context",
    default=None,
)


def get_scheduling_tool_context() -> SchedulingToolContext | None:
    """Get the current scheduling tool context."""
    return _SCHEDULING_TOOL_CONTEXT.get()


@contextmanager
def scheduling_tool_context(context: SchedulingToolContext) -> Iterator[None]:
    """Set scheduling tool context for the current async execution scope."""
    token = _SCHEDULING_TOOL_CONTEXT.set(context)
    try:
        yield
    finally:
        _SCHEDULING_TOOL_CONTEXT.reset(token)
