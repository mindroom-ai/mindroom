"""Approval interruptions that settle in place with a visible interruption note."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from mindroom.approval_manager import initialize_approval_store
from mindroom.constants import STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.delivery_gateway import DeliveryGateway, EditTextRequest
from mindroom.event_journal import ApprovalContinuation, DeliveryStage
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.response_sources import ResponseSources
from mindroom.runtime_shutdown import ENTITY_REMOVED_SHUTDOWN
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot
from tests.test_response_runner_focused import _admit_approval_source

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.runtime_shutdown import RuntimeShutdownIntent


@pytest_asyncio.fixture
async def approval(tmp_path: Path) -> tuple[AgentBot, ApprovalContinuation]:
    """Create real journal ownership, an acknowledged visible INITIAL, and a claim by the current runtime."""
    bot = _bot(tmp_path)
    initialize_approval_store(bot.runtime_paths, cards=bot.journal_principal())
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "Waiting"},
    )
    await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.INITIAL)
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        event_id="$waiting",
        delivered_projections=(),
    )
    continuation = ApprovalContinuation(
        approval_id="approval-recovery",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        sources=ResponseSources(("$source",), ("$source",)),
        calls=(),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    claimed = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    return bot, claimed


async def _acknowledge(bot: AgentBot, request: EditTextRequest) -> bool:
    """Persist an actual FINAL ACK at the mocked Matrix transport seam."""
    store = bot.journal_principal()
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        edits_event_id="$waiting",
        payload={"body": "* " + request.new_text, "m.new_content": {"body": request.new_text, **request.extra_content}},
    )
    await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$final-edit",
        delivered_projections=(),
    )
    return True


async def _settle(
    bot: AgentBot,
    claimed: ApprovalContinuation,
    edit: AsyncMock,
    *,
    body: str | None = "partial answer",
) -> None:
    """Settle an active restart cancellation with Matrix transport and body reads replaced."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with (
        patch.object(DeliveryGateway, "edit_text", new=edit),
        patch("mindroom.response_runner.fetch_latest_visible_body", new=AsyncMock(return_value=body)),
    ):
        await runner._settle_failed_approval_outcome(
            claimed,
            FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id="$waiting",
                is_visible_response=True,
                failure_reason="sync_restart_cancelled",
            ),
        )


async def _assert_settled_with_interruption_note(bot: AgentBot, claimed: ApprovalContinuation, edit: AsyncMock) -> None:
    """The reply keeps its partial text, ends with the restart note, and nothing resumes it."""
    edit.assert_awaited_once()
    request = edit.await_args.args[0]
    assert request.event_id == "$waiting"
    assert request.new_text.startswith("partial answer")
    assert request.new_text.rstrip().endswith(RESTART_INTERRUPTED_RESPONSE_NOTE)
    assert request.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_ERROR}
    store = bot.journal_principal()
    assert await store.approval_continuation(claimed.approval_id) is None
    assert not await store.is_pending("$source")
    assert not await store.recovery_initial_deliveries()
    async with bot.response_recovery_scope("!room:localhost", "$waiting") as permitted:
        assert not permitted


@pytest.mark.asyncio
async def test_approval_interruption_settles_in_place(approval: tuple[AgentBot, ApprovalContinuation]) -> None:
    """An active restart cancellation edits its reply with the interruption note and settles."""
    bot, claimed = approval

    async def edit_notice(request: EditTextRequest) -> bool:
        return await _acknowledge(bot, request)

    edit = AsyncMock(side_effect=edit_notice)
    await _settle(bot, claimed, edit)
    await _assert_settled_with_interruption_note(bot, claimed, edit)


@pytest.mark.asyncio
async def test_unreadable_reply_body_leaves_approval_failing(approval: tuple[AgentBot, ApprovalContinuation]) -> None:
    """Without a committed visible body there is no interruption note to write, so the claim stays fenced."""
    bot, claimed = approval
    edit = AsyncMock()
    await _settle(bot, claimed, edit, body=None)
    edit.assert_not_awaited()
    current = await bot.journal_principal().approval_continuation(claimed.approval_id)
    assert current is not None
    assert current.state == "failing"


@pytest.mark.asyncio
async def test_missing_approval_owner_does_not_edit_the_reply(
    approval: tuple[AgentBot, ApprovalContinuation],
) -> None:
    """Successful no-op retirement is not evidence of a visible interruption."""
    bot, claimed = approval
    runner = unwrap_extracted_collaborator(bot._response_runner)
    failing = await runner._approval_responses.request_failure(claimed, "sync_restart_cancelled")
    assert failing is not None
    await bot._journal_store.backend.write(lambda tx: tx.execute("DELETE FROM approval_continuations"))
    edit = AsyncMock()
    await _settle(bot, failing, edit)
    edit.assert_not_awaited()


async def _fence_bot(bot: AgentBot, intent: RuntimeShutdownIntent) -> None:
    """Enter the real bot shutdown boundary before unrelated response drains."""
    with (
        patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock(side_effect=RuntimeError("drain boundary"))),
        pytest.raises(RuntimeError, match="drain boundary"),
    ):
        await bot.prepare_for_sync_shutdown(shutdown_intent=intent)


@pytest.mark.asyncio
async def test_approval_settlement_after_removal_still_settles_in_place(
    approval: tuple[AgentBot, ApprovalContinuation],
) -> None:
    """A removed lifecycle still edits its reply with the interruption note and settles."""
    bot, claimed = approval
    await _fence_bot(bot, ENTITY_REMOVED_SHUTDOWN)

    async def edit_notice(request: EditTextRequest) -> bool:
        return await _acknowledge(bot, request)

    edit = AsyncMock(side_effect=edit_notice)
    await _settle(bot, claimed, edit)
    await _assert_settled_with_interruption_note(bot, claimed, edit)
