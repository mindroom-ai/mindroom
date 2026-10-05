"""Approvals granted while scheduling one tool call, spent once when its agent runs the stored call."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.run import RunContext
from agno.tools import Toolkit

from mindroom.agents import apply_tool_approval_capability
from mindroom.approval_inbound import parse_approval_response_event
from mindroom.approval_manager import ApprovalManager
from mindroom.config.agent import AgentConfig, TeamConfig
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
    ScheduledCallClaim,
    approval_arguments_digest,
    scheduled_call_run_id,
)
from mindroom.mcp.config import MCPServerConfig
from mindroom.message_target import MessageTarget
from mindroom.response_sources import ResponseSources
from mindroom.scheduled_tool_calls import canonical_arguments
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
from mindroom.tool_approval import ToolApprovalTransportError, scheduled_call_offers_any_arguments
from mindroom.tool_approval_grants import ScheduledCallBinding
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
    from mindroom.event_journal import ScheduledCallRefusal

_ROOM = "!room:test"
_THREAD = "$thread"
_REQUESTER = "@human:test"
_AGENT = "code"
_TOOLKIT = "slack"
_TASK = "task-1"
_SCHEDULED_CARD = "$scheduled-approval:" + _TASK
_RECEIPT = "scheduled-receipt:" + _TASK
_ARGUMENTS: dict[str, object] = {"channel": "U123", "text": "Good morning!"}
_OTHER_ARGUMENTS: dict[str, object] = {"channel": "U999", "text": "Other"}


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


def _binding(*, execute_at: datetime | None = None, workflow_digest: str = "workflow") -> ScheduledCallBinding:
    return ScheduledCallBinding(
        task_id=_TASK,
        room_id=_ROOM,
        thread_id=_THREAD,
        requester_id=_REQUESTER,
        agent_name=_AGENT,
        toolkit_name=_TOOLKIT,
        tool_name="post_slack_message",
        arguments_json=canonical_arguments(_ARGUMENTS),
        workflow_digest=workflow_digest,
        execute_at_ns=int((execute_at or datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1_000_000_000),
    )


async def _schedule(
    manager: ApprovalManager,
    *,
    execute_at: datetime | None = None,
    workflow_digest: str = "workflow",
    any_arguments_offered: bool = False,
    approver: str = _REQUESTER,
) -> bool:
    return await manager.request_scheduled_call_approval(
        _binding(execute_at=execute_at, workflow_digest=workflow_digest),
        approver_user_id=approver,
        scheduled_for_text="9:00 AM EDT",
        any_arguments_offered=any_arguments_offered,
    )


async def _arm(
    manager: ApprovalManager,
    workflow_digest: str = "workflow",
    *,
    any_arguments_allowed: bool = True,
) -> str:
    return await manager.arm_scheduled_call_approval(
        _TASK,
        workflow_digest,
        any_arguments_allowed=any_arguments_allowed,
    )


async def _decide(
    manager: ApprovalManager,
    status: str,
    *,
    scheduled_scope: str | None = None,
    sender: str = _REQUESTER,
) -> ApprovalActionResult:
    return await manager.handle_card_response(
        room_id=_ROOM,
        sender_id=sender,
        card_event_id=_SCHEDULED_CARD,
        status=status,
        reason=None,
        authorize_responder=lambda _agent: True,
        scheduled_scope=scheduled_scope,
    )


async def _claim(
    manager: ApprovalManager,
    *,
    arguments: dict[str, object] | None = None,
    **claimant: str,
) -> ScheduledCallClaim | ScheduledCallRefusal | None:
    """Claim the stored call as its agent would, optionally from elsewhere or with other arguments."""
    call = await manager.scheduled_call(_TASK)
    assert call is not None
    arguments_json = call.arguments_json if arguments is None else canonical_arguments(arguments)
    return await manager.claim_scheduled_call(
        replace(call, **claimant),
        arguments_json=arguments_json,
        approver_user_id=_REQUESTER,
    )


async def _armed(manager: ApprovalManager, *, scope: str | None = None) -> None:
    assert await _schedule(manager, any_arguments_offered=scope is not None)
    assert (await _decide(manager, "approved", scheduled_scope=scope)).consumed is True
    assert await _arm(manager) == "armed"


async def _ordinary_card(journal: EventJournalStore, manager: ApprovalManager, name: str) -> ApprovalContinuation:
    """Pause one agent run on a gated call made directly, which asks for approval the ordinary way."""
    responder = journal.principal("agent@" + _AGENT)
    await responder.admit(
        InboundEvent(
            event_id="$source-" + name,
            room_id=_ROOM,
            thread_id=_THREAD,
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
        entity_name=_AGENT,
        room_id=_ROOM,
        thread_id=_THREAD,
        requester_id=_REQUESTER,
        response_event_id="$waiting-" + name,
        sources=ResponseSources(("$source-" + name,), ("$source-" + name,)),
        calls=(
            ApprovalCall(
                tool_call_id="call-" + name,
                tool_name="post_slack_message",
                invoking_agent=_AGENT,
                expires_at_ns=9_000_000_000_000_000_000,
                arguments_digest=approval_arguments_digest(_ARGUMENTS),
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
        entity_name=_AGENT,
        response_event_id=continuation.response_event_id,
        tool_call_id="call-" + name,
        tool_name="post_slack_message",
        arguments=dict(_ARGUMENTS),
        room_id=_ROOM,
        requester_id=_REQUESTER,
        approver_user_id=_REQUESTER,
        expires_at_ns=9_000_000_000_000_000_000,
        agent_name=_AGENT,
        thread_id=_THREAD,
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


async def _binding_column(journal: EventJournalStore, column: str) -> object:
    row = await journal.backend.read(
        lambda transaction: transaction.fetchone(
            f"SELECT {column} AS value FROM scheduled_call_approvals WHERE task_id = ?",  # noqa: S608 - test column
            (_TASK,),
        ),
    )
    return None if row is None else row["value"]


@pytest.mark.asyncio
async def test_scheduling_card_shows_the_stored_call_and_send_time(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """The requester reviews the real tool, its arguments, and when it will run."""
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
        call = await manager.scheduled_call(_TASK)
        assert call is not None
        assert call.toolkit_name == _TOOLKIT
        assert call.arguments_json == canonical_arguments(_ARGUMENTS)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_armed_approval_is_spent_once_with_its_receipt(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Claiming the stored call publishes an approved receipt; a second claim is refused."""
    journal = journal_database()
    sent: list[MatrixDelivery] = []
    manager = _manager(journal, tmp_path, sent)
    try:
        await _armed(manager)
        sent.clear()

        claimed = await _claim(manager)

        assert isinstance(claimed, ScheduledCallClaim)
        assert claimed.tool_name == "post_slack_message"
        assert claimed.toolkit_name == _TOOLKIT
        assert claimed.arguments_json == canonical_arguments(_ARGUMENTS)
        [receipt] = sent
        assert receipt.delivery_id == _RECEIPT
        assert receipt.stage is DeliveryStage.INITIAL
        assert receipt.thread_id == _THREAD
        assert receipt.payload["status"] == "approved"
        assert receipt.payload["approvable"] is False
        assert receipt.payload["resolved_by"] == _REQUESTER
        assert receipt.payload["arguments"] == _ARGUMENTS
        assert receipt.payload["scheduled_task_id"] == _TASK
        provenance = receipt.payload["approval_provenance"]
        assert provenance == claimed.provenance
        assert provenance["kind"] == "scheduled_approval"
        assert provenance["task_id"] == _TASK
        assert provenance["approved_by"] == _REQUESTER
        assert provenance["approved_at"] is not None
        assert provenance["approval_card_event_id"] == _SCHEDULED_CARD
        assert provenance["scope"] == "exact_arguments"
        assert provenance["arguments_digest"] == approval_arguments_digest(_ARGUMENTS)
        cards = journal.principal("router@shared")
        assert await cards.is_terminal_approval_card(room_id=_ROOM, card_event_id="$" + _RECEIPT)

        assert await _claim(manager) == "used"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_concurrent_claims_spend_the_approval_once(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Two claims racing for the same task run the call at most once."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        await _armed(manager)

        results = await asyncio.gather(_claim(manager), _claim(manager))

        assert sorted(isinstance(result, ScheduledCallClaim) for result in results) == [False, True]
        assert "used" in results
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_outcome_is_recorded_once_and_unknown_until_then(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A claimed call has no outcome until it returns, and its first recorded outcome stands."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        await _armed(manager)
        await manager.record_scheduled_call_outcome(_TASK, "completed")
        assert await _binding_column(journal, "outcome") is None

        assert isinstance(await _claim(manager), ScheduledCallClaim)
        assert await _binding_column(journal, "outcome") is None
        await manager.record_scheduled_call_outcome(_TASK, "failed")
        await manager.record_scheduled_call_outcome(_TASK, "completed")

        assert await _binding_column(journal, "outcome") == "failed"
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
        await _armed(manager)
        assert isinstance(await _claim(manager), ScheduledCallClaim)
        cards = journal.principal("router@shared")

        await cards.maintain_automatic_approvals()

        remaining = await journal.backend.read(
            lambda transaction: transaction.fetchone(
                "SELECT 1 AS present FROM matrix_delivery_outbox WHERE delivery_id = ?",
                (_RECEIPT,),
            ),
        )
        assert remaining is None
        assert await cards.is_terminal_approval_card(room_id=_ROOM, card_event_id="$" + _RECEIPT)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("armed", "claim", "refusal"),
    [
        (False, {}, "not_armed"),
        (True, {"arguments": {"channel": "U123", "text": "Good morning!!"}}, "arguments"),
        (True, {"arguments": _OTHER_ARGUMENTS}, "arguments"),
        (True, {"thread_id": "$other-thread"}, "elsewhere"),
        (True, {"room_id": "!other:test"}, "elsewhere"),
        (True, {"requester_id": "@other:test"}, "elsewhere"),
        (True, {"agent_name": "other"}, "elsewhere"),
        (True, {"toolkit_name": "other_slack"}, "elsewhere"),
        (True, {"tool_name": "delete_slack_message"}, "elsewhere"),
    ],
    ids=[
        "unarmed",
        "changed-text",
        "changed-destination",
        "other-thread",
        "other-room",
        "other-requester",
        "other-agent",
        "other-toolkit",
        "other-tool",
    ],
)
async def test_anything_but_the_armed_stored_call_is_refused(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    armed: bool,
    claim: dict,
    refusal: str,
) -> None:
    """An exact approval spends only for its own agent, conversation, and stored arguments."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        await _decide(manager, "approved")
        if armed:
            assert await _arm(manager) == "armed"

        assert await _claim(manager, **claim) == refusal
        assert isinstance(await _claim(manager), ScheduledCallClaim) is armed
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "refusal"),
    [(None, "not_approved"), ("denied", "not_approved")],
    ids=["pending", "denied"],
)
async def test_unapproved_card_is_refused(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    decision: str | None,
    refusal: str,
) -> None:
    """Without the requester's approval nothing is spent."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        if decision is not None:
            await _decide(manager, decision)

        assert await _claim(manager) == refusal
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_claim_long_after_the_send_time_is_refused(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """An approval armed in time but claimed outside its window is not spent."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        await _armed(manager)
        await journal.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE scheduled_call_approvals SET execute_at_ns = 0 WHERE task_id = ?",
                (_TASK,),
            ),
        )

        call = await manager.scheduled_call(_TASK)
        assert call is not None
        result = await manager.claim_scheduled_call(
            replace(call, execute_at_ns=0),
            arguments_json=call.arguments_json,
            approver_user_id=_REQUESTER,
        )
        assert result == "late"
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
        assert await _arm(manager) == "none"
        assert await _schedule(manager)
        assert await _arm(manager) == "unarmed"
        await _decide(manager, "approved")
        assert await _arm(manager, "edited-workflow") == "unarmed"
        assert await _arm(manager) == "armed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_approval_far_from_the_send_time_does_not_arm(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A task firing outside the window around its approved time does not arm."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager, execute_at=datetime.now(UTC) + timedelta(minutes=16))
        await _decide(manager, "approved")
        assert await _arm(manager) == "unarmed"
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
        assert await _arm(manager) == "denied"
        assert await _arm(manager, "edited-workflow") == "unarmed"
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
        assert await _arm(manager) == "unarmed"
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
        await _armed(manager)
        cards = journal.principal("router@shared")
        await admit_room_membership(cards, _ROOM, "leave")
        await admit_room_membership(cards, _ROOM, "join")

        assert await _claim(manager) == "left_room"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_direct_call_to_the_tool_still_asks_for_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """An armed scheduled approval approves nothing but its own claim; calling the tool directly asks as usual."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        await _armed(manager)

        direct = await _ordinary_card(journal, manager, "direct")

        assert direct.calls[0].decision is None
        assert (
            await journal.principal("router@shared").pending_approval_card(room_id=_ROOM, card_event_id="$card-direct")
            is not None
        )
        assert isinstance(await _claim(manager), ScheduledCallClaim)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("offered", [True, False])
async def test_scheduling_card_offers_any_arguments_only_when_asked(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    offered: bool,
) -> None:
    """Cards always state the send window and offer the broader scope only when it is allowed."""
    journal = journal_database()
    sent: list[MatrixDelivery] = []
    manager = _manager(journal, tmp_path, sent)
    try:
        assert await _schedule(manager, any_arguments_offered=offered)
        [card] = sent
        assert card.payload["scheduled_window_seconds"] == 900
        assert card.payload.get("scheduled_scope_options") == (
            ["exact_arguments", "any_arguments"] if offered else None
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_any_arguments_approval_runs_one_call_with_different_arguments(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """The broader scope accepts other arguments for the same stored tool, still only once."""
    journal = journal_database()
    sent: list[MatrixDelivery] = []
    manager = _manager(journal, tmp_path, sent)
    try:
        await _armed(manager, scope="any_arguments")
        decision = next(delivery for delivery in sent if delivery.stage is DeliveryStage.FINAL)
        assert decision.payload["scheduled_scope"] == "any_arguments"
        sent.clear()

        claimed = await _claim(manager, arguments=_OTHER_ARGUMENTS)

        assert isinstance(claimed, ScheduledCallClaim)
        assert claimed.arguments_json == canonical_arguments(_OTHER_ARGUMENTS)
        [receipt] = sent
        assert receipt.payload["arguments"] == _OTHER_ARGUMENTS
        assert receipt.payload["approval_provenance"]["scope"] == "any_arguments"
        assert receipt.payload["approval_provenance"]["arguments_digest"] == approval_arguments_digest(
            _OTHER_ARGUMENTS,
        )
        assert await _claim(manager) == "used"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_exact_approval_is_recorded_as_exact_scope(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """An approval without a scope, such as a reaction, covers only the stored arguments."""
    journal = journal_database()
    sent: list[MatrixDelivery] = []
    manager = _manager(journal, tmp_path, sent)
    try:
        assert await _schedule(manager, any_arguments_offered=True)
        await _decide(manager, "approved")
        decision = next(delivery for delivery in sent if delivery.stage is DeliveryStage.FINAL)
        assert decision.payload["scheduled_scope"] == "exact_arguments"
        await _arm(manager)

        assert await _claim(manager, arguments=_OTHER_ARGUMENTS) == "arguments"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_disabling_any_arguments_narrows_an_existing_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Turning the broader scope off applies to approvals already given, which then run the stored call."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager, any_arguments_offered=True)
        await _decide(manager, "approved", scheduled_scope="any_arguments")
        assert await _arm(manager, any_arguments_allowed=False) == "armed"

        assert await _claim(manager, arguments=_OTHER_ARGUMENTS) == "arguments"
        assert isinstance(await _claim(manager), ScheduledCallClaim)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offered", "status", "scope"),
    [
        (False, "approved", "any_arguments"),
        (True, "denied", "any_arguments"),
        (True, "approved", "everything"),
    ],
    ids=["not-offered", "with-denial", "unknown-scope"],
)
async def test_unoffered_or_malformed_scope_is_ignored(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    offered: bool,
    status: str,
    scope: str,
) -> None:
    """A scope the card did not offer, or one sent with a denial, does not decide the card."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager, any_arguments_offered=offered)

        result = await _decide(manager, status, scheduled_scope=scope)

        assert result.consumed is False
        assert await _arm(manager) == "unarmed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_scope_is_ignored_on_an_ordinary_approval_card(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Only scheduling-time cards accept a scheduled scope."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        await _ordinary_card(journal, manager, "plain")

        result = await manager.handle_card_response(
            room_id=_ROOM,
            sender_id=_REQUESTER,
            card_event_id="$card-plain",
            status="approved",
            reason=None,
            authorize_responder=lambda _agent: True,
            scheduled_scope="any_arguments",
        )

        assert result.consumed is False
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cancelling_an_armed_task_revokes_its_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A task cancelled after it armed cannot spend its approval."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        await _armed(manager)

        await manager.withdraw_scheduled_call_approval(_TASK, reason="Schedule cancelled.")

        assert await _claim(manager) == "withdrawn"
        assert await _arm(manager) == "unarmed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_only_the_requesters_denial_skips_the_send(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A card MindRoom denies on its own, such as on room departure, does not skip the send."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        assert await _schedule(manager)
        await admit_room_membership(journal.principal("router@shared"), _ROOM, "leave")

        assert await _arm(manager) == "unarmed"
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
        assert await _arm(manager) == "unarmed"
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

    try:
        await _armed(manager)
        assert isinstance(await _claim(manager), ScheduledCallClaim)
        await journal.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE scheduled_call_approvals SET execute_at_ns = 0 WHERE task_id = ?",
                (_TASK,),
            ),
        )
        await journal.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE matrix_delivery_outbox SET acknowledged_event_id = NULL WHERE delivery_id = ?",
                (_RECEIPT,),
            ),
        )

        await cards.maintain_automatic_approvals()
        assert await _binding_column(journal, "task_id") == _TASK

        await journal.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE matrix_delivery_outbox SET acknowledged_event_id = ? WHERE delivery_id = ?",
                ("$" + _RECEIPT, _RECEIPT),
            ),
        )
        await manager.recover_cards_on_startup()

        assert await _binding_column(journal, "task_id") is None
        assert await cards.is_terminal_approval_card(room_id=_ROOM, card_event_id="$" + _RECEIPT)
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
        assert await _schedule(manager, approver="@canonical:test")
        assert (await _decide(manager, "approved", sender="@canonical:test")).consumed is True
        assert await _arm(manager) == "armed"
        sent.clear()

        assert isinstance(await _claim(manager), ScheduledCallClaim)

        [receipt] = sent
        assert receipt.payload["resolved_by"] == "@canonical:test"
        assert receipt.payload["approval_provenance"]["approved_by"] == "@canonical:test"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "abandon",
    [
        "UPDATE matrix_delivery_outbox SET acknowledged_event_id = NULL, retired = 1 WHERE delivery_id = ?",
        "UPDATE matrix_delivery_outbox SET acknowledged_event_id = NULL, permanent_failure_reason = 'gone' "
        "WHERE delivery_id = ?",
    ],
    ids=["retired", "permanently-failed"],
)
async def test_receipt_that_will_never_be_sent_does_not_block_pruning(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    abandon: str,
) -> None:
    """A receipt abandoned before Matrix accepted it, such as after the router left, needs no binding."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    monkeypatch.setattr(manager, "_ensure_deadline_sweep", lambda: None)
    try:
        await _armed(manager)
        assert isinstance(await _claim(manager), ScheduledCallClaim)
        await journal.backend.write(lambda transaction: transaction.execute(abandon, (_RECEIPT,)))
        await journal.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE scheduled_call_approvals SET execute_at_ns = 0 WHERE task_id = ?",
                (_TASK,),
            ),
        )

        await manager.recover_cards_on_startup()

        assert await _binding_column(journal, "task_id") is None
        receipt = await journal.backend.read(
            lambda transaction: transaction.fetchone(
                "SELECT 1 AS present FROM matrix_delivery_outbox WHERE delivery_id = ?",
                (_RECEIPT,),
            ),
        )
        assert receipt is None
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

        assert await _binding_column(journal, "task_id") is None
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
        await _armed(manager)
        assert isinstance(await _claim(manager), ScheduledCallClaim)
        cards = journal.principal("router@shared")

        await cards.remember_terminal_approval_alias(
            room_id=_ROOM,
            card_event_id="$receipt-copy",
            delivery_id=_RECEIPT,
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


class _SlackTools(Toolkit):
    """A configured toolkit whose one gated function records each real run."""

    def __init__(self, runs: list[dict[str, object]], *, outcome: object = "sent") -> None:
        self.runs = runs
        self.outcome = outcome
        super().__init__(name="slack", tools=[self.post_slack_message])

    def post_slack_message(self, channel: str, text: str = "") -> object:
        """Post a Slack message.

        Args:
            channel: Channel or user ID.
            text: Message text.

        """
        self.runs.append({"channel": channel, "text": text, "thread": threading.get_ident()})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _live_agent(
    runs: list[dict[str, object]] | None = None,
    *,
    outcome: object = "sent",
    toolkit_name: str = _TOOLKIT,
    authored_confirmation: bool = False,
    extra_toolkits: tuple[Toolkit, ...] = (),
) -> Agent:
    """Build a live agent whose toolkit carries its configured identity, as agent assembly sets it."""
    toolkit = _SlackTools([] if runs is None else runs, outcome=outcome)
    for function in toolkit.get_async_functions().values():
        function.owning_toolkit = toolkit_name
        if authored_confirmation:
            function.requires_confirmation = True
    return Agent(id="general", model=OpenAIChat(), tools=[toolkit, *extra_toolkits])


async def _schedule_from_tool(
    *,
    arguments_json: str = '{"text": "Good morning!", "channel": "U123"}',
    execute_at: str = "2030-01-02T09:00:00-05:00",
    agent: Agent | None = None,
) -> str:
    return await SchedulerTools().schedule_tool_call(
        tool_name="post_slack_message",
        arguments_json=arguments_json,
        execute_at=execute_at,
        description="Morning DM",
        agent=agent or _live_agent(),
    )


@pytest.mark.asyncio
async def test_schedule_tool_call_stores_the_call_and_keeps_its_arguments_out_of_matrix() -> None:
    """The card binds the stored call, while the saved task and trigger name only the task to run."""
    config = _gated_config()
    context = _tool_context(config)
    request = AsyncMock(return_value=True)

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        patch("mindroom.scheduling._start_scheduled_task") as start,
        tool_runtime_context(context),
        _responders(context.config, "general"),
    ):
        result = await _schedule_from_tool()

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
    assert workflow.pre_approved_call is True
    assert workflow.message.startswith("@general ")
    assert f'run_scheduled_call` with task_id "{record.task_id}"' in workflow.message
    assert "`post_slack_message`" in workflow.message
    assert "U123" not in workflow.message
    assert "Good morning" not in workflow.message
    assert "Morning DM" in workflow.message
    [binding] = request.await_args.args
    assert binding == ScheduledCallBinding(
        task_id=record.task_id,
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        agent_name="general",
        toolkit_name=_TOOLKIT,
        tool_name="post_slack_message",
        arguments_json=canonical_arguments({"channel": "U123", "text": "Good morning!"}),
        workflow_digest=_scheduled_call_workflow_digest(record.task_id, workflow),
        execute_at_ns=int(workflow.execute_at.timestamp() * 1_000_000_000),
    )
    kwargs = request.await_args.kwargs
    assert kwargs["approver_user_id"] == "@user:localhost"
    assert kwargs["scheduled_for_text"] == "2030-01-02 14:00 UTC"
    assert kwargs["any_arguments_offered"] is True
    start.assert_called_once()
    assert record.task_id in result
    assert "approve" in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approver", "authored_confirmation"),
    [("@owner:localhost", False), ("@user:localhost", True)],
    ids=["approver-elsewhere", "tool-asks-itself"],
)
async def test_schedule_tool_call_offers_only_exact_approval_where_any_arguments_cannot_apply(
    approver: str,
    authored_confirmation: bool,
) -> None:
    """Approvals sent to another account, or a tool that confirms its own calls, get an exact-only card."""
    config = _gated_config()
    context = _tool_context(config)
    request = AsyncMock(return_value=True)

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        patch("mindroom.scheduling.resolve_tool_approval_approver", return_value=approver),
        patch("mindroom.scheduling._start_scheduled_task"),
        tool_runtime_context(context),
        _responders(context.config, "general"),
    ):
        await _schedule_from_tool(agent=_live_agent(authored_confirmation=authored_confirmation))

    [(_status, record)] = _persisted_workflows(context)
    assert request.await_args.kwargs["any_arguments_offered"] is False
    assert "arguments_json" not in record.workflow.message


