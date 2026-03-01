"""Shared runtime context for tool calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import nio

    from .config.main import Config


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


_TOOL_RUNTIME_CONTEXT: ContextVar[ToolRuntimeContext | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)


def get_tool_runtime_context() -> ToolRuntimeContext | None:
    """Get the current shared tool runtime context."""
    return _TOOL_RUNTIME_CONTEXT.get()


@contextmanager
def tool_runtime_context(context: ToolRuntimeContext | None) -> Iterator[None]:
    """Set shared tool runtime context for the current async execution scope."""
    if context is None:
        yield
        return
    token = _TOOL_RUNTIME_CONTEXT.set(context)
    try:
        yield
    finally:
        _TOOL_RUNTIME_CONTEXT.reset(token)
