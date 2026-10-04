"""Approvals granted while scheduling one exact tool call and consumed once when it fires."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.approval_manager import ApprovalManager
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.scheduler import SchedulerTools
from mindroom.entity_resolution import entity_identity_registry
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
from mindroom.message_target import MessageTarget
from mindroom.response_sources import ResponseSources
from mindroom.scheduling import (
    ScheduledTaskRecord,
    ScheduledWorkflow,
    _parse_scheduled_task_record,
    _run_once_task,
    _scheduled_call_workflow_digest,
    cancel_scheduled_task,
    save_edited_scheduled_task,
)
from mindroom.scheduling_executor import ScheduledWorkflowOutcome
from mindroom.tool_approval import ToolApprovalTransportError
from mindroom.tool_approval_grants import ApprovalOperation
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_scheduling_runtime_from_tool_runtime_context,
    tool_runtime_context,
)
from tests.conftest import runtime_paths_for, test_runtime_paths
from tests.journal_membership_helpers import admit_room_membership
from tests.scheduling_helpers import joined_member_state
from tests.test_scheduler_tool import _bind_runtime_paths, _make_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

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
    member: str | None = None,
) -> ApprovalContinuation:
    """Pause one agent or team run on a gated call, the way the fire-time turn does."""
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
                invoking_agent=member or agent,
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
        agent_name=member or agent,
        thread_id=thread,
        grant_operation=ApprovalOperation("binding", "post_slack_message"),
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
        assert "approval_scope" not in receipt.payload
        assert "auto_approve_options" not in receipt.payload
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

        await manager.withdraw_scheduled_call_approval(_TASK, reason="Schedule cancelled.")

        cards = journal.principal("router@shared")
        decision = await cards.background_approval_decision(run_id=scheduled_call_run_id(_TASK), call_id=_TASK)
        assert decision is not None
        assert decision.status == "denied"
        assert decision.reason == "Schedule cancelled."
        assert [delivery.stage for delivery in sent] == [DeliveryStage.FINAL]
        assert sent[0].payload["status"] == "denied"
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "unarmed"
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


@pytest.mark.asyncio
async def test_team_member_call_consumes_the_team_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A team schedules as itself, while the member that makes the call is recorded on the paused run."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        await _decide(manager, "approved")
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "armed"

        continuation = await _fire_time_call(journal, manager, "team-call", member="writer")

        assert continuation.calls[0].invoking_agent == "writer"
        assert continuation.calls[0].decision is not None
        assert continuation.calls[0].decision.value == "approved"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cancelling_an_armed_task_revokes_its_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A task cancelled after it armed cannot lend its approval to a later identical call."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        await _decide(manager, "approved")
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "armed"

        await manager.withdraw_scheduled_call_approval(_TASK, reason="Schedule cancelled.")
        continuation = await _fire_time_call(journal, manager, "after-cancel")

        assert continuation.calls[0].decision is None
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "unarmed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_only_the_requesters_denial_skips_the_send(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A card MindRoom denies on its own, such as on room departure, falls back to approval at send time."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        await admit_room_membership(journal.principal("router@shared"), _ROOM, "leave")

        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "unarmed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_editing_the_task_withdraws_an_approval_given_for_the_old_one(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """After an edit, the old card stops being approvable and the edited task asks again when it runs."""
    journal = journal_database()
    sent: list[MatrixDelivery] = []
    manager = _manager(journal, tmp_path, sent)
    try:
        assert await _schedule(manager)
        sent.clear()

        await manager.withdraw_scheduled_call_approval(_TASK, reason="Schedule edited.")

        [edit] = sent
        assert edit.payload["status"] == "denied"
        assert edit.payload["resolution_reason"] == "Schedule edited."
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "unarmed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_approval_maintenance_prunes_old_bindings_after_their_receipts_retire(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention runs with periodic approval maintenance and outlives no delivery it still explains."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    monkeypatch.setattr(manager, "_ensure_deadline_sweep", lambda: None)
    cards = journal.principal("router@shared")

    async def binding_present() -> bool:
        row = await journal.backend.read(
            lambda transaction: transaction.fetchone(
                "SELECT 1 AS present FROM scheduled_call_approvals WHERE task_id = ?",
                (_TASK,),
            ),
        )
        return row is not None

    try:
        assert await _schedule(manager)
        await _decide(manager, "approved")
        await manager.arm_scheduled_call_approval(_TASK, "workflow")
        await _fire_time_call(journal, manager, "first")
        await journal.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE scheduled_call_approvals SET execute_at_ns = 0 WHERE task_id = ?",
                (_TASK,),
            ),
        )
        await journal.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE matrix_delivery_outbox SET acknowledged_event_id = NULL WHERE delivery_id = ?",
                ("card-first",),
            ),
        )

        await cards.maintain_approval_grants()
        assert await binding_present()

        await journal.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE matrix_delivery_outbox SET acknowledged_event_id = ? WHERE delivery_id = ?",
                ("$card-first", "card-first"),
            ),
        )
        await manager.recover_cards_on_startup()

        assert not await binding_present()
        assert await cards.is_terminal_approval_card(room_id=_ROOM, card_event_id="$card-first")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_receipt_names_the_account_that_approved_the_card(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """When the requester's alias maps to another account, the audit trail names the account that clicked."""
    journal = journal_database()
    sent: list[MatrixDelivery] = []
    manager = _manager(journal, tmp_path, sent)
    try:
        assert await manager.request_scheduled_call_approval(
            task_id=_TASK,
            room_id=_ROOM,
            thread_id=_THREAD,
            requester_id=_REQUESTER,
            approver_user_id="@canonical:test",
            agent_name=_AGENT,
            tool_name="post_slack_message",
            arguments=_ARGUMENTS,
            execute_at=datetime.now(UTC) + timedelta(minutes=1),
            workflow_digest="workflow",
            scheduled_for_text="9:00 AM EDT",
        )
        result = await manager.handle_card_response(
            room_id=_ROOM,
            sender_id="@canonical:test",
            card_event_id=_SCHEDULED_CARD,
            status="approved",
            reason=None,
            authorize_responder=lambda _agent: True,
        )
        assert result.consumed is True
        assert await manager.arm_scheduled_call_approval(_TASK, "workflow") == "armed"
        sent.clear()

        await _fire_time_call(journal, manager, "first")

        [receipt] = sent
        assert receipt.payload["resolved_by"] == "@canonical:test"
        assert receipt.payload["approval_provenance"]["approved_by"] == "@canonical:test"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_withdrawn_bindings_are_pruned_without_waiting_for_their_send_time(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A far-future call cancelled long ago does not keep its binding until the original send time."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    monkeypatch.setattr(manager, "_ensure_deadline_sweep", lambda: None)
    try:
        assert await _schedule(manager, execute_at=datetime.now(UTC) + timedelta(days=3650))
        await manager.withdraw_scheduled_call_approval(_TASK, reason="Schedule cancelled.")
        await journal.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE scheduled_call_approvals SET revoked_at_ns = 1 WHERE task_id = ?",
                (_TASK,),
            ),
        )

        await manager.recover_cards_on_startup()

        remaining = await journal.backend.read(
            lambda transaction: transaction.fetchone(
                "SELECT 1 AS present FROM scheduled_call_approvals WHERE task_id = ?",
                (_TASK,),
            ),
        )
        assert remaining is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_republished_scheduled_receipt_alias_stays_terminal(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A second physical copy of a scheduled-call receipt is recognized as already decided."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        await _decide(manager, "approved")
        await manager.arm_scheduled_call_approval(_TASK, "workflow")
        await _fire_time_call(journal, manager, "first")
        cards = journal.principal("router@shared")

        await cards.remember_terminal_approval_alias(
            room_id=_ROOM,
            card_event_id="$receipt-copy",
            delivery_id="card-first",
        )

        assert await cards.is_terminal_approval_card(room_id=_ROOM, card_event_id="$receipt-copy")
    finally:
        await manager.shutdown()


def _gated_config() -> Config:
    return _bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            tool_approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="post_slack_message", action="require_approval")],
            ),
        ),
    )