@pytest.mark.asyncio
async def test_schedule_tool_call_refuses_a_team() -> None:
    """A team never makes calls itself, so only an agent can store one."""
    config = _bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            teams={"crew": TeamConfig(display_name="Crew", role="Ship things", agents=["general"])},
            tool_approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="post_slack_message", action="require_approval")],
            ),
        ),
    )
    context = replace(_tool_context(config), agent_name="crew")
    request = AsyncMock(return_value=True)

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        tool_runtime_context(context),
        _responders(context.config, "crew"),
        pytest.raises(RuntimeError, match="Only an agent"),
    ):
        await _schedule_from_tool()

    request.assert_not_awaited()


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
        await _schedule_from_tool()

    request.assert_not_awaited()
    context.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_tool_call_stores_arguments_exactly_as_given() -> None:
    """String values such as "true" or "None" stay strings; nothing reinterprets them before they run."""
    context = _tool_context(_gated_config())
    request = AsyncMock(return_value=True)

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        patch("mindroom.scheduling._start_scheduled_task"),
        tool_runtime_context(context),
        _responders(context.config, "general"),
    ):
        await _schedule_from_tool(arguments_json='{"channel": " TRUE ", "text": "None"}')

    [binding] = request.await_args.args
    assert binding.arguments_json == '{"channel":" TRUE ","text":"None"}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments_json", "execute_at", "thread_id", "gated", "agent", "error"),
    [
        ("{", "2030-01-02T09:00:00-05:00", "$thread", True, None, "not valid JSON"),
        ("[1]", "2030-01-02T09:00:00-05:00", "$thread", True, None, "must be a JSON object"),
        ('{"channel": "a", "channel": "b"}', "2030-01-02T09:00:00-05:00", "$thread", True, None, "duplicate key"),
        ('{"channel": NaN}', "2030-01-02T09:00:00-05:00", "$thread", True, None, "not valid JSON"),
        ('{"channel": "U1", "text": 1e999}', "2030-01-02T09:00:00-05:00", "$thread", True, None, "non-finite"),
        ('{"text": "no channel"}', "2030-01-02T09:00:00-05:00", "$thread", True, None, "current parameters"),
        ('{"channel": "U1"}', "2030-01-02T09:00:00-05:00", "$thread", True, "elsewhere", "not one of this agent"),
        ('{"channel": "U1"}', "2030-01-02T09:00:00-05:00", "$thread", True, "twice", "more than one"),
        ('{"channel": "U1"}', "tomorrow at nine", "$thread", True, None, "ISO 8601"),
        ('{"channel": "U1"}', "2030-01-02T09:00:00", "$thread", True, None, "UTC offset"),
        ('{"channel": "U1"}', "2020-01-02T09:00:00+00:00", "$thread", True, None, "future"),
        ('{"channel": "U1"}', "2030-01-02T09:00:00-05:00", None, True, None, "thread"),
        ('{"channel": "U1"}', "2030-01-02T09:00:00-05:00", "$thread", False, None, "does not require approval"),
    ],
    ids=[
        "invalid-json",
        "not-object",
        "duplicate-key",
        "nan",
        "overflow",
        "schema",
        "unknown-tool",
        "ambiguous-tool",
        "not-iso",
        "naive",
        "past",
        "no-thread",
        "ungated",
    ],
)
async def test_schedule_tool_call_rejects_what_it_cannot_store(
    arguments_json: str,
    execute_at: str,
    thread_id: str | None,
    gated: bool,
    agent: str | None,
    error: str,
) -> None:
    """Nothing is saved and no card is posted for a call that cannot be stored and run as approved."""
    config = (
        _gated_config()
        if gated
        else _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))
    )
    context = _tool_context(config, thread_id=thread_id)
    request = AsyncMock(return_value=True)
    live_agent = {
        None: _live_agent(),
        "elsewhere": Agent(id="general", model=OpenAIChat(), tools=[]),
        "twice": _live_agent(extra_toolkits=(_named_slack_tools("other_slack"),)),
    }[agent]

    with (
        patch("mindroom.scheduling.request_scheduled_call_approval", new=request),
        tool_runtime_context(context),
        _responders(context.config, "general"),
        pytest.raises(RuntimeError, match=error),
    ):
        await _schedule_from_tool(arguments_json=arguments_json, execute_at=execute_at, agent=live_agent)

    context.client.room_put_state.assert_not_awaited()
    request.assert_not_awaited()


