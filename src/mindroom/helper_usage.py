"""Explicit conversation ownership for paid runs without their own session storage."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

from mindroom.agent_storage import run_session_storage_operation, save_independent_usage
from mindroom.history.storage import new_scope_session

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agno.db.base import BaseDb
    from agno.run.agent import RunOutput
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.history.session_context import ScopeSessionContext


@dataclass(frozen=True)
class HelperUsageOwner:
    """Actual caller conversation, independent of a helper's synthetic session ID."""

    storage_factory: Callable[[], BaseDb]
    session_id: str
    initial_session: AgentSession | TeamSession | None = None


_HELPER_USAGE_OWNER: ContextVar[HelperUsageOwner | None] = ContextVar("helper_usage_owner", default=None)


def get_helper_usage_owner() -> HelperUsageOwner | None:
    """Return only the owner bound by the current conversation response boundary."""
    return _HELPER_USAGE_OWNER.get()


@contextmanager
def helper_usage_context(scope_context: ScopeSessionContext | None) -> Iterator[None]:
    """Bind exact agent or team ownership for one async call, stream pull, or close."""
    owner = None
    if scope_context is not None and scope_context.storage_factory is not None and scope_context.session_id is not None:
        owner = HelperUsageOwner(
            storage_factory=scope_context.storage_factory,
            session_id=scope_context.session_id,
            initial_session=(
                new_scope_session(
                    session_id=scope_context.session_id,
                    scope_id=scope_context.scope.scope_id,
                    is_team=scope_context.scope.kind == "team",
                )
                if not scope_context.session_exists
                else None
            ),
        )
    token = _HELPER_USAGE_OWNER.set(owner)
    try:
        yield
    finally:
        _HELPER_USAGE_OWNER.reset(token)


async def record_helper_usage(
    response: RunOutput,
    *,
    owner: HelperUsageOwner,
    invocation_id: str,
    kind: Literal["memory_auto_flush", "dynamic_workflow"],
    requester_id: str | None,
) -> None:
    """Keep returned usage before output validation, without inventing request boundaries."""
    if response.metrics is None:
        return
    await run_session_storage_operation(
        owner.storage_factory,
        partial(
            save_independent_usage,
            session_id=owner.session_id,
            usage_id=f"{kind}:{invocation_id}",
            kind=kind,
            requester_id=requester_id,
            run=response.to_dict(),
            initial_session=owner.initial_session,
        ),
    )
