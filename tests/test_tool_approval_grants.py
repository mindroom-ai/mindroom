"""Behavioral coverage for durable timed thread approvals."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.approval.inbound import parse_approval_response_event
from mindroom.approval.manager import ApprovalManager
from mindroom.approval.receipt import build_approval_receipt
from mindroom.approval.response import plan_approval_calls, publish_generation, timed_grant_operation
from mindroom.approval.tool_approval import BackgroundScriptToolOrigin
from mindroom.approval.transport import ApprovalMatrixTransport, _approval_delivery_content
from mindroom.config.approval import ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalCardView,
    ApprovalDecisionMetadata,
    ApprovalFailure,
    BackgroundApprovalDecision,
    CallKey,
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    MatrixDelivery,
    approval_calls,
    approval_grants,
)
from mindroom.event_journal.identity import journal_principal_id
from mindroom.llm.runs import ToolExecution
from mindroom.matrix.large_messages import content_fits_normal_event
from mindroom.mcp.config import MCPServerConfig
from mindroom.message_target import MessageTarget
from mindroom.reply_lifecycle import ApprovalFailureKind
from mindroom.response_sources import ResponseSources
from mindroom.tool_approval_grants import AUTO_APPROVE_OPTIONS, ApprovalOperation, grant_operation
from mindroom.tool_system.registry import builtin_tool_registry
from tests.approval_card_helpers import (
    card_content,
    card_is_terminal,
    pending_card,
    resolve_card_event,
    wait_for_pending_card,
)
from tests.approval_continuation_helpers import approval_continuation
from tests.conftest import (
    approval_manager_for_test,
    approval_transport_for_test,
    handle_card_response_for_test,
    test_runtime_paths,
)
from tests.journal_membership_helpers import admit_room_membership
from tests.reply_span_helpers import paused_for_approval

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.approval.manager import ApprovalActionResult


@pytest.mark.asyncio
async def test_terminal_wire_edits_preserve_thread_scope_for_grant_and_revocation(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """SDK replacement content must retain the scope needed to revoke the grant."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    wire_edits = {}
    revoked = asyncio.Event()

    async def send(delivery: MatrixDelivery) -> str:
        content = _approval_delivery_content(delivery)
        if delivery.stage is DeliveryStage.FINAL:
            wire_edits[delivery.transaction_id] = content
            if content["m.new_content"]["auto_approval"]["revoked_at"] is not None:
                revoked.set()
        return "$" + delivery.delivery_id

    manager.transport.send_approval_delivery = send
    try:
        card = await _card(journal, manager, "first")
        await _approve(manager, card)
        grant = await journal.principal("router@shared").approvals.approval_grant_for_card(
            room_id="!room:test",
            card_event_id=card,
        )
        assert grant is not None
        await manager.handle_grant_revocation(
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=card,
            grant_id=grant.grant_id,
            authorize_responder=lambda _agent: True,
            before_consume=None,
        )
        await asyncio.wait_for(revoked.wait(), timeout=2)
        edits = list(wire_edits.values())
        assert len(edits) == 2
        for content in edits:
            assert content["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$card-first"}
            assert content["m.new_content"]["thread_id"] == "$thread"
            assert content["m.new_content"]["auto_approval"]["grant_id"] == grant.grant_id
        assert edits[0]["m.new_content"]["auto_approval"]["revoked_at"] is None
        assert edits[1]["m.new_content"]["auto_approval"]["revoked_at"] is not None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_origin_expiring_during_decision_does_not_advertise_a_nonexistent_grant(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persisted deadline fence also removes rejected grant acknowledgement."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    sent = []

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery)
        return "$" + delivery.delivery_id

    manager.transport.send_approval_delivery = send
    try:
        card = await _card(journal, manager, "first")
        monkeypatch.setattr("time.time_ns", lambda: 9_000_000_000_000_000_000)
        await _approve(manager, card)
        final = next(delivery for delivery in sent if delivery.stage is DeliveryStage.FINAL)
        assert final.payload["status"] == "expired"
        assert "auto_approval" not in final.payload
        assert (
            await journal.principal("router@shared").approvals.approval_grant_for_card(
                room_id="!room:test",
                card_event_id=card,
            )
            is None
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_type", ["mindroom_policy", "tool_authored"])
async def test_only_policy_pause_offers_timed_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approval_type: str,
) -> None:
    """Native confirmation remains per-call through the actual pause publisher."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    monkeypatch.setattr("mindroom.approval.manager._MANAGER", manager)
    responder = journal.principal("agent@code")
    config = Config(tool_approval=ToolApprovalConfig(default="require_approval"))
    runtime_paths = test_runtime_paths(tmp_path)
    tool = ToolExecution(
        tool_call_id="call-authored",
        tool_name="shell",
        tool_args={"command": "true"},
        requires_confirmation=True,
        approval_type=approval_type,
    )
    plan = await plan_approval_calls(
        ((tool, "code"),),
        config=config,
        runtime_paths=runtime_paths,
        requester_id="@human:test",
        toolkit_owners={("code", "shell"): "test_toolkit"},
        room_id="!room:test",
        thread_id="$thread",
    )
    await responder.admit(
        InboundEvent(
            event_id="$source-authored",
            room_id="!room:test",
            thread_id="$thread",
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@human:test",
            origin_server_ts=1000,
            source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "run"}},
        ),
    )
    continuation = approval_continuation(
        approval_id="authored",
        run_id="run",
        session_id="session",
        entity_name="code",
        room_id="!room:test",
        requester_id="@human:test",
        sources=ResponseSources(("$source-authored",), ("$source-authored",)),
        calls=plan.calls,
        state="waiting",
        runtime_generation="runtime",
    )
    assert await paused_for_approval(responder, continuation) is not None
    try:
        await publish_generation(
            continuation,
            plan,
            config=config,
            runtime_paths=runtime_paths,
            store=responder.approvals,
            retry_sources=lambda _room, _sources: None,
            target=MessageTarget(
                room_id="!room:test",
                source_thread_id="$thread",
                resolved_thread_id="$thread",
                reply_to_event_id=None,
                session_id="session",
            ),
            failure_reason="publication failed",
        )
        card = await pending_card(
            journal.principal("router@shared").approvals,
            room_id="!room:test",
            card_event_id="$authored-0-0",
        )
        assert card is not None
        if approval_type == "mindroom_policy":
            assert (await card_content(journal.principal("router@shared"), card))["auto_approve_options"] == [
                300,
                600,
                1800,
            ]
        else:
            assert "auto_approve_options" not in (await card_content(journal.principal("router@shared"), card))
            await _approve(manager, "$authored-0-0")
            current = await responder.approvals.approval_continuation("authored")
            assert current is not None
            assert current.calls[0].decision is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_policy_pause_receipt_accepts_timed_authorization_without_claiming_a_card(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reused human grant must not tell the model that another card was shown."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    monkeypatch.setattr("mindroom.approval.manager._MANAGER", manager)
    responder = journal.principal("agent@code")
    config = Config(tool_approval=ToolApprovalConfig(default="require_approval"))
    runtime_paths = test_runtime_paths(tmp_path)
    sent = []

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery)
        return "$" + delivery.delivery_id

    manager.transport.send_approval_delivery = send
    try:
        for name in ("origin", "reuse"):
            tool = ToolExecution(
                tool_call_id="call-" + name,
                tool_name="shell",
                tool_args={"command": name},
                requires_confirmation=True,
                approval_type="mindroom_policy",
            )
            plan = await plan_approval_calls(
                ((tool, "code"),),
                config=config,
                runtime_paths=runtime_paths,
                requester_id="@human:test",
                toolkit_owners={("code", "shell"): "test_toolkit"},
                room_id="!room:test",
                thread_id="$thread",
            )
            assert plan.calls[0].human_approval_required is True
            await responder.admit(
                InboundEvent(
                    event_id="$source-" + name,
                    room_id="!room:test",
                    thread_id="$thread",
                    kind=EventKind.MESSAGE,
                    event_class=EventClass.ACTIONABLE,
                    sender="@human:test",
                    origin_server_ts=1000,
                    source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "run"}},
                ),
            )
            continuation = await paused_for_approval(
                responder,
                approval_continuation(
                    approval_id=name,
                    run_id="run-" + name,
                    session_id="session",
                    entity_name="code",
                    room_id="!room:test",
                    requester_id="@human:test",
                    response_event_id="$waiting-" + name,
                    sources=ResponseSources(("$source-" + name,), ("$source-" + name,)),
                    calls=plan.calls,
                    state="waiting",
                    runtime_generation="runtime",
                ),
            )
            assert continuation is not None
            await publish_generation(
                continuation,
                plan,
                config=config,
                runtime_paths=runtime_paths,
                store=responder.approvals,
                retry_sources=lambda _room, _sources: None,
                target=MessageTarget(
                    room_id="!room:test",
                    source_thread_id="$thread",
                    resolved_thread_id="$thread",
                    reply_to_event_id=None,
                    session_id="session",
                ),
                failure_reason="publication failed",
            )
            if name == "origin":
                assert any(delivery.stage is DeliveryStage.INITIAL for delivery in sent)
                assert (await _approve(manager, "$origin-0-0")).consumed
                sent.clear()
            else:
                assert len(sent) == 1
                assert sent[0].payload["status"] == "approved"
                assert sent[0].payload["approvable"] is False
                stored = await responder.approvals.approval_continuation(name)
                assert stored is not None
                assert stored.state == "ready"
                receipt = build_approval_receipt(stored.calls)
                assert "human approval was required and granted" in receipt
                assert "matching timed approval window" in receipt
                assert "card was shown" not in receipt
                assert "human approval was not required" not in receipt
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_grant_batch_terminal_edits_fit_after_large_inline_argument_cards(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Batch edits must keep compact previews rather than doubling inline full arguments."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    sent = []

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery)
        return "$" + delivery.delivery_id

    manager.transport.send_approval_delivery = send
    try:
        first = await _card(journal, manager, "first", command="x" * 40_000)
        await _card(journal, manager, "sibling", command="y" * 40_000)
        initial = [delivery for delivery in sent if delivery.stage is DeliveryStage.INITIAL]
        assert len(initial) == 2
        assert all("full_arguments" in delivery.payload for delivery in initial)
        assert all(content_fits_normal_event(_approval_delivery_content(delivery)) for delivery in initial)

        assert (await _approve(manager, first)).consumed
        finals = [delivery for delivery in sent if delivery.stage is DeliveryStage.FINAL]
        assert {delivery.delivery_id for delivery in finals} == {"card-first", "card-sibling"}
        for delivery in finals:
            wire = _approval_delivery_content(delivery)
            assert content_fits_normal_event(wire)
            assert wire["m.new_content"]["status"] == "approved"
            assert wire["m.new_content"]["thread_id"] == "$thread"
            assert wire["m.new_content"]["arguments_truncated"] is True
            assert "full_arguments" not in wire["m.new_content"]
            assert "auto_approve_options" not in wire["m.new_content"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_subsequent_granted_call_publishes_exact_terminal_receipt(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Automatic calls retain exact reviewable history without another pending card."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    sent = []

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery)
        return "$" + delivery.delivery_id

    manager.transport.send_approval_delivery = send
    try:
        first = await _card(journal, manager, "first")
        await _approve(manager, first)
        sent.clear()
        receipt_event_id = await _card(journal, manager, "subsequent")
        assert len(sent) == 1
        receipt = sent[0]
        assert receipt.stage is DeliveryStage.INITIAL
        assert receipt.payload["status"] == "approved"
        assert receipt.payload["approvable"] is False
        assert receipt.payload["arguments"] == {"command": "subsequent"}
        assert receipt.payload["approval_provenance"]["grant_card_event_id"] == first
        assert receipt.payload["response_event_id"] == "$waiting-subsequent"
        assert "auto_approval" not in receipt.payload
        assert "auto_approve_options" not in receipt.payload
        approvals = journal.principal("router@shared").approvals
        card = await approvals.approval_card(room_id="!room:test", card_event_id=receipt_event_id)
        assert card is not None
        assert card.approvable is False
        assert await pending_card(approvals, room_id="!room:test", card_event_id=receipt_event_id) is None
        continuation = await journal.principal("agent@code").approvals.approval_continuation("subsequent")
        assert continuation is not None
        assert continuation.state == "ready"
        assert continuation.calls[0].decision.value == "approved"
        audit = await journal.backend.read(
            lambda transaction: transaction.fetchone(
                "SELECT cards.owner_id, cards.tool_call_id, calls.grant_id FROM approval_cards AS cards "
                "JOIN approval_calls AS calls ON calls.owner_id = cards.owner_id "
                "AND calls.generation = cards.generation AND calls.tool_call_id = cards.tool_call_id "
                "WHERE cards.delivery_id = ?",
                ("card-subsequent",),
            ),
        )
        assert audit is not None
        assert audit["owner_id"] == "subsequent"
        assert audit["tool_call_id"] == "call-subsequent"
        assert audit["grant_id"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_acknowledgement", [False, True])
async def test_automatic_receipt_reply_is_consumed_before_maintenance(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lost_acknowledgement: bool,
) -> None:
    """Receipt replies cannot become chat turns while background cleanup is queued."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    monkeypatch.setattr(manager, "_ensure_deadline_sweep", lambda: None)
    owner = journal.principal("router@shared")
    try:
        first = await _card(journal, manager, "first")
        await _approve(manager, first)
        if lost_acknowledgement:

            async def lose_acknowledgement(_delivery: MatrixDelivery) -> str:
                msg = "Matrix accepted the receipt but its response was lost"
                raise TimeoutError(msg)

            manager.transport.send_approval_delivery = lose_acknowledgement
        receipt_event_id = await _card(journal, manager, "subsequent")

        async def resolve_receipt(room_id: str, event_id: str) -> str | None:
            return "card-subsequent" if (room_id, event_id) == ("!room:test", receipt_event_id) else None

        manager.transport.resolve_approval_action_delivery = resolve_receipt
        delivery = await owner.load_matrix_delivery(delivery_id="card-subsequent", stage=DeliveryStage.INITIAL)
        assert delivery is not None
        assert delivery.acknowledged_event_id == (None if lost_acknowledgement else receipt_event_id)
        before_consume = AsyncMock()
        result = await handle_card_response_for_test(
            manager,
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=receipt_event_id,
            status="denied",
            reason="Do not run this.",
            authorize_responder=lambda _agent: True,
            before_consume=before_consume,
        )
        assert result.consumed is True
        before_consume.assert_awaited_once()
        assert await card_is_terminal(owner.approvals, room_id="!room:test", card_event_id=receipt_event_id)
        assert not await card_is_terminal(
            owner.approvals,
            room_id="!other:test",
            card_event_id=receipt_event_id,
        )
        assert not await card_is_terminal(owner.approvals, room_id="!room:test", card_event_id="$unknown")
        continuation = await journal.principal("agent@code").approvals.approval_continuation("subsequent")
        assert continuation is not None
        assert continuation.calls[0].decision.value == "approved"
        await owner.approvals.maintain_automatic_approvals()
        assert await card_is_terminal(owner.approvals, room_id="!room:test", card_event_id=receipt_event_id)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("retire_before_reply", [False, True])