def _named_slack_tools(owner: str) -> Toolkit:
    toolkit = _SlackTools([])
    for function in toolkit.get_async_functions().values():
        function.owning_toolkit = owner
    return toolkit


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
        await _schedule_from_tool()

    context.client.room_put_state.assert_not_awaited()
    withdraw.assert_awaited_once_with(request.await_args.args[0].task_id, reason="Schedule cancelled.")
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
        await _schedule_from_tool()

    withdraw.assert_awaited_once_with(request.await_args.args[0].task_id, reason="Schedule cancelled.")
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
        pre_approved_call=True,
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

    arm.assert_awaited_once_with(
        "task1234",
        _scheduled_call_workflow_digest("task1234", workflow),
        any_arguments_allowed=True,
    )
    assert execute.await_count == int(fires)
    assert client.room_put_state.await_args.kwargs["content"]["status"] == final_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_state", "outcome", "withdraws"),
    [
        ("armed", "suppressed", True),
        ("armed", "failed", True),
        ("armed", "delivered", False),
        ("unarmed", "failed", False),
    ],
)
async def test_a_trigger_that_never_went_out_leaves_no_armed_approval(
    approval_state: str,
    outcome: str,
    withdraws: bool,
) -> None:
    """A suppressed or failed fire withdraws its armed approval, so no other call can use it."""
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
        pre_approved_call=True,
    )
    record = ScheduledTaskRecord(
        task_id="task1234",
        room_id="!test:server",
        status="pending",
        created_at=datetime.now(UTC),
        workflow=workflow,
    )
    withdraw = AsyncMock()

    with (
        patch("mindroom.scheduling.get_scheduled_task", new=AsyncMock(side_effect=[record, record])),
        patch("mindroom.scheduling.arm_scheduled_call_approval", new=AsyncMock(return_value=approval_state)),
        patch("mindroom.scheduling.withdraw_scheduled_call_approval", new=withdraw),
        patch(
            "mindroom.scheduling_executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status=outcome, failure_reason="no")),
        ),
    ):
        await _run_once_task(
            client,
            "task1234",
            workflow,
            Config(),
            resolve_runtime_paths(config_path=Path("config.yaml"), process_env={}),
            AsyncMock(),
        )

    assert withdraw.await_count == int(withdraws)
    expected = "completed" if outcome == "delivered" else "failed"
    assert client.room_put_state.await_args.kwargs["content"]["status"] == expected


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
        pre_approved_call=True,
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
@pytest.mark.parametrize(
    ("pre_approved_call", "arm_error", "armed"),
    [(False, None, False), (True, RuntimeError("db down"), True)],
    ids=["plain-reminder", "arming-fails"],
)
async def test_tasks_fire_without_an_armed_approval(
    pre_approved_call: bool,
    arm_error: Exception | None,
    armed: bool,
) -> None:
    """Plain reminders never touch call approvals, and a failed arming still fires a scheduled call."""
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
        pre_approved_call=pre_approved_call,
    )
    record = ScheduledTaskRecord(
        task_id="task1234",
        room_id="!test:server",
        status="pending",
        created_at=datetime.now(UTC),
        workflow=workflow,
    )
    arm = AsyncMock(side_effect=arm_error)

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

    assert arm.await_count == int(armed)
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


