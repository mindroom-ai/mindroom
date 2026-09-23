"""Current function authority rechecked immediately before application entry."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from agno.tools.function import Function

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

type _ExecutionAuthorizer = Callable[[ToolExecutionIdentity, Function, Mapping[str, Any]], None]
_AUTHORIZERS: dict[Path, _ExecutionAuthorizer] = {}
_CALL: ContextVar[tuple[ToolExecutionIdentity, Function, Mapping[str, Any]] | None] = ContextVar(
    "tool_job_authority",
    default=None,
)


def set_execution_authorizer(runtime_paths: RuntimePaths, authorize: _ExecutionAuthorizer | None) -> None:
    """Install or withdraw only this managed runtime's permission policy."""
    key = runtime_paths.storage_root
    if authorize is None:
        _AUTHORIZERS.pop(key, None)
    else:
        _AUTHORIZERS[key] = authorize


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
    authorize = _AUTHORIZERS.get(context.runtime_paths.storage_root) if context is not None else None
    if authorize is not None:
        owner, function, accepted_arguments = call
        authorize(owner, function, accepted_arguments if arguments is None else arguments)