async def test_republished_receipt_aliases_remain_terminal(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retire_before_reply: bool,
) -> None:
    """Both physical copies remain approval actions after a changed-device resend."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    monkeypatch.setattr(manager, "_ensure_deadline_sweep", lambda: None)
    manager.transport.sender_device_id = AsyncMock(return_value="original-device")
    owner = journal.principal("router@shared")
    try:
        first = await _card(journal, manager, "first")
        await _approve(manager, first)

        async def lose_acknowledgement(_delivery: MatrixDelivery) -> str:
            msg = "Matrix accepted the receipt but its response was lost"
            raise TimeoutError(msg)

        manager.transport.send_approval_delivery = lose_acknowledgement
        old_event_id = await _card(journal, manager, "subsequent")
        manager.transport.sender_device_id = AsyncMock(return_value="replacement-device")
        manager.transport.resolve_approval_delivery = AsyncMock(return_value=None)
        manager.transport.send_approval_delivery = AsyncMock(return_value="$replacement-receipt")
        assert (
            await (await manager._worker("card-subsequent")).flush(
                delivery_id="card-subsequent",
                stage=DeliveryStage.INITIAL,
            )
            == "$replacement-receipt"
        )
        if retire_before_reply:
            await owner.approvals.maintain_automatic_approvals()

        async def get_event(room_id: str, event_id: str) -> nio.RoomGetEventResponse:
            response = nio.RoomGetEventResponse()
            response.event = nio.UnknownEvent.from_dict(
                {
                    "event_id": event_id,
                    "room_id": room_id,
                    # The account of the receipt's sender, whose journal principal is "agent@code".
                    "sender": "@other:test" if event_id == "$untrusted-receipt" else "code",
                    "origin_server_ts": 1000,
                    "type": "io.mindroom.tool_approval",
                    "content": {
                        "approval_id": "unknown" if event_id == "$unknown-delivery" else "card-subsequent",
                    },
                },
            )
            return response

        router = MagicMock(
            agent_name="router",
            running=True,
            approval_room_ids=frozenset({"!room:test"}),
            client=MagicMock(user_id="@router:test", room_get_event=AsyncMock(side_effect=get_event)),
        )
        transport = approval_transport_for_test(lambda name: router if name == "router" else None, cards=owner)
        manager.transport.resolve_approval_action_delivery = transport.resolve_approval_action_delivery
        for event_id in ("$untrusted-receipt", "$unknown-delivery"):
            result = await handle_card_response_for_test(
                manager,
                room_id="!room:test",
                sender_id="@human:test",
                card_event_id=event_id,
                status="denied",
                reason="Do not run this.",
                authorize_responder=lambda _agent: True,
            )
            assert result.consumed is False
            assert not await card_is_terminal(owner.approvals, room_id="!room:test", card_event_id=event_id)
        for event_id in (old_event_id, "$replacement-receipt"):
            before_consume = AsyncMock()
            result = await handle_card_response_for_test(
                manager,
                room_id="!room:test",
                sender_id="@human:test",
                card_event_id=event_id,
                status="denied",
                reason="Do not run this.",
                authorize_responder=lambda _agent: True,
                before_consume=before_consume,
            )
            assert result.consumed is True
            before_consume.assert_awaited_once()
        await owner.approvals.maintain_automatic_approvals()
        assert await card_is_terminal(owner.approvals, room_id="!room:test", card_event_id=old_event_id)
        assert await card_is_terminal(
            owner.approvals,
            room_id="!room:test",
            card_event_id="$replacement-receipt",
        )
        continuation = await journal.principal("agent@code").approvals.approval_continuation("subsequent")
        assert continuation is not None
        assert continuation.calls[0].decision.value == "approved"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_approval_receipts_preserve_scope_and_original_timed_decision(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Grouping uses journal scope, while each call retains its exact decision provenance."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    sent: list[MatrixDelivery] = []

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery)
        return "$" + delivery.delivery_id

    manager.transport.send_approval_delivery = send
    try:
        first = await _card(journal, manager, "first")
        await _card(journal, manager, "sibling")
        await _card(journal, manager, "other", requester="@other:test")
        originals = {delivery.delivery_id: delivery.payload for delivery in sent}
        first_scope = originals["card-first"]["approval_scope"]
        assert first_scope["id"] == originals["card-sibling"]["approval_scope"]["id"]
        assert first_scope["id"] != originals["card-other"]["approval_scope"]["id"]
        assert first_scope["operation"] == {"tool_name": "shell"}
        assert first_scope["entity_name"] == "code"
        assert originals["card-first"]["response_event_id"] == "$waiting-first"
        await _approve(manager, first)
        await _card(journal, manager, "later")
        origin = next(
            delivery.payload
            for delivery in sent
            if delivery.delivery_id == "card-first" and delivery.stage is DeliveryStage.FINAL
        )
        sibling = next(
            delivery.payload
            for delivery in sent
            if delivery.delivery_id == "card-sibling" and delivery.stage is DeliveryStage.FINAL
        )
        later = next(delivery.payload for delivery in sent if delivery.delivery_id == "card-later")
        provenance = origin["approval_provenance"]
        assert provenance["kind"] == "timed_grant"
        assert provenance["grant_card_event_id"] == first
        assert provenance["granted_by"] == "@human:test"
        assert provenance["duration_seconds"] == 600
        assert provenance["expires_at"] == origin["auto_approval"]["expires_at"]
        assert sibling["approval_provenance"] == provenance
        assert later["approval_provenance"] == provenance
        assert origin["approval_scope"] == first_scope
        assert origin["tool_call_id"] == "call-first"
        assert origin["response_event_id"] == "$waiting-first"
        assert "auto_approval" not in sibling
        assert "auto_approval" not in later
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_device", ["test-device", "replacement-device"])
async def test_automatic_receipt_recovery_retires_only_acknowledged_payload(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    recovery_device: str,
) -> None:
    """A failed receipt send survives recovery and leaves an approval-only tombstone."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    manager.transport.sender_device_id = AsyncMock(return_value="test-device")
    delivered: list[MatrixDelivery] = []
    try:
        first = await _card(journal, manager, "first")
        await _approve(manager, first)

        async def fail_send(_delivery: MatrixDelivery) -> str:
            msg = "Matrix is temporarily unavailable"
            raise TimeoutError(msg)

        manager.transport.send_approval_delivery = fail_send
        await _card(journal, manager, "deferred", command="original command")
        await manager.cards.approvals.maintain_automatic_approvals()
        pending = await manager.cards.load_matrix_delivery(delivery_id="card-deferred", stage=DeliveryStage.INITIAL)
        assert pending is not None
        assert pending.payload["arguments"] == {"command": "original command"}
        assert pending.acknowledged_event_id is None
    finally:
        await manager.shutdown()
    manager = _manager(journal, tmp_path)

    async def recover_send(delivery: MatrixDelivery) -> str:
        delivered.append(delivery)
        return "$receipt-deferred"

    manager.transport.send_approval_delivery = recover_send
    manager.transport.sender_device_id = AsyncMock(side_effect=lambda _delivery_id: recovery_device)

    async def no_receipt_in_history(_delivery: MatrixDelivery) -> str | None:
        return None

    manager.transport.resolve_approval_delivery = no_receipt_in_history
    try:
        await manager.recover_cards_on_startup()
        assert len(delivered) == 1
        assert delivered[0].payload["arguments"] == {"command": "original command"}
        assert (
            await manager.cards.load_matrix_delivery(delivery_id="card-deferred", stage=DeliveryStage.INITIAL) is None
        )
        assert await card_is_terminal(
            manager.cards.approvals,
            room_id="!room:test",
            card_event_id="$receipt-deferred",
        )
        await manager.recover_cards_on_startup()
        assert len(delivered) == 1
        continuation = await journal.principal("agent@code").approvals.approval_continuation("deferred")
        assert continuation is not None
        assert continuation.calls[0].decision.value == "approved"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_transport_refused_arguments_cannot_receive_automatic_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A sidecar preparation refusal must disable matching as well as timed controls."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        first = await _card(journal, manager, "first")
        await _approve(manager, first)

        async def refuse_arguments(_room: str, _thread: str | None, content: dict, _sender_entity: str) -> dict:
            return {**content, "approvable": False}

        manager.transport.prepare_approval_event = refuse_arguments
        await _card(journal, manager, "refused")
        continuation = await journal.principal("agent@code").approvals.approval_continuation("refused")
        assert continuation is not None
        assert continuation.calls[0].decision is None
        stored = await pending_card(
            journal.principal("router@shared").approvals,
            room_id="!room:test",
            card_event_id="$card-refused",
        )
        assert stored is not None
        assert "auto_approve_options" not in (await card_content(journal.principal("router@shared"), stored))
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_redacted_arguments_keep_timed_approval(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Redacting a secret still shows the complete call, so every approval option remains."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "redacted", command="api_key=" + "sk-" + "Ab3Z" * 6)
        stored = await pending_card(
            journal.principal("router@shared").approvals,
            room_id="!room:test",
            card_event_id=card,
        )
        assert stored is not None
        assert (await card_content(journal.principal("router@shared"), stored))["arguments"] == {
            "command": "api_key=\u27e6secret-1\u27e7",
        }
        assert (await card_content(journal.principal("router@shared"), stored)).get("approvable", True) is True
        assert (await card_content(journal.principal("router@shared"), stored))["auto_approve_options"] == list(
            AUTO_APPROVE_OPTIONS,
        )

        assert (await _approve(manager, card)).consumed
        grant = await journal.principal("router@shared").approvals.approval_grant_for_card(
            room_id="!room:test",
            card_event_id=card,
        )
        assert grant is not None
    finally:
        await manager.shutdown()


@pytest.mark.parametrize(
    ("command", "shown"),
    [
        (
            "export OPENAI_API_KEY=" + "sk-" + "Ab3Z" * 6 + "; rm -rf ~/important",
            "export OPENAI_API_KEY=\u27e6secret-1\u27e7; rm -rf ~/important",
        ),
        ("export TOKEN=abc; rm -rf ~/important", "export TOKEN=abc; rm -rf ~/important"),
        ("rm -rf ~/important # 'token:' ", "rm -rf ~/important # 'token:' "),
    ],
)
@pytest.mark.asyncio
async def test_approval_card_shows_commands_as_written(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    command: str,
    shown: str,
) -> None:
    """The approver sees the command as it will run, with only known token formats hidden."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "visible", command=command)
        stored = await pending_card(
            journal.principal("router@shared").approvals,
            room_id="!room:test",
            card_event_id=card,
        )
        assert stored is not None
        assert (await card_content(journal.principal("router@shared"), stored))["arguments"] == {"command": shown}
        assert (await card_content(journal.principal("router@shared"), stored)).get("approvable", True) is True
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_arguments_beyond_redaction_depth_cannot_be_approved(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Redaction truncates deeply nested arguments, so the approver cannot see the whole call."""
    nested: object = "rm -rf ~/important"
    for _ in range(40):
        nested = {"child": nested}
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "nested", command=nested)
        stored = await pending_card(
            journal.principal("router@shared").approvals,
            room_id="!room:test",
            card_event_id=card,
        )
        assert stored is not None
        assert (await card_content(journal.principal("router@shared"), stored))["approvable"] is False
        assert "full_arguments" not in (await card_content(journal.principal("router@shared"), stored))
        assert "auto_approve_options" not in (await card_content(journal.principal("router@shared"), stored))

        result = await _approve(manager, card, seconds=None)

        assert result.consumed
        assert result.error_reason is not None
        continuation = await journal.principal("agent@code").approvals.approval_continuation("nested")
        assert continuation is not None
        assert continuation.calls[0].decision is not None
        assert continuation.calls[0].decision.value == "denied"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_departure_and_changed_binding_invalidate_grants(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Neither a new room tenure nor a changed operation binding inherits consent."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        first = await _card(journal, manager, "first")
        await _approve(manager, first)
        await _card(journal, manager, "binding-changed", operation="new-binding:shell")
        continuation = await journal.principal("agent@code").approvals.approval_continuation("binding-changed")
        assert continuation is not None
        assert continuation.calls[0].decision is None
        await admit_room_membership(journal.principal("agent@code"), "!room:test", "leave")
        await admit_room_membership(journal.principal("agent@code"), "!room:test", "join")
        await _card(journal, manager, "rejoined")
        continuation = await journal.principal("agent@code").approvals.approval_continuation("rejoined")
        assert continuation is not None
        assert continuation.calls[0].decision is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["denied", "expired", "failing"])
