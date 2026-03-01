"""Runtime context for the scheduler tool."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .tool_runtime_context import ToolRuntimeContext, get_tool_runtime_context, tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterator

    import nio

    from .config.main import Config


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
    context = _SCHEDULING_TOOL_CONTEXT.get()
    if context is not None:
        return context

    runtime_context = get_tool_runtime_context()
    if runtime_context is None or runtime_context.room is None:
        return None

    return SchedulingToolContext(
        client=runtime_context.client,
        room=runtime_context.room,
        room_id=runtime_context.room_id,
        thread_id=runtime_context.resolved_thread_id,
        requester_id=runtime_context.requester_id,
        config=runtime_context.config,
    )


@contextmanager
def scheduling_tool_context(context: SchedulingToolContext | None) -> Iterator[None]:
    """Set scheduling tool context for the current async execution scope."""
    if context is None:
        yield
        return
    legacy_token = _SCHEDULING_TOOL_CONTEXT.set(context)
    previous_runtime = get_tool_runtime_context()
    thread_id = (
        previous_runtime.thread_id
        if previous_runtime is not None and previous_runtime.room_id == context.room_id
        else context.thread_id
    )
    runtime_context = ToolRuntimeContext(
        agent_name=previous_runtime.agent_name if previous_runtime is not None else "",
        room_id=context.room_id,
        thread_id=thread_id,
        resolved_thread_id=context.thread_id,
        requester_id=context.requester_id,
        client=context.client,
        config=context.config,
        room=context.room,
        reply_to_event_id=previous_runtime.reply_to_event_id if previous_runtime is not None else None,
        storage_path=previous_runtime.storage_path if previous_runtime is not None else None,
    )
    try:
        with tool_runtime_context(runtime_context):
            yield
    finally:
        _SCHEDULING_TOOL_CONTEXT.reset(legacy_token)
