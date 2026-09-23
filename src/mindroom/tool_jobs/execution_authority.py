"""Current function authority rechecked immediately before application entry."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from agno.tools.function import Function

    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_CALL: ContextVar[tuple[ToolExecutionIdentity, Function, Mapping[str, Any]] | None] = ContextVar(
    "tool_job_authority",
    default=None,
)


@contextmanager
def authorized_tool_call(
    owner: ToolExecutionIdentity,
    function: Function,
    *,
    arguments: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    """Retain authenticated actor and exact callable across hooks and waits."""
    token = _CALL.set((owner, function, arguments or {}))
    try:
        yield
    finally:
        _CALL.reset(token)


def check_current_execution_authority(*, arguments: Mapping[str, Any] | None = None) -> None:
    """Revalidate after each cooperative checkpoint, including nested calls."""
    call = _CALL.get()
    if call is None:
        return
    context = get_tool_runtime_context()
    runtime = get_background_runtime(context.runtime_paths) if context is not None else None
    if runtime is not None:
        owner, function, accepted_arguments = call
        runtime.authorize_execution(owner, function, accepted_arguments if arguments is None else arguments)
