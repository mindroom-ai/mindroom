"""Stdlib-only active tool call, conversation, and detached-requester context scopes."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.history.types import HistoryScope
    from mindroom.tool_system.invocation import ToolCall
    from mindroom.tool_system.runtime_context import DetachedRequesterContext, ToolRuntimeContext

_TOOL_RUNTIME_CONTEXT: ContextVar[ToolRuntimeContext | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)
_DETACHED_REQUESTER_CONTEXT: ContextVar[DetachedRequesterContext | None] = ContextVar(
    "detached_requester_context",
    default=None,
)
_CURRENT_TOOL_CALL: ContextVar[ToolCall | None] = ContextVar("mindroom_current_tool_call", default=None)


def current_tool_call() -> ToolCall | None:
    """Return the tool call ``run_tool_call`` is running in this task, with the arguments it was entered with."""
    return _CURRENT_TOOL_CALL.get()


@contextmanager
def bind_current_tool_call(call: ToolCall) -> Iterator[None]:
    """Bind the current call for this task, restoring the previous call on exit."""
    token = _CURRENT_TOOL_CALL.set(call)
    try:
        yield
    finally:
        _CURRENT_TOOL_CALL.reset(token)


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentCall:
    """The tool call a function runs for.

    ``call_id`` is the provider's id of the call when a model made it. ``toolkit`` is the authored toolkit the agent
    was offered the function from; None for a function generated for the agent. ``session_state`` is the session's
    state, which the function may change in place and MindRoom stores after the run. ``history_scope`` names a team's
    stored conversation for a call of a team run. ``runtime`` is the context ``get_tool_runtime_context()`` returns,
    with the room, the requester, and the model the call runs for.
    """

    call_id: str | None
    toolkit: str | None
    session_id: str
    session_state: dict[str, Any]
    history_scope: HistoryScope | None
    runtime: ToolRuntimeContext | None


def current_call() -> CurrentCall | None:
    """Return the tool call this task is running; None outside one, such as when code calls a tool method directly."""
    call = current_tool_call()
    if call is None:
        return None
    return CurrentCall(
        call_id=call.context.call_id,
        toolkit=call.function.toolkit,
        session_id=call.context.session_id,
        session_state=call.context.session_state,
        history_scope=call.context.history_scope,
        runtime=get_tool_runtime_context(),
    )


def get_tool_runtime_context() -> ToolRuntimeContext | None:
    """Get the current shared tool runtime context."""
    return _TOOL_RUNTIME_CONTEXT.get()


def get_detached_requester_context() -> DetachedRequesterContext | None:
    """Return authority established by the current detached request boundary."""
    return _DETACHED_REQUESTER_CONTEXT.get()


def current_budget_monitor() -> BudgetMonitor | None:
    """Return the spend monitor of the current conversation or detached API request."""
    if (context := _TOOL_RUNTIME_CONTEXT.get()) is not None:
        return context.budget_monitor
    detached = _DETACHED_REQUESTER_CONTEXT.get()
    return detached.budget_monitor if detached is not None else None


def current_requester_id() -> str | None:
    """Return the requester of the current conversation, or of the detached API request when there is none.

    An OpenAI-compatible request has no Matrix conversation, only its authenticated requester.
    """
    context = _TOOL_RUNTIME_CONTEXT.get() or _DETACHED_REQUESTER_CONTEXT.get()
    return context.requester_id if context is not None else None


@contextmanager
def tool_runtime_context(context: ToolRuntimeContext | None) -> Iterator[None]:
    """Set shared tool runtime context for the current async execution scope."""
    token = _TOOL_RUNTIME_CONTEXT.set(context)
    try:
        yield
    finally:
        _TOOL_RUNTIME_CONTEXT.reset(token)


@contextmanager
def detached_requester_context(context: DetachedRequesterContext | None) -> Iterator[None]:
    """Bind detached authority for an operation without leaking between requests."""
    token = _DETACHED_REQUESTER_CONTEXT.set(context)
    try:
        yield
    finally:
        _DETACHED_REQUESTER_CONTEXT.reset(token)
