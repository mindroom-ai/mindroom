"""Runtime context for session and sub-agent tool calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import nio

    from .config import Config


@dataclass(frozen=True)
class SessionToolsContext:
    """Runtime context needed by session and sub-agent tools."""

    agent_name: str
    room_id: str
    thread_id: str | None
    requester_id: str
    client: nio.AsyncClient
    config: Config
    storage_path: Path


_SESSION_TOOLS_CONTEXT: ContextVar[SessionToolsContext | None] = ContextVar(
    "session_tools_context",
    default=None,
)


def get_session_tools_context() -> SessionToolsContext | None:
    """Get the current session/sub-agent tool context."""
    return _SESSION_TOOLS_CONTEXT.get()


@contextmanager
def session_tools_context(context: SessionToolsContext | None) -> Iterator[None]:
    """Set session/sub-agent tool context for the current async scope."""
    if context is None:
        yield
        return
    token = _SESSION_TOOLS_CONTEXT.set(context)
    try:
        yield
    finally:
        _SESSION_TOOLS_CONTEXT.reset(token)