async def test_terminal_calls_win_over_grants(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    terminal: str,
) -> None:
    """A later grant cannot revive a declined, elapsed, or failed pending call."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        first = await _card(journal, manager, "first")
        second = await _card(journal, manager, "second")
        responder = journal.principal("agent@code")
        if terminal == "failing":
            await responder.approvals.request_approval_failure(
                "second",
                ApprovalFailure(ApprovalFailureKind.ERROR, "execution binding changed"),
                expected_state="waiting",
                expected_generation=0,
                expected_runtime_generation=None,
            )
        else:
            await resolve_card_event(
                journal.principal("router@shared").approvals,
                room_id="!room:test",
                card_event_id=second,
                requested_status=terminal,
                reason="human declined",
                metadata=ApprovalDecisionMetadata(),
            )
        await _approve(manager, first)
        continuation = await responder.approvals.approval_continuation("second")
        assert continuation is not None
        assert continuation.calls[0].decision.value == ("denied" if terminal == "failing" else terminal)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_changed_binding_refuses_originating_timed_action(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A stale card cannot mint a grant after its configured binding changes."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "first")
        await handle_card_response_for_test(
            manager,
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=card,
            status="approved",
            auto_approve_seconds=600,
            current_binding="new-binding",
            authorize_responder=lambda _agent: True,
        )
        continuation = await journal.principal("agent@code").approvals.approval_continuation("first")
        assert continuation is not None
        assert continuation.calls[0].decision is None
        assert (
            await journal.principal("router@shared").approvals.approval_grant_for_card(
                room_id="!room:test",
                card_event_id=card,
            )
            is None
        )
    finally:
        await manager.shutdown()


def test_mcp_dispatch_identity_includes_remote_operation_and_binding() -> None:
    """Generic dispatch must not authorize every operation exposed by a server."""
    config = Config(
        mcp_servers={"files": MCPServerConfig(transport="streamable-http", url="https://files.example/mcp")},
    )
    read = grant_operation(config, "files_call_tool", {"tool_name": "read", "arguments": {"path": "/one"}})
    assert read is not None
    assert read.scope_wire("scope", "team", "code") == {
        "id": "scope",
        "entity_name": "team",
        "invoking_agent": "code",
        "operation": {"tool_name": "files_call_tool", "mcp_server_id": "files", "mcp_tool_name": "read"},
    }
    assert read == grant_operation(config, "files_call_tool", {"tool_name": "read", "arguments": {"path": "/two"}})
    assert read != grant_operation(config, "files_call_tool", {"tool_name": "delete", "arguments": {"path": "/one"}})
    assert grant_operation(config, "files_call_tool", {"arguments": {}}) is None
    changed = Config(
        mcp_servers={"files": MCPServerConfig(transport="streamable-http", url="https://changed.example/mcp")},
    )
    assert read != grant_operation(changed, "files_call_tool", {"tool_name": "read", "arguments": {}})


@pytest.mark.parametrize(
    ("toolkit_name", "tool_name", "actions"),
    [
        (None, "triggers", ("list", "delete", "rotate_key")),
        (None, "memory", ("list", "search", "delete")),
        (None, "todo", ("list", "add", "update")),
        (None, "scheduler", ("list", "create", "cancel")),
        ("twilio", "twilio_read", ("get_call_details", "list_messages")),
        ("discord", "discord_read", ("get_channel_messages", "get_channel_info", "list_channels")),
        ("telegram", "telegram_read", ("get_chat", "get_file")),
        ("telegram", "telegram_send", ("message", "document")),
        ("telegram", "telegram_manage", ("edit", "delete", "pin")),
        ("whatsapp", "whatsapp_send", ("text", "template", "reaction")),
        ("zoom", "zoom_read", ("get_meeting", "get_meeting_recordings")),
        ("zoom", "zoom_manage", ("schedule_meeting", "delete_meeting")),
        ("x", "x_read", ("search_posts", "get_home_timeline")),
        ("x", "x_write", ("create_post", "reply_to_post", "send_dm")),
        (None, "attachment", ("list", "get", "register", "inline_image")),
    ],
)
def test_action_dispatched_grant_operation_binds_to_action(
    toolkit_name: str | None,
    tool_name: str,
    actions: tuple[str, ...],
) -> None:
    """Timed grants for unified action-dispatched tools must be scoped per action."""
    config = Config()
    registry = builtin_tool_registry()

    def operation(arguments: dict[str, object]) -> ApprovalOperation | None:
        return grant_operation(config, tool_name, arguments, tools=registry, toolkit_name=toolkit_name)

    ops = [operation({"action": action}) for action in actions]
    assert all(op is not None for op in ops)
    assert ops[0] == operation({"action": f" {actions[0]} "})
    assert ops[0] is not None
    assert ops[0].mcp_server_id == (toolkit_name or tool_name)
    assert ops[0].mcp_tool_name == actions[0]
    other = grant_operation(config, tool_name, {"action": actions[0]}, tools=registry, toolkit_name="other_toolkit")
    if toolkit_name is None:
        assert other == ops[0]
    else:
        assert other is not None
        assert other.mcp_server_id is None
        assert other.key != ops[0].key
    assert len({op.key for op in ops if op is not None}) == len(actions)
    assert operation({}) is None
    assert operation({"action": ""}) is None
    assert operation({"action": "   "}) is None
    assert operation({"action": 123}) is None


@pytest.mark.parametrize(
    ("toolkit_name", "tool_name", "action"),
    [
        (None, "triggers", "list"),
        (None, "memory", "list"),
        (None, "todo", "list"),
        (None, "scheduler", "list"),
        (None, "attachment", "list"),
        ("zoom", "zoom_manage", "schedule_meeting"),
    ],
)
def test_action_dispatched_scheduled_calls_never_offer_any_arguments(
    toolkit_name: str | None,
    tool_name: str,
    action: str,
) -> None:
    """Action-dispatched tools cannot offer any-arguments scheduled approvals that could switch actions."""
    from mindroom.approval.tool_approval import scheduled_call_offers_any_arguments  # noqa: PLC0415

    config = Config.model_validate({"tool_approval": {"scheduled_any_arguments": True}})
    assert (
        scheduled_call_offers_any_arguments(
            config,
            tool_name,
            {"action": action},
            requester_id="@alice:example.org",
            approver_id="@alice:example.org",
            authored_confirmation=False,
            tools=builtin_tool_registry(),
            toolkit_name=toolkit_name,
        )
        is False
    )


@pytest.mark.parametrize(
    ("toolkit_name", "tool_name", "arg_name", "values"),
    [
        ("unsplash", "unsplash", "action", ("search", "get", "download")),
        ("file_generation", "generate_file", "format", ("txt", "pdf", "code")),
        ("visualization", "create_chart", "chart_type", ("bar", "line", "scatter")),
    ],
)
def test_toolkit_scoped_dispatch_grant_operation_binds_to_owner_and_value(
    toolkit_name: str,
    tool_name: str,
    arg_name: str,
    values: tuple[str, str, str],
) -> None:
    """Wave 5 dispatch-scoped tools bind per (toolkit_name, value) without hijacking other toolkits."""
    from mindroom.approval.tool_approval import scheduled_call_offers_any_arguments  # noqa: PLC0415
    from mindroom.tool_system.registry import builtin_tool_registry  # noqa: PLC0415

    config = Config()
    registry = builtin_tool_registry()
    ops = [
        grant_operation(config, tool_name, {arg_name: value}, tools=registry, toolkit_name=toolkit_name)
        for value in values
    ]
    assert all(op is not None for op in ops)
    assert ops[0] == grant_operation(
        config,
        tool_name,
        {arg_name: f" {values[0]} "},
        tools=registry,
        toolkit_name=toolkit_name,
    )
    assert ops[0] is not None
    assert ops[0].mcp_server_id == toolkit_name
    assert ops[0].mcp_tool_name == values[0]
    assert len(set(ops)) == 3
    assert len({op.key for op in ops if op is not None}) == 3
    for bad_args in ({}, {arg_name: ""}, {arg_name: "   "}, {arg_name: 123}):
        assert grant_operation(config, tool_name, bad_args, tools=registry, toolkit_name=toolkit_name) is None

    # Fallback without toolkit_name still scopes when the owning toolkit is registered.
    fallback_op = grant_operation(config, tool_name, {arg_name: values[0]}, tools=registry)
    assert fallback_op == ops[0]

    # Scheduled calls for the owning toolkit never offer approving any arguments.
    assert (
        scheduled_call_offers_any_arguments(
            config,
            tool_name,
            {arg_name: values[0]},
            requester_id="@alice:example.org",
            approver_id="@alice:example.org",
            authored_confirmation=False,
            tools=registry,
            toolkit_name=toolkit_name,
        )
        is False
    )

    # Another toolkit exposing the same function name is NOT hijacked into dispatch scoping.
    other_op = grant_operation(config, tool_name, {}, tools=registry, toolkit_name="other_toolkit")
    assert other_op is not None
    assert other_op.tool_name == tool_name
    assert other_op.mcp_server_id is None
    assert other_op.mcp_tool_name is None
    assert (
        scheduled_call_offers_any_arguments(
            config,
            tool_name,
            {},
            requester_id="@alice:example.org",
            approver_id="@alice:example.org",
            authored_confirmation=False,
            tools=registry,
            toolkit_name="other_toolkit",
        )
        is True
    )


@pytest.mark.parametrize(
    ("toolkit", "old_name", "new_name"),
    [
        ("memory", "delete_memory", "memory"),
        ("memory", "add_memory", "memory"),
        ("todo", "update_todo", "todo"),
        ("scheduler", "schedule", "scheduler"),
        ("searxng", "image_search", "searxng_search"),
        ("searxng", "search_web", "searxng_search"),
        ("gmail", "send_email", "gmail_send"),
        ("gmail", "send_email_to_self", "gmail_send_to_self"),
        ("gmail", "create_draft_email", "gmail_draft"),
        ("gmail", "delete_custom_label", "gmail_manage"),
        ("gmail", "search_emails", "gmail_read"),
        ("google_calendar", "create_event", "google_calendar_write"),
        ("google_calendar", "list_events", "google_calendar_read"),
        ("github", "delete_repository", "github_write_repo"),
        ("github", "create_pull_request", "github_write_pr"),
        ("github", "create_issue", "github_write_issues"),
        ("github", "list_repositories", "github_read_repo"),
        ("github", "get_pull_request", "github_read_issues_prs"),
        ("google_drive", "google_drive_trash_file", "google_drive_write"),
        ("google_drive", "google_drive_download_file", "google_drive_read"),
        ("google_sheets", "create_sheet", "google_sheets_write"),
        ("google_sheets", "create_duplicate_sheet", "google_sheets_write"),
        ("google_sheets", "read_sheet", "google_sheets_read"),
        ("homeassistant", "call_service", "homeassistant_control"),
        ("homeassistant", "list_entities", "homeassistant_read"),
        ("file_generation", "generate_json_file", "generate_file"),
        ("file_generation", "generate_code_file", "generate_file"),
        ("visualization", "create_bar_chart", "create_chart"),
        ("visualization", "create_histogram", "create_chart"),
        ("unsplash", "search_photos", "unsplash"),
        ("unsplash", "download_photo", "unsplash"),
        ("e2b", "list_files", "e2b_inspect"),
        ("daytona", "list_files", "daytona_inspect"),
        ("email", "email_user", "smtp_send_email"),
        ("twilio", "get_call_details", "twilio_read"),
        ("discord", "get_channel_messages", "discord_read"),
        ("discord", "send_message", "discord_send"),
        ("discord", "delete_message", "discord_manage"),
        ("telegram", "get_file", "telegram_read"),
        ("telegram", "send_photo", "telegram_send"),
        ("telegram", "delete_message", "telegram_manage"),
        ("whatsapp", "send_text_message", "whatsapp_send"),
        ("webex", "list_rooms", "webex_read"),
        ("zoom", "get_meeting_recordings", "zoom_read"),
        ("zoom", "delete_meeting", "zoom_manage"),
        ("x", "search_posts", "x_read"),
        ("x", "send_dm", "x_write"),
        ("attachments", "list_attachments", "attachment"),
        ("attachments", "get_attachment", "attachment"),
        ("attachments", "register_attachment", "attachment"),
        ("attachments", "inline_image", "attachment"),
    ],
)
def test_retired_consolidated_approval_rules_fail_closed(
    tmp_path: Path,
    toolkit: str,
    old_name: str,
    new_name: str,
) -> None:
    """Approval rules targeting retired consolidated function names must fail config load instead of failing open."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    options = {"searxng": {"host": "https://search.example.test"}, "telegram": {"chat_id": "1234"}}
    tool_entry: object = {toolkit: options[toolkit]} if toolkit in options else toolkit
    stale_data = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {"a": {"display_name": "A", "tools": [tool_entry], "include_default_tools": False}},
        "tool_approval": {"rules": [{"match": old_name, "action": "require_approval"}]},
    }
    with pytest.raises(ConfigRuntimeValidationError, match=new_name):
        Config.validate_with_runtime(stale_data, runtime_paths)

    updated_data = {
        **stale_data,
        "tool_approval": {"rules": [{"match": new_name, "action": "require_approval"}]},
    }
    loaded = Config.validate_with_runtime(updated_data, runtime_paths)
    assert loaded.tool_approval.rules[0].match == new_name