def _tool_context(config: Config, *, thread_id: str | None = "$thread") -> ToolRuntimeContext:
    context = _make_context(config)
    client = context.client
    client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$state"}, room_id="!room:localhost"),
    )
    return replace(
        context,
        target=MessageTarget.resolve(room_id="!room:localhost", thread_id=thread_id, reply_to_event_id=None),
    )


def _responders(config: Config, *entity_names: str) -> AbstractContextManager[object]:
    """Let only these agents or teams reply to the requester in the scheduling room."""
    registry = entity_identity_registry(config, runtime_paths_for(config))
    responder_ids = [registry.current_id(name) for name in entity_names]
    build_runtime = build_scheduling_runtime_from_tool_runtime_context
    return patch(
        "mindroom.custom_tools.scheduler.build_scheduling_runtime_from_tool_runtime_context",
        side_effect=lambda context: replace(
            build_runtime(context),
            responder_candidates_for_room=AsyncMock(return_value=responder_ids),
        ),
    )


def _persisted_workflows(context: ToolRuntimeContext) -> list[tuple[str, ScheduledTaskRecord]]:
    records = []
    for put in context.client.room_put_state.await_args_list:
        content = put.kwargs["content"]
        record = _parse_scheduled_task_record(put.kwargs["room_id"], put.kwargs["state_key"], content)
        assert record is not None
        records.append((str(content["status"]), record))
    return records


