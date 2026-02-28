"""Runtime context for OpenClaw-compatible tool calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
    attachment_ids: tuple[str, ...] = field(default_factory=tuple)


_OPENCLAW_TOOL_CONTEXT: ContextVar[OpenClawToolContext | None] = ContextVar(
    "openclaw_tool_context",
    default=None,
)


def get_openclaw_tool_context() -> OpenClawToolContext | None:
    """Get the current OpenClaw-compatible tool context."""
    return _OPENCLAW_TOOL_CONTEXT.get()


@contextmanager
def openclaw_tool_context(context: OpenClawToolContext | None) -> Iterator[None]:
    """Set OpenClaw-compatible tool context for the current async scope."""
    if context is None:
        yield
        return
    token = _OPENCLAW_TOOL_CONTEXT.set(context)
    try:
        yield
    finally:
        _OPENCLAW_TOOL_CONTEXT.reset(token)
