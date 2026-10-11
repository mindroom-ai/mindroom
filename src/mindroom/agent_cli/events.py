"""Feed hidden events into the ordinary response stream before presentation."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING

from agno.models.response import ToolExecution
from agno.run.agent import ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.tool_system.context_bound_streams import callback_event_stream, closing_async_stream
from mindroom.tool_system.events import AuditedToolExecution

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from mindroom.tool_system.agent_tool_calls import AgentToolCallEvent

__all__ = [
    "emit_cli_run_event",
    "emit_cli_suspension",
    "emit_cli_tool_event",
    "project_cli_execution",
    "stream_cli_events",
]

_SINK: ContextVar[Callable[[object], None] | None] = ContextVar("cli_tool_event_sink", default=None)


@dataclass(frozen=True)
class _CliSuspension:
    error: BaseException


def emit_cli_suspension(error: BaseException) -> None:
    """Leave Agno through the response stream even when its tool runner catches errors."""
    sink = _SINK.get()
    if sink is not None:
        sink(_CliSuspension(error))


def emit_cli_tool_event(event: AgentToolCallEvent, parent: str | None) -> None:
    """Publish trusted inner audit events, excluding the canonical outer shell alias."""
    sink = _SINK.get()
    if sink is None or event.call_id == parent:
        return
    if event.execution is None:
        if event.progress is not None:
            sink(event.progress)
        return
    if event.kind not in {"started", "completed", "failed", "continuation_required"}:
        return
    cls = ToolCallStartedEvent if event.kind == "started" else ToolCallCompletedEvent
    emit_cli_run_event(cls(tool=event.execution), parent=parent, toolkit_name=event.key.toolkit)


def emit_cli_run_event(
    event: ToolCallStartedEvent | ToolCallCompletedEvent,
    *,
    parent: str | None,
    toolkit_name: str | None,
) -> None:
    """Retain native event provenance while adding trusted hidden-call audit identity."""
    sink = _SINK.get()
    if sink is None or event.tool is None:
        return
    sink(replace(event, tool=project_cli_execution(event.tool, parent=parent, toolkit_name=toolkit_name)))


def project_cli_execution(
    tool: ToolExecution,
    *,
    parent: str | None,
    toolkit_name: str | None,
) -> AuditedToolExecution:
    """Copy a native execution for live or paused presentation without changing its owner."""
    return AuditedToolExecution(
        **{item.name: getattr(tool, item.name) for item in fields(ToolExecution)},
        parent_bash_call_id=parent,
        toolkit_name=toolkit_name,
    )


async def stream_cli_events(source: AsyncIterator[object]) -> AsyncIterator[object]:
    """Merge actual provider and hidden events; may start before tool preparation.

    The existing attempt and presentation consumers remain the only trace owners.
    Bash stays a coroutine returning ToolResult, preserving Agno's media handling.
    """

    async def produce(on_event: Callable[[object], None]) -> None:
        token = _SINK.set(on_event)
        try:
            async with closing_async_stream(source):
                async for event in source:
                    on_event(event)
        finally:
            _SINK.reset(token)

    events = callback_event_stream(produce)
    async with closing_async_stream(events):
        async for event in events:
            if isinstance(event, _CliSuspension):
                raise event.error
            if event is not None:
                yield event