@pytest.mark.asyncio
async def test_schedule_tool_call_saves_the_exact_call_and_requests_its_card() -> None:
    """The saved task carries the exact call, and the card binds that task as it will fire."""
    config = _gated_config()
    context = _tool_context(config)
    request = AsyncMock(return_value=True)

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        patch("mindroom.scheduling._start_scheduled_task") as start,
        tool_runtime_context(context),
        _responders(context.config, "general"),
    ):
        result = await SchedulerTools().schedule_tool_call(
            tool_name="post_slack_message",
            arguments_json='{"text": "Good morning!", "channel": "U123"}',
            execute_at="2030-01-02T09:00:00-05:00",
            description="Morning DM",
        )

    [(status, record)] = _persisted_workflows(context)
    workflow = record.workflow
    assert status == "pending"
    assert workflow.schedule_type == "once"
    assert workflow.execute_at == datetime(2030, 1, 2, 14, 0, tzinfo=UTC)
    assert workflow.room_id == "!room:localhost"
    assert workflow.thread_id == "$thread"
    assert workflow.new_thread is False
    assert workflow.history_limit == 0
    assert workflow.created_by == "@user:localhost"
    assert workflow.description == "Morning DM"
    assert workflow.message.startswith("@general ")
    assert "`post_slack_message`" in workflow.message
    assert '"channel": "U123"' in workflow.message
    assert '"text": "Good morning!"' in workflow.message
    kwargs = request.await_args.kwargs
    assert kwargs["task_id"] == record.task_id
    assert kwargs["room_id"] == "!room:localhost"
    assert kwargs["thread_id"] == "$thread"
    assert kwargs["requester_id"] == "@user:localhost"
    assert kwargs["approver_user_id"] == "@user:localhost"
    assert kwargs["agent_name"] == "general"
    assert kwargs["tool_name"] == "post_slack_message"
    assert kwargs["arguments"] == {"channel": "U123", "text": "Good morning!"}
    assert kwargs["execute_at"] == workflow.execute_at
    assert kwargs["scheduled_for_text"] == "2030-01-02 14:00 UTC"
    assert kwargs["workflow_digest"] == _scheduled_call_workflow_digest(record.task_id, workflow)
    start.assert_called_once()
    assert record.task_id in result
    assert "approve" in result.lower()