@pytest.mark.asyncio
async def test_cancelling_a_pre_approved_task_says_its_approval_is_withdrawn() -> None:
    """The requester learns that an approval they already gave no longer applies."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$state"}, room_id="!test:server"),
    )
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(hours=1),
        message="@general call it",
        description="Morning DM",
        room_id="!test:server",
        thread_id="$thread123",
        pre_approved_call=True,
    )
    existing = {"status": "pending", "workflow": workflow.model_dump_json()}

    with (
        patch("mindroom.scheduling._read_scheduled_task_state", new=AsyncMock(return_value=existing)),
        patch("mindroom.scheduling.withdraw_scheduled_call_approval", new=AsyncMock()),
    ):
        result = await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task1234",
            runtime_paths=resolve_runtime_paths(config_path=Path("config.yaml"), process_env={}),
        )

    assert result == "✅ Cancelled task `task1234`; any approval given for its scheduled call is withdrawn."


def _response_event(content: dict[str, object]) -> nio.UnknownEvent:
    return nio.UnknownEvent.from_dict(
        {
            "type": "io.mindroom.tool_approval_response",
            "event_id": "$action",
            "sender": "@human:test",
            "origin_server_ts": 1000,
            "content": {
                **content,
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread",
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": "$card"},
                },
            },
        },
    )


@pytest.mark.parametrize("scope", ["exact_arguments", "any_arguments"])
def test_response_carries_an_approved_scheduled_scope(scope: str) -> None:
    """A client approves a scheduled call's card for one of the offered scopes."""
    payload = parse_approval_response_event(_response_event({"status": "approved", "scheduled_scope": scope}))

    assert payload.status == "approved"
    assert payload.scheduled_scope == scope
    assert payload.card_event_id == "$card"


