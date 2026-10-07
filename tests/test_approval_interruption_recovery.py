"""Approval interruptions that settle in place with a visible interruption note."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from mindroom.approval_manager import initialize_approval_store
from mindroom.constants import STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.event_journal import ApprovalContinuation, DeliveryStage
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.response_sources import ResponseSources
from mindroom.runtime_shutdown import ENTITY_REMOVED_SHUTDOWN
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import unwrap_extracted_collaborator
from tests.legacy_reply_helpers import keep_main_paused_answer, resumed_main_left_approval, store_main_continuation
from tests.response_runner_helpers import _bot
from tests.test_response_runner_focused import _admit_approval_source

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.runtime_shutdown import RuntimeShutdownIntent


@pytest_asyncio.fixture
async def approval(tmp_path: Path) -> AsyncIterator[tuple[AgentBot, ApprovalContinuation]]:
    """Create real journal ownership, an acknowledged visible INITIAL, and this runtime's claim of an adopted approval."""
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
    # An approved run main left; this instance adopts its paused reply and stops while resuming it.
    await store_main_continuation(store, continuation)
    await keep_main_paused_answer(store, continuation.approval_id, text="partial answer")
    async with resumed_main_left_approval(bot, continuation) as claimed:
        assert claimed.state == "claimed"
    # The next instance takes the replies over and recovers the resume the stopped one left.
    restarted = _bot(tmp_path)
    initialize_approval_store(restarted.runtime_paths, cards=restarted.journal_principal())
    await restarted._reply_runtime.start()
    left = await restarted.journal_principal().approval_continuation(continuation.approval_id)
    assert left is not None
    assert left.state == "claimed"
    unique_room_send_responses(restarted.client)
    yield restarted, left


def _sent_bodies(bot: AgentBot) -> list[dict[str, object]]:
    return [
        call.kwargs["content"].get("m.new_content", call.kwargs["content"])
        for call in bot.client.room_send.await_args_list
    ]


async def _settle(bot: AgentBot, claimed: ApprovalContinuation) -> None:
    """Settle an active restart cancellation of the claimed approval."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await runner._settle_failed_approval_outcome(
        claimed,
        FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id="$waiting",
            is_visible_response=True,
            failure_reason="sync_restart_cancelled",
        ),
    )


async def _assert_settled_with_interruption_note(bot: AgentBot, claimed: ApprovalContinuation, shown: str) -> None:
    """The reply keeps what it showed, ends with the restart note, and nothing resumes it."""
    note = _sent_bodies(bot)[-1]
    assert str(note["body"]).startswith(shown)
    assert str(note["body"]).rstrip().endswith(RESTART_INTERRUPTED_RESPONSE_NOTE)
    assert note[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR
    store = bot.journal_principal()
    assert await store.approval_continuation(claimed.approval_id) is None
    assert not await store.is_pending("$source")


@pytest.mark.asyncio
async def test_approval_interruption_settles_in_place(approval: tuple[AgentBot, ApprovalContinuation]) -> None:
    """An active restart cancellation writes the interruption note below what the reply showed, and settles."""
    bot, claimed = approval
    await _settle(bot, claimed)
    await _assert_settled_with_interruption_note(bot, claimed, "partial answer")


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
    sends = bot.client.room_send.await_count
    await _settle(bot, failing)
    assert bot.client.room_send.await_count == sends


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
    await _settle(bot, claimed)
    await _assert_settled_with_interruption_note(bot, claimed, "partial answer")