@pytest.mark.asyncio
async def test_schedule_tool_call_requires_the_agent_to_be_able_to_reply_in_the_room() -> None:
    """An agent that cannot receive the trigger, such as a delegated child outside the room, cannot schedule it."""
    context = _tool_context(_gated_config())
    request = AsyncMock(return_value=True)

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        tool_runtime_context(context),
        _responders(context.config),
        pytest.raises(RuntimeError, match="cannot receive"),
    ):
        await SchedulerTools().schedule_tool_call(
            tool_name="post_slack_message",
            arguments_json='{"channel": "U123"}',
            execute_at="2030-01-02T09:00:00-05:00",
            description="Morning DM",
        )

    request.assert_not_awaited()
    context.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_tool_call_binds_the_arguments_the_call_will_run_with() -> None:
    """The card and digest show arguments as the model runtime decodes them, so the fire-time call can match."""
    context = _tool_context(_gated_config())
    request = AsyncMock(return_value=True)

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        patch("mindroom.scheduling._start_scheduled_task"),
        tool_runtime_context(context),
        _responders(context.config, "general"),
    ):
        await SchedulerTools().schedule_tool_call(
            tool_name="post_slack_message",
            arguments_json='{"text": "None", "unfurl": " TRUE ", "blocks": {"hidden": "false"}}',
            execute_at="2030-01-02T09:00:00-05:00",
            description="Morning DM",
        )

    assert request.await_args.kwargs["arguments"] == {"text": None, "unfurl": True, "blocks": {"hidden": "false"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments_json", "execute_at", "thread_id", "gated", "error"),
    [
        ("{", "2030-01-02T09:00:00-05:00", "$thread", True, "arguments_json must be a JSON object"),
        ("[1]", "2030-01-02T09:00:00-05:00", "$thread", True, "arguments_json must be a JSON object"),
        ("{}", "tomorrow at nine", "$thread", True, "ISO 8601"),
        ("{}", "2030-01-02T09:00:00", "$thread", True, "UTC offset"),
        ("{}", "2020-01-02T09:00:00+00:00", "$thread", True, "future"),
        ("{}", "2030-01-02T09:00:00-05:00", None, True, "thread"),
        ("{}", "2030-01-02T09:00:00-05:00", "$thread", False, "does not require approval"),
    ],
    ids=["invalid-json", "not-object", "not-iso", "naive", "past", "no-thread", "ungated"],
)
async def test_schedule_tool_call_rejects_what_it_cannot_bind(
    arguments_json: str,
    execute_at: str,
    thread_id: str | None,
    gated: bool,
    error: str,
) -> None:
    """Nothing is saved and no card is posted for a call that cannot be pre-approved exactly."""
    config = (
        _gated_config()
        if gated
        else _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))
    )
    context = _tool_context(config, thread_id=thread_id)
    request = AsyncMock(return_value=True)

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        tool_runtime_context(context),
        _responders(context.config, "general"),
        pytest.raises(RuntimeError, match=error),
    ):
        await SchedulerTools().schedule_tool_call(
            tool_name="post_slack_message",
            arguments_json=arguments_json,
            execute_at=execute_at,
            description="Morning DM",
        )

    context.client.room_put_state.assert_not_awaited()
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_outcome", "raised"),
    [
        (False, RuntimeError),
        (ToolApprovalTransportError("router missing"), ToolApprovalTransportError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
    ids=["refused", "raised", "interrupted"],
)
async def test_no_task_is_published_when_its_card_cannot_be_posted(
    request_outcome: bool | BaseException,
    raised: type[BaseException],
) -> None:
    """The task becomes visible only after its card exists, and any card left by a failed request is withdrawn."""
    context = _tool_context(_gated_config())
    request = (
        AsyncMock(side_effect=request_outcome)
        if isinstance(request_outcome, BaseException)
        else AsyncMock(return_value=request_outcome)
    )
    withdraw = AsyncMock()

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        patch("mindroom.scheduling.withdraw_scheduled_call_approval", new=withdraw),
        patch("mindroom.scheduling._start_scheduled_task") as start,
        tool_runtime_context(context),
        _responders(context.config, "general"),
        pytest.raises(raised),
    ):
        await SchedulerTools().schedule_tool_call(
            tool_name="post_slack_message",
            arguments_json='{"channel": "U123"}',
            execute_at="2030-01-02T09:00:00-05:00",
            description="Morning DM",
        )

    context.client.room_put_state.assert_not_awaited()
    withdraw.assert_awaited_once_with(request.await_args.kwargs["task_id"], reason="Schedule cancelled.")
    start.assert_not_called()