@pytest.mark.parametrize(
    "content",
    [
        {"status": "denied", "scheduled_scope": "any_arguments"},
        {"status": "approved", "scheduled_scope": "everything"},
        {"status": "approved", "scheduled_scope": "any_arguments", "auto_approve_seconds": 300},
        {"action": "revoke_auto_approval", "grant_id": "grant-1", "scheduled_scope": "any_arguments"},
    ],
    ids=["with-denial", "unknown-scope", "with-timed-approval", "with-revocation"],
)
def test_malformed_scheduled_scope_never_becomes_a_decision(content: dict[str, object]) -> None:
    """A scope that cannot apply makes the whole response inert rather than a plain approval."""
    payload = parse_approval_response_event(_response_event(content))

    assert payload.status is None
    assert payload.action is None
    assert payload.scheduled_scope is None


@pytest.mark.parametrize(
    ("config", "tool_name", "arguments", "approver_id", "authored_confirmation", "offered"),
    [
        (Config(), "post_slack_message", {"channel": "U1"}, "@user:server", False, True),
        (
            Config(tool_approval=ToolApprovalConfig(scheduled_any_arguments=False)),
            "post_slack_message",
            {},
            "@user:server",
            False,
            False,
        ),
        (
            Config(
                mcp_servers={"files": MCPServerConfig(transport="streamable-http", url="https://files.example/mcp")},
            ),
            "files_call_tool",
            {"tool_name": "delete", "arguments": {"path": "/one"}},
            "@user:server",
            False,
            False,
        ),
        (Config(), "post_slack_message", {}, "@owner:server", False, False),
        (Config(), "post_slack_message", {}, "@user:server", True, False),
    ],
    ids=["plain-tool", "operator-disabled", "generic-mcp-dispatch", "approver-elsewhere", "tool-asks-itself"],
)
def test_any_arguments_is_offered_only_where_it_can_apply(
    config: Config,
    tool_name: str,
    arguments: dict[str, object],
    approver_id: str,
    authored_confirmation: bool,
    offered: bool,
) -> None:
    """Generic MCP dispatch, an operator opt-out, approvals sent elsewhere, or self-confirming tools stay exact."""
    assert (
        scheduled_call_offers_any_arguments(
            config,
            tool_name,
            arguments,
            requester_id="@user:server",
            approver_id=approver_id,
            authored_confirmation=authored_confirmation,
        )
        is offered
    )


