"""Current function authority rechecked immediately before application entry.

A ``JobTool`` is what admission and grant checks read about one function an actor offers, and a ``JobCall`` is one
call of it; the call a task is running under is bound here, so every cooperative checkpoint can recheck its grant
and a management tool can find the exact call whose result it consumes.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from mindroom.tool_system.agno_toolkits import Toolkit
    from mindroom.tool_system.invocation import ToolCallContext
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True, slots=True, kw_only=True)
class JobTool:
    """One function a MindRoom-assembled toolkit offers an actor, with the evidence its grant is checked against.

    ``toolkit_name`` is the authored toolkit, ``toolkit`` the concrete toolkit built for the actor, and
    ``authority`` the actor's execution scope and file access with the toolkit's construction, as they were when the
    actor was built.
    """

    function: ToolFunction
    toolkit_name: str
    toolkit: Toolkit
    authority: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class JobCall:
    """One call of a ``JobTool``: its arguments, the run it belongs to, and whether a team's run saves it."""

    tool: JobTool
    arguments: Mapping[str, Any]
    context: ToolCallContext
    team: bool


@dataclass(frozen=True)
class _BoundCall:
    """The actor, the call its grant belongs to, and whether that call is the one entering the application."""

    owner: ToolExecutionIdentity
    call: JobCall
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
def authorized_tool_call(owner: ToolExecutionIdentity, call: JobCall) -> Iterator[None]:
    """Retain the authenticated actor and exact call across hooks, waits, and result consumption."""
    with _bound_call(_BoundCall(owner, call, entering=True)):
        yield


@contextmanager
def nested_tool_call(call: JobCall | None) -> Iterator[None]:
    """Check a call nested inside owned work under the same actor.

    A call of an actor built with its own grant is checked as itself. One without, such as a function the framework
    generated or a team's, runs under its enclosing call's grant, which is then checked with that call's own
    arguments.
    """
    current = _CALL.get()
    if current is None:
        yield
        return
    if call is not None and "scope" in call.tool.authority:
        bound = _BoundCall(current.owner, call, entering=True)
    else:
        bound = replace(current, entering=False)
    with _bound_call(bound):
        yield


def current_job_call() -> JobCall | None:
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
        accepted = arguments if arguments is not None and current.entering else current.call.arguments
        runtime.authorize_execution(current.owner, current.call.tool, accepted)