@pytest.mark.asyncio
async def test_card_is_withdrawn_when_its_task_cannot_be_published() -> None:
    """A card whose task never reached Matrix state cannot stay approvable."""
    context = _tool_context(_gated_config())
    context.client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    request = AsyncMock(return_value=True)
    withdraw = AsyncMock()

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        patch("mindroom.scheduling.withdraw_scheduled_call_approval", new=withdraw),
        patch("mindroom.scheduling._start_scheduled_task") as start,
        tool_runtime_context(context),
        _responders(context.config, "general"),
        pytest.raises(RuntimeError, match="Failed to schedule"),
    ):
        await SchedulerTools().schedule_tool_call(
            tool_name="post_slack_message",
            arguments_json='{"channel": "U123"}',
            execute_at="2030-01-02T09:00:00-05:00",
            description="Morning DM",
        )

    withdraw.assert_awaited_once_with(request.await_args.kwargs["task_id"], reason="Schedule cancelled.")
    start.assert_not_called()


def test_workflow_digest_survives_matrix_state_round_trip() -> None:
    """The digest read back from stored task state equals the one the approval was bound to."""
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2030, 1, 2, 9, 0, tzinfo=timezone(timedelta(hours=-5))).astimezone(UTC),
        message='@general call `post_slack_message`\n```json\n{"text": "Grüße 👋"}\n```',
        description="Morning DM",
        history_limit=0,
        created_by="@user:localhost",
        thread_id="$thread",
        room_id="!room:localhost",
    )
    stored = {"status": "pending", "workflow": workflow.model_dump_json(), "created_at": "2030-01-01T00:00:00+00:00"}

    record = _parse_scheduled_task_record("!room:localhost", "task1234", stored)

    assert record is not None
    assert _scheduled_call_workflow_digest("task1234", record.workflow) == _scheduled_call_workflow_digest(
        "task1234",
        workflow,
    )
    original = _scheduled_call_workflow_digest("task1234", workflow)
    for change in (
        {"message": workflow.message.replace("Grüße", "Hi")},
        {"execute_at": workflow.execute_at + timedelta(minutes=1)},
        {"thread_id": "$other"},
        {"created_by": "@other:localhost"},
    ):
        assert _scheduled_call_workflow_digest("task1234", workflow.model_copy(update=change)) != original
    # Fields that do not define the call, including ones a later release may add, leave the binding intact.
    assert (
        _scheduled_call_workflow_digest("task1234", workflow.model_copy(update={"description": "Renamed"})) == original
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_state", "fires", "final_status"),
    [
        ("armed", True, "completed"),
        ("unarmed", True, "completed"),
        ("none", True, "completed"),
        ("denied", False, "cancelled"),
    ],
)
async def test_firing_task_arms_its_approval_and_skips_a_denied_send(
    approval_state: str,
    fires: bool,
    final_status: str,
) -> None:
    """The runner arms the exact unchanged task; a send the requester denied never fires."""
    client = AsyncMock()
    client.room_get_state_event.side_effect = joined_member_state
    client.room_put_state = AsyncMock()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        message="@general call it",
        description="Morning DM",
        room_id="!test:server",
        thread_id="$thread123",
    )
    record = ScheduledTaskRecord(
        task_id="task1234",
        room_id="!test:server",
        status="pending",
        created_at=datetime.now(UTC),
        workflow=workflow,
    )
    arm = AsyncMock(return_value=approval_state)

    with (
        patch("mindroom.scheduling.get_scheduled_task", new=AsyncMock(side_effect=[record, record])),
        patch("mindroom.scheduling.arm_scheduled_call_approval", new=arm),
        patch(
            "mindroom.scheduling_executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute,
    ):
        await _run_once_task(
            client,
            "task1234",
            workflow,
            Config(),
            resolve_runtime_paths(config_path=Path("config.yaml"), process_env={}),
            AsyncMock(),
        )

    arm.assert_awaited_once_with("task1234", _scheduled_call_workflow_digest("task1234", workflow))
    assert execute.await_count == int(fires)
    assert client.room_put_state.await_args.kwargs["content"]["status"] == final_status


@pytest.mark.asyncio
async def test_failed_withdrawal_reports_a_failed_cancel_without_publishing_it() -> None:
    """Cancelling keeps its error result when the approval cannot be withdrawn, and publishes nothing."""
    client = AsyncMock()

    with (
        patch(
            "mindroom.scheduling._read_scheduled_task_state",
            new=AsyncMock(return_value={"status": "pending", "workflow": "{}"}),
        ),
        patch(
            "mindroom.scheduling.withdraw_scheduled_call_approval",
            new=AsyncMock(side_effect=RuntimeError("journal unavailable")),
        ),
    ):
        result = await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task1234",
            runtime_paths=resolve_runtime_paths(config_path=Path("config.yaml"), process_env={}),
        )

    assert result.startswith("❌ Failed to cancel task `task1234`: ")
    assert "journal unavailable" in result
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_withdrawal_rejects_an_edit_before_publishing_it() -> None:
    """An edit whose old approval cannot be withdrawn fails like other rejected edits."""
    client = AsyncMock()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(hours=1),
        message="Remind me",
        description="Reminder",
        room_id="!test:server",
    )
    existing = ScheduledTaskRecord(
        task_id="task1234",
        room_id="!test:server",
        status="pending",
        created_at=datetime.now(UTC),
        workflow=workflow,
    )

    with (
        patch("mindroom.scheduling.get_scheduled_task", new=AsyncMock(return_value=existing)),
        patch(
            "mindroom.scheduling.withdraw_scheduled_call_approval",
            new=AsyncMock(side_effect=RuntimeError("journal unavailable")),
        ),
        pytest.raises(ValueError, match="journal unavailable"),
    ):
        await save_edited_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task1234",
            workflow=workflow.model_copy(update={"message": "Remind me later"}),
            existing_task=existing,
            runtime_paths=resolve_runtime_paths(config_path=Path("config.yaml"), process_env={}),
            timezone="UTC",
        )

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_tasks_still_fire_when_scheduled_call_arming_fails() -> None:
    """An approval-journal error must not stop an ordinary reminder; an unarmed call still asks for approval."""
    client = AsyncMock()
    client.room_get_state_event.side_effect = joined_member_state
    client.room_put_state = AsyncMock()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        message="Remind me",
        description="Reminder",
        room_id="!test:server",
        thread_id="$thread123",
    )
    record = ScheduledTaskRecord(
        task_id="task1234",
        room_id="!test:server",
        status="pending",
        created_at=datetime.now(UTC),
        workflow=workflow,
    )

    with (
        patch("mindroom.scheduling.get_scheduled_task", new=AsyncMock(side_effect=[record, record])),
        patch("mindroom.scheduling.arm_scheduled_call_approval", new=AsyncMock(side_effect=RuntimeError("db down"))),
        patch(
            "mindroom.scheduling_executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute,
    ):
        await _run_once_task(
            client,
            "task1234",
            workflow,
            Config(),
            resolve_runtime_paths(config_path=Path("config.yaml"), process_env={}),
            AsyncMock(),
        )

    execute.assert_awaited_once()
    assert client.room_put_state.await_args.kwargs["content"]["status"] == "completed"


@pytest.mark.asyncio
async def test_cancelling_a_task_settles_its_scheduled_call_card() -> None:
    """Cancelling withdraws the approval before the cancellation is published, so a crash between them fails closed."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$state"}, room_id="!test:server"),
    )
    existing = {"status": "pending", "workflow": "{}"}
    order: list[str] = []
    cancel = AsyncMock(side_effect=lambda *_args, **_kwargs: order.append("withdraw"))
    client.room_put_state.side_effect = lambda **_kwargs: (
        order.append("state") or nio.RoomPutStateResponse.from_dict({"event_id": "$state"}, room_id="!test:server")
    )

    with (
        patch("mindroom.scheduling._read_scheduled_task_state", new=AsyncMock(return_value=existing)),
        patch("mindroom.scheduling.withdraw_scheduled_call_approval", new=cancel),
    ):
        result = await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task1234",
            runtime_paths=resolve_runtime_paths(config_path=Path("config.yaml"), process_env={}),
        )

    assert result == "✅ Cancelled task `task1234`"
    cancel.assert_awaited_once_with("task1234", reason="Schedule cancelled.")
    assert order == ["withdraw", "state"]
