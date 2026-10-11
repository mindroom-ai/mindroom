"""The record each tool call leaves before it runs, which a reply a restart interrupted reads back."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping


class _ToolCallRecorder(Protocol):
    """Records tool calls where the work that made them keeps its durable state."""

    async def started(self, tool_name: str, args: Mapping[str, object]) -> str | None:
        """Record a call before the tool runs; return its id, or ``None`` when nothing records it.

        Raises ``asyncio.CancelledError`` when the work the call belongs to was stopped or ended, so the tool never runs.
        """
        ...

    async def finished(self, call_id: str, tool_name: str, args: Mapping[str, object], result: object) -> None:
        """Record that a call returned, or raised ``result``; never raises, since the call already ran."""
        ...

    async def admits(self) -> bool:
        """Return whether the work still lets a tool start, as recording a call's start checks."""
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


async def record_call(tool_name: str, args: Mapping[str, object]) -> Callable[[object], Awaitable[None]]:
    """Record a call whose tool then runs outside this work's recording, as a background job does; return its finish.

    The work's record then names the call, and, as for any recorded call, a Stop committed first raises
    ``asyncio.CancelledError`` here so the call never starts.
    """
    recorder = _recorder.get()
    record_id = None if recorder is None else await recorder.started(tool_name, args)

    async def finished(result: object) -> None:
        if recorder is not None and record_id is not None:
            await recorder.finished(record_id, tool_name, args, result)

    return finished


async def admits_tool_start() -> bool:
    """Return whether the work the current task runs tools for still lets a tool start; unrecorded work always does.

    A background job's call asks again once its job is admitted: a Stop or deletion that committed after the call's
    recorded start, but before its job was saved, found no job to cancel.
    """
    recorder = _recorder.get()
    return recorder is None or await recorder.admits()


@contextmanager
def without_tool_call_recording() -> Iterator[None]:
    """Run tools outside the work that started them, as a background job does: the job keeps its own outcome."""
    token = _recorder.set(None)
    try:
        yield
    finally:
        _recorder.reset(token)
