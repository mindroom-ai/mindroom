"""Runtime context for native Matrix messaging tool calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    return _MATRIX_MESSAGE_TOOL_CONTEXT.get()


@contextmanager
def matrix_message_tool_context(context: MatrixMessageToolContext | None) -> Iterator[None]:
    """Set Matrix messaging tool context for the current async execution scope."""
    if context is None:
        yield
        return
    token = _MATRIX_MESSAGE_TOOL_CONTEXT.set(context)
    try:
        yield
    finally:
        _MATRIX_MESSAGE_TOOL_CONTEXT.reset(token)
