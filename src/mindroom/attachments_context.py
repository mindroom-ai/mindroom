"""Runtime context for the attachments toolkit."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import nio


@dataclass(frozen=True)
class AttachmentToolContext:
    """Runtime context needed by the attachments toolkit."""

    client: nio.AsyncClient
    room_id: str
    thread_id: str | None
    requester_id: str
    storage_path: Path
    attachment_ids: tuple[str, ...] = field(default_factory=tuple)


_ATTACHMENT_TOOL_CONTEXT: ContextVar[AttachmentToolContext | None] = ContextVar(
    "attachment_tool_context",
    default=None,
)


def get_attachment_tool_context() -> AttachmentToolContext | None:
    """Get the current attachments tool context."""
    return _ATTACHMENT_TOOL_CONTEXT.get()


@contextmanager
def attachment_tool_context(context: AttachmentToolContext | None) -> Iterator[None]:
    """Set attachments tool context for the current async scope."""
    if context is None:
        yield
        return
    token = _ATTACHMENT_TOOL_CONTEXT.set(context)
    try:
        yield
    finally:
        _ATTACHMENT_TOOL_CONTEXT.reset(token)