@pytest.mark.parametrize("old_name", ["get_skill_instructions", "get_skill_reference", "get_skill_script"])
def test_retired_skill_approval_rules_fail_closed_unconditionally(tmp_path: Path, old_name: str) -> None:
    """Retired skill approval rules fail closed even when no skills or tools are configured."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    stale_data = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {"a": {"display_name": "A", "tools": [], "include_default_tools": False}},
        "tool_approval": {"rules": [{"match": old_name, "action": "require_approval"}]},
    }
    with pytest.raises(ConfigRuntimeValidationError, match="skill"):
        Config.validate_with_runtime(stale_data, runtime_paths)

    updated_data = {
        **stale_data,
        "tool_approval": {"rules": [{"match": "skill", "action": "require_approval"}]},
    }
    loaded = Config.validate_with_runtime(updated_data, runtime_paths)
    assert loaded.tool_approval.rules[0].match == "skill"


def test_retired_consolidated_approval_rules_fail_closed_across_agents_sharing_name(tmp_path: Path) -> None:
    """Shared names like `search_web` (serper) or `add_memory` (mem0/memory) still fail closed until each replacement has a rule."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    base_data = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {
            "a": {
                "display_name": "A",
                "tools": ["memory", {"searxng": {"host": "https://search.example.test"}}],
                "include_default_tools": False,
            },
            "b": {
                "display_name": "B",
                "tools": ["mem0", "serper"],
                "include_default_tools": False,
            },
        },
        "tool_approval": {
            "rules": [
                {"match": "add_memory", "action": "require_approval"},
                {"match": "search_web", "action": "require_approval"},
            ],
        },
    }
    with pytest.raises(ConfigRuntimeValidationError, match=r"searxng_search|memory|mem0_add"):
        Config.validate_with_runtime(base_data, runtime_paths)

    explicit_data = {
        **base_data,
        "tool_approval": {
            "rules": [
                {"match": "add_memory", "action": "require_approval"},
                {"match": "memory", "action": "require_approval"},
                {"match": "mem0_add", "action": "require_approval"},
                {"match": "search_web", "action": "require_approval"},
                {"match": "searxng_search", "action": "auto_approve"},
            ],
        },
    }
    loaded = Config.validate_with_runtime(explicit_data, runtime_paths)
    assert [rule.match for rule in loaded.tool_approval.rules] == [
        "add_memory",
        "memory",
        "mem0_add",
        "search_web",
        "searxng_search",
    ]


def _write_consolidated_plugin(
    tmp_path: Path,
    *,
    plugin_name: str = "demo_consolidated",
    function_name: str = "demo_action_tool",
    retired_name: str | None = "unlink_demo",
) -> Path:
    plugin_root = tmp_path / "plugins" / plugin_name
    plugin_root.mkdir(parents=True)
    manifest = {"name": plugin_name, "mindroom_plugin_api": "1.35", "tools_module": "tools.py"}
    (plugin_root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    retired_arg = f", retired_functions={{{retired_name!r}: {function_name!r}}}" if retired_name is not None else ""
    (plugin_root / "tools.py").write_text(
        f"""from mindroom.plugin_api import ToolCategory, ToolFileAccess, Toolkit, register_tool_with_metadata, tool

class ConsolidatedTools(Toolkit):
    name = {plugin_name!r}
    @tool
    def {function_name}(self, action: str) -> str:
        \"\"\"Run an action-dispatched demo operation.\"\"\"
        return action

@register_tool_with_metadata(
    name={plugin_name!r}, display_name="Demo Consolidated", description="Demo consolidated plugin tool",
    category=ToolCategory.DEVELOPMENT, file_access=ToolFileAccess.NONE, function_names=({function_name!r},),
    action_scoped_grants=({function_name!r},){retired_arg},
)
def consolidated_tools() -> type[ConsolidatedTools]:
    return ConsolidatedTools
""",
        encoding="utf-8",
    )
    return plugin_root


@pytest.mark.asyncio
async def test_plugin_action_scoped_grants_scope_timed_approvals_per_action(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Plugin tools declaring action_scoped_grants bind timed grants to (tool_name, action)."""
    from mindroom.approval.tool_approval import scheduled_call_offers_any_arguments  # noqa: PLC0415
    from mindroom.tool_system.plugins import refresh_plugins  # noqa: PLC0415

    plugin_root = _write_consolidated_plugin(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "plugins": [{"path": str(plugin_root)}],
            "agents": {
                "code": {"display_name": "Code", "tools": ["demo_consolidated"], "include_default_tools": False},
            },
            "tool_approval": {"scheduled_any_arguments": True},
        },
        runtime_paths,
    )
    plugins = refresh_plugins(config, runtime_paths)
    registry = plugins.tools

    list_op = grant_operation(
        config,
        "demo_action_tool",
        {"action": "list"},
        tools=registry,
        toolkit_name="demo_consolidated",
    )
    unlink_op = grant_operation(
        config,
        "demo_action_tool",
        {"action": "unlink"},
        tools=registry,
        toolkit_name="demo_consolidated",
    )
    assert list_op is not None
    assert unlink_op is not None
    assert list_op == grant_operation(
        config,
        "demo_action_tool",
        {"action": " list "},
        tools=registry,
        toolkit_name="demo_consolidated",
    )
    assert list_op.mcp_server_id == "demo_consolidated"
    assert list_op.mcp_tool_name == "list"
    fallback_op = grant_operation(config, "demo_action_tool", {"action": "list"}, tools=registry)
    assert fallback_op is not None
    assert fallback_op.mcp_server_id == "demo_action_tool"
    assert fallback_op.mcp_tool_name == "list"
    assert list_op.key != unlink_op.key
    for bad_args in ({}, {"action": ""}, {"action": "   "}, {"action": 123}):
        assert (
            grant_operation(
                config,
                "demo_action_tool",
                bad_args,
                tools=registry,
                toolkit_name="demo_consolidated",
            )
            is None
        )
    assert not scheduled_call_offers_any_arguments(
        config,
        "demo_action_tool",
        {"action": "list"},
        requester_id="@human:test",
        approver_id="@human:test",
        authored_confirmation=False,
        tools=registry,
        toolkit_name="demo_consolidated",
    )
    # tools=None must never consult process-wide plugins, even for configs with plugins loaded.
    for target_config in (config, Config()):
        without_tools = grant_operation(target_config, "demo_action_tool", {}, tools=None)
        assert without_tools is not None
        assert without_tools.mcp_server_id is None
        without_tools_named = grant_operation(
            target_config,
            "demo_action_tool",
            {},
            tools=None,
            toolkit_name="demo_consolidated",
        )
        assert without_tools_named is not None
        assert without_tools_named.mcp_server_id is None

    # A timed grant on list auto-approves another list call but leaves unlink gated.
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        first_list = await _card(journal, manager, "list-1", tool_name="demo_action_tool", grant_op=list_op)
        assert (await _approve(manager, first_list)).consumed
        await _card(journal, manager, "list-2", tool_name="demo_action_tool", grant_op=list_op)
        await _card(journal, manager, "unlink-1", tool_name="demo_action_tool", grant_op=unlink_op)
        assert await _call_row(journal, "list-2") == ("approved", "@human:test")
        assert await _call_row(journal, "unlink-1") == (None, None)
    finally:
        await manager.shutdown()


def test_plugin_action_scoped_grants_stay_isolated_to_owning_toolkit(tmp_path: Path) -> None:
    """A plugin's action_scoped_grants must not affect a same-named tool owned by another toolkit."""
    from mindroom.approval.tool_approval import scheduled_call_offers_any_arguments  # noqa: PLC0415
    from mindroom.tool_system.plugins import refresh_plugins  # noqa: PLC0415

    plugin_root = _write_consolidated_plugin(
        tmp_path,
        plugin_name="custom_plugin",
        function_name="search_web",
        retired_name=None,
    )
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "plugins": [{"path": str(plugin_root)}],
            "agents": {
                "code": {
                    "display_name": "Code",
                    "tools": ["serper", "custom_plugin"],
                    "include_default_tools": False,
                },
            },
            "tool_approval": {"scheduled_any_arguments": True},
        },
        runtime_paths,
    )
    registry = refresh_plugins(config, runtime_paths).tools
    assert "serper" in registry.entries
    assert "custom_plugin" in registry.entries
    assert "search_web" not in registry.entries["serper"].action_scoped_grants
    assert "search_web" in registry.entries["custom_plugin"].action_scoped_grants

    # Another toolkit exposing the same function name keeps normal tool-wide grants and scheduled any-arguments.
    serper_op = grant_operation(config, "search_web", {"query": "x"}, tools=registry, toolkit_name="serper")
    assert serper_op is not None
    assert serper_op.tool_name == "search_web"
    assert serper_op.mcp_server_id is None
    assert serper_op.mcp_tool_name is None
    assert scheduled_call_offers_any_arguments(
        config,
        "search_web",
        {"query": "x"},
        requester_id="@human:test",
        approver_id="@human:test",
        authored_confirmation=False,
        tools=registry,
        toolkit_name="serper",
    )

    # The owning plugin gets per-action scoping and fails closed when action is missing.
    plugin_op = grant_operation(
        config,
        "search_web",
        {"action": "read"},
        tools=registry,
        toolkit_name="custom_plugin",
    )
    assert plugin_op is not None
    assert plugin_op.tool_name == "search_web"
    assert plugin_op.mcp_server_id == "custom_plugin"
    assert plugin_op.mcp_tool_name == "read"
    assert grant_operation(config, "search_web", {"query": "x"}, tools=registry, toolkit_name="custom_plugin") is None
    assert not scheduled_call_offers_any_arguments(
        config,
        "search_web",
        {"action": "read"},
        requester_id="@human:test",
        approver_id="@human:test",
        authored_confirmation=False,
        tools=registry,
        toolkit_name="custom_plugin",
    )

    # timed_grant_operation threads call.toolkit_name into grant_operation.
    serper_tool = ToolExecution(
        tool_call_id="call-serper",
        tool_name="search_web",
        tool_args={"query": "x"},
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )
    serper_call = ApprovalCall(
        tool_call_id="call-serper",
        tool_name="search_web",
        invoking_agent="code",
        expires_at_ns=9_000_000_000_000_000_000,
        toolkit_name="serper",
    )
    assert timed_grant_operation(config, serper_tool, serper_call, tools=registry) == serper_op

    plugin_tool = ToolExecution(
        tool_call_id="call-plugin",
        tool_name="search_web",
        tool_args={"action": "read"},
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )
    plugin_call = ApprovalCall(
        tool_call_id="call-plugin",
        tool_name="search_web",
        invoking_agent="code",
        expires_at_ns=9_000_000_000_000_000_000,
        toolkit_name="custom_plugin",
    )
    assert timed_grant_operation(config, plugin_tool, plugin_call, tools=registry) == plugin_op
    assert timed_grant_operation(config, serper_tool, plugin_call, tools=registry) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "action"), [("dispatch", "create"), ("telegram_send", "message")])
async def test_plugin_action_scoped_grants_distinguish_same_named_tools_across_toolkits(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    tool_name: str,
    action: str,
) -> None:
    """Two plugins declaring action_scoped_grants for the same function name keep distinct grant keys."""
    from mindroom.tool_system.plugins import refresh_plugins  # noqa: PLC0415

    issues_root = _write_consolidated_plugin(
        tmp_path,
        plugin_name="issues",
        function_name=tool_name,
        retired_name=None,
    )
    deployments_root = _write_consolidated_plugin(
        tmp_path,
        plugin_name="deployments",
        function_name=tool_name,
        retired_name=None,
    )
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "plugins": [{"path": str(issues_root)}, {"path": str(deployments_root)}],
            "agents": {
                "code": {
                    "display_name": "Code",
                    "tools": ["issues", "deployments"],
                    "include_default_tools": False,
                },
            },
        },
        runtime_paths,
    )
    registry = refresh_plugins(config, runtime_paths).tools

    issues_op = grant_operation(
        config,
        tool_name,
        {"action": action},
        tools=registry,
        toolkit_name="issues",
    )
    deployments_op = grant_operation(
        config,
        tool_name,
        {"action": action},
        tools=registry,
        toolkit_name="deployments",
    )
    assert issues_op is not None
    assert deployments_op is not None
    assert issues_op.mcp_server_id == "issues"
    assert deployments_op.mcp_server_id == "deployments"
    assert issues_op.mcp_tool_name == action
    assert deployments_op.mcp_tool_name == action
    assert issues_op.key != deployments_op.key
    for op, owner in ((issues_op, "issues"), (deployments_op, "deployments")):
        assert op.scope_wire("scope", "code", "code")["operation"] == {
            "tool_name": tool_name,
            "mcp_server_id": owner,
            "mcp_tool_name": action,
        }

    dispatch_tool = ToolExecution(
        tool_call_id="call-dispatch",
        tool_name=tool_name,
        tool_args={"action": action},
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )
    issues_call = ApprovalCall(
        tool_call_id="call-issues",
        tool_name=tool_name,
        invoking_agent="code",
        expires_at_ns=9_000_000_000_000_000_000,
        toolkit_name="issues",
    )
    deployments_call = ApprovalCall(
        tool_call_id="call-deployments",
        tool_name=tool_name,
        invoking_agent="code",
        expires_at_ns=9_000_000_000_000_000_000,
        toolkit_name="deployments",
    )
    assert timed_grant_operation(config, dispatch_tool, issues_call, tools=registry) == issues_op
    assert timed_grant_operation(config, dispatch_tool, deployments_call, tools=registry) == deployments_op

    # Timed approval covers the same action in issues, but not the same action in deployments.
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        first_issues = await _card(journal, manager, "issues-1", tool_name=tool_name, grant_op=issues_op)
        assert (await _approve(manager, first_issues)).consumed
        await _card(journal, manager, "issues-2", tool_name=tool_name, grant_op=issues_op)
        await _card(journal, manager, "deployments-1", tool_name=tool_name, grant_op=deployments_op)
        assert await _call_row(journal, "issues-2") == ("approved", "@human:test")
        assert await _call_row(journal, "deployments-1") == (None, None)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("builtin_toolkit", "function_name", "builtin_arg", "dispatch_value"),
    [
        ("unsplash", "unsplash", "action", "search"),
        ("file_generation", "generate_file", "format", "pdf"),
        ("visualization", "create_chart", "chart_type", "bar"),
    ],
)
async def test_builtin_dispatch_scoped_tools_stay_isolated_from_same_named_plugin_tools(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    builtin_toolkit: str,
    function_name: str,
    builtin_arg: str,
    dispatch_value: str,
) -> None:
    """A plugin exposing unsplash/generate_file/create_chart keeps its own grant owner and dispatch arg."""
    from mindroom.tool_system.plugins import refresh_plugins  # noqa: PLC0415

    plugin_name = f"alt_{builtin_toolkit}"
    plugin_root = _write_consolidated_plugin(
        tmp_path,
        plugin_name=plugin_name,
        function_name=function_name,
        retired_name=None,
    )
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "plugins": [{"path": str(plugin_root)}],
            "agents": {
                "code": {
                    "display_name": "Code",
                    "tools": [builtin_toolkit, plugin_name],
                    "include_default_tools": False,
                },
            },
        },
        runtime_paths,
    )
    registry = refresh_plugins(config, runtime_paths).tools

    builtin_op = grant_operation(
        config,
        function_name,
        {builtin_arg: dispatch_value},
        tools=registry,
        toolkit_name=builtin_toolkit,
    )
    plugin_op = grant_operation(
        config,
        function_name,
        {"action": dispatch_value},
        tools=registry,
        toolkit_name=plugin_name,
    )
    assert builtin_op is not None
    assert plugin_op is not None
    assert builtin_op.mcp_server_id == builtin_toolkit
    assert builtin_op.mcp_tool_name == dispatch_value
    assert plugin_op.mcp_server_id == plugin_name
    assert plugin_op.mcp_tool_name == dispatch_value
    assert builtin_op.key != plugin_op.key

    # When the plugin uses action_scoped_grants, passing only the built-in's format/chart_type arg fails closed.
    if builtin_arg != "action":
        assert (
            grant_operation(
                config,
                function_name,
                {builtin_arg: dispatch_value},
                tools=registry,
                toolkit_name=plugin_name,
            )
            is None
        )

    builtin_tool = ToolExecution(
        tool_call_id="call-builtin",
        tool_name=function_name,
        tool_args={builtin_arg: dispatch_value},
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )
    plugin_tool = ToolExecution(
        tool_call_id="call-plugin",
        tool_name=function_name,
        tool_args={"action": dispatch_value},
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )
    builtin_call = ApprovalCall(
        tool_call_id="call-builtin",
        tool_name=function_name,
        invoking_agent="code",
        expires_at_ns=9_000_000_000_000_000_000,
        toolkit_name=builtin_toolkit,
    )
    plugin_call = ApprovalCall(
        tool_call_id="call-plugin",
        tool_name=function_name,
        invoking_agent="code",
        expires_at_ns=9_000_000_000_000_000_000,
        toolkit_name=plugin_name,
    )
    assert timed_grant_operation(config, builtin_tool, builtin_call, tools=registry) == builtin_op
    assert timed_grant_operation(config, plugin_tool, plugin_call, tools=registry) == plugin_op

    # Approving the built-in call auto-approves another built-in call but leaves the plugin gated.
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        first_builtin = await _card(journal, manager, "builtin-1", tool_name=function_name, grant_op=builtin_op)
        assert (await _approve(manager, first_builtin)).consumed
        await _card(journal, manager, "builtin-2", tool_name=function_name, grant_op=builtin_op)
        await _card(journal, manager, "plugin-1", tool_name=function_name, grant_op=plugin_op)
        assert await _call_row(journal, "builtin-2") == ("approved", "@human:test")
        assert await _call_row(journal, "plugin-1") == (None, None)
    finally:
        await manager.shutdown()


