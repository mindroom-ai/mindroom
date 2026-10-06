"""Approval failure settles delegated work before releasing its sources and shows only redacted reasons."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.approval_recovery import ApprovalRecovery
from mindroom.approval_response import ApprovalResponseCoordinator
from mindroom.config.main import Config
from mindroom.constants import STREAM_STATUS_CANCELLED, STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.event_journal import ApprovalContinuation, EventJournalStore, PrincipalStore
from mindroom.response_sources import ResponseSources
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _continuation() -> ApprovalContinuation:
    return ApprovalContinuation(
        approval_id="approval-1",
        run_id="parent-run",
        session_id="parent-session",
        entity_kind="agent",
        entity_name="leader",
        room_id="!room:test",
        thread_id="$thread",
        requester_id="@human:test",
        response_event_id="$response",
        sources=ResponseSources(("$source",), ("$source",)),
        calls=(),
        state="failing",
        failure_reason="cancelled_by_user",
    )


@pytest.mark.asyncio
async def test_source_failure_cancels_children_before_finishing(tmp_path: Path) -> None:
    """A stopped approval cannot leave paused child records after its source is released."""
    continuation = _continuation()
    config = Config()
    paths = test_runtime_paths(tmp_path)
    order: list[str] = []

    async def cancel(*_args: object, **_kwargs: object) -> None:
        order.append("children")

    async def finish(_approval_id: str) -> bool:
        order.append("source")
        return True

    store = MagicMock(spec=PrincipalStore)
    store.approval_continuation = AsyncMock(return_value=continuation)
    store.finish_approval_continuation = AsyncMock(side_effect=finish)
    coordinator = ApprovalResponseCoordinator(
        config=lambda: config,
        runtime_paths=paths,
        store=store,
        delivery_gateway=MagicMock(spec=DeliveryGateway),
        retry_sources=lambda _room, _sources: None,
    )
    with (
        patch.object(coordinator, "successful_final_delivery", new=AsyncMock(return_value=None)),
        patch("mindroom.approval_response.prepare_approval_failure", new=AsyncMock(return_value=continuation)),
        patch(
            "mindroom.approval_response.cancel_approval_delegations",
            new=AsyncMock(side_effect=cancel),
        ) as cancel_children,
    ):
        assert await coordinator.settle_failure(continuation, "cancelled_by_user")

    assert order == ["children", "source"]
    cancel_children.assert_awaited_once_with(
        continuation,
        config=config,
        runtime_paths=paths,
        reason="cancelled_by_user",
    )


@pytest.mark.asyncio
async def test_failure_reply_redacts_credentials_from_reason(tmp_path: Path) -> None:
    """The room-visible failure reply never shows credentials carried by a raw exception reason."""
    continuation = _continuation()
    api_key = "sk-" + "test" + "A1b2C3d4E5f6G7h8J9k0"
    password = "hunter" + "2secret"
    reason = f"Incorrect API key provided: {api_key} at https://user:{password}@mcp.internal/sse"
    store = MagicMock(spec=PrincipalStore)
    store.approval_continuation = AsyncMock(return_value=continuation)
    store.finish_approval_continuation = AsyncMock(side_effect=[False, True])
    gateway = MagicMock(spec=DeliveryGateway)
    gateway.edit_text = AsyncMock(return_value=True)
    # A continuation older than reply records: main's failure edit shows the note.
    gateway.write_approval_failure_note = AsyncMock(return_value=None)
    coordinator = ApprovalResponseCoordinator(
        config=Config,
        runtime_paths=test_runtime_paths(tmp_path),
        store=store,
        delivery_gateway=gateway,
        retry_sources=lambda _room, _sources: None,
    )
    with (
        patch.object(coordinator, "successful_final_delivery", new=AsyncMock(return_value=None)),
        patch("mindroom.approval_response.prepare_approval_failure", new=AsyncMock(return_value=continuation)),
        patch("mindroom.approval_response.cancel_approval_delegations", new=AsyncMock()),
    ):
        assert await coordinator.settle_failure(continuation, reason)

    visible = gateway.edit_text.await_args.args[0].new_text
    assert api_key not in visible
    assert password not in visible
    assert "Incorrect API key provided" in visible


@pytest.mark.parametrize(
    ("reason", "visible_text", "stream_status"),
    [
        ("cancelled_by_user", "**[Response cancelled by user]**", STREAM_STATUS_CANCELLED),
        ("Paused run is no longer available", "Paused run is no longer available", STREAM_STATUS_ERROR),
    ],
)
@pytest.mark.asyncio
async def test_failure_reply_is_marked_interrupted(
    tmp_path: Path,
    reason: str,
    visible_text: str,
    stream_status: str,
) -> None:
    """Clients and later turns treat a stopped or failed continuation as interrupted, not as a finished reply."""
    continuation = _continuation()
    store = MagicMock(spec=PrincipalStore)
    store.approval_continuation = AsyncMock(return_value=continuation)
    store.finish_approval_continuation = AsyncMock(side_effect=[False, True])
    gateway = MagicMock(spec=DeliveryGateway)
    gateway.edit_text = AsyncMock(return_value=True)
    # A continuation older than reply records: main's failure edit shows the note.
    gateway.write_approval_failure_note = AsyncMock(return_value=None)
    coordinator = ApprovalResponseCoordinator(
        config=Config,
        runtime_paths=test_runtime_paths(tmp_path),
        store=store,
        delivery_gateway=gateway,
        retry_sources=lambda _room, _sources: None,
    )
    with (
        patch.object(coordinator, "successful_final_delivery", new=AsyncMock(return_value=None)),
        patch("mindroom.approval_response.prepare_approval_failure", new=AsyncMock(return_value=continuation)),
        patch("mindroom.approval_response.cancel_approval_delegations", new=AsyncMock()),
    ):
        assert await coordinator.settle_failure(continuation, reason)

    request = gateway.edit_text.await_args.args[0]
    assert request.new_text == visible_text
    assert request.extra_content == {STREAM_STATUS_KEY: stream_status}


@pytest.mark.asyncio
async def test_unavailable_owner_cancels_children_before_discarding() -> None:
    """Startup cleanup must settle descendants even when their owning bot cannot start."""
    continuation = _continuation()
    order: list[str] = []

    async def cancel(_continuation: ApprovalContinuation, _reason: str) -> None:
        order.append("children")

    async def discard(*_args: object, **_kwargs: object) -> bool:
        order.append("source")
        return True

    store = MagicMock(spec=PrincipalStore)
    store.approval_continuation = AsyncMock(return_value=continuation)
    store.load_matrix_delivery = AsyncMock(return_value=None)
    store.discard_unavailable_approval_continuation = AsyncMock(side_effect=discard)
    journal = MagicMock(spec=EventJournalStore)
    journal.principal.return_value = store
    notice_store = MagicMock(spec=PrincipalStore)
    notice_store.principal_id = "router@test"
    recovery = ApprovalRecovery(
        deliver_unavailable_notice=AsyncMock(return_value=notice_store),
        journal_provider=lambda: journal,
    )
    recovery.cancel_delegations = AsyncMock(side_effect=cancel)
    with patch("mindroom.approval_recovery.prepare_approval_failure", new=AsyncMock(return_value=continuation)):
        assert await recovery._discard_unavailable("leader@test", continuation, "owner removed")

    assert order == ["children", "source"]
    recovery.cancel_delegations.assert_awaited_once_with(continuation, "owner removed")
