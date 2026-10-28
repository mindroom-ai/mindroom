"""Explicit usage ownership for paid runs without their own session storage."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import resolve_session_state_root
from mindroom.history.storage import new_scope_session
from mindroom.llm.messages import model_provenance
from mindroom.logging_config import get_logger
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.usage.storage import (
    SYSTEM_USAGE_ENTITY,
    SYSTEM_USAGE_STORAGE_NAME,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mindroom.constants import RuntimePaths
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.llm.runs import RunRecord
    from mindroom.llm.usage import HelperUsage, RequestUsage
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.usage.storage import IndependentUsageKind


@dataclass(frozen=True)
class HelperUsageOwner:
    """Storage and grouping identity for independently recorded usage."""

    storage_factory: Callable[[], ConversationStore]
    session_id: str
    initial_session: ConversationSession | None = None


_HELPER_USAGE_OWNER: ContextVar[HelperUsageOwner | None] = ContextVar("helper_usage_owner", default=None)
logger = get_logger(__name__)


def get_helper_usage_owner() -> HelperUsageOwner | None:
    """Return only the owner bound by the current conversation response boundary."""
    return _HELPER_USAGE_OWNER.get()


@contextmanager
def helper_usage_context(scope_context: ScopeSessionContext | None) -> Iterator[None]:
    """Bind exact agent or team ownership for one async call, stream pull, or close."""
    owner = None
    if scope_context is not None:
        owner = HelperUsageOwner(
            storage_factory=scope_context.storage_factory,
            session_id=scope_context.session_id,
            initial_session=scope_context.first_session() if not scope_context.session_exists else None,
        )
    with helper_usage_owner_context(owner):
        yield


@contextmanager
def helper_usage_owner_context(owner: HelperUsageOwner | None) -> Iterator[None]:
    """Bind one explicit usage owner for an async call, stream pull, or close."""
    token = _HELPER_USAGE_OWNER.set(owner)
    try:
        yield
    finally:
        _HELPER_USAGE_OWNER.reset(token)


async def record_helper_usage(
    usage: HelperUsage,
    *,
    owner: HelperUsageOwner,
    kind: IndependentUsageKind,
    requester_id: str | None,
) -> None:
    """Keep returned usage before output validation, without inventing request boundaries."""
    await _save_usage(_usage_run(usage), usage.run_id, owner=owner, kind=kind, requester_id=requester_id)


async def record_run_usage(
    run: RunRecord,
    *,
    owner: HelperUsageOwner,
    kind: IndependentUsageKind,
    requester_id: str | None,
) -> None:
    """Keep the usage of a helper that ran as a whole agent run: a workflow participant's turn or a skill review."""
    await _save_usage(run.to_dict(), run.run_id, owner=owner, kind=kind, requester_id=requester_id)


async def _save_usage(
    run: dict[str, object],
    invocation_id: str,
    *,
    owner: HelperUsageOwner,
    kind: IndependentUsageKind,
    requester_id: str | None,
) -> None:
    await run_session_storage_operation(
        owner.storage_factory,
        lambda storage: storage.save_independent_usage(
            session_id=owner.session_id,
            usage_id=f"{kind}:{invocation_id}",
            kind=kind,
            requester_id=requester_id,
            run=run,
            initial_session=owner.initial_session,
        ),
    )


def _usage_run(usage: HelperUsage) -> dict[str, object]:
    """Return ``usage`` as the run dict the usage ledger projects: its model, time, metrics, and requests."""
    # TODO(simplify): the ledger projects helper usage from a run dict, so each request is written as an assistant
    # message. Have `save_independent_usage` project a `HelperUsage` itself (#3109). Stored rows would not notice.
    attribution = {"model": usage.model, "model_provider": usage.provider}
    return {
        **{key: value for key, value in attribution.items() if value is not None},
        "created_at": usage.created_at,
        "metrics": usage.metrics.to_dict(),
        "messages": [_request_message(request) for request in usage.requests],
    }


def _request_message(request: RequestUsage) -> dict[str, object]:
    message: dict[str, object] = {
        "role": "assistant",
        "created_at": request.created_at,
        "metrics": request.metrics.to_dict(),
    }
    if request.model is not None:
        message["provider_data"] = model_provenance(request.model.model_id, request.model.provider)
    return message


async def record_system_usage(
    usage: HelperUsage,
    *,
    runtime_paths: RuntimePaths,
    kind: IndependentUsageKind,
) -> None:
    """Persist one internal provider result under an admin-only, content-free source that names no requester."""
    try:
        session_root = resolve_session_state_root(runtime_paths.storage_root, runtime_paths)
        owner = HelperUsageOwner(
            storage_factory=lambda: create_state_storage(
                SYSTEM_USAGE_STORAGE_NAME,
                session_root / SYSTEM_USAGE_STORAGE_NAME,
            ),
            session_id=kind,
            initial_session=new_scope_session(session_id=kind, scope_id=SYSTEM_USAGE_ENTITY, is_team=False),
        )
        await record_helper_usage(usage, owner=owner, kind=kind, requester_id=None)
    except Exception as exc:
        logger.warning("system_usage_recording_failed", kind=kind, error_type=type(exc).__name__)