def test_plugin_retired_functions_fail_closed_in_config_validation(tmp_path: Path) -> None:
    """Enabled plugin tools declaring retired_functions fail closed until an exact replacement rule is authored."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415
    from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess  # noqa: PLC0415
    from mindroom.tool_system.registration import register_tool_with_metadata  # noqa: PLC0415

    plugin_root = _write_consolidated_plugin(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    stale_data = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "plugins": [{"path": str(plugin_root)}],
        "agents": {"a": {"display_name": "A", "tools": ["demo_consolidated"], "include_default_tools": False}},
        "tool_approval": {
            "default": "auto_approve",
            "rules": [{"match": "unlink_demo", "action": "require_approval"}],
        },
    }
    with pytest.raises(ConfigRuntimeValidationError, match="demo_action_tool"):
        Config.validate_with_runtime(stale_data, runtime_paths)

    explicit_rules = [
        {"match": "unlink_demo", "action": "require_approval"},
        {"match": "demo_action_tool", "action": "require_approval"},
    ]
    loaded = Config.validate_with_runtime(
        {**stale_data, "tool_approval": {"default": "auto_approve", "rules": explicit_rules}},
        runtime_paths,
    )
    assert [rule.match for rule in loaded.tool_approval.rules] == ["unlink_demo", "demo_action_tool"]

    # When the plugin tool is not enabled on any agent or defaults, the legacy rule does not trigger.
    Config.validate_with_runtime(
        {**stale_data, "agents": {"a": {"display_name": "A", "tools": [], "include_default_tools": False}}},
        runtime_paths,
    )

    with pytest.raises(TypeError, match="action_scoped_grants"):
        register_tool_with_metadata(
            name="bad_demo",
            display_name="Bad Demo",
            description="Bad",
            category=ToolCategory.DEVELOPMENT,
            file_access=ToolFileAccess.NONE,
            action_scoped_grants="demo_action_tool",
        )


@pytest.mark.parametrize("seconds", [300, 600, 1800])
def test_timed_wire_round_trip_keeps_original_card(seconds: int) -> None:
    """A custom threaded action targets the reply card, not the thread root."""
    event = nio.UnknownEvent.from_dict(
        {
            "type": "io.mindroom.tool_approval_response",
            "event_id": "$action",
            "sender": "@human:test",
            "origin_server_ts": 1000,
            "content": {
                "status": "approved",
                "auto_approve_seconds": seconds,
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread",
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": "$card"},
                },
            },
        },
    )
    payload = parse_approval_response_event(event)
    assert payload.status == "approved"
    assert payload.auto_approve_seconds == seconds
    assert payload.card_event_id == "$card"


def test_revoke_wire_parses_without_ordinary_approval_status() -> None:
    """Revocation is a distinct action and cannot be mistaken for approval."""
    event = nio.UnknownEvent.from_dict(
        {
            "type": "io.mindroom.tool_approval_response",
            "event_id": "$action",
            "sender": "@human:test",
            "origin_server_ts": 1000,
            "content": {
                "action": "revoke_auto_approval",
                "grant_id": "grant-1",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread",
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": "$card"},
                },
            },
        },
    )
    payload = parse_approval_response_event(event)
    assert payload.status is None
    assert payload.action == "revoke_auto_approval"
    assert payload.grant_id == "grant-1"
    assert payload.card_event_id == "$card"


@pytest.mark.asyncio
async def test_revocation_ack_waits_for_original_edit_and_recovers_after_restart(tmp_path: Path) -> None:
    """A delayed acceptance edit cannot overwrite an acknowledged revocation."""
    journal = EventJournalStore.open_sqlite(tmp_path / "ordered.db")
    manager = _manager(journal, tmp_path)
    delivered = []

    async def send(delivery: MatrixDelivery) -> str:
        if delivery.stage is DeliveryStage.FINAL and delivery.delivery_id == "card-first":
            msg = "Matrix temporarily unavailable"
            raise TimeoutError(msg)
        delivered.append(delivery)
        return "$" + delivery.delivery_id

    manager.transport.send_approval_delivery = send
    try:
        card = await _card(journal, manager, "first")
        await _approve(manager, card)
        grant = await manager.cards.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=card)
        assert grant is not None
        await manager.handle_grant_revocation(
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=card,
            grant_id=grant.grant_id,
            authorize_responder=lambda _agent: True,
            before_consume=None,
        )
        assert [delivery for delivery in delivered if delivery.stage is DeliveryStage.FINAL] == []
    finally:
        await manager.shutdown()
        await journal.close()
    journal = EventJournalStore.open_sqlite(tmp_path / "ordered.db")
    manager = _manager(journal, tmp_path)
    delivered.clear()

    async def recovered_send(delivery: MatrixDelivery) -> str:
        delivered.append(delivery)
        return "$recovered-" + delivery.delivery_id

    manager.transport.send_approval_delivery = recovered_send
    try:
        await manager.recover_cards_on_startup()
        assert len(delivered) == 2
        assert delivered[0].payload["auto_approval"]["revoked_at"] is None
        assert delivered[1].payload["auto_approval"]["revoked_at"] is not None
        assert delivered[1].edits_event_id == "$card-first"
    finally:
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_revocation_edit_goes_out_after_restart_through_the_account_that_sent_the_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revocation left unsent is sent after a restart by the account whose card gave the grant, not another one."""
    journal = EventJournalStore.open_sqlite(tmp_path / "revoked.db")
    manager = _manager(journal, tmp_path)
    fake_send = manager.transport.send_approval_delivery

    async def send(delivery: MatrixDelivery) -> str:
        if delivery.delivery_id.startswith("approval-grant-revoked:"):
            msg = "Matrix temporarily unavailable"
            raise TimeoutError(msg)
        return await fake_send(delivery)

    manager.transport.send_approval_delivery = send
    manager.transport.sender_device_id = AsyncMock(return_value="agent-device")
    try:
        card = await _card(journal, manager, "first")
        await _approve(manager, card)
        grant = await manager.cards.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=card)
        assert grant is not None
        await manager.handle_grant_revocation(
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=card,
            grant_id=grant.grant_id,
            authorize_responder=lambda _agent: True,
            before_consume=None,
        )
    finally:
        await manager.shutdown()
    bots = {}
    for name in ("other", "agent"):
        client = MagicMock(spec=nio.AsyncClient, user_id=f"@{name}:test", device_id=f"{name}-device")
        client.rooms = {"!room:test": MagicMock()}
        bots[name] = MagicMock(agent_name=name, running=True, client=client)
    sent: list[tuple[nio.AsyncClient, dict]] = []

    async def send_event(
        client: nio.AsyncClient,
        room_id: str,
        _type: str,
        content: dict,
        **_options: object,
    ) -> object:
        sent.append((client, content))
        return nio.RoomSendResponse("$revoked", room_id)

    monkeypatch.setattr("mindroom.approval.transport.send_room_event_result", send_event)
    cards = journal.principal("router@shared")
    transport = ApprovalMatrixTransport(bots=lambda: bots, room_poster=lambda _room_id: None, cards=lambda: cards)
    manager = ApprovalManager(test_runtime_paths(tmp_path), transport=transport, cards=cards)
    try:
        assert (await manager.recover_cards_on_startup()).complete

        [(client, content)] = sent
        assert client is bots["agent"].client
        assert content["m.new_content"]["auto_approval"]["revoked_at"] is not None
    finally:
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_matching_card_does_not_advertise_a_revoke_target_it_does_not_own(tmp_path: Path) -> None:
    """Only the origin card can revoke; automatic decisions still retain audit references."""
    journal = EventJournalStore.open_sqlite(tmp_path / "origin.db")
    manager = _manager(journal, tmp_path)
    sent = []

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery)
        return "$" + delivery.delivery_id

    manager.transport.send_approval_delivery = send
    try:
        first = await _card(journal, manager, "first")
        await _card(journal, manager, "second")
        await _approve(manager, first)
        second_final = next(
            delivery
            for delivery in sent
            if delivery.delivery_id == "card-second" and delivery.stage is DeliveryStage.FINAL
        )
        assert second_final.payload["status"] == "approved"
        assert "auto_approval" not in second_final.payload
    finally:
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_two_store_reservation_and_grant_race_cannot_strand_pending_call(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Independent writer queues must share the database transaction fence."""
    journal = journal_database()
    second_journal = journal_database()
    manager = _manager(journal, tmp_path)
    second_manager = _manager(second_journal, tmp_path)
    try:
        first = await _card(journal, manager, "first")
        await asyncio.gather(_approve(manager, first), _card(second_journal, second_manager, "racing"))
        continuation = await journal.principal("agent@code").approvals.approval_continuation("racing")
        assert continuation is not None
        assert continuation.calls[0].decision.value == "approved"
    finally:
        await manager.shutdown()
        await second_manager.shutdown()
        await journal.close()
        await second_journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "denied"])
async def test_grant_maintenance_removes_unused_retired_card_scope(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    status: Literal["approved", "denied"],
) -> None:
    """Ordinary decisions must not leave grant-specific scope records behind."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "ordinary")
        await handle_card_response_for_test(
            manager,
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=card,
            status=status,
            authorize_responder=lambda _agent: True,
        )
        await manager.recover_cards_on_startup()
        scopes = await journal.backend.read(
            lambda transaction: transaction.fetchall(
                "SELECT delivery_id FROM approval_card_scopes WHERE principal_id = ?",
                ("router@shared",),
            ),
        )
        assert not scopes
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["expiry", "router@shared", "agent@code"])
async def test_grant_maintenance_releases_inactive_payload_but_keeps_identity(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    """Expired or departed grants retain audit identity without retaining tool arguments."""
    monkeypatch.setattr("time.time_ns", lambda: 2_000_000_000_000_000_000)
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    owner = journal.principal("router@shared")
    try:
        card = await _card(journal, manager, "first")
        await _approve(manager, card)
        grant = await owner.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=card)
        assert grant is not None
        await manager.recover_cards_on_startup()
        assert '"command": "first"' in await _grant_payload(journal, grant.grant_id)
        if terminal == "expiry":
            monkeypatch.setattr("time.time_ns", lambda: 2_000_000_600_000_000_000)
        else:
            await admit_room_membership(journal.principal(terminal), "!room:test", "leave")
        await manager.recover_cards_on_startup()
        if terminal == "router@shared":
            # The owner account leaving the room ends no grant that an agent's card began.
            assert '"command": "first"' in await _grant_payload(journal, grant.grant_id)
            return
        assert await _grant_payload(journal, grant.grant_id) == ""
        assert await owner.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=card) == grant
        assert (
            await owner.approvals.revoke_approval_grant(
                room_id="!room:test",
                card_event_id=card,
                sender_id="@human:test",
                grant_id=grant.grant_id,
            )
            is None
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_grant_maintenance_preserves_revocation_debt_until_acknowledged(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry cannot discard a pending stop acknowledgement; replay cannot recreate retired payloads."""
    monkeypatch.setattr("time.time_ns", lambda: 2_000_000_000_000_000_000)
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    owner = journal.principal("router@shared")
    fail_revocation = True
    delivered = []

    async def send(delivery: MatrixDelivery) -> str:
        if fail_revocation and delivery.delivery_id.startswith("approval-grant-revoked:"):
            msg = "Matrix temporarily unavailable"
            raise TimeoutError(msg)
        delivered.append(delivery)
        return "$" + delivery.delivery_id

    manager.transport.send_approval_delivery = send
    try:
        card = await _card(journal, manager, "first")
        await _approve(manager, card)
        grant = await owner.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=card)
        assert grant is not None
        delivery_id = await owner.approvals.revoke_approval_grant(
            room_id="!room:test",
            card_event_id=card,
            sender_id="@human:test",
            grant_id=grant.grant_id,
        )
        assert delivery_id is not None
        monkeypatch.setattr("time.time_ns", lambda: 2_000_000_600_000_000_000)
        await manager.recover_cards_on_startup()
        assert await _grant_payload(journal, grant.grant_id)
        pending = await owner.load_matrix_delivery(delivery_id=delivery_id.delivery_id, stage=DeliveryStage.FINAL)
        assert pending is not None
        assert pending.acknowledged_event_id is None
        fail_revocation = False
        await manager.recover_cards_on_startup()
        await manager.recover_cards_on_startup()
        assert await _grant_payload(journal, grant.grant_id) == ""
        assert await owner.load_matrix_delivery(delivery_id=delivery_id.delivery_id, stage=DeliveryStage.FINAL) is None
        sent_count = len(delivered)
        assert (
            await owner.approvals.revoke_approval_grant(
                room_id="!room:test",
                card_event_id=card,
                sender_id="@human:test",
                grant_id=grant.grant_id,
            )
            == delivery_id
        )
        await manager.recover_cards_on_startup()
        assert len(delivered) == sent_count
        retained = await owner.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=card)
        assert retained is not None
        assert retained.revoked_at_ns is not None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_second_delivery_pass_waits_for_an_in_flight_send_instead_of_repeating_it(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline sweep and an explicit pass in one process send each delivery once."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    owner = journal.principal("router@shared")
    revocation_sending = asyncio.Event()
    release_revocation = asyncio.Event()
    second_pass_listed = asyncio.Event()
    revocations: list[MatrixDelivery] = []

    async def send(delivery: MatrixDelivery) -> str:
        if delivery.delivery_id.startswith("approval-grant-revoked:"):
            revocations.append(delivery)
            revocation_sending.set()
            await release_revocation.wait()
        return "$" + delivery.delivery_id

    list_unacknowledged = type(owner).unacknowledged_matrix_deliveries

    async def list_and_report(self: object, **kwargs: object) -> object:
        batch = await list_unacknowledged(self, **kwargs)
        # Set before this pass continues to its next await, so the test resumes only once the pass is
        # either waiting for the in-flight send or claiming the same delivery itself.
        if revocation_sending.is_set():
            second_pass_listed.set()
        return batch

    manager.transport.send_approval_delivery = send
    try:
        card = await _card(journal, manager, "first")
        await _approve(manager, card)
        grant = await owner.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=card)
        assert grant is not None
        assert await owner.approvals.revoke_approval_grant(
            room_id="!room:test",
            card_event_id=card,
            sender_id="@human:test",
            grant_id=grant.grant_id,
        )
        monkeypatch.setattr(type(owner), "unacknowledged_matrix_deliveries", list_and_report)
        first_pass = asyncio.create_task(manager.recover_cards_on_startup())
        await revocation_sending.wait()
        second_pass = asyncio.create_task(manager.recover_cards_on_startup())
        await second_pass_listed.wait()
        release_revocation.set()
        await asyncio.gather(first_pass, second_pass)
        assert len(revocations) == 1
    finally:
        await manager.shutdown()


async def _grant_payload(journal: EventJournalStore, grant_id: str) -> str:
    row = await journal.backend.read(
        lambda transaction: transaction.fetchone(
            "SELECT resolution_json FROM approval_grants WHERE principal_id = ? AND grant_id = ?",
            ("router@shared", grant_id),
        ),
    )
    assert row is not None
    return str(row["resolution_json"])


async def _card(
    journal: EventJournalStore,
    manager: ApprovalManager,
    name: str,
    *,
    agent: str = "code",
    requester: str = "@human:test",
    thread: str | None = "$thread",
    tool_name: str = "shell",
    operation: str | None = "binding:shell",
    grant_op: ApprovalOperation | None = None,
    command: object = None,
    expires_at_ns: int = 9_000_000_000_000_000_000,
) -> str:
    responder = journal.principal(journal_principal_id("agent", agent))
    await responder.admit(
        InboundEvent(
            event_id="$source-" + name,
            room_id="!room:test",
            thread_id=thread,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=requester,
            origin_server_ts=1000,
            source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "run"}},
        ),
    )
    continuation = approval_continuation(
        approval_id=name,
        run_id="run-" + name,
        session_id="session-" + name,
        entity_name=agent,
        room_id="!room:test",
        thread_id=thread,
        requester_id=requester,
        response_event_id="$waiting-" + name,
        sources=ResponseSources(("$source-" + name,), ("$source-" + name,)),
        calls=(
            ApprovalCall(
                tool_call_id="call-" + name,
                tool_name=tool_name,
                invoking_agent=agent,
                expires_at_ns=expires_at_ns,
            ),
        ),
        state="waiting",
        runtime_generation="runtime",
    )
    assert await paused_for_approval(responder, continuation) is not None
    card = await manager.prepare_reply_card(
        approval_id="card-" + name,
        continuation_id=name,
        continuation_generation=0,
        entity_name=agent,
        response_event_id=continuation.response_event_id,
        tool_call_id="call-" + name,
        tool_name=tool_name,
        arguments={"command": name if command is None else command},
        room_id="!room:test",
        requester_id=requester,
        approver_user_id=requester,
        expires_at_ns=expires_at_ns,
        agent_name=agent,
        thread_id=thread,
        grant_operation=(
            grant_op
            if grant_op is not None
            else (ApprovalOperation(operation, tool_name) if operation is not None else None)
        ),
    )
    assert card is not None
    assert await manager.reserve_and_publish(
        continuation_principal_id=responder.principal_id,
        continuation_id=name,
        continuation_generation=0,
        cards=(card,),
    )
    return "$card-" + name


def _manager(journal: EventJournalStore, tmp_path: Path) -> ApprovalManager:
    async def prepare(_room: str, _thread: str | None, content: dict, _sender_entity: str) -> dict:
        return content

    async def send(delivery: MatrixDelivery) -> str:
        return "$" + delivery.delivery_id + ("-edit" if delivery.stage is DeliveryStage.FINAL else "")

    return approval_manager_for_test(
        test_runtime_paths(tmp_path),
        cards=journal.principal("router@shared"),
        prepare_approval_event=prepare,
        send_approval_delivery=send,
        sender_bot=lambda _entity_name, _room_id: object(),
        responder_principal_id=lambda entity_name: journal_principal_id("agent", entity_name),
    )


async def _approve(
    manager: ApprovalManager,
    card: str,
    *,
    seconds: int | None = 600,
    sender: str = "@human:test",
) -> ApprovalActionResult:
    return await handle_card_response_for_test(
        manager,
        room_id="!room:test",
        sender_id=sender,
        card_event_id=card,
        status="approved",
        auto_approve_seconds=seconds,
        authorize_responder=lambda _agent: True,
    )


@pytest.mark.asyncio
async def test_grant_batches_pending_calls_and_survives_restart_with_fixed_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing atomic batch matching or persisted deadlines leaves calls waiting."""
    monkeypatch.setattr("time.time_ns", lambda: 2_000_000_000_000_000_000)
    journal = EventJournalStore.open_sqlite(tmp_path / "grants.db")
    manager = _manager(journal, tmp_path)
    try:
        first = await _card(journal, manager, "first")
        await _card(journal, manager, "second")
        await _card(journal, manager, "other-agent", agent="helper")
        await _card(journal, manager, "other-operation", operation="binding:remote-delete")
        await _card(journal, manager, "other-requester", requester="@other:test")
        assert (await _approve(manager, first)).consumed
        for name in ("first", "second"):
            continuation = await journal.principal("agent@code").approvals.approval_continuation(name)
            assert continuation is not None
            assert continuation.calls[0].decision.value == "approved"
        for name, agent in (("other-agent", "helper"), ("other-operation", "code"), ("other-requester", "code")):
            continuation = await journal.principal(
                journal_principal_id("agent", agent),
            ).approvals.approval_continuation(name)
            assert continuation is not None
            assert continuation.calls[0].decision is None
        grant = await manager.cards.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=first)
        assert grant is not None
        assert grant.expires_at_ns == 2_000_000_600_000_000_000
    finally:
        await manager.shutdown()
        await journal.close()
    journal = EventJournalStore.open_sqlite(tmp_path / "grants.db")
    manager = _manager(journal, tmp_path)
    try:
        monkeypatch.setattr("time.time_ns", lambda: 2_000_000_599_000_000_000)
        await _card(journal, manager, "later")
        continuation = await journal.principal("agent@code").approvals.approval_continuation("later")
        assert continuation is not None
        assert continuation.calls[0].decision.value == "approved"
        await _approve(manager, first)
        monkeypatch.setattr("time.time_ns", lambda: 2_000_000_600_000_000_000)
        await _card(journal, manager, "expired")
        continuation = await journal.principal("agent@code").approvals.approval_continuation("expired")
        assert continuation is not None
        assert continuation.calls[0].decision is None
    finally:
        await manager.shutdown()
        await journal.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "approved", "auto_approve_seconds": value}
        for value in (True, False, "600", 600.0, 0, -1, 601, None, [], {})
    ]
    + [{"status": "denied", "auto_approve_seconds": 600}, {"status": "approved", "action": "unexpected"}],
)
def test_invalid_timed_intent_never_becomes_an_ordinary_approval(fields: dict) -> None:
    """Discarding unsupported fields would silently widen human intent."""
    event = nio.UnknownEvent.from_dict(
        {
            "type": "io.mindroom.tool_approval_response",
            "event_id": "$action",
            "sender": "@human:test",
            "origin_server_ts": 1000,
            "content": {
                **fields,
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread",
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": "$card"},
                },
            },
        },
    )
    payload = parse_approval_response_event(event)
    assert payload.status is None


