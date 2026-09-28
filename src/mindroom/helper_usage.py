"""Explicit usage ownership for paid runs without their own session storage."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.agent_storage import create_state_storage, run_session_storage_operation, save_independent_usage
from mindroom.constants import resolve_session_state_root
from mindroom.history.storage import new_scope_session
from mindroom.logging_config import get_logger
from mindroom.usage_storage import (
    SYSTEM_USAGE_ENTITY,
    SYSTEM_USAGE_SESSION_TABLE,
    SYSTEM_USAGE_STORAGE_NAME,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agno.db.base import BaseDb
    from agno.run.agent import RunOutput
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.constants import RuntimePaths
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.usage_storage import IndependentUsageKind


@dataclass(frozen=True)
class HelperUsageOwner:
    """Storage and grouping identity for independently recorded usage."""

    storage_factory: Callable[[], BaseDb]
    session_id: str
    initial_session: AgentSession | TeamSession | None = None


_HELPER_USAGE_OWNER: ContextVar[HelperUsageOwner | None] = ContextVar("helper_usage_owner", default=None)
logger = get_logger(__name__)


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
    kind: IndependentUsageKind,
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


async def record_system_usage(
    response: RunOutput,
    *,
    runtime_paths: RuntimePaths,
    kind: IndependentUsageKind,
    requester_id: str | None = None,
) -> None:
    """Persist one internal provider result under an admin-only, content-free source."""
    if response.metrics is None:
        return
    try:
        session_root = resolve_session_state_root(runtime_paths.storage_root, runtime_paths)
        owner = HelperUsageOwner(
            storage_factory=lambda: create_state_storage(
                storage_name=SYSTEM_USAGE_STORAGE_NAME,
                state_root=session_root / SYSTEM_USAGE_STORAGE_NAME,
                subdir="sessions",
                session_table=SYSTEM_USAGE_SESSION_TABLE,
            ),
            session_id=kind,
            initial_session=new_scope_session(session_id=kind, scope_id=SYSTEM_USAGE_ENTITY, is_team=False),
        )
        await record_helper_usage(
            response,
            owner=owner,
            invocation_id=response.run_id or uuid4().hex,
            kind=kind,
            requester_id=requester_id,
        )
    except Exception as exc:
        logger.warning("system_usage_recording_failed", kind=kind, error_type=type(exc).__name__)
