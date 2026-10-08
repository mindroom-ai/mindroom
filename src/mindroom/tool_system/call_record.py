"""The record each tool call leaves before it runs, which a reply a restart interrupted reads back."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


class _ToolCallRecorder(Protocol):
    """Records tool calls where the work that made them keeps its durable state."""

    async def started(self, tool_name: str, args: Mapping[str, object]) -> str | None:
        """Record a call before the tool runs; return its id, or ``None`` when nothing records it."""
        ...

    async def finished(self, call_id: str, tool_name: str, args: Mapping[str, object], result: object) -> None:
        """Record that a call returned, or raised ``result``; never raises, since the call already ran."""
        ...


_recorder: ContextVar[_ToolCallRecorder | None] = ContextVar("mindroom_tool_call_recorder", default=None)


def tool_call_recorder() -> _ToolCallRecorder | None:
    """Return the recorder of the work the current task runs tools for, if any."""
    return _recorder.get()


@contextmanager
def recording_tool_calls(recorder: _ToolCallRecorder) -> Iterator[None]:
    """Record every tool call made in this context, child tasks included."""
    token = _recorder.set(recorder)
    try:
        yield
    finally:
        _recorder.reset(token)
