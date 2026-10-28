"""Approval failure settles delegated work before releasing its sources and shows only redacted reasons."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.approval import continuation_runner
from mindroom.approval.recovery import ApprovalRecovery
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.event_journal import ApprovalContinuation, ApprovalFailure, EventJournalStore, PrincipalStore
from mindroom.event_journal.approval_store import ApprovalStore
from mindroom.reply_lifecycle import ApprovalFailureKind
from tests.approval_continuation_helpers import approval_continuation
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.approval.continuation_runner import ApprovalContinuationRunner


def _continuation() -> ApprovalContinuation:
    return approval_continuation(
        approval_id="approval-1",
        run_id="parent-run",
        session_id="parent-session",
        entity_name="leader",
        room_id="!room:test",
        requester_id="@human:test",
        response_event_id="$response",
        state="failing",
        failure=ApprovalFailure(ApprovalFailureKind.USER_STOP),
    )


@contextmanager
def _settling(
    tmp_path: Path,
    continuation: ApprovalContinuation,
    *,
    finish: Callable[[str], Awaitable[bool]],
    cancel: Callable[..., Awaitable[None]] | None = None,
) -> Iterator[ApprovalContinuationRunner]:
    """Run the paused reply's settlement over ``continuation``, which is already fenced and has its cards expired."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = MagicMock(spec=ApprovalStore)
    store.approval_continuation = AsyncMock(return_value=continuation)
    runner.deps = replace(runner.deps, approval_store=store)
    with (
        patch.object(runner._approvals, "_answer_stands", new=AsyncMock(return_value=False)),
        patch(
            "mindroom.approval.continuation_runner.prepare_approval_failure",
            new=AsyncMock(return_value=continuation),
        ),
        patch("mindroom.approval.continuation_runner.cancel_approval_delegations", new=AsyncMock(side_effect=cancel)),
        patch.object(runner.deps.replies, "finish_approval", new=AsyncMock(side_effect=finish)),
    ):
        yield runner._approvals


@pytest.mark.asyncio
async def test_source_failure_cancels_children_before_finishing(tmp_path: Path) -> None:
    """A stopped approval cannot leave paused child records after its source is released."""
    continuation = _continuation()
    order: list[str] = []

    async def cancel(*_args: object, **_kwargs: object) -> None:
        order.append("children")

    async def finish(_approval_id: str) -> bool:
        order.append("source")
        return True

    with _settling(tmp_path, continuation, finish=finish, cancel=cancel) as approvals:
        assert await approvals._settle(continuation, ApprovalFailure(ApprovalFailureKind.USER_STOP))
        cancel_children = cast("AsyncMock", continuation_runner.cancel_approval_delegations)
        cancel_children.assert_awaited_once_with(
            continuation,
            config=approvals.deps.runtime.config,
            runtime_paths=approvals.deps.runtime_paths,
            reason=ApprovalFailure(ApprovalFailureKind.USER_STOP).reason,
        )

    assert order == ["children", "source"]


@pytest.mark.asyncio
async def test_failure_reply_redacts_credentials_from_reason(tmp_path: Path) -> None:
    """The room-visible failure reply never shows credentials carried by a raw exception reason."""
    continuation = _continuation()
    api_key = "sk-" + "test" + "A1b2C3d4E5f6G7h8J9k0"
    password = "hunter" + "2secret"
    reason = f"Incorrect API key provided: {api_key} at https://user:{password}@mcp.internal/sse"
    write_note = AsyncMock(return_value=True)
    with (
        _settling(tmp_path, continuation, finish=AsyncMock(side_effect=[False, True])) as approvals,
        patch.object(DeliveryGateway, "write_approval_failure_note", new=write_note),
    ):
        assert await approvals._settle(continuation, ApprovalFailure(ApprovalFailureKind.ERROR, reason))

    visible = write_note.await_args.kwargs["text"]
    assert api_key not in visible
    assert password not in visible
    assert "Incorrect API key provided" in visible


@pytest.mark.parametrize(
    ("failure", "note"),
    [
        (ApprovalFailure(ApprovalFailureKind.USER_STOP), "cancelled"),
        (ApprovalFailure(ApprovalFailureKind.ERROR, "Paused run is no longer available"), "error"),
    ],
)
@pytest.mark.asyncio
async def test_failure_reply_is_marked_interrupted(tmp_path: Path, failure: ApprovalFailure, note: str) -> None:
    """Clients and later turns treat a stopped or failed continuation as interrupted, not as a finished reply."""
    continuation = _continuation()
    write_note = AsyncMock(return_value=True)
    with (
        _settling(tmp_path, continuation, finish=AsyncMock(side_effect=[False, True])) as approvals,
        patch.object(DeliveryGateway, "write_approval_failure_note", new=write_note),
    ):
        assert await approvals._settle(continuation, failure)

    assert write_note.await_args.kwargs["note"] == note


@pytest.mark.asyncio
async def test_unavailable_owner_cancels_children_before_discarding() -> None:
    """Startup cleanup must settle descendants even when their owning bot cannot start."""
    continuation = _continuation()
    order: list[str] = []

    async def cancel(_continuation: ApprovalContinuation, _reason: str) -> None:
        order.append("children")

    async def discard(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        order.append("source")
        return ()

    store = MagicMock(spec=PrincipalStore)
    store.approvals.approval_continuation = AsyncMock(return_value=continuation)
    store.load_matrix_delivery = AsyncMock(return_value=None)
    store.approvals.discard_unavailable_approval_continuation = AsyncMock(side_effect=discard)
    journal = MagicMock(spec=EventJournalStore)
    journal.principal.return_value = store
    notice_store = MagicMock(spec=PrincipalStore)
    notice_store.principal_id = "router@test"
    recovery = ApprovalRecovery(
        deliver_unavailable_notice=AsyncMock(return_value=notice_store),
        journal_provider=lambda: journal,
    )
    recovery.cancel_delegations = AsyncMock(side_effect=cancel)
    with patch("mindroom.approval.recovery.prepare_approval_failure", new=AsyncMock(return_value=continuation)):
        assert await recovery._discard_unavailable("leader@test", continuation, "owner removed")

    assert order == ["children", "source"]
    recovery.cancel_delegations.assert_awaited_once_with(continuation, "owner removed")
