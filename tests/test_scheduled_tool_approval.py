"""Approvals granted while scheduling one exact tool call and consumed once when it fires."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from mindroom.approval_manager import ApprovalManager
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalContinuation,
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    MatrixDelivery,
    approval_arguments_digest,
    scheduled_call_run_id,
)
from mindroom.response_sources import ResponseSources
from tests.conftest import test_runtime_paths
from tests.journal_membership_helpers import admit_room_membership

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.approval_manager import ApprovalActionResult

_ROOM = "!room:test"
_THREAD = "$thread"
_REQUESTER = "@human:test"
_AGENT = "code"
_TASK = "task-1"
_SCHEDULED_CARD = "$scheduled-approval:" + _TASK
_ARGUMENTS: dict[str, object] = {"channel": "U123", "text": "Good morning!"}


def _manager(journal: EventJournalStore, tmp_path: Path, sent: list[MatrixDelivery]) -> ApprovalManager:
    async def prepare(_room: str, _thread: str | None, content: dict) -> dict:
        return content

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery)
        return "$" + delivery.delivery_id + ("-edit" if delivery.stage is DeliveryStage.FINAL else "")

    return ApprovalManager(
        test_runtime_paths(tmp_path),
        cards=journal.principal("router@shared"),
        prepare_event=prepare,
        send_delivery=send,
        transport_sender=lambda: "@router:test",
    )


async def _schedule(
    manager: ApprovalManager,
    *,
    execute_at: datetime | None = None,
    workflow_digest: str = "workflow",
) -> bool:
    return await manager.request_scheduled_call_approval(
        task_id=_TASK,
        room_id=_ROOM,
        thread_id=_THREAD,
        requester_id=_REQUESTER,
        approver_user_id=_REQUESTER,
        agent_name=_AGENT,
        tool_name="post_slack_message",
        arguments=_ARGUMENTS,
        execute_at=execute_at or datetime.now(UTC) + timedelta(minutes=1),
        workflow_digest=workflow_digest,
        scheduled_for_text="9:00 AM EDT",
    )


async def _decide(manager: ApprovalManager, status: str) -> ApprovalActionResult:
    return await manager.handle_card_response(
        room_id=_ROOM,
        sender_id=_REQUESTER,
        card_event_id=_SCHEDULED_CARD,
        status=status,
        reason=None,
        authorize_responder=lambda _agent: True,
    )


async def _fire_time_call(
    journal: EventJournalStore,
    manager: ApprovalManager,
    name: str,
    *,
    arguments: dict[str, object] = _ARGUMENTS,
    thread: str = _THREAD,
    agent: str = _AGENT,
) -> ApprovalContinuation:
    """Pause one agent run on a gated call, the way the fire-time turn does."""
    responder = journal.principal("agent@" + agent)
    await responder.admit(
        InboundEvent(
            event_id="$source-" + name,
            room_id=_ROOM,
            thread_id=thread,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=_REQUESTER,
            origin_server_ts=1000,
            source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "send it"}},
        ),
    )
    continuation = ApprovalContinuation(
        approval_id=name,
        run_id="run-" + name,
        session_id="session-" + name,
        entity_kind="agent",
        entity_name=agent,
        room_id=_ROOM,
        thread_id=thread,
        requester_id=_REQUESTER,
        response_event_id="$waiting-" + name,
        sources=ResponseSources(("$source-" + name,), ("$source-" + name,)),
        calls=(
            ApprovalCall(
                tool_call_id="call-" + name,
                tool_name="post_slack_message",
                invoking_agent=agent,
                expires_at_ns=9_000_000_000_000_000_000,
                arguments_digest=approval_arguments_digest(arguments),
            ),
        ),
        state="waiting",
        runtime_generation="runtime",
    )
    assert await responder.create_approval_continuation(continuation) is not None
    card = await manager.prepare_detached_approval(
        approval_id="card-" + name,
        continuation_id=name,
        continuation_generation=0,
        entity_name=agent,
        response_event_id=continuation.response_event_id,
        tool_call_id="call-" + name,
        tool_name="post_slack_message",
        arguments=dict(arguments),
        room_id=_ROOM,
        requester_id=_REQUESTER,
        approver_user_id=_REQUESTER,
        expires_at_ns=9_000_000_000_000_000_000,
        agent_name=agent,
        thread_id=thread,
    )
    assert card is not None
    assert await manager.reserve_and_publish(
        continuation_principal_id=responder.principal_id,
        continuation_id=name,
        continuation_generation=0,
        cards=(card,),
    )
    stored = await responder.approval_continuation(name)
    assert stored is not None
    return stored


@pytest.mark.asyncio
async def test_scheduling_card_shows_the_exact_call_and_send_time(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """The requester reviews the real tool, its exact arguments, and when it will run."""
    journal = journal_database()
    sent: list[MatrixDelivery] = []
    manager = _manager(journal, tmp_path, sent)
    execute_at = datetime(2030, 1, 2, 14, 0, tzinfo=UTC)
    try:
        assert await _schedule(manager, execute_at=execute_at)
        [card] = sent
        assert card.stage is DeliveryStage.INITIAL
        assert card.thread_id == _THREAD
        assert card.payload["tool_name"] == "post_slack_message"
        assert card.payload["arguments"] == _ARGUMENTS
        assert card.payload["approval_target"] == "scheduled_call"
        assert card.payload["scheduled_task_id"] == _TASK
        assert card.payload["scheduled_for"] == "2030-01-02T14:00:00+00:00"
        assert card.payload["expires_at"] == "2030-01-02T14:00:00+00:00"
        assert card.payload["body"] == "🔒 Approval required: post_slack_message (scheduled for 9:00 AM EDT)"
        assert "auto_approve_options" not in card.payload
        stored = await journal.principal("router@shared").pending_approval_card(
            room_id=_ROOM,
            card_event_id=_SCHEDULED_CARD,
        )
        assert stored is not None
        assert stored.target_kind == "scheduled_call"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_armed_approval_runs_the_exact_call_once(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """The fire-time call publishes an approved receipt instead of waiting; a repeat waits again."""
    journal = journal_database()
    sent: list[MatrixDelivery] = []
    manager = _manager(journal, tmp_path, sent)
    try:
        assert await _schedule(manager)
        assert (await _decide(manager, "approved")).consumed is True
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "armed"
        sent.clear()

        first = await _fire_time_call(journal, manager, "first")

        assert first.state == "ready"
        assert first.calls[0].decision is not None
        assert first.calls[0].decision.value == "approved"
        [receipt] = sent
        assert receipt.stage is DeliveryStage.INITIAL
        assert receipt.payload["status"] == "approved"
        assert receipt.payload["approvable"] is False
        assert receipt.payload["resolved_by"] == _REQUESTER
        assert receipt.payload["arguments"] == _ARGUMENTS
        provenance = receipt.payload["approval_provenance"]
        assert provenance["kind"] == "scheduled_approval"
        assert provenance["task_id"] == _TASK
        assert provenance["approved_by"] == _REQUESTER
        assert provenance["approved_at"] is not None
        assert provenance["approval_card_event_id"] == _SCHEDULED_CARD
        assert provenance["arguments_digest"] == approval_arguments_digest(_ARGUMENTS)
        cards = journal.principal("router@shared")
        assert await cards.pending_approval_card(room_id=_ROOM, card_event_id="$card-first") is None
        assert await cards.is_terminal_approval_card(room_id=_ROOM, card_event_id="$card-first")

        repeat = await _fire_time_call(journal, manager, "repeat")

        assert repeat.calls[0].decision is None
        assert await cards.pending_approval_card(room_id=_ROOM, card_event_id="$card-repeat") is not None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_consumed_receipt_retires_into_a_terminal_tombstone(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Approval maintenance retires the delivered receipt but still recognizes actions on it."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        await _decide(manager, "approved")
        await manager.arm_scheduled_call_approval(_TASK, "workflow")
        await _fire_time_call(journal, manager, "first")
        cards = journal.principal("router@shared")

        await cards.maintain_approval_grants()

        remaining = await journal.backend.read(
            lambda transaction: transaction.fetchone(
                "SELECT 1 AS present FROM matrix_delivery_outbox WHERE delivery_id = ?",
                ("card-first",),
            ),
        )
        assert remaining is None
        assert await cards.is_terminal_approval_card(room_id=_ROOM, card_event_id="$card-first")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("armed", "call"),
    [
        (False, {}),
        (True, {"arguments": {"channel": "U123", "text": "Good morning!!"}}),
        (True, {"arguments": {"channel": "U999", "text": "Good morning!"}}),
        (True, {"thread": "$other-thread"}),
        (True, {"agent": "other"}),
    ],
    ids=["unarmed", "changed-text", "changed-destination", "other-thread", "other-agent"],
)
async def test_anything_but_the_armed_exact_call_still_waits_for_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    armed: bool,
    call: dict,
) -> None:
    """A call that differs from the approved one in any way gets today's pending card."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        await _decide(manager, "approved")
        if armed:
            assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "armed"

        continuation = await _fire_time_call(journal, manager, "mismatch", **call)

        assert continuation.calls[0].decision is None
        assert (
            await journal.principal("router@shared").pending_approval_card(
                room_id=_ROOM,
                card_event_id="$card-mismatch",
            )
            is not None
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_arming_requires_an_approved_unchanged_task_firing_on_time(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Only the approved, unchanged task within its window arms; a denial skips the send."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "none"
        assert await _schedule(manager)
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "unarmed"
        await _decide(manager, "approved")
        assert await manager.arm_scheduled_call_approval(_TASK, "edited-workflow") == "unarmed"
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "armed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_approval_far_from_the_send_time_does_not_arm(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A task firing outside the window around its approved time falls back to a card."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager, execute_at=datetime.now(UTC) + timedelta(minutes=16))
        await _decide(manager, "approved")
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "unarmed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_denied_card_reports_denied_unless_the_task_changed(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A denial cancels that exact send, while an edited task fires with today's approval."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        await _decide(manager, "denied")
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "denied"
        assert await manager.arm_scheduled_call_approval(_TASK, "edited-workflow") == "unarmed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cancelling_the_task_denies_its_pending_card(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """No live approval card outlives its cancelled schedule."""
    journal = journal_database()
    sent: list[MatrixDelivery] = []
    manager = _manager(journal, tmp_path, sent)
    try:
        assert await _schedule(manager)
        sent.clear()

        await manager.cancel_scheduled_call_approval(_TASK)

        cards = journal.principal("router@shared")
        decision = await cards.background_approval_decision(run_id=scheduled_call_run_id(_TASK), call_id=_TASK)
        assert decision is not None
        assert decision.status == "denied"
        assert decision.reason == "Schedule cancelled."
        assert [delivery.stage for delivery in sent] == [DeliveryStage.FINAL]
        assert sent[0].payload["status"] == "denied"
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "denied"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_new_room_tenure_does_not_inherit_the_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Leaving and rejoining the room voids an approval given under the old membership."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        await _decide(manager, "approved")
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "armed"
        cards = journal.principal("router@shared")
        await admit_room_membership(cards, _ROOM, "leave")
        await admit_room_membership(cards, _ROOM, "join")

        continuation = await _fire_time_call(journal, manager, "rejoined")

        assert continuation.calls[0].decision is None
    finally:
        await manager.shutdown()
