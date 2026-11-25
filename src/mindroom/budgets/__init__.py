"""Per-user monthly spending budgets."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mindroom.budgets.monitor import BudgetMonitor

    type _BudgetScope = Callable[[], tuple[BudgetMonitor | None, str | None]]

_CONVERSATION_BUDGET: ContextVar[_BudgetScope | None] = ContextVar(
    "conversation_budget",
    default=None,
)
_DETACHED_BUDGET: ContextVar[_BudgetScope | None] = ContextVar(
    "detached_budget",
    default=None,
)


@contextmanager
def bind_budget_scope(scope: _BudgetScope | None, *, detached: bool = False) -> Iterator[None]:
    """Bind a lazy ``(monitor, requester_id)`` reader for the current conversation or detached scope."""
    var = _DETACHED_BUDGET if detached else _CONVERSATION_BUDGET
    token = var.set(scope)
    try:
        yield
    finally:
        var.reset(token)


def current_budget_scope() -> tuple[BudgetMonitor | None, str | None]:
    """Return ``(monitor, requester_id)`` from the current conversation or detached request scope."""
    scope = _CONVERSATION_BUDGET.get() or _DETACHED_BUDGET.get()
    return scope() if scope is not None else (None, None)