@pytest.mark.asyncio
async def test_revoke_survives_card_retirement_and_replay_cannot_regrant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retirement must preserve grant identity and durable revocation delivery."""
    monkeypatch.setattr("time.time_ns", lambda: 2_000_000_000_000_000_000)
    journal = EventJournalStore.open_sqlite(tmp_path / "grants.db")
    manager = _manager(journal, tmp_path)
    try:
        first = await _card(journal, manager, "first")
        await _approve(manager, first)
        grant = await manager.cards.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=first)
        assert grant is not None
        result = await manager.handle_grant_revocation(
            room_id="!room:test",
            sender_id="@other:test",
            card_event_id=first,
            grant_id=grant.grant_id,
            authorize_responder=lambda _agent: True,
            before_consume=None,
        )
        assert not result.consumed
        result = await manager.handle_grant_revocation(
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=first,
            grant_id=grant.grant_id,
            authorize_responder=lambda _agent: True,
            before_consume=None,
        )
        assert result.consumed
        await _approve(manager, first)
        await _card(journal, manager, "after")
        continuation = await journal.principal("agent@code").approvals.approval_continuation("after")
        assert continuation is not None
        assert continuation.calls[0].decision is None
        grant = await manager.cards.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=first)
        assert grant.revoked_at_ns == 2_000_000_000_000_000_000
    finally:
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thread", "operation", "sender"),
    [
        (None, "binding:shell", "@human:test"),
        ("$thread", None, "@human:test"),
        ("$thread", "binding:shell", "@other:test"),
    ],
)
async def test_ineligible_timed_request_leaves_call_pending(
    tmp_path: Path,
    thread: str | None,
    operation: str | None,
    sender: str,
) -> None:
    """Unsupported grant scope must never fall back to approval once."""
    journal = EventJournalStore.open_sqlite(tmp_path / "grants.db")
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "first", thread=thread, operation=operation)
        await _approve(manager, card, sender=sender)
        continuation = await journal.principal("agent@code").approvals.approval_continuation("first")
        assert continuation is not None
        assert continuation.calls[0].decision is None
    finally:
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_a_timed_approval_past_the_deadline_records_the_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late "approve for N minutes" click expires the call and its card at once and grants nothing."""
    monkeypatch.setattr("time.time_ns", lambda: 2_000_000_000_000_000_000)
    journal = EventJournalStore.open_sqlite(tmp_path / "grants.db")
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "late", expires_at_ns=2_000_000_060_000_000_000)
        monkeypatch.setattr("time.time_ns", lambda: 2_000_000_060_000_000_000)

        assert (await _approve(manager, card)).consumed

        continuation = await journal.principal("agent@code").approvals.approval_continuation("late")
        assert continuation is not None
        assert continuation.calls[0].decision.value == "expired"
        router = journal.principal("router@shared").approvals
        assert await router.approval_grant_for_card(room_id="!room:test", card_event_id=card) is None
        assert await card_is_terminal(router, room_id="!room:test", card_event_id=card)
    finally:
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_timed_action_delivers_its_scope_without_startup_recovery(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Command delivery does not scan or settle unrelated approval debt."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        first = await _card(journal, manager, "selected")
        await _card(journal, manager, "matching")
        await _card(journal, manager, "unrelated", thread="$other-thread")
        await manager.shutdown()
        monkeypatch.setattr(manager, "_ensure_deadline_sweep", lambda: None)
        recover = AsyncMock()
        monkeypatch.setattr(manager, "recover_cards_on_startup", recover)
        sent: list[MatrixDelivery] = []

        async def send(delivery: MatrixDelivery) -> str:
            sent.append(delivery)
            return "$" + delivery.delivery_id

        manager.transport.send_approval_delivery = send
        await _approve(manager, first)
        recover.assert_not_awaited()
        assert {item.delivery_id for item in sent} == {"card-selected", "card-matching"}
        assert all(item.stage is DeliveryStage.FINAL for item in sent)
        unrelated = await journal.principal("agent@code").approvals.approval_continuation("unrelated")
        assert unrelated is not None
        assert unrelated.calls[0].decision is None
        grant = await manager.cards.approvals.approval_grant_for_card(room_id="!room:test", card_event_id=first)
        assert grant is not None
        sent.clear()
        await manager.handle_grant_revocation(
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=first,
            grant_id=grant.grant_id,
            authorize_responder=lambda _name: True,
            before_consume=None,
        )
        recover.assert_not_awaited()
        assert len(sent) == 1
        assert sent[0].payload["auto_approval"]["revoked_at"] is not None
    finally:
        await manager.shutdown()


# A job's approval wait rereads its durable decision once a second.
_JOB_DECISION_WAIT_SECONDS = 5.0


def _job_card_event_id(name: str) -> str:
    return f"$script-approval:tool-job:{name}:call-{name}"


def _job_call(
    manager: ApprovalManager,
    name: str,
    *,
    agent: str = "code",
    requester: str = "@human:test",
    approver: str | None = None,
    thread: str | None = "$thread",
    operation: str | None = "binding:shell",
) -> asyncio.Task[BackgroundApprovalDecision]:
    """Ask about one gated call the way a background job does, through a card the job owns."""
    return asyncio.create_task(
        manager.request_background_approval(
            owner_kind="job",
            origin=BackgroundScriptToolOrigin(
                run_id="tool-job:" + name,
                call_id="call-" + name,
                requester_id=requester,
                toolkit_name="test_toolkit",
                function_name="shell",
            ),
            room_id="!room:test",
            thread_id=thread,
            agent_name=agent,
            requester_id=requester,
            approver_user_id=approver or requester,
            tool_name="shell",
            arguments={"command": name},
            timeout_seconds=600,
            entity_name=agent,
            grant_operation=None if operation is None else ApprovalOperation(operation, "shell"),
        ),
    )


async def _posted_job_card(journal: EventJournalStore, name: str) -> ApprovalCardView:
    return await wait_for_pending_card(
        journal.principal("router@shared").approvals,
        room_id="!room:test",
        card_event_id=_job_card_event_id(name),
    )


async def _stop(tasks: list[asyncio.Task[BackgroundApprovalDecision]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ineligible",
    [None, "room-level conversation", "tool's own confirmation", "another approver"],
)
async def test_job_card_offers_timed_approval_when_a_paused_reply_card_would(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    ineligible: str | None,
) -> None:
    """A job's card offers the same options under the same conditions, scoped to the same conversation."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    call = {
        "room-level conversation": {"thread": None},
        "tool's own confirmation": {"operation": None},
        "another approver": {"approver": "@admin:test"},
    }.get(ineligible or "", {})
    waiting = [_job_call(manager, "first", **call)]
    try:
        content = await card_content(journal.principal("router@shared"), await _posted_job_card(journal, "first"))
        if ineligible is None:
            assert content["auto_approve_options"] == list(AUTO_APPROVE_OPTIONS)
            scope = content["approval_scope"]
            assert (scope["entity_name"], scope["invoking_agent"], scope["operation"]) == (
                "code",
                "code",
                {"tool_name": "shell"},
            )
            assert scope["id"] != "0" * 64
        else:
            assert "auto_approve_options" not in content
            assert "approval_scope" not in content
            approver = str(call.get("approver", "@human:test"))
            assert not (await _approve(manager, _job_card_event_id("first"), sender=approver)).consumed
            decision = await journal.principal("router@shared").approvals.detached_decision(
                CallKey.detached("tool-job:first", "call-first"),
            )
            assert decision is None
    finally:
        await _stop(waiting)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_timed_approval_on_a_job_card_covers_later_job_and_reply_calls(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Choosing a timed option on a job's card approves its call and records the grant a paused reply's would."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    sent: list[MatrixDelivery] = []

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery)
        return "$" + delivery.delivery_id

    manager.transport.send_approval_delivery = send
    first = _job_call(manager, "first")
    try:
        await _posted_job_card(journal, "first")
        assert (await _approve(manager, _job_card_event_id("first"))).consumed
        assert (await asyncio.wait_for(first, _JOB_DECISION_WAIT_SECONDS)).status == "approved"
        grant = await journal.principal("router@shared").approvals.approval_grant_for_card(
            room_id="!room:test",
            card_event_id=_job_card_event_id("first"),
        )
        assert grant is not None
        assert (grant.thread_id, grant.requester_id, grant.entity_name, grant.invoking_agent) == (
            "$thread",
            "@human:test",
            "code",
            "code",
        )
        sent.clear()
        # A later job call is approved without a card, and its receipt names the grant.
        later = await asyncio.wait_for(_job_call(manager, "later"), _JOB_DECISION_WAIT_SECONDS)
        assert later == BackgroundApprovalDecision(status="approved", reason=None)
        [receipt] = sent
        assert receipt.stage is DeliveryStage.INITIAL
        assert receipt.payload["status"] == "approved"
        assert receipt.payload["arguments"] == {"command": "later"}
        assert receipt.payload["approval_provenance"]["grant_id"] == grant.grant_id
        assert "auto_approve_options" not in receipt.payload
        assert (
            await pending_card(
                journal.principal("router@shared").approvals,
                room_id="!room:test",
                card_event_id=_job_card_event_id("later"),
            )
            is None
        )
        # So is a later call that pauses a reply in the same conversation.
        await _card(journal, manager, "reply")
        continuation = await journal.principal("agent@code").approvals.approval_continuation("reply")
        assert continuation is not None
        assert continuation.calls[0].decision is not None
        assert continuation.calls[0].decision.value == "approved"
    finally:
        await _stop([first])
        await manager.shutdown()


@pytest.mark.asyncio
async def test_timed_approval_on_a_reply_card_covers_pending_and_later_job_calls(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A grant from a paused reply's card approves a job's waiting card and its later calls in that conversation."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    waiting = _job_call(manager, "waiting")
    try:
        await _posted_job_card(journal, "waiting")
        first = await _card(journal, manager, "first")
        assert (await _approve(manager, first)).consumed
        assert (await asyncio.wait_for(waiting, _JOB_DECISION_WAIT_SECONDS)).status == "approved"
        later = await asyncio.wait_for(_job_call(manager, "later"), _JOB_DECISION_WAIT_SECONDS)
        assert later.status == "approved"
    finally:
        await _stop([waiting])
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("outside", ["agent", "requester", "thread", "operation", "revoked", "expired"])
async def test_job_call_outside_an_active_grant_still_asks(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outside: str,
) -> None:
    """A grant covers only its own conversation, agent, and operation, and never once revoked or expired."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    first = await _card(journal, manager, "first")
    await _approve(manager, first)
    grant = await journal.principal("router@shared").approvals.approval_grant_for_card(
        room_id="!room:test",
        card_event_id=first,
    )
    assert grant is not None
    if outside == "revoked":
        revoked = await manager.handle_grant_revocation(
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=first,
            grant_id=grant.grant_id,
            authorize_responder=lambda _entity_name: True,
            before_consume=None,
        )
        assert revoked.consumed
    if outside == "expired":
        monkeypatch.setattr("time.time_ns", lambda: grant.expires_at_ns)
    call = {
        "agent": {"agent": "helper"},
        "requester": {"requester": "@other:test"},
        "thread": {"thread": "$other-thread"},
        "operation": {"operation": "binding:remote-delete"},
    }.get(outside, {})
    asking = _job_call(manager, "asking", **call)
    try:
        await _posted_job_card(journal, "asking")
        assert not asking.done()
        assert (
            await journal.principal("router@shared").approvals.detached_decision(
                CallKey.detached("tool-job:asking", "call-asking"),
            )
            is None
        )
    finally:
        await _stop([asking])
        await manager.shutdown()


@pytest.mark.asyncio
async def test_ended_job_denies_its_open_timed_card(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A job that ends before the decision denies its card, whose timed option then grants nothing."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    ended = _job_call(manager, "ended")
    later = None
    try:
        stored = await _posted_job_card(journal, "ended")
        assert (await card_content(journal.principal("router@shared"), stored))["auto_approve_options"] == list(
            AUTO_APPROVE_OPTIONS,
        )
        await _stop([ended])
        assert await manager.settle_pending_background_approvals("tool-job:ended", reason="The job ended.") == 1
        await _approve(manager, _job_card_event_id("ended"))
        cards = journal.principal("router@shared")
        assert await cards.approvals.detached_decision(CallKey.detached("tool-job:ended", "call-ended")) == (
            BackgroundApprovalDecision(status="denied", reason="The job ended.")
        )
        assert (
            await cards.approvals.approval_grant_for_card(
                room_id="!room:test",
                card_event_id=_job_card_event_id("ended"),
            )
            is None
        )
        later = _job_call(manager, "later")
        await _posted_job_card(journal, "later")
        assert not later.done()
    finally:
        await _stop([ended] if later is None else [ended, later])
        await manager.shutdown()


async def _call_row(journal: EventJournalStore, name: str) -> tuple[object, ...]:
    row = await journal.backend.read(
        lambda transaction: transaction.fetchone(
            "SELECT decision, resolved_by FROM approval_calls WHERE owner_id = ?",
            (name,),
        ),
    )
    assert row is not None
    return row["decision"], row["resolved_by"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fence",
    [
        "UPDATE approval_continuations SET state = 'ready' WHERE approval_id = 'fenced'",
        "UPDATE approval_continuations SET generation = 1 WHERE approval_id = 'fenced'",
    ],
    ids=["no-longer-waiting", "another-generation"],
)
async def test_a_call_is_decided_only_while_its_paused_reply_waits_at_its_generation(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    fence: str,
) -> None:
    """A decision cannot land on a paused reply that moved past the state or generation its card was posted for."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "fenced")
        await journal.backend.write(lambda transaction: transaction.execute(fence))
        recorded = await resolve_card_event(
            journal.principal("router@shared").approvals,
            room_id="!room:test",
            card_event_id=card,
            requested_status="approved",
            reason=None,
            metadata=ApprovalDecisionMetadata(resolved_by="@human:test"),
        )
        assert recorded.recorded is False
        assert await _call_row(journal, "fenced") == (None, None)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_decision_the_deadline_overrode_names_no_decider(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late approval records the expiry, not the person whose approval came too late."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "late")
        monkeypatch.setattr("time.time_ns", lambda: 9_000_000_000_000_000_000)
        await resolve_card_event(
            journal.principal("router@shared").approvals,
            room_id="!room:test",
            card_event_id=card,
            requested_status="approved",
            reason=None,
            metadata=ApprovalDecisionMetadata(resolved_by="@human:test"),
        )
        assert await _call_row(journal, "late") == ("expired", None)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_only_a_receipt_takes_another_event_as_its_alias(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A transport-verified copy of a request card is not a second actionable card."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        await _card(journal, manager, "aliased")
        approvals = journal.principal("router@shared").approvals
        await approvals.remember_terminal_approval_alias(
            room_id="!room:test",
            card_event_id="$copy-of-card",
            delivery_id="card-aliased",
        )
        assert await approvals.approval_card(room_id="!room:test", card_event_id="$copy-of-card") is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_click_on_a_card_whose_membership_is_fenced_is_not_consumed(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """While the membership of the account that sent a card is fenced for departure, the card decides nothing."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "fenced-room")
        await journal.backend.write(
            lambda transaction: transaction.execute(
                "UPDATE room_membership SET departure_fenced = 1 WHERE principal_id = ? AND room_id = ?",
                ("agent@code", "!room:test"),
            ),
        )
        view = await journal.principal("router@shared").approvals.approval_card(
            room_id="!room:test",
            card_event_id=card,
        )
        assert isinstance(view, ApprovalCardView)
        assert view.current is False
        assert (await _approve(manager, card, seconds=None)).consumed is False
        assert await _call_row(journal, "fenced-room") == (None, None)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_an_active_grant_does_not_approve_a_call_already_past_its_deadline(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """A call reserved after its deadline gets its own card, which the deadline then expires."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        assert (await _approve(manager, await _card(journal, manager, "granted"))).consumed
        late = await _card(journal, manager, "past-deadline", expires_at_ns=1)
        # The deadline sweep the card's publication woke may already be expiring it; one more pass settles it either way.
        await manager.recover_cards_on_startup()
        card = await journal.principal("router@shared").approvals.approval_card(
            room_id="!room:test",
            card_event_id=late,
        )
        assert card is not None
        assert card.kind == "request"
        assert card.retired
        assert await _call_row(journal, "past-deadline") == ("expired", None)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_grant_receipt_and_its_decision_judge_the_deadline_at_one_instant(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call the grant covered just before its deadline is approved, even when the decision is written after it."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        assert (await _approve(manager, await _card(journal, manager, "granted"))).consumed
        deadline = time.time_ns() + 60 * 1_000_000_000
        monkeypatch.setattr(approval_grants, "time", SimpleNamespace(time_ns=lambda: deadline - 1))
        monkeypatch.setattr(approval_calls, "time", SimpleNamespace(time_ns=lambda: deadline + 1))
        await _card(journal, manager, "at-deadline", expires_at_ns=deadline)
        assert await _call_row(journal, "at-deadline") == ("approved", "@human:test")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_only_the_requester_can_open_a_timed_grant_on_a_card(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """The journal refuses a grant from anyone but the requester the card's consent names."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        card = await _card(journal, manager, "consent")
        decisions = await journal.principal("router@shared").approvals.create_approval_grant(
            room_id="!room:test",
            card_event_id=card,
            sender_id="@intruder:test",
            seconds=600,
            metadata=ApprovalDecisionMetadata(resolved_by="@intruder:test"),
        )
        assert decisions == ()
        assert await _call_row(journal, "consent") == (None, None)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_expiring_one_paused_replys_cards_leaves_the_others_waiting(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
) -> None:
    """Failure settlement expires only the cards of the paused reply that failed."""
    journal = journal_database()
    manager = _manager(journal, tmp_path)
    try:
        await _card(journal, manager, "failed")
        await _card(journal, manager, "still-waiting")
        assert await manager.expire_continuation_cards("failed", "The reply was stopped.")
        assert (await _call_row(journal, "failed"))[0] == "expired"
        assert await _call_row(journal, "still-waiting") == (None, None)
    finally:
        await manager.shutdown()


@pytest.mark.parametrize(
    ("toolkit_name", "tool_name", "actions"),
    [
        ("gmail", "gmail_read", ("latest", "search_emails", "get_draft")),
        ("gmail", "gmail_draft", ("create", "update")),
        ("gmail", "gmail_send", ("send", "reply")),
        ("gmail", "gmail_manage", ("star", "apply_label", "delete_custom_label")),
        ("google_calendar", "google_calendar_read", ("list_events", "check_availability")),
        ("google_calendar", "google_calendar_write", ("create_event", "delete_event")),
        ("github", "github_read_repo", ("list_repositories", "get_file_content")),
        ("github", "github_read_issues_prs", ("list_issues", "get_pull_request")),
        ("github", "github_write_issues", ("create_issue", "close_issue")),
        ("github", "github_write_pr", ("create_pull_request", "create_review_request")),
        ("github", "github_write_repo", ("create_file", "delete_repository")),
        ("google_drive", "google_drive_read", ("list", "download")),
        ("google_drive", "google_drive_write", ("upload", "trash")),
        ("google_sheets", "google_sheets_write", ("create", "update", "batch_update")),
        ("homeassistant", "homeassistant_read", ("list_entities", "get_state")),
        ("homeassistant", "homeassistant_control", ("turn_on", "call_service")),
    ],
)
def test_wave3b_registry_action_scoped_grants_bind_per_action(
    toolkit_name: str,
    tool_name: str,
    actions: tuple[str, ...],
) -> None:
    """Consolidated Wave 3b tools bind timed approval grants to their action via registry metadata."""
    from mindroom.tool_system.registry import builtin_tool_registry  # noqa: PLC0415

    registry = builtin_tool_registry()
    assert tool_name in registry.entries[toolkit_name].action_scoped_grants
    config = Config()
    ops = [
        grant_operation(config, tool_name, {"action": action}, tools=registry, toolkit_name=toolkit_name)
        for action in actions
    ]
    assert all(op is not None for op in ops)
    assert len({op.key for op in ops if op is not None}) == len(actions)
    assert grant_operation(config, tool_name, {}, tools=registry, toolkit_name=toolkit_name) is None
    assert grant_operation(config, tool_name, {"action": ""}, tools=registry, toolkit_name=toolkit_name) is None


@pytest.mark.parametrize(
    ("toolkit_name", "tool_name"),
    [
        ("gmail", "gmail_send_to_self"),
        ("google_sheets", "google_sheets_read"),
    ],
)
def test_wave3b_single_operation_tools_keep_tool_wide_grants(toolkit_name: str, tool_name: str) -> None:
    """Single-operation Wave 3b tools without an action enum use normal tool-wide grants."""
    from mindroom.tool_system.registry import builtin_tool_registry  # noqa: PLC0415

    registry = builtin_tool_registry()
    assert tool_name not in registry.entries[toolkit_name].action_scoped_grants
    op = grant_operation(Config(), tool_name, {}, tools=registry, toolkit_name=toolkit_name)
    assert op is not None
    assert op.tool_name == tool_name
    assert op.mcp_server_id is None
    assert op.mcp_tool_name is None


def test_secondary_google_workspace_retired_approval_rules_fail_closed(tmp_path: Path) -> None:
    """Prefixed secondary Google Workspace plugin clones inherit both fail-closed rules and action-scoped grants."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415
    from mindroom.tool_system.plugins import refresh_plugins  # noqa: PLC0415

    plugin_root = tmp_path / "plugins" / "work_google"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps(
            {
                "name": "work_google",
                "mindroom_plugin_api": "1.35",
                "tools_module": "tools.py",
                "oauth_module": "oauth.py",
            },
        ),
        encoding="utf-8",
    )
    workspace_def = (
        "from mindroom.tool_system.google_workspaces import GoogleWorkspaceConfig\n"
        "WORKSPACE = GoogleWorkspaceConfig(\n"
        "    name='work',\n"
        "    display_name='Work Google',\n"
        "    client_config_service='work_google_oauth_client',\n"
        "    allowed_hosted_domains=('work.example',),\n"
        "    services=('gmail',),\n"
        ")\n"
    )
    (plugin_root / "oauth.py").write_text(
        workspace_def
        + "from mindroom.tool_system.google_workspaces import google_workspace_oauth_providers\n"
        + "def register_oauth_providers():\n"
        + "    return google_workspace_oauth_providers(WORKSPACE)\n",
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        workspace_def
        + "from mindroom.tool_system.google_workspaces import register_google_workspace_tools\n"
        + "register_google_workspace_tools(WORKSPACE)\n",
        encoding="utf-8",
    )

    runtime_paths = test_runtime_paths(tmp_path)
    base_data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "plugins": [str(plugin_root)],
        "agents": {"a": {"display_name": "A", "tools": ["work_gmail"], "include_default_tools": False}},
    }
    with pytest.raises(ConfigRuntimeValidationError, match="work_gmail_send"):
        Config.validate_with_runtime(
            {**base_data, "tool_approval": {"rules": [{"match": "work_send_email", "action": "require_approval"}]}},
            runtime_paths,
        )

    loaded = Config.validate_with_runtime(
        {**base_data, "tool_approval": {"rules": [{"match": "work_gmail_send", "action": "require_approval"}]}},
        runtime_paths,
    )
    registry = refresh_plugins(loaded, runtime_paths).tools
    assert "work_gmail_manage" in registry.entries["work_gmail"].action_scoped_grants
    star_op = grant_operation(
        loaded,
        "work_gmail_manage",
        {"action": "star"},
        tools=registry,
        toolkit_name="work_gmail",
    )
    del_op = grant_operation(
        loaded,
        "work_gmail_manage",
        {"action": "delete_custom_label"},
        tools=registry,
        toolkit_name="work_gmail",
    )
    assert star_op is not None
    assert del_op is not None
    assert star_op != del_op


def test_attachment_action_scoped_grants_when_implied_by_matrix_message_and_openclaw_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Action-scoped grants and scheduled_any_arguments stay per-action when attachments is implied or preset."""
    from mindroom.approval.tool_approval import scheduled_call_offers_any_arguments  # noqa: PLC0415

    config = Config.model_validate({"tool_approval": {"scheduled_any_arguments": True}})
    registry = builtin_tool_registry()

    for toolkit_name, tools_arg in (
        ("attachments", registry),
        ("matrix_message", registry),
        ("openclaw_compat", registry),
        ("matrix_message", None),
        (None, None),
    ):
        list_op = grant_operation(
            config,
            "attachment",
            {"action": "list"},
            tools=tools_arg,
            toolkit_name=toolkit_name,
        )
        inline_op = grant_operation(
            config,
            "attachment",
            {"action": "inline_image"},
            tools=tools_arg,
            toolkit_name=toolkit_name,
        )
        assert list_op is not None
        assert inline_op is not None
        assert list_op.key != inline_op.key
        assert list_op.mcp_server_id == "attachment"
        assert list_op.mcp_tool_name == "list"
        assert inline_op.mcp_tool_name == "inline_image"
        assert grant_operation(config, "attachment", {}, tools=tools_arg, toolkit_name=toolkit_name) is None
        assert (
            scheduled_call_offers_any_arguments(
                config,
                "attachment",
                {"action": "list"},
                requester_id="@alice:example.org",
                approver_id="@alice:example.org",
                authored_confirmation=False,
                tools=tools_arg,
                toolkit_name=toolkit_name,
            )
            is False
        )
        assert (
            scheduled_call_offers_any_arguments(
                config,
                "attachment",
                {"action": "inline_image"},
                requester_id="@alice:example.org",
                approver_id="@alice:example.org",
                authored_confirmation=False,
                tools=tools_arg,
                toolkit_name=toolkit_name,
            )
            is False
        )

    # Standalone view_file keeps normal tool-wide scheduled_any_arguments behavior.
    assert (
        scheduled_call_offers_any_arguments(
            config,
            "view_file",
            {"path": "chart.png"},
            requester_id="@alice:example.org",
            approver_id="@alice:example.org",
            authored_confirmation=False,
            tools=registry,
            toolkit_name="matrix_message",
        )
        is True
    )

    # Implier fallback in _action_scoped_tool also resolves non-_ACTION_SCOPED_TOOLS action-scoped owners.
    monkeypatch.setitem(Config.IMPLIED_TOOLS, "matrix_message", ("attachments", "matrix_room", "gmail"))
    star_op = grant_operation(
        config,
        "gmail_manage",
        {"action": "star"},
        tools=registry,
        toolkit_name="matrix_message",
    )
    delete_op = grant_operation(
        config,
        "gmail_manage",
        {"action": "delete_custom_label"},
        tools=registry,
        toolkit_name="matrix_message",
    )
    assert star_op is not None
    assert delete_op is not None
    assert star_op.mcp_server_id == "gmail"
    assert star_op.key != delete_op.key
    assert (
        scheduled_call_offers_any_arguments(
            config,
            "gmail_manage",
            {"action": "star"},
            requester_id="@alice:example.org",
            approver_id="@alice:example.org",
            authored_confirmation=False,
            tools=registry,
            toolkit_name="matrix_message",
        )
        is False
    )


def test_wildcard_approval_script_rule_fails_closed_for_retired_attachment_and_skill_names(tmp_path: Path) -> None:
    """A wildcard approval script rule fails config load until explicit replacement rules are authored."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")

    # Even with no tools enabled, unconditional skills validation rejects a wildcard script rule.
    skills_only_data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": []},
        "agents": {"a": {"display_name": "A", "tools": [], "include_default_tools": False}},
        "tool_approval": {"rules": [{"match": "*", "script": "check.py"}]},
    }
    with pytest.raises(ConfigRuntimeValidationError, match=r"check\.py.*skill"):
        Config.validate_with_runtime(skills_only_data, runtime_paths)

    loaded_skills = Config.validate_with_runtime(
        {
            **skills_only_data,
            "tool_approval": {
                "rules": [
                    {"match": "skill", "script": "check.py"},
                    {"match": "*", "script": "check.py"},
                ],
            },
        },
        runtime_paths,
    )
    assert [rule.match for rule in loaded_skills.tool_approval.rules] == ["skill", "*"]

    # With attachments enabled, wildcard script rule fails until both `attachment` and `skill` have explicit rules first.
    attachments_data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": []},
        "agents": {"a": {"display_name": "A", "tools": ["attachments"], "include_default_tools": False}},
        "tool_approval": {"rules": [{"match": "*", "script": "check.py"}]},
    }
    with pytest.raises(ConfigRuntimeValidationError, match=r"check\.py.*attachment"):
        Config.validate_with_runtime(attachments_data, runtime_paths)

    # Putting the wildcard rule before the explicit replacement rule still fails because rule 0 shadows rule 1.
    with pytest.raises(ConfigRuntimeValidationError, match=r"check\.py.*attachment"):
        Config.validate_with_runtime(
            {
                **attachments_data,
                "tool_approval": {
                    "rules": [
                        {"match": "*", "script": "check.py"},
                        {"match": "attachment", "script": "check.py"},
                        {"match": "skill", "script": "check.py"},
                    ],
                },
            },
            runtime_paths,
        )

    loaded_both = Config.validate_with_runtime(
        {
            **attachments_data,
            "tool_approval": {
                "rules": [
                    {"match": "attachment", "script": "check.py"},
                    {"match": "skill", "script": "check.py"},
                    {"match": "*", "script": "check.py"},
                ],
            },
        },
        runtime_paths,
    )
    assert [rule.match for rule in loaded_both.tool_approval.rules] == ["attachment", "skill", "*"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"exclude_tools": ["register_attachment"]},
        {"include_tools": ["get_attachment"]},
        {"exclude_tools": ["list_attachments"]},
        {"include_tools": ["inline_image"]},
        {"exclude_tools": "register_attachment,inline_image"},
    ],
)
def test_retired_attachments_include_exclude_overrides_fail_config_load(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    """include_tools/exclude_tools naming retired attachment functions fail fast at config load with guidance."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {"a": {"display_name": "A", "tools": [{"attachments": overrides}], "include_default_tools": False}},
    }
    with pytest.raises(
        ConfigRuntimeValidationError,
        match=r"attachments\.(in|ex)clude_tools: .* was removed; .*attachment.*view_file",
    ):
        Config.validate_with_runtime(data, runtime_paths)

    valid = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "agents": {
                "a": {
                    "display_name": "A",
                    "tools": [{"attachments": {"exclude_tools": ["view_file"]}}],
                    "include_default_tools": False,
                },
            },
        },
        runtime_paths,
    )
    assert valid.agents["a"].tools[0].overrides == {"exclude_tools": ["view_file"]}
