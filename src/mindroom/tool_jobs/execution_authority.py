"""Current function authority rechecked immediately before application entry."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agno.tools.function import Function

    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

type _ExecutionAuthorizer = Callable[[ToolExecutionIdentity, Function], None]
_AUTHORIZE: _ExecutionAuthorizer | None = None
_CALL: ContextVar[tuple[ToolExecutionIdentity, Function] | None] = ContextVar("tool_job_authority", default=None)


def set_execution_authorizer(authorize: _ExecutionAuthorizer | None) -> None:
    """Install the managed runtime's current local permission policy."""
    global _AUTHORIZE
    _AUTHORIZE = authorize


@contextmanager
def authorized_tool_call(owner: ToolExecutionIdentity, function: Function) -> Iterator[None]:
    """Retain authenticated actor and exact callable across hooks and waits."""
    token = _CALL.set((owner, function))
    try:
        yield
    finally:
        _CALL.reset(token)


def check_current_execution_authority() -> None:
    """Revalidate after each cooperative checkpoint, including nested calls."""
    call = _CALL.get()
    if call is not None and _AUTHORIZE is not None:
        _AUTHORIZE(*call)
