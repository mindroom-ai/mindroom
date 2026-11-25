"""Stdlib-only active conversation and detached-requester context scopes."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.tool_system.runtime_context import DetachedRequesterContext, ToolRuntimeContext

_TOOL_RUNTIME_CONTEXT: ContextVar[ToolRuntimeContext | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)
_DETACHED_REQUESTER_CONTEXT: ContextVar[DetachedRequesterContext | None] = ContextVar(
    "detached_requester_context",
    default=None,
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
