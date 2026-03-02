"""Shared runtime context for tool calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import nio

    from mindroom.config.main import Config


@dataclass(frozen=True)
class ToolRuntimeContext:
    """Shared runtime metadata available to all tools."""

    agent_name: str
    room_id: str
    thread_id: str | None
    resolved_thread_id: str | None
    requester_id: str
    client: nio.AsyncClient
    config: Config
    room: nio.MatrixRoom | None = None
    reply_to_event_id: str | None = None
    storage_path: Path | None = None
    attachment_ids: tuple[str, ...] = field(default_factory=tuple)
    runtime_attachment_ids: list[str] = field(default_factory=list)


_TOOL_RUNTIME_CONTEXT: ContextVar[ToolRuntimeContext | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)


def get_tool_runtime_context() -> ToolRuntimeContext | None:
    """Get the current shared tool runtime context."""
    return _TOOL_RUNTIME_CONTEXT.get()


def attachment_id_available_in_tool_runtime_context(
    context: ToolRuntimeContext,
    attachment_id: str,
) -> bool:
    """Return whether an attachment ID is currently available in context."""
    normalized_attachment_id = attachment_id.strip()
    if not normalized_attachment_id:
        return False
    return (
        normalized_attachment_id in context.attachment_ids or normalized_attachment_id in context.runtime_attachment_ids
    )


def list_tool_runtime_attachment_ids(context: ToolRuntimeContext) -> list[str]:
    """Return all attachment IDs currently available in runtime context order."""
    attachment_ids: list[str] = []
    for attachment_id in (*context.attachment_ids, *context.runtime_attachment_ids):
        if attachment_id and attachment_id not in attachment_ids:
            attachment_ids.append(attachment_id)
    return attachment_ids


def append_tool_runtime_attachment_id(attachment_id: str) -> ToolRuntimeContext | None:
    """Append an attachment ID to the current tool context, preserving order."""
    context = get_tool_runtime_context()
    if context is None:
        return None

    normalized_attachment_id = attachment_id.strip()
    if not normalized_attachment_id:
        return context
    if attachment_id_available_in_tool_runtime_context(context, normalized_attachment_id):
        return context

    context.runtime_attachment_ids.append(normalized_attachment_id)
    return context


@contextmanager
def tool_runtime_context(context: ToolRuntimeContext | None) -> Iterator[None]:
    """Set shared tool runtime context for the current async execution scope."""
    token = _TOOL_RUNTIME_CONTEXT.set(context)
    try:
        yield
    finally:
        _TOOL_RUNTIME_CONTEXT.reset(token)
