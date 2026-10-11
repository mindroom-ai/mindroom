"""Current function authority rechecked immediately before application entry."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from agno.tools.function import FunctionCall

    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class _BoundCall:
    """The actor, the call its grant belongs to, and whether that call is the one entering the application."""

    owner: ToolExecutionIdentity
    call: FunctionCall
    entering: bool


_CALL: ContextVar[_BoundCall | None] = ContextVar("tool_job_call", default=None)


@contextmanager
def _bound_call(current: _BoundCall) -> Iterator[None]:
    token = _CALL.set(current)
    try:
        yield
    finally:
        _CALL.reset(token)


@contextmanager
def authorized_tool_call(owner: ToolExecutionIdentity, call: FunctionCall) -> Iterator[None]:
    """Retain the authenticated actor and exact call across hooks, waits, and result consumption."""
    with _bound_call(_BoundCall(owner, call, entering=True)):
        yield


@contextmanager
def nested_tool_call(call: FunctionCall, *, own_grant: bool) -> Iterator[None]:
    """Check a call nested inside owned work under the same actor.

    A call with its own grant is checked as itself. One without, such as an embedded workflow participant's tool,
    runs under its enclosing call's grant, which is then checked with that call's own arguments.
    """
    current = _CALL.get()
    if current is None:
        yield
        return
    with _bound_call(
        _BoundCall(current.owner, call, entering=True) if own_grant else replace(current, entering=False),
    ):
        yield


def current_tool_call() -> FunctionCall | None:
    """Return the exact call whose result a management tool consumes."""
    current = _CALL.get()
    return current.call if current is not None else None


def check_current_execution_authority(*, arguments: Mapping[str, Any] | None = None) -> None:
    """Revalidate after each cooperative checkpoint, including nested calls."""
    current = _CALL.get()
    if current is None:
        return
    context = get_tool_runtime_context()
    runtime = get_background_runtime(context.runtime_paths) if context is not None else None
    if runtime is not None:
        # Only the entering call is checked with its accepted arguments; an enclosing grant keeps its own call's.
        accepted = arguments if arguments is not None and current.entering else current.call.arguments or {}
        runtime.authorize_execution(current.owner, current.call.function, accepted)