def test_workflow_without_flag_loads_as_ordinary_task() -> None:
    """Tasks stored before scheduled tool calls existed carry no call approval."""
    stored = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2030, 1, 2, 14, 0, tzinfo=UTC),
        message="Remind me",
        description="Reminder",
        created_by="@user:localhost",
        room_id="!room:localhost",
    ).model_dump(mode="json")
    stored.pop("pre_approved_call")

    assert ScheduledWorkflow.model_validate(stored).pre_approved_call is False


@pytest.mark.asyncio
async def test_cancelling_a_plain_reminder_leaves_call_approvals_alone() -> None:
    """Ordinary tasks cancel without touching the approval journal."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$state"}, room_id="!test:server"),
    )
    plain = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2030, 1, 2, 14, 0, tzinfo=UTC),
        message="Remind me",
        description="Reminder",
        created_by="@user:server",
        room_id="!test:server",
    )
    withdraw = AsyncMock()

    with (
        patch(
            "mindroom.scheduling._read_scheduled_task_state",
            new=AsyncMock(return_value={"status": "pending", "workflow": plain.model_dump_json()}),
        ),
        patch("mindroom.scheduling.withdraw_scheduled_call_approval", new=withdraw),
    ):
        result = await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task1234",
            runtime_paths=resolve_runtime_paths(config_path=Path("config.yaml"), process_env={}),
        )

    assert result == "✅ Cancelled task `task1234`"
    withdraw.assert_not_awaited()


def _code_config() -> Config:
    return _bind_runtime_paths(
        Config(
            agents={_AGENT: AgentConfig(display_name="Code Agent")},
            tool_approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="post_slack_message", action="require_approval")],
            ),
        ),
    )


def _turn_context(config: Config, *, thread_id: str = _THREAD) -> ToolRuntimeContext:
    """The tool context of the scheduling agent's own turn in the scheduling thread."""
    return replace(
        _make_context(config),
        agent_name=_AGENT,
        requester_id=_REQUESTER,
        target=MessageTarget.resolve(room_id=_ROOM, thread_id=thread_id, reply_to_event_id=None),
    )


