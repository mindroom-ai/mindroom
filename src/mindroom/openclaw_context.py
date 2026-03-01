"""Runtime context for OpenClaw-compatible tool calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .tool_runtime_context import ToolRuntimeContext, get_tool_runtime_context, tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import nio

    from .config.main import Config


@dataclass(frozen=True)
class OpenClawToolContext:
    """Runtime context needed by OpenClaw-compatible tools."""

    agent_name: str
    room_id: str
    thread_id: str | None
    requester_id: str
    client: nio.AsyncClient
    config: Config
    storage_path: Path


_OPENCLAW_TOOL_CONTEXT: ContextVar[OpenClawToolContext | None] = ContextVar(
    "openclaw_tool_context",
    default=None,
)


def get_openclaw_tool_context() -> OpenClawToolContext | None:
    """Get the current OpenClaw-compatible tool context."""
    context = _OPENCLAW_TOOL_CONTEXT.get()
    if context is not None:
        return context

    runtime_context = get_tool_runtime_context()
    if runtime_context is None or runtime_context.storage_path is None:
        return None

    return OpenClawToolContext(
        agent_name=runtime_context.agent_name,
        room_id=runtime_context.room_id,
        thread_id=runtime_context.thread_id,
        requester_id=runtime_context.requester_id,
        client=runtime_context.client,
        config=runtime_context.config,
        storage_path=runtime_context.storage_path,
    )


@contextmanager
def openclaw_tool_context(context: OpenClawToolContext | None) -> Iterator[None]:
    """Set OpenClaw-compatible tool context for the current async scope."""
    if context is None:
        yield
        return
    legacy_token = _OPENCLAW_TOOL_CONTEXT.set(context)
    previous_runtime = get_tool_runtime_context()
    room = (
        previous_runtime.room if previous_runtime is not None and previous_runtime.room_id == context.room_id else None
    )
    reply_to_event_id = previous_runtime.reply_to_event_id if previous_runtime is not None else None
    resolved_thread_id = context.thread_id
    if resolved_thread_id is None and previous_runtime is not None and previous_runtime.room_id == context.room_id:
        resolved_thread_id = previous_runtime.resolved_thread_id
    runtime_context = ToolRuntimeContext(
        agent_name=context.agent_name,
        room_id=context.room_id,
        thread_id=context.thread_id,
        resolved_thread_id=resolved_thread_id,
        requester_id=context.requester_id,
        client=context.client,
        config=context.config,
        room=room,
        reply_to_event_id=reply_to_event_id,
        storage_path=context.storage_path,
    )
    try:
        with tool_runtime_context(runtime_context):
            yield
    finally:
        _OPENCLAW_TOOL_CONTEXT.reset(legacy_token)
