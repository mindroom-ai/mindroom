"""Runtime context for native Matrix messaging tool calls."""

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
class MatrixMessageToolContext:
    """Runtime context needed by native Matrix messaging tools."""

    agent_name: str
    room_id: str
    thread_id: str | None
    requester_id: str
    client: nio.AsyncClient
    config: Config
    reply_to_event_id: str | None = None


_MATRIX_MESSAGE_TOOL_CONTEXT: ContextVar[MatrixMessageToolContext | None] = ContextVar(
    "matrix_message_tool_context",
    default=None,
)


def get_matrix_message_tool_context() -> MatrixMessageToolContext | None:
    """Get the current native Matrix messaging tool context."""
    context = _MATRIX_MESSAGE_TOOL_CONTEXT.get()
    if context is not None:
        return context

    runtime_context = get_tool_runtime_context()
    if runtime_context is None:
        return None

    return MatrixMessageToolContext(
        agent_name=runtime_context.agent_name,
        room_id=runtime_context.room_id,
        thread_id=runtime_context.resolved_thread_id,
        requester_id=runtime_context.requester_id,
        client=runtime_context.client,
        config=runtime_context.config,
        reply_to_event_id=runtime_context.reply_to_event_id,
    )


@contextmanager
def matrix_message_tool_context(context: MatrixMessageToolContext | None) -> Iterator[None]:
    """Set Matrix messaging tool context for the current async execution scope."""
    if context is None:
        yield
        return
    legacy_token = _MATRIX_MESSAGE_TOOL_CONTEXT.set(context)
    previous_runtime = get_tool_runtime_context()
    room = (
        previous_runtime.room if previous_runtime is not None and previous_runtime.room_id == context.room_id else None
    )
    thread_id = (
        previous_runtime.thread_id
        if previous_runtime is not None and previous_runtime.room_id == context.room_id
        else context.thread_id
    )
    runtime_context = ToolRuntimeContext(
        agent_name=context.agent_name,
        room_id=context.room_id,
        thread_id=thread_id,
        resolved_thread_id=context.thread_id,
        requester_id=context.requester_id,
        client=context.client,
        config=context.config,
        room=room,
        reply_to_event_id=context.reply_to_event_id,
        storage_path=previous_runtime.storage_path if previous_runtime is not None else None,
    )
    try:
        with tool_runtime_context(runtime_context):
            yield
    finally:
        _MATRIX_MESSAGE_TOOL_CONTEXT.reset(legacy_token)