async def _run(
    manager: ApprovalManager,
    agent: Agent | None,
    *,
    arguments_json: str | None = None,
    context: ToolRuntimeContext | None = None,
) -> object:
    with (
        patch("mindroom.approval_manager.get_approval_store", return_value=manager),
        tool_runtime_context(context or _turn_context(_code_config())),
    ):
        return await SchedulerTools().run_scheduled_call(
            task_id=_TASK,
            arguments_json=arguments_json,
            agent=agent,
            run_context=RunContext(run_id="run", session_id="session", session_state={}),
        )


@pytest.mark.asyncio
async def test_live_function_runs_with_its_hooks_and_keeps_tool_instructions(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """The stored call runs once through the live function and its hooks, off the event loop, in this turn."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    runs: list[dict[str, object]] = []
    hooked: list[str] = []
    agent = _live_agent(runs)

    def hook(function_name: str, function_call: Callable[..., object], arguments: dict[str, object]) -> object:
        hooked.append(function_name)
        return function_call(**arguments)

    for toolkit in agent.tools or ():
        assert isinstance(toolkit, Toolkit)
        for function in toolkit.get_async_functions().values():
            function.tool_hooks = [hook]
    instructions = ["keep me"]
    agent._tool_instructions = instructions
    try:
        await _armed(manager)

        result = await _run(manager, agent)

        assert result == "sent"
        assert [{key: run[key] for key in ("channel", "text")} for run in runs] == [_ARGUMENTS]
        assert runs[0]["thread"] != threading.get_ident()
        assert hooked == ["post_slack_message"]
        assert agent._tool_instructions is instructions
        assert await _binding_column(journal, "outcome") == "completed"

        repeat = await _run(manager, agent)

        assert isinstance(repeat, str)
        assert "already used" in repeat
        assert len(runs) == 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "authored_confirmation", "runs_replacement"),
    [(None, False, False), ("any_arguments", False, True), ("any_arguments", True, False)],
    ids=["exact", "any-arguments", "self-confirming-tool"],
)
async def test_replacement_arguments_run_only_under_an_any_arguments_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    scope: str | None,
    authored_confirmation: bool,
    runs_replacement: bool,
) -> None:
    """The agent's own arguments replace the stored ones only when any arguments were approved for that tool."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    runs: list[dict[str, object]] = []
    agent = _live_agent(runs, authored_confirmation=authored_confirmation)
    try:
        await _armed(manager, scope=scope)

        result = await _run(manager, agent, arguments_json='{"channel": "U999", "text": "Other"}')

        if runs_replacement:
            assert result == "sent"
            assert [{key: run[key] for key in ("channel", "text")} for run in runs] == [_OTHER_ARGUMENTS]
        else:
            assert isinstance(result, str)
            assert "only the stored arguments" in result
            assert runs == []
            assert await _binding_column(journal, "consumed_at_ns") is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_unapproved_call_offers_the_ordinary_way_with_its_stored_arguments(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Without an approval the call does not run, and the agent learns how to ask for approval now."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    runs: list[dict[str, object]] = []
    try:
        assert await _schedule(manager)

        result = await _run(manager, _live_agent(runs))

        assert isinstance(result, str)
        assert "has not approved it" in result
        assert "call `post_slack_message` with these arguments" in result
        assert canonical_arguments(_ARGUMENTS) in result
        assert runs == []
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_kind", "thread_id", "reason"),
    [
        ("none", _THREAD, "own conversation turn"),
        ("live", "$other-thread", "another agent, conversation, or requester"),
        ("without-tool", _THREAD, "not one of this agent's tools"),
        ("other-toolkit", _THREAD, "not one of this agent's tools"),
    ],
    ids=["no-live-agent", "other-thread", "tool-gone", "same-name-other-toolkit"],
)
async def test_call_that_cannot_run_here_spends_nothing(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    agent_kind: str,
    thread_id: str,
    reason: str,
) -> None:
    """A run outside the agent's own turn, elsewhere, or without the stored toolkit is refused before claiming."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    agent = {
        "none": None,
        "live": _live_agent(),
        "without-tool": Agent(id="general", model=OpenAIChat(), tools=[]),
        "other-toolkit": _live_agent(toolkit_name="other_slack"),
    }[agent_kind]
    try:
        await _armed(manager)

        result = await _run(manager, agent, context=_turn_context(_code_config(), thread_id=thread_id))

        assert isinstance(result, str)
        assert reason in result
        assert await _binding_column(journal, "consumed_at_ns") is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "message"),
    [(RuntimeError("slack is down"), "slack is down"), (iter(["streamed"]), "streams its result")],
    ids=["tool-error", "streaming-result"],
)
async def test_failed_call_spends_its_approval_and_records_the_failure(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    outcome: object,
    message: str,
) -> None:
    """A claimed call that fails is never retried; its outcome and reason are reported."""
    journal = journal_database()
    manager = _manager(journal, tmp_path, [])
    try:
        await _armed(manager)

        result = await _run(manager, _live_agent(outcome=outcome))

        assert isinstance(result, str)
        assert message in result
        assert await _binding_column(journal, "outcome") == "failed"
        assert await _claim(manager) == "used"
    finally:
        await manager.shutdown()


def test_only_the_built_in_scheduled_call_runner_skips_approval_gating() -> None:
    """A rule gating everything leaves run_scheduled_call ungated, but not a same-named plugin function."""
    config = Config(tool_approval=ToolApprovalConfig(default="require_approval"))

    def run_scheduled_call() -> str:
        """Plugin function that shares the runner's name."""
        return "plugin"

    scheduler = apply_tool_approval_capability(
        SchedulerTools(),
        config,
        supports_native_tool_approval=True,
        registered_tool_name="scheduler",
    )
    plugin = apply_tool_approval_capability(
        Toolkit(name="plugin", tools=[run_scheduled_call]),
        config,
        supports_native_tool_approval=True,
        registered_tool_name="plugin",
    )

    assert scheduler is not None
    assert plugin is not None
    assert scheduler.async_functions["run_scheduled_call"].requires_confirmation is not True
    assert scheduler.async_functions["schedule_tool_call"].requires_confirmation is True
    assert plugin.functions["run_scheduled_call"].requires_confirmation is True
