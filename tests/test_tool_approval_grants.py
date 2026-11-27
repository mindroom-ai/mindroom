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
        ("triggers", "triggers_read", ("list", "history", "test")),
        ("triggers", "triggers_write", ("delete", "rotate_key", "set_enabled")),
        ("desktop", "desktop_read", ("status", "list_apps", "read_file")),
        ("desktop", "desktop_control", ("launch_app", "click", "type_text")),
        ("browser", "browser_read", ("status", "tabs", "snapshot")),
        ("browser", "browser_control", ("start", "open", "act")),
        ("matrix_api", "matrix_api_read", ("get_event", "get_state", "search")),
        ("matrix_api", "matrix_api_write", ("send_event", "put_state", "redact")),
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
        ("triggers", "triggers_read", "list"),
        ("triggers", "triggers_write", "delete"),
        ("desktop", "desktop_read", "status"),
        ("desktop", "desktop_control", "click"),
        ("browser", "browser_read", "tabs"),
        ("browser", "browser_control", "open"),
        ("matrix_api", "matrix_api_read", "search"),
        ("matrix_api", "matrix_api_write", "send_event"),
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
        ("workflow", "start_workflow", "workflow_write"),
        ("workflow", "get_workflow", "workflow_read"),
        ("workflow", "cancel_workflow", "workflow_write"),
        ("workflow", "resume_workflow", "workflow_write"),
        ("workflow", "list_workflow_runs", "workflow_read"),
        ("workflow", "save_workflow", "workflow_write"),
        ("thread_tags", "tag_thread", "thread_tags"),
        ("thread_tags", "untag_thread", "thread_tags"),
        ("thread_tags", "list_thread_tags", "thread_tags"),
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
        ("google_docs", "google_docs_get_document", "google_docs_read"),
        ("google_docs", "google_docs_create_document", "google_docs_write"),
        ("google_docs", "google_docs_insert_text", "google_docs_write"),
        ("google_docs", "google_docs_replace_text", "google_docs_write"),
        ("google_docs", "google_docs_batch_update", "google_docs_write"),
        ("google_sheets", "create_sheet", "google_sheets_write"),
        ("google_sheets", "create_duplicate_sheet", "google_sheets_write"),
        ("google_sheets", "read_sheet", "google_sheets_read"),
        ("google_tasks", "google_tasks_list_task_lists", "google_tasks_read"),
        ("google_tasks", "google_tasks_list_tasks", "google_tasks_read"),
        ("google_tasks", "google_tasks_create_task", "google_tasks_write"),
        ("google_tasks", "google_tasks_update_task", "google_tasks_write"),
        ("google_tasks", "google_tasks_delete_task", "google_tasks_write"),
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
        ("twilio", "send_sms", "twilio_send"),
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
        (None, "get_skill_instructions", "skill"),
        (None, "get_skill_reference", "skill"),
        (None, "get_skill_script", "skill"),
        ("script", "start_script", "script_write"),
        ("script", "cancel_script", "script_write"),
        ("script", "get_script", "script_read"),
        ("script", "list_scripts", "script_read"),
        ("script", "get_script_resource_profiles", "script_read"),
        ("terminal", "terminal_open", "terminal_write"),
        ("terminal", "terminal_run", "terminal_write"),
        ("terminal", "terminal_send", "terminal_write"),
        ("terminal", "terminal_list", "terminal_read"),
        ("claude_agent", "claude_session_status", "claude_agent_read"),
        ("claude_agent", "claude_agent_status", "claude_agent_read"),
        ("claude_agent", "claude_start_session", "claude_agent_write"),
        ("claude_agent", "claude_send", "claude_agent_write"),
        ("claude_agent", "claude_interrupt", "claude_agent_write"),
        ("claude_agent", "claude_end_session", "claude_agent_write"),
        ("thread_model", "list_models", "thread_model"),
        ("thread_model", "get_thread_model", "thread_model"),
        ("thread_model", "switch_thread_model", "thread_model"),
        ("thread_model", "reset_thread_model", "thread_model"),
        ("thread_model", "thread_model_read", "thread_model"),
        ("thread_model", "thread_model_write", "thread_model"),
        ("thread_snooze", "snooze_thread", "thread_snooze"),
        ("thread_snooze", "unsnooze_thread", "thread_snooze"),
        ("thread_resolution", "resolve_thread", "thread_resolution"),
        ("thread_resolution", "reopen_thread", "thread_resolution"),
        ("serper", "serper_search", "serper"),
        ("serper", "scrape_webpage", "serper"),
        ("hackernews", "get_top_hackernews_stories", "hackernews"),
        ("hackernews", "get_user_details", "hackernews"),
        ("google_bigquery", "list_tables", "google_bigquery_inspect"),
        ("google_bigquery", "describe_table", "google_bigquery_inspect"),
        ("usage_stats", "get_my_usage", "usage_stats"),
        ("usage_stats", "get_my_private_usage", "usage_stats"),
        ("usage_stats", "get_all_usage", "usage_stats"),
    ],
)
def test_retired_consolidated_approval_rules_fail_closed(
    tmp_path: Path,
    toolkit: str | None,
    old_name: str,
    new_name: str,
) -> None:
    """Approval rules targeting retired consolidated function names must fail config load instead of failing open."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    options = {"searxng": {"host": "https://search.example.test"}, "telegram": {"chat_id": "1234"}}
    tools_list: list[object] = (
        [] if toolkit is None else [{toolkit: options[toolkit]} if toolkit in options else toolkit]
    )
    stale_data = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {"a": {"display_name": "A", "tools": tools_list, "include_default_tools": False}},
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
                {"match": "serper", "action": "auto_approve"},
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
        "serper",
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
        function_name="read_url",
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
                    "tools": ["website", "custom_plugin"],
                    "include_default_tools": False,
                },
            },
            "tool_approval": {"scheduled_any_arguments": True},
        },
        runtime_paths,
    )
    registry = refresh_plugins(config, runtime_paths).tools
    assert "website" in registry.entries
    assert "custom_plugin" in registry.entries
    assert "read_url" not in registry.entries["website"].action_scoped_grants
    assert "read_url" in registry.entries["custom_plugin"].action_scoped_grants

    # Another toolkit exposing the same function name keeps normal tool-wide grants and scheduled any-arguments.
    website_op = grant_operation(
        config,
        "read_url",
        {"url": "https://example.com"},
        tools=registry,
        toolkit_name="website",
    )
    assert website_op is not None
    assert website_op.tool_name == "read_url"
    assert website_op.mcp_server_id is None
    assert website_op.mcp_tool_name is None
    assert scheduled_call_offers_any_arguments(
        config,
        "read_url",
        {"url": "https://example.com"},
        requester_id="@human:test",
        approver_id="@human:test",
        authored_confirmation=False,
        tools=registry,
        toolkit_name="website",
    )

    # The owning plugin gets per-action scoping and fails closed when action is missing.
    plugin_op = grant_operation(
        config,
        "read_url",
        {"action": "read"},
        tools=registry,
        toolkit_name="custom_plugin",
    )
    assert plugin_op is not None
    assert plugin_op.tool_name == "read_url"
    assert plugin_op.mcp_server_id == "custom_plugin"
    assert plugin_op.mcp_tool_name == "read"
    assert (
        grant_operation(
            config,
            "read_url",
            {"url": "https://example.com"},
            tools=registry,
            toolkit_name="custom_plugin",
        )
        is None
    )
    assert not scheduled_call_offers_any_arguments(
        config,
        "read_url",
        {"action": "read"},
        requester_id="@human:test",
        approver_id="@human:test",
        authored_confirmation=False,
        tools=registry,
        toolkit_name="custom_plugin",
    )

    # timed_grant_operation threads call.toolkit_name into grant_operation.
    website_tool = ToolExecution(
        tool_call_id="call-website",
        tool_name="read_url",
        tool_args={"url": "https://example.com"},
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )
    website_call = ApprovalCall(
        tool_call_id="call-website",
        tool_name="read_url",
        invoking_agent="code",
        expires_at_ns=9_000_000_000_000_000_000,
        toolkit_name="website",
    )
    assert timed_grant_operation(config, website_tool, website_call, tools=registry) == website_op

    plugin_tool = ToolExecution(
        tool_call_id="call-plugin",
        tool_name="read_url",
        tool_args={"action": "read"},
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )
    plugin_call = ApprovalCall(
        tool_call_id="call-plugin",
        tool_name="read_url",
        invoking_agent="code",
        expires_at_ns=9_000_000_000_000_000_000,
        toolkit_name="custom_plugin",
    )
    assert timed_grant_operation(config, plugin_tool, plugin_call, tools=registry) == plugin_op
    assert timed_grant_operation(config, website_tool, plugin_call, tools=registry) is None


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


def test_plugin_named_like_builtin_approval_target_does_not_inherit_builtin_probe(tmp_path: Path) -> None:
    """A plugin toolkit named `slack_watch` validates cleanly without inheriting built-in `slack_watch` probe rules."""
    plugin_root = _write_consolidated_plugin(
        tmp_path,
        plugin_name="slack_watch",
        function_name="poll",
        retired_name="old_poll",
    )
    runtime_paths = test_runtime_paths(tmp_path)
    loaded = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "plugins": [{"path": str(plugin_root)}],
            "agents": {"a": {"display_name": "A", "tools": ["slack_watch"], "include_default_tools": False}},
            "tool_approval": {
                "default": "auto_approve",
                "rules": [
                    {"match": "old_poll", "action": "require_approval"},
                    {"match": "poll", "action": "auto_approve"},
                ],
            },
        },
        runtime_paths,
    )
    assert [rule.match for rule in loaded.tool_approval.rules] == ["old_poll", "poll"]


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
        ("google_docs", "google_docs_write", ("create", "insert_text", "replace_text", "batch_update")),
        ("google_sheets", "google_sheets_write", ("create", "update", "batch_update")),
        ("google_tasks", "google_tasks_read", ("list_task_lists", "list_tasks")),
        ("google_tasks", "google_tasks_write", ("create", "update", "delete")),
        ("homeassistant", "homeassistant_read", ("list_entities", "get_state")),
        ("homeassistant", "homeassistant_control", ("turn_on", "call_service")),
        ("workflow", "workflow_read", ("list", "get")),
        ("workflow", "workflow_write", ("start", "cancel", "resume", "save")),
        ("thread_tags", "thread_tags", ("tag", "untag", "list")),
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
        ("google_docs", "google_docs_read"),
        ("twilio", "twilio_send"),
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
        "    services=('gmail', 'google_docs', 'google_tasks'),\n"
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
        "agents": {
            "a": {
                "display_name": "A",
                "tools": ["work_gmail", "work_google_docs", "work_google_tasks"],
                "include_default_tools": False,
            },
        },
    }
    for retired_match, replacement in (
        ("work_send_email", "work_gmail_send"),
        ("work_google_docs_get_document", "work_google_docs_read"),
        ("work_google_docs_batch_update", "work_google_docs_write"),
        ("work_google_tasks_delete_task", "work_google_tasks_write"),
    ):
        with pytest.raises(ConfigRuntimeValidationError, match=replacement):
            Config.validate_with_runtime(
                {**base_data, "tool_approval": {"rules": [{"match": retired_match, "action": "require_approval"}]}},
                runtime_paths,
            )

    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")
    with pytest.raises(ConfigRuntimeValidationError, match=r"check\.py.*work_google_docs_read"):
        Config.validate_with_runtime(
            {**base_data, "tool_approval": {"rules": [{"match": "work_google_docs_*", "script": "check.py"}]}},
            runtime_paths,
        )
    with_explicit_script = Config.validate_with_runtime(
        {
            **base_data,
            "tool_approval": {
                "rules": [
                    {"match": "work_google_docs_read", "script": "check.py"},
                    {"match": "work_google_docs_write", "script": "check.py"},
                    {"match": "work_google_docs_*", "script": "check.py"},
                ],
            },
        },
        runtime_paths,
    )
    assert [rule.match for rule in with_explicit_script.tool_approval.rules] == [
        "work_google_docs_read",
        "work_google_docs_write",
        "work_google_docs_*",
    ]
    # Non-strict Google services (gmail) must not be forced into strict wildcard script rejection.
    gmail_only = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "plugins": [str(plugin_root)],
            "agents": {"a": {"display_name": "A", "tools": ["work_gmail"], "include_default_tools": False}},
            "tool_approval": {"rules": [{"match": "work_*", "script": "check.py"}]},
        },
        runtime_paths,
    )
    assert [rule.match for rule in gmail_only.tool_approval.rules] == ["work_*"]

    # Secondary Google Workspace include_tools/exclude_tools filters fail closed with accurate guidance.
    for filter_key in ("include_tools", "exclude_tools"):
        with pytest.raises(ConfigRuntimeValidationError) as exc_info:
            Config.validate_with_runtime(
                {
                    "models": {"default": {"provider": "openai", "id": "x"}},
                    "plugins": [str(plugin_root)],
                    "agents": {
                        "a": {
                            "display_name": "A",
                            "tools": [{"work_google_docs": {filter_key: ["work_google_docs_get_document"]}}],
                            "include_default_tools": False,
                        },
                    },
                },
                runtime_paths,
            )
        msg = str(exc_info.value)
        assert (
            f"replace `work_google_docs_get_document` with `work_google_docs_read` in `work_google_docs.{filter_key}` "
            "while preserving any other entries in the list"
        ) in msg
        guidance = msg.split("was removed; ", 1)[1]
        assert "action" not in guidance
        assert "remove" not in guidance

    with pytest.raises(ConfigRuntimeValidationError) as batch_exc_info:
        Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "x"}},
                "plugins": [str(plugin_root)],
                "agents": {
                    "a": {
                        "display_name": "A",
                        "tools": [{"work_google_docs": {"exclude_tools": ["work_google_docs_batch_update"]}}],
                        "include_default_tools": False,
                    },
                },
            },
            runtime_paths,
        )
    batch_msg = str(batch_exc_info.value)
    assert "replace `work_google_docs_batch_update` with `work_google_docs_write`" in batch_msg
    assert "removes all sibling actions in `work_google_docs_write`" in batch_msg

    loaded = Config.validate_with_runtime(
        {**base_data, "tool_approval": {"rules": [{"match": "work_gmail_send", "action": "require_approval"}]}},
        runtime_paths,
    )
    registry = refresh_plugins(loaded, runtime_paths).tools
    assert "work_gmail_manage" in registry.entries["work_gmail"].action_scoped_grants
    assert registry.entries["work_google_docs"].action_scoped_grants == ("work_google_docs_write",)
    assert registry.entries["work_google_tasks"].action_scoped_grants == (
        "work_google_tasks_read",
        "work_google_tasks_write",
    )
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

    # Stored dashboard settings naming retired prefixed Google Docs functions also fail closed.
    from mindroom.credentials.manager import get_runtime_credentials_manager  # noqa: PLC0415
    from mindroom.tool_system.catalog import ToolConfigOverrideError  # noqa: PLC0415
    from mindroom.tool_system.metadata import get_tool_by_name  # noqa: PLC0415

    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "work_google_docs",
        {"_source": "ui", "exclude_tools": ["work_google_docs_get_document"]},
    )
    with pytest.raises(
        ToolConfigOverrideError,
        match=(
            r"work_google_docs: stored setting exclude_tools: work_google_docs_get_document was removed; "
            r"replace `work_google_docs_get_document` with `work_google_docs_read` in `work_google_docs\.exclude_tools`"
        ),
    ):
        get_tool_by_name(
            "work_google_docs",
            runtime_paths,
            tools=registry,
            credentials_manager=manager,
            worker_target=None,
        )


def test_attachment_action_scoped_grants_when_implied_by_matrix_message_and_openclaw_compat() -> None:
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
        ops = {}
        for action in ("list", "inline_image"):
            op = grant_operation(
                config,
                "attachment",
                {"action": action},
                tools=tools_arg,
                toolkit_name=toolkit_name,
            )
            assert op is not None
            assert op.mcp_server_id == "attachment"
            assert op.mcp_tool_name == action
            assert (
                scheduled_call_offers_any_arguments(
                    config,
                    "attachment",
                    {"action": action},
                    requester_id="@alice:example.org",
                    approver_id="@alice:example.org",
                    authored_confirmation=False,
                    tools=tools_arg,
                    toolkit_name=toolkit_name,
                )
                is False
            )
            ops[action] = op
        assert ops["list"].key != ops["inline_image"].key
        assert grant_operation(config, "attachment", {}, tools=tools_arg, toolkit_name=toolkit_name) is None

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


@pytest.mark.parametrize(
    ("tools", "matches", "error_pattern"),
    [
        ([], ["*"], r"check\.py.*skill"),
        ([], ["skill", "*"], None),
        (["attachments"], ["*"], r"check\.py.*attachment"),
        (["attachments"], ["*", "attachment", "skill"], r"check\.py.*attachment"),
        (["attachments"], ["attachment", "skill", "*"], None),
        (["memory"], ["*memor*"], None),
    ],
)
def test_wildcard_approval_script_rule_fails_closed_for_retired_attachment_and_skill_names(
    tmp_path: Path,
    tools: list[str],
    matches: list[str],
    error_pattern: str | None,
) -> None:
    """Wildcard approval scripts fail closed for retired skills/attachments names until explicit rules precede them."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "agents": {"a": {"display_name": "A", "tools": tools, "include_default_tools": False}},
        "tool_approval": {"rules": [{"match": match, "script": "check.py"} for match in matches]},
    }
    if error_pattern is not None:
        with pytest.raises(ConfigRuntimeValidationError, match=error_pattern):
            Config.validate_with_runtime(data, runtime_paths)
        return
    loaded = Config.validate_with_runtime(data, runtime_paths)
    assert [rule.match for rule in loaded.tool_approval.rules] == matches


def test_wildcard_approval_script_with_shared_conditional_prefix_for_retired_skill_names(tmp_path: Path) -> None:
    """A script-free shared prefix ahead of explicit `skill` loads, while a leading wildcard script still fails closed."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    (tmp_path / "review.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")
    base: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "agents": {"a": {"display_name": "A", "tools": [], "include_default_tools": False}},
    }

    allowed = {
        **base,
        "tool_approval": {
            "rules": [
                {"match": "*", "agents": "trusted", "action": "require_approval"},
                {"match": "skill", "action": "auto_approve"},
                {"match": "*", "script": "review.py"},
            ],
        },
    }
    assert len(Config.validate_with_runtime(allowed, runtime_paths).tool_approval.rules) == 3

    shared_argument_prefix = {
        **base,
        "tool_approval": {
            "rules": [
                {"match": "*", "arguments": {"mindroom_output_path": "*"}, "action": "require_approval"},
                {"match": "skill", "action": "auto_approve"},
                {"match": "*", "script": "review.py"},
            ],
        },
    }
    assert len(Config.validate_with_runtime(shared_argument_prefix, runtime_paths).tool_approval.rules) == 3

    leading_argument_script = {
        **base,
        "tool_approval": {
            "rules": [
                {"match": "*", "arguments": {"mindroom_output_path": "*"}, "script": "review.py"},
                {"match": "skill", "action": "auto_approve"},
            ],
        },
    }
    with pytest.raises(ConfigRuntimeValidationError, match=r"review\.py.*skill"):
        Config.validate_with_runtime(leading_argument_script, runtime_paths)

    leading_script = {
        **base,
        "tool_approval": {
            "rules": [
                {"match": "*", "agents": "trusted", "script": "review.py"},
                {"match": "skill", "action": "auto_approve"},
            ],
        },
    }
    with pytest.raises(ConfigRuntimeValidationError, match=r"review\.py.*skill"):
        Config.validate_with_runtime(leading_script, runtime_paths)

    grouped_script = {
        **base,
        "tool_approval": {
            "rules": [{"match": ["skill", "get_*"], "script": "review.py"}],
        },
    }
    assert len(Config.validate_with_runtime(grouped_script, runtime_paths).tool_approval.rules) == 1

    agent_scoped_explicit_and_retired = {
        **base,
        "tool_approval": {
            "rules": [
                {"match": "skill", "agents": "research", "script": "review.py"},
                {"match": "get_*", "agents": "research", "script": "review.py"},
            ],
        },
    }
    assert len(Config.validate_with_runtime(agent_scoped_explicit_and_retired, runtime_paths).tool_approval.rules) == 2

    agent_scoped_explicit_action_and_retired_script = {
        **base,
        "tool_approval": {
            "rules": [
                {"match": "skill", "agents": "research", "action": "require_approval"},
                {"match": "get_*", "agents": "research", "script": "review.py"},
            ],
        },
    }
    assert (
        len(
            Config.validate_with_runtime(
                agent_scoped_explicit_action_and_retired_script,
                runtime_paths,
            ).tool_approval.rules,
        )
        == 2
    )

    agent_scoped_explicit_before_unconditional_wildcard = {
        **base,
        "tool_approval": {
            "rules": [
                {"match": "skill", "agents": "research", "action": "auto_approve"},
                {"match": "*", "script": "review.py"},
            ],
        },
    }
    with pytest.raises(ConfigRuntimeValidationError, match=r"review\.py.*skill"):
        Config.validate_with_runtime(agent_scoped_explicit_before_unconditional_wildcard, runtime_paths)


@pytest.mark.parametrize(
    ("tools", "matches", "error_pattern"),
    [
        (["serper"], ["skill", "*"], r"check\.py.*serper"),
        (["serper"], ["serper", "skill", "*"], None),
        (["hackernews"], ["skill", "*"], r"check\.py.*hackernews"),
        (["hackernews"], ["hackernews", "skill", "*"], None),
        (["google_bigquery"], ["skill", "*"], r"check\.py.*google_bigquery_inspect"),
        (["google_bigquery"], ["google_bigquery_inspect", "skill", "*"], None),
        (["browserbase"], ["skill", "*"], r"check\.py.*browserbase"),
        (["browserbase"], ["browserbase", "skill", "*"], None),
    ],
)
def test_wildcard_approval_script_rule_fails_closed_for_wave2_and_wave4_toolkits(
    tmp_path: Path,
    tools: list[str],
    matches: list[str],
    error_pattern: str | None,
) -> None:
    """Wildcard approval scripts fail closed for retired Wave 2/4 function names until explicit rules precede them."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "agents": {"a": {"display_name": "A", "tools": tools, "include_default_tools": False}},
        "tool_approval": {"rules": [{"match": match, "script": "check.py"} for match in matches]},
    }
    if error_pattern is not None:
        with pytest.raises(ConfigRuntimeValidationError, match=error_pattern):
            Config.validate_with_runtime(data, runtime_paths)
        return
    loaded = Config.validate_with_runtime(data, runtime_paths)
    assert [rule.match for rule in loaded.tool_approval.rules] == matches


@pytest.mark.parametrize(
    ("tools", "matches", "error_pattern"),
    [
        (["workflow"], ["*_workflow*"], r"check\.py.*start_workflow.*workflow_write"),
        (["workflow"], ["*workflow*"], r"check\.py.*start_workflow.*workflow_write"),
        (["workflow"], ["*workflow*", "workflow_write"], r"check\.py.*start_workflow.*workflow_write"),
        (["workflow"], ["skill", "*"], r"check\.py.*start_workflow.*workflow_write"),
        (["workflow"], ["workflow_read", "workflow_write", "*_workflow*"], None),
        (["workflow"], ["workflow_read", "workflow_write", "*workflow*"], None),
        (["workflow"], ["workflow_read", "workflow_write", "skill", "*"], None),
        (["thread_tags"], ["*thread_tags*"], r"now decides thread_tags"),
        (["thread_tags"], ["*tag*"], r"check\.py.*tag_thread.*thread_tags"),
        (["thread_tags"], ["*thread*"], r"check\.py.*tag_thread.*thread_tags"),
        (["thread_tags"], ["*thread*", "thread_tags"], r"check\.py.*tag_thread.*thread_tags"),
        (["thread_tags"], ["*thread_tags*", "thread_tags"], r"now decides thread_tags"),
        (["thread_tags"], ["skill", "*"], r"check\.py.*tag_thread.*thread_tags"),
        (["thread_tags"], ["thread_tags", "*thread_tags*"], None),
        (["thread_tags"], ["thread_tags", "*tag*"], None),
        (["thread_tags"], ["thread_tags", "skill", "*"], None),
        (["dynamic_workflow"], ["*_workflow*"], r"check\.py.*run_workflow.*workflow_write"),
        (["dynamic_workflow"], ["*workflow*"], r"check\.py.*run_workflow.*workflow_write"),
        (["dynamic_workflow"], ["run_*"], r"check\.py.*run_workflow.*workflow_write"),
        (["dynamic_workflow"], ["*workflow*", "workflow_write"], r"check\.py.*run_workflow.*workflow_write"),
        (["dynamic_workflow"], ["skill", "*"], r"check\.py.*run_workflow.*workflow_write"),
        (["dynamic_workflow"], ["workflow_read", "workflow_write", "*_workflow*"], None),
        (["dynamic_workflow"], ["workflow_read", "workflow_write", "*workflow*"], None),
        (["dynamic_workflow"], ["workflow_read", "workflow_write", "skill", "*"], None),
    ],
)
def test_wildcard_approval_script_rule_fails_closed_for_retired_workflow_and_thread_tags_names(
    tmp_path: Path,
    tools: list[str],
    matches: list[str],
    error_pattern: str | None,
) -> None:
    """Wildcard approval scripts fail closed for retired workflow/thread_tags/dynamic_workflow names until preceded by exact rules."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "agents": {"a": {"display_name": "A", "tools": tools, "include_default_tools": False}},
        "tool_approval": {"rules": [{"match": match, "script": "check.py"} for match in matches]},
    }
    if error_pattern is not None:
        with pytest.raises(ConfigRuntimeValidationError, match=error_pattern):
            Config.validate_with_runtime(data, runtime_paths)
        return
    loaded = Config.validate_with_runtime(data, runtime_paths)
    assert [rule.match for rule in loaded.tool_approval.rules] == matches


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
        match=r"attachments\.(in|ex)clude_tools: .* was removed; .*attachment.*view_file.*tool_approval\.rules",
    ):
        Config.validate_with_runtime(data, runtime_paths)


def test_attachments_exclude_view_file_override_loads_cleanly(tmp_path: Path) -> None:
    """Excluding standalone view_file remains a valid attachments override."""
    runtime_paths = test_runtime_paths(tmp_path)
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


def test_runtime_crud_action_scoped_grants_and_fail_closed_migration(tmp_path: Path) -> None:
    """Consolidated runtime/session/thread CRUD tools enforce action-scoped grants, wildcard scripts, and filter migration."""
    from mindroom.approval.tool_approval import scheduled_call_offers_any_arguments  # noqa: PLC0415
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    registry = builtin_tool_registry()
    config = Config()
    runtime_paths = test_runtime_paths(tmp_path)

    action_cases = (
        ("script", "script_read", "list", "get"),
        ("script", "script_write", "start", "cancel"),
        ("terminal", "terminal_read", "list", "read"),
        ("terminal", "terminal_write", "open", "run"),
        ("claude_agent", "claude_agent_write", "start", "send"),
        ("thread_model", "thread_model", "list", "switch"),
        ("thread_snooze", "thread_snooze", "snooze", "unsnooze"),
        ("thread_resolution", "thread_resolution", "resolve", "reopen"),
        ("hackernews", "hackernews", "top_stories", "user"),
        ("google_bigquery", "google_bigquery_inspect", "list_tables", "describe_table"),
        ("usage_stats", "usage_stats", "self", "all"),
    )
    for toolkit_name, tool_name, first_action, second_action in action_cases:
        assert tool_name in registry.entries[toolkit_name].action_scoped_grants
        op_a = grant_operation(config, tool_name, {"action": first_action}, tools=registry, toolkit_name=toolkit_name)
        op_b = grant_operation(config, tool_name, {"action": second_action}, tools=registry, toolkit_name=toolkit_name)
        assert op_a is not None
        assert op_b is not None
        assert op_a != op_b
        assert (op_a.mcp_server_id, op_a.mcp_tool_name) == (toolkit_name, first_action)
        assert grant_operation(config, tool_name, {}, tools=registry, toolkit_name=toolkit_name) is None
        assert grant_operation(config, tool_name, {"action": "  "}, tools=registry, toolkit_name=toolkit_name) is None
        assert not scheduled_call_offers_any_arguments(
            config,
            tool_name,
            {"action": first_action},
            requester_id="@alice:example.org",
            approver_id="@alice:example.org",
            authored_confirmation=False,
            tools=registry,
            toolkit_name=toolkit_name,
        )

    # Single-verb claude_agent_read has no action= param and is NOT action-scoped.
    assert "claude_agent_read" not in registry.entries["claude_agent"].action_scoped_grants
    status_op = grant_operation(config, "claude_agent_read", {}, tools=registry, toolkit_name="claude_agent")
    assert status_op is not None
    assert (status_op.tool_name, status_op.mcp_server_id, status_op.mcp_tool_name) == (
        "claude_agent_read",
        None,
        None,
    )
    assert scheduled_call_offers_any_arguments(
        config,
        "claude_agent_read",
        {},
        requester_id="@alice:example.org",
        approver_id="@alice:example.org",
        authored_confirmation=False,
        tools=registry,
        toolkit_name="claude_agent",
    )

    # Wildcard approval scripts fail closed for all 5 toolkits until explicit rules precede `*`.
    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")
    wildcard_cases = (
        ("script", ["script_read", "script_write"]),
        ("terminal", ["terminal_read", "terminal_write"]),
        ("claude_agent", ["claude_agent_read", "claude_agent_write"]),
        ("google_docs", ["google_docs_read", "google_docs_write"]),
        ("twilio", ["twilio_send", "twilio_read"]),
        ("thread_model", ["thread_model"]),
        ("thread_snooze", ["thread_snooze"]),
        ("thread_resolution", ["thread_resolution"]),
        ("usage_stats", ["usage_stats"]),
    )
    for toolkit_name, explicit_matches in wildcard_cases:
        stale = {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
            "agents": {"a": {"display_name": "A", "tools": [toolkit_name], "include_default_tools": False}},
            "tool_approval": {
                "rules": [
                    {"match": "skill", "script": "check.py"},
                    {"match": "*", "script": "check.py"},
                ],
            },
        }
        with pytest.raises(ConfigRuntimeValidationError, match=r"check\.py"):
            Config.validate_with_runtime(stale, runtime_paths)
        explicit = {
            **stale,
            "tool_approval": {
                "rules": [
                    {"match": "skill", "script": "check.py"},
                    *[{"match": match, "script": "check.py"} for match in explicit_matches],
                    {"match": "*", "script": "check.py"},
                ],
            },
        }
        loaded = Config.validate_with_runtime(explicit, runtime_paths)
        assert [rule.match for rule in loaded.tool_approval.rules] == ["skill", *explicit_matches, "*"]

    # Retired include_tools/exclude_tools filters fail closed on multi-action consolidated targets.
    retired_filter_cases = (
        ("terminal", {"exclude_tools": ["terminal_open"]}, "terminal_write"),
        ("terminal", {"include_tools": ["terminal_list"]}, "terminal_read"),
        ("claude_agent", {"include_tools": ["claude_send"]}, "claude_agent_write"),
        ("thread_model", {"exclude_tools": ["switch_thread_model"]}, "thread_model"),
        ("thread_model", {"exclude_tools": ["thread_model_write"]}, "thread_model"),
        ("thread_snooze", {"exclude_tools": ["snooze_thread"]}, "thread_snooze"),
        ("thread_resolution", {"exclude_tools": ["resolve_thread"]}, "thread_resolution"),
        ("usage_stats", {"exclude_tools": ["get_all_usage"]}, "usage_stats"),
    )
    for toolkit_name, overrides, replacement in retired_filter_cases:
        bad = {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "agents": {
                "a": {"display_name": "A", "tools": [{toolkit_name: overrides}], "include_default_tools": False},
            },
        }
        expected_verb = "enables" if "include_tools" in overrides else "disables"
        with pytest.raises(
            ConfigRuntimeValidationError,
            match=rf"{toolkit_name}\.(in|ex)clude_tools:.*{replacement}.*{expected_verb} all of `{replacement}`'s actions",
        ):
            Config.validate_with_runtime(bad, runtime_paths)

    # Multi-action twilio/google_docs targets keep their consolidated/enable_* guidance.
    for toolkit_name, overrides, expected_pattern in (
        ("twilio", {"include_tools": ["list_messages"]}, r"twilio\.include_tools:.*twilio_read.*enable_\*"),
        (
            "google_docs",
            {"include_tools": ["google_docs_create_document"]},
            r"google_docs\.include_tools:.*google_docs_write.*`action` parameter",
        ),
    ):
        bad = {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "agents": {
                "a": {"display_name": "A", "tools": [{toolkit_name: overrides}], "include_default_tools": False},
            },
        }
        with pytest.raises(ConfigRuntimeValidationError, match=expected_pattern):
            Config.validate_with_runtime(bad, runtime_paths)

    # Retired filters on toggleable toolkits point to their actual toggle names and tool_approval.rules.
    for toolkit_name, overrides, replacement, expected_toggle in (
        ("serper", {"include_tools": ["scrape_webpage"]}, "serper", "`enable_*` settings"),
        ("hackernews", {"include_tools": ["get_user_details"]}, "hackernews", "`enable_*` settings"),
        (
            "google_bigquery",
            {"include_tools": ["describe_table"]},
            "google_bigquery_inspect",
            "`list_tables`/`describe_table` settings",
        ),
    ):
        bad = {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "agents": {
                "a": {"display_name": "A", "tools": [{toolkit_name: overrides}], "include_default_tools": False},
            },
        }
        with pytest.raises(ConfigRuntimeValidationError, match=replacement) as exc_info:
            Config.validate_with_runtime(bad, runtime_paths)
        message = str(exc_info.value)
        assert expected_toggle in message
        assert "tool_approval.rules" in message
        assert "no per-action allowlist filter" not in message

    # Kept name `terminal_read` in terminal include_tools still loads cleanly.
    valid_terminal = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "agents": {
                "a": {
                    "display_name": "A",
                    "tools": [{"terminal": {"include_tools": ["terminal_read"]}}],
                    "include_default_tools": False,
                },
            },
        },
        runtime_paths,
    )
    assert valid_terminal.agents["a"].tools[0].overrides == {"include_tools": ["terminal_read"]}


def test_identity_access_graph_action_scoped_grants_and_fail_closed_migration(tmp_path: Path) -> None:
    """Action-scoped grants separate actions; retired exact, glob, and wildcard script rules fail closed."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    registry = builtin_tool_registry()
    config = Config()

    for toolkit, fn, first_action, second_action in (
        ("shared_directories", "shared_directories_write", "create", "delete"),
        ("contacts", "contacts_write", "request", "remove"),
        ("knowledge_graph", "knowledge_graph", "search", "delete_fact"),
        ("agent_manager", "agent_manager_write", "create", "delete"),
        ("email_invite", "email_invite_write", "invite", "revoke"),
    ):
        op_a = grant_operation(config, fn, {"action": first_action}, tools=registry, toolkit_name=toolkit)
        op_b = grant_operation(config, fn, {"action": second_action}, tools=registry, toolkit_name=toolkit)
        assert op_a is not None
        assert op_b is not None
        assert op_a.mcp_server_id == toolkit
        assert (op_a.mcp_tool_name, op_b.mcp_tool_name) == (first_action, second_action)
        assert op_a.key != op_b.key
        assert grant_operation(config, fn, {}, tools=registry, toolkit_name=toolkit) is None

    # Single-verb zero-arg reads remain tool-wide grants (not action-scoped).
    for toolkit, fn in (
        ("shared_directories", "shared_directories_read"),
        ("contacts", "contacts_read"),
        ("agent_manager", "agent_settings"),
        ("email_invite", "email_invite_read"),
    ):
        op = grant_operation(config, fn, {}, tools=registry, toolkit_name=toolkit)
        assert op is not None
        assert op.mcp_server_id is None

    runtime_paths = test_runtime_paths(tmp_path)
    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")

    # Exact, glob, and wildcard script approval rules targeting retired names fail closed for all 5 toolkits.
    cases: list[tuple[dict[str, object], str, str, list[str]]] = [
        (
            {"agents": {"a": {"display_name": "A", "tools": ["shared_directories"], "include_default_tools": False}}},
            "delete_shared_directory",
            "*_shared_directory",
            ["shared_directories_read", "shared_directories_write"],
        ),
        (
            {
                "agents": {
                    "a": {"display_name": "A", "tools": ["email_invite"], "include_default_tools": False},
                },
            },
            "invite_by_email",
            "*_email*",
            ["email_invite_read", "email_invite_write"],
        ),
        (
            {
                "knowledge_graph": {"model": "default"},
                "agents": {
                    "a": {
                        "display_name": "A",
                        "knowledge_graph": True,
                        "knowledge_graph_edits": True,
                        "include_default_tools": False,
                    },
                },
            },
            "knowledge_graph_write",
            "knowledge_graph_*",
            ["knowledge_graph"],
        ),
        (
            {
                "agents": {
                    "a": {
                        "display_name": "A",
                        "created_agents_per_owner": 1,
                        "include_default_tools": False,
                    },
                },
            },
            "create_agent",
            "*_agent",
            ["agent_manager_write"],
        ),
        (
            {
                "personal_agents": {
                    "mind": {
                        "agent": {
                            "display_name": "{user_first_name}",
                            "role": "Personal",
                            "contacts": {},
                            "include_default_tools": False,
                        },
                    },
                },
            },
            "request_contact",
            "*_contact",
            ["agent_manager_write", "contacts_read", "contacts_write"],
        ),
    ]
    for extra, exact_old, glob_old, replacements in cases:
        base: dict[str, object] = {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
            **extra,
        }
        expected_target = replacements[-1]
        with pytest.raises(ConfigRuntimeValidationError, match=expected_target):
            Config.validate_with_runtime(
                {**base, "tool_approval": {"rules": [{"match": exact_old, "action": "require_approval"}]}},
                runtime_paths,
            )
        with pytest.raises(ConfigRuntimeValidationError, match="|".join(replacements)):
            Config.validate_with_runtime(
                {**base, "tool_approval": {"rules": [{"match": glob_old, "action": "require_approval"}]}},
                runtime_paths,
            )
        with pytest.raises(ConfigRuntimeValidationError, match=r"check\.py"):
            Config.validate_with_runtime(
                {**base, "tool_approval": {"rules": [{"match": "*", "script": "check.py"}]}},
                runtime_paths,
            )
        loaded = Config.validate_with_runtime(
            {
                **base,
                "tool_approval": {
                    "rules": [
                        *[{"match": name, "script": "check.py"} for name in (*replacements, "skill")],
                        {"match": "*", "script": "check.py"},
                    ],
                },
            },
            runtime_paths,
        )
        assert loaded.tool_approval.rules[-1].match == "*"


@pytest.mark.parametrize(
    ("template_extra", "defaults_extra", "retired_match", "replacement"),
    [
        # Template-only personal_agents enables agent_manager even before any generated agent exists.
        ({}, {}, "update_agent", "agent_manager_write"),
        ({}, {}, "set_avatar", "agent_manager_write"),
        # Contacts enabled on template or in a per-user override.
        ({"agent_extra": {"contacts": {}}}, {}, "request_contact", "contacts_write"),
        ({"users": {"@alice:example.org": {"contacts": {}}}}, {}, "remove_contact", "contacts_write"),
        ({"agent_extra": {"contacts": {}}}, {}, "list_contacts", "contacts_read"),
        # Knowledge graph enabled on template, defaults, or per-user override.
        ({"agent_extra": {"knowledge_graph": True}}, {}, "search_graph", "knowledge_graph"),
        ({}, {"knowledge_graph": True}, "knowledge_graph_read", "knowledge_graph"),
        (
            {"users": {"@alice:example.org": {"knowledge_graph": True}}},
            {},
            "knowledge_graph_write",
            "knowledge_graph",
        ),
        # Tools enabled via template.users overrides.
        (
            {"users": {"@alice:example.org": {"tools": ["email_invite"]}}},
            {},
            "revoke_email_invite",
            "email_invite_write",
        ),
        (
            {"users": {"@alice:example.org": {"tools": ["email_invite"]}}},
            {},
            "list_email_invites",
            "email_invite_read",
        ),
        (
            {"users": {"@alice:example.org": {"tools": ["shared_directories"]}}},
            {},
            "accept_shared_directory",
            "shared_directories_write",
        ),
        (
            {"users": {"@alice:example.org": {"tools": ["shared_directories"]}}},
            {},
            "list_shared_directories",
            "shared_directories_read",
        ),
    ],
)
def test_template_only_personal_agents_fail_closed_for_retired_approval_rules(
    tmp_path: Path,
    template_extra: dict[str, object],
    defaults_extra: dict[str, object],
    retired_match: str,
    replacement: str,
) -> None:
    """Template-only personal_agents configs fail closed on retired approval rules before any user is onboarded."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    agent_cfg: dict[str, object] = {
        "display_name": "{user_first_name}",
        "role": "Personal assistant",
        "include_default_tools": False,
        **dict(template_extra.get("agent_extra", {})),  # type: ignore[arg-type]
    }
    template: dict[str, object] = {"agent": agent_cfg}
    if "users" in template_extra:
        template["users"] = template_extra["users"]
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "knowledge_graph": {"model": "default"},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only", **defaults_extra},
        "personal_agents": {"mind": template},
        "tool_approval": {"rules": [{"match": retired_match, "action": "require_approval"}]},
    }
    with pytest.raises(ConfigRuntimeValidationError, match=replacement):
        Config.validate_with_runtime(data, runtime_paths)


@pytest.mark.parametrize(
    ("toolkit", "retired_name", "replacement", "kept_read"),
    [
        ("shared_directories", "delete_shared_directory", "shared_directories_write", "shared_directories_read"),
        ("shared_directories", "list_shared_directories", "shared_directories_read", None),
        ("contacts", "request_contact", "contacts_write", None),
        ("contacts", "list_contacts", "contacts_read", None),
        ("knowledge_graph", "search_graph", "knowledge_graph", None),
        ("knowledge_graph", "knowledge_graph_read", "knowledge_graph", None),
        ("knowledge_graph", "knowledge_graph_write", "knowledge_graph", None),
        ("agent_manager", "create_agent", "agent_manager_write", None),
        ("email_invite", "invite_by_email", "email_invite_write", "email_invite_read"),
        ("email_invite", "list_email_invites", "email_invite_read", None),
    ],
)
def test_retired_identity_access_graph_include_exclude_overrides_fail_config_load(
    tmp_path: Path,
    toolkit: str,
    retired_name: str,
    replacement: str,
    kept_read: str | None,
) -> None:
    """Retired include_tools/exclude_tools fail closed at config load while kept read names stay valid."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    for filter_key in ("include_tools", "exclude_tools"):
        data: dict[str, object] = {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "agents": {
                "a": {
                    "display_name": "A",
                    "tools": [{toolkit: {filter_key: [retired_name]}}],
                    "include_default_tools": False,
                },
            },
        }
        with pytest.raises(
            ConfigRuntimeValidationError,
            match=rf"{toolkit}\.{filter_key}: {retired_name} was removed; .*`{replacement}`",
        ):
            Config.validate_with_runtime(data, runtime_paths)

    if kept_read is not None:
        valid = Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "x"}},
                "agents": {
                    "a": {
                        "display_name": "A",
                        "tools": [{toolkit: {"exclude_tools": [kept_read]}}],
                        "include_default_tools": False,
                    },
                },
            },
            runtime_paths,
        )
        assert valid.agents["a"].tools[0].overrides == {"exclude_tools": [kept_read]}


def test_malformed_personal_agent_user_override_keeps_template_error_path_with_approval_rules(
    tmp_path: Path,
) -> None:
    """Malformed personal_agents user tool overrides still report the template path when approval rules are active."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "personal_agents": {
            "mind": {
                "agent": {
                    "display_name": "{user_first_name}",
                    "role": "Personal assistant",
                    "include_default_tools": False,
                },
                "users": {"@alice:example.org": {"tools": [123]}},
            },
        },
        "tool_approval": {"rules": [{"match": "agent_manager_write", "action": "require_approval"}]},
    }
    with pytest.raises(ConfigRuntimeValidationError, match="personal_agents"):
        Config.validate_with_runtime(data, runtime_paths)


@pytest.mark.parametrize(
    ("toolkit_name", "tool_name", "actions"),
    [
        ("atlassian", "jira_read", ("search", "get")),
        ("atlassian", "jira_write", ("create", "update", "comment", "transition")),
        ("atlassian", "confluence_read", ("search", "get", "list_attachments", "download_attachment")),
        ("atlassian", "confluence_write", ("create", "update", "comment")),
        ("slack", "slack_read", ("search", "recent", "read", "download")),
        ("slack", "slack_send", ("post", "upload")),
        ("slack", "slack_manage", ("edit", "delete", "react")),
        ("outlook_mail", "outlook_mail_read", ("search", "get_message", "get_automatic_replies")),
        ("outlook_mail", "outlook_mail_send_message", ("send", "reply")),
        ("outlook_mail", "outlook_mail_manage", ("mark_read", "move", "set_automatic_replies")),
        ("outlook_calendar", "outlook_calendar_read", ("list_calendars", "list_events", "get_free_busy")),
        ("outlook_calendar", "outlook_calendar_write", ("create_event", "delete_event", "respond_to_event")),
        ("microsoft_teams", "microsoft_teams_read", ("list_teams", "list_channel_messages", "get_presence")),
        ("microsoft_teams", "microsoft_teams_send", ("send_channel_message", "send_chat_message", "start_chat")),
        ("microsoft_files", "microsoft_files_read", ("search", "download_file", "excel_read_range")),
        ("microsoft_files", "microsoft_files_write", ("replace_file", "upload_file", "excel_update_range")),
        ("microsoft_sharepoint", "microsoft_sharepoint_read", ("search_sites", "list_items", "get_page")),
        ("microsoft_sharepoint", "microsoft_sharepoint_write", ("create_item", "update_item")),
        ("microsoft_onenote", "microsoft_onenote_read", ("list_notebooks", "list_pages", "get_page")),
        ("microsoft_onenote", "microsoft_onenote_write", ("create_page", "append_to_page")),
        ("microsoft_todo", "microsoft_todo_read", ("list_lists", "list_tasks")),
        ("microsoft_todo", "microsoft_todo_write", ("create_task", "update_task", "delete_task")),
        ("microsoft_meetings", "microsoft_meetings_read", ("find", "list_transcripts", "get_transcript")),
        ("microsoft_people", "microsoft_people_read", ("search", "get_profile", "list_direct_reports")),
    ],
)
def test_atlassian_slack_and_microsoft_365_registry_action_scoped_grants_bind_per_action(
    toolkit_name: str,
    tool_name: str,
    actions: tuple[str, ...],
) -> None:
    """Consolidated Atlassian, Slack, and Microsoft 365 action-dispatched tools bind timed grants per action."""
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


@pytest.mark.parametrize(
    ("tools", "default", "rules", "error_pattern"),
    [
        (
            ["atlassian"],
            "auto_approve",
            [{"match": "jira_create_issue", "action": "require_approval"}],
            r"jira_create_issue.*jira_write",
        ),
        (["atlassian"], "auto_approve", [{"match": "jira_*", "script": "check.py"}], r"check\.py.*jira_"),
        (
            ["atlassian"],
            "auto_approve",
            [
                {"match": "jira_read", "script": "check.py"},
                {"match": "jira_write", "script": "check.py"},
                {"match": "jira_*", "script": "check.py"},
            ],
            None,
        ),
        # Documented Atlassian approval examples (exact function names) load cleanly under both defaults.
        (
            ["atlassian"],
            "auto_approve",
            [
                {"match": "jira_write", "action": "require_approval"},
                {"match": "confluence_write", "action": "require_approval"},
            ],
            None,
        ),
        (
            ["atlassian"],
            "require_approval",
            [
                {"match": "jira_read", "action": "auto_approve"},
                {"match": "confluence_read", "action": "auto_approve"},
            ],
            None,
        ),
        # Retired slack_inspect fails closed under require_approval default and with slack_* catch-all.
        (
            ["slack"],
            "require_approval",
            [{"match": "slack_inspect", "action": "auto_approve"}],
            r"slack_inspect.*slack_read",
        ),
        (
            ["slack"],
            "auto_approve",
            [
                {"match": "slack_inspect", "action": "auto_approve"},
                {"match": "slack_*", "action": "require_approval"},
            ],
            r"slack_inspect.*slack_read",
        ),
        (
            ["slack"],
            "auto_approve",
            [
                {"match": "slack_read", "action": "auto_approve"},
                {"match": "slack_*", "action": "require_approval"},
            ],
            None,
        ),
        (["slack"], "auto_approve", [{"match": "slack_*", "script": "check.py"}], r"check\.py.*slack_"),
        (
            ["slack"],
            "auto_approve",
            [
                {"match": "slack_read", "script": "check.py"},
                {"match": "slack_send", "script": "check.py"},
                {"match": "slack_manage", "script": "check.py"},
                {"match": "slack_*", "script": "check.py"},
            ],
            None,
        ),
    ],
)
def test_atlassian_and_slack_retired_approval_rules_fail_closed(
    tmp_path: Path,
    tools: list[str],
    default: str,
    rules: list[dict[str, str]],
    error_pattern: str | None,
) -> None:
    """Retired Atlassian/Slack approval rules (including slack_read) and wildcard scripts fail closed."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "agents": {"a": {"display_name": "A", "tools": tools, "include_default_tools": False}},
        "tool_approval": {"default": default, "rules": rules},
    }
    if error_pattern is not None:
        with pytest.raises(ConfigRuntimeValidationError, match=error_pattern):
            Config.validate_with_runtime(data, runtime_paths)
        return
    loaded = Config.validate_with_runtime(data, runtime_paths)
    assert len(loaded.tool_approval.rules) == len(rules)


def test_outlook_mail_create_draft_keeps_tool_wide_grants() -> None:
    """Single-operation outlook_mail_create_draft without an action enum uses normal tool-wide grants."""
    from mindroom.tool_system.registry import builtin_tool_registry  # noqa: PLC0415

    registry = builtin_tool_registry()
    assert "outlook_mail_create_draft" not in registry.entries["outlook_mail"].action_scoped_grants
    op = grant_operation(Config(), "outlook_mail_create_draft", {}, tools=registry, toolkit_name="outlook_mail")
    assert op is not None
    assert op.tool_name == "outlook_mail_create_draft"
    assert op.mcp_server_id is None
    assert op.mcp_tool_name is None


@pytest.mark.parametrize(
    ("toolkit_name", "retired_fn", "replacement_fn"),
    [
        ("outlook_mail", "outlook_mail_send", "outlook_mail_send_message"),
        ("outlook_mail", "outlook_mail_reply", "outlook_mail_send_message"),
        ("outlook_mail", "outlook_mail_set_automatic_replies", "outlook_mail_manage"),
        ("outlook_calendar", "outlook_calendar_create_event", "outlook_calendar_write"),
        ("microsoft_teams", "microsoft_teams_send_channel_message", "microsoft_teams_send"),
        ("microsoft_files", "microsoft_files_replace_file", "microsoft_files_write"),
        ("microsoft_sharepoint", "microsoft_sharepoint_create_item", "microsoft_sharepoint_write"),
        ("microsoft_onenote", "microsoft_onenote_create_page", "microsoft_onenote_write"),
        ("microsoft_todo", "microsoft_todo_delete_task", "microsoft_todo_write"),
        ("microsoft_meetings", "microsoft_meetings_get_transcript", "microsoft_meetings_read"),
        ("microsoft_people", "microsoft_people_get_profile", "microsoft_people_read"),
    ],
)
def test_microsoft_365_retired_approval_rules_fail_closed(
    tmp_path: Path,
    toolkit_name: str,
    retired_fn: str,
    replacement_fn: str,
) -> None:
    """Approval rules targeting retired Microsoft 365 function names fail closed until migrated."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    base_data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {"a": {"display_name": "A", "tools": [toolkit_name], "include_default_tools": False}},
    }
    with pytest.raises(ConfigRuntimeValidationError, match=replacement_fn):
        Config.validate_with_runtime(
            {**base_data, "tool_approval": {"rules": [{"match": retired_fn, "action": "require_approval"}]}},
            runtime_paths,
        )
    Config.validate_with_runtime(
        {**base_data, "tool_approval": {"rules": [{"match": replacement_fn, "action": "require_approval"}]}},
        runtime_paths,
    )


@pytest.mark.parametrize(
    "tool_approval",
    [
        {
            "default": "require_approval",
            "rules": [{"match": "outlook_mail_send", "action": "auto_approve"}],
        },
        {
            "default": "auto_approve",
            "rules": [
                {"match": "outlook_mail_send", "action": "auto_approve"},
                {"match": "outlook_mail_reply", "action": "require_approval"},
            ],
        },
        {
            "default": "require_approval",
            "rules": [
                {"match": "outlook_mail_send", "action": "auto_approve"},
                {"match": "outlook_mail_reply", "action": "require_approval"},
            ],
        },
    ],
)
def test_outlook_mail_send_and_reply_migration_rules_fail_closed(
    tmp_path: Path,
    tool_approval: dict[str, object],
) -> None:
    """Legacy outlook_mail_send / outlook_mail_reply approval combinations fail closed until migrated."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    base_data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {"a": {"display_name": "A", "tools": ["outlook_mail"], "include_default_tools": False}},
    }
    with pytest.raises(ConfigRuntimeValidationError, match="outlook_mail_send_message"):
        Config.validate_with_runtime({**base_data, "tool_approval": tool_approval}, runtime_paths)

    migrated = Config.validate_with_runtime(
        {
            **base_data,
            "tool_approval": {
                "default": "require_approval",
                "rules": [
                    {"match": "outlook_mail_send_message", "action": "auto_approve"},
                    {"match": "outlook_mail_reply", "action": "require_approval"},
                ],
            },
        },
        runtime_paths,
    )
    assert migrated.tool_approval.rules[0].match == "outlook_mail_send_message"


@pytest.mark.parametrize(
    ("toolkit_name", "matches", "error_pattern"),
    [
        ("outlook_mail", ["outlook_mail_*"], r"check_m365\.py.*outlook_mail_"),
        ("outlook_mail", ["outlook_mail_send*"], r"check_m365\.py.*outlook_mail_send_message"),
        (
            "outlook_mail",
            ["outlook_mail_send*", "outlook_mail_send_message"],
            r"check_m365\.py.*outlook_mail_send_message",
        ),
        ("outlook_mail", ["outlook_mail_send_message", "outlook_mail_send*"], None),
        (
            "outlook_mail",
            ["outlook_mail_read", "outlook_mail_send_message", "outlook_mail_manage", "outlook_mail_*"],
            None,
        ),
        ("outlook_calendar", ["outlook_calendar_*"], r"check_m365\.py.*outlook_calendar_"),
        ("outlook_calendar", ["outlook_calendar_read", "outlook_calendar_write", "outlook_calendar_*"], None),
        ("microsoft_teams", ["microsoft_teams_*"], r"check_m365\.py.*microsoft_teams_"),
        ("microsoft_teams", ["microsoft_teams_read", "microsoft_teams_send", "microsoft_teams_*"], None),
        ("microsoft_files", ["microsoft_files_*"], r"check_m365\.py.*microsoft_files_"),
        ("microsoft_files", ["microsoft_files_read", "microsoft_files_write", "microsoft_files_*"], None),
        ("microsoft_sharepoint", ["microsoft_sharepoint_*"], r"check_m365\.py.*microsoft_sharepoint_"),
        (
            "microsoft_sharepoint",
            ["microsoft_sharepoint_read", "microsoft_sharepoint_write", "microsoft_sharepoint_*"],
            None,
        ),
        ("microsoft_onenote", ["microsoft_onenote_*"], r"check_m365\.py.*microsoft_onenote_"),
        ("microsoft_onenote", ["microsoft_onenote_read", "microsoft_onenote_write", "microsoft_onenote_*"], None),
        ("microsoft_todo", ["microsoft_todo_*"], r"check_m365\.py.*microsoft_todo_"),
        ("microsoft_todo", ["microsoft_todo_read", "microsoft_todo_write", "microsoft_todo_*"], None),
        ("microsoft_meetings", ["microsoft_meetings_*"], r"check_m365\.py.*microsoft_meetings_read"),
        ("microsoft_meetings", ["microsoft_meetings_read", "microsoft_meetings_*"], None),
        ("microsoft_people", ["microsoft_people_*"], r"check_m365\.py.*microsoft_people_read"),
        ("microsoft_people", ["microsoft_people_read", "microsoft_people_*"], None),
    ],
)
def test_wildcard_approval_script_rule_fails_closed_for_retired_microsoft_365_names(
    tmp_path: Path,
    toolkit_name: str,
    matches: list[str],
    error_pattern: str | None,
) -> None:
    """Wildcard approval scripts fail closed for retired Microsoft 365 names until explicit rules precede them."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    (tmp_path / "check_m365.py").write_text(
        "def check(tool_name, arguments, agent_name): return True\n",
        encoding="utf-8",
    )
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "agents": {"a": {"display_name": "A", "tools": [toolkit_name], "include_default_tools": False}},
        "tool_approval": {"rules": [{"match": match, "script": "check_m365.py"} for match in matches]},
    }
    if error_pattern is not None:
        with pytest.raises(ConfigRuntimeValidationError, match=error_pattern):
            Config.validate_with_runtime(data, runtime_paths)
        return
    loaded = Config.validate_with_runtime(data, runtime_paths)
    assert [rule.match for rule in loaded.tool_approval.rules] == matches


def test_prefixed_atlassian_connection_grants_and_fail_closed_migrations(tmp_path: Path) -> None:
    """Prefixed Atlassian connections get action-scoped grants and filtered fail-closed rules/overrides."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415
    from mindroom.tool_system.plugins import refresh_plugins  # noqa: PLC0415

    plugin_root = tmp_path / "plugins" / "partner_atlassian"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps(
            {
                "name": "partner_atlassian",
                "mindroom_plugin_api": "1.35",
                "tools_module": "tools.py",
                "oauth_module": "oauth.py",
            },
        ),
        encoding="utf-8",
    )
    connections_def = (
        "from mindroom.tool_system.atlassian_connections import AtlassianConnectionConfig\n"
        "PARTNER = AtlassianConnectionConfig(\n"
        "    name='partner',\n"
        "    display_name='Partner Confluence',\n"
        "    site_url='https://partner.atlassian.net',\n"
        "    products=('confluence',),\n"
        "    write=True,\n"
        ")\n"
        "ARCHIVE = AtlassianConnectionConfig(\n"
        "    name='archive',\n"
        "    display_name='Archive Atlassian',\n"
        "    site_url='https://archive.atlassian.net',\n"
        "    write=False,\n"
        ")\n"
    )
    (plugin_root / "oauth.py").write_text(
        connections_def
        + "from mindroom.tool_system.atlassian_connections import atlassian_connection_oauth_provider\n"
        + "def register_oauth_providers():\n"
        + "    return [atlassian_connection_oauth_provider(PARTNER), atlassian_connection_oauth_provider(ARCHIVE)]\n",
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        connections_def
        + "from mindroom.tool_system.atlassian_connections import register_atlassian_connection_tools\n"
        + "register_atlassian_connection_tools(PARTNER)\n"
        + "register_atlassian_connection_tools(ARCHIVE)\n",
        encoding="utf-8",
    )
    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")
    runtime_paths = test_runtime_paths(tmp_path)

    def _cfg(
        tools: list[object],
        rules: list[dict[str, str]] | None = None,
        *,
        default: str = "auto_approve",
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "models": {"default": {"provider": "openai", "id": "x"}},
            "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
            "plugins": [str(plugin_root)],
            "agents": {"a": {"display_name": "A", "tools": tools, "include_default_tools": False}},
        }
        if rules is not None:
            payload["tool_approval"] = {"default": default, "rules": rules}
        return payload

    # Single-product partner_atlassian fails closed on retired Confluence names and wildcard scripts,
    # but does NOT complain about non-existent Jira functions.
    with pytest.raises(ConfigRuntimeValidationError, match="partner_confluence_write"):
        Config.validate_with_runtime(
            _cfg(["partner_atlassian"], [{"match": "partner_confluence_create_page", "action": "require_approval"}]),
            runtime_paths,
        )
    with pytest.raises(ConfigRuntimeValidationError, match=r"check\.py.*partner_confluence_"):
        Config.validate_with_runtime(
            _cfg(["partner_atlassian"], [{"match": "partner_confluence_*", "script": "check.py"}]),
            runtime_paths,
        )
    Config.validate_with_runtime(
        _cfg(["partner_atlassian"], [{"match": "partner_jira_search_issues", "action": "require_approval"}]),
        runtime_paths,
    )
    # Exact documented rules for prefixed connections load cleanly under both defaults.
    Config.validate_with_runtime(
        _cfg(["partner_atlassian"], [{"match": "partner_confluence_write", "action": "require_approval"}]),
        runtime_paths,
    )
    Config.validate_with_runtime(
        _cfg(
            ["partner_atlassian"],
            [{"match": "partner_confluence_read", "action": "auto_approve"}],
            default="require_approval",
        ),
        runtime_paths,
    )

    # Read-only archive_atlassian fails closed on retired read names, but does NOT ask for non-existent write rules.
    with pytest.raises(ConfigRuntimeValidationError, match="archive_jira_read"):
        Config.validate_with_runtime(
            _cfg(["archive_atlassian"], [{"match": "archive_jira_search_issues", "action": "require_approval"}]),
            runtime_paths,
        )
    loaded = Config.validate_with_runtime(
        _cfg(
            ["partner_atlassian", "archive_atlassian"],
            [
                {"match": "archive_jira_create_issue", "action": "require_approval"},
                {"match": "archive_jira_read", "script": "check.py"},
                {"match": "archive_confluence_read", "script": "check.py"},
                {"match": "archive_*", "script": "check.py"},
            ],
        ),
        runtime_paths,
    )

    # include_tools / exclude_tools fail closed for prefixed retired functions and allow canonical ones.
    with pytest.raises(
        ConfigRuntimeValidationError,
        match=r"partner_atlassian\.include_tools.*partner_confluence_read",
    ):
        Config.validate_with_runtime(
            _cfg([{"partner_atlassian": {"include_tools": ["partner_confluence_search"]}}]),
            runtime_paths,
        )
    Config.validate_with_runtime(
        _cfg([{"partner_atlassian": {"include_tools": ["partner_confluence_read"]}}]),
        runtime_paths,
    )

    # Action-scoped grants bind per action for prefixed functions.
    registry = refresh_plugins(loaded, runtime_paths).tools
    assert registry.entries["partner_atlassian"].action_scoped_grants == (
        "partner_confluence_read",
        "partner_confluence_write",
    )
    assert registry.entries["archive_atlassian"].action_scoped_grants == (
        "archive_jira_read",
        "archive_confluence_read",
    )
    get_op = grant_operation(
        loaded,
        "partner_confluence_read",
        {"action": "get"},
        tools=registry,
        toolkit_name="partner_atlassian",
    )
    dl_op = grant_operation(
        loaded,
        "partner_confluence_read",
        {"action": "download_attachment"},
        tools=registry,
        toolkit_name="partner_atlassian",
    )
    assert get_op is not None
    assert dl_op is not None
    assert get_op.key != dl_op.key


def test_plugin_declared_retired_functions_fail_closed_on_wildcard_scripts(tmp_path: Path) -> None:
    """Any plugin toolkit declaring retired_functions rejects wildcard script rules until exact rules precede them."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    plugin_root = _write_consolidated_plugin(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    (tmp_path / "check.py").write_text("def check(tool_name, arguments, agent_name): return True\n", encoding="utf-8")
    base_data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "plugins": [{"path": str(plugin_root)}],
        "agents": {"a": {"display_name": "A", "tools": ["demo_consolidated"], "include_default_tools": False}},
    }
    with pytest.raises(ConfigRuntimeValidationError, match=r"check\.py.*demo_action_tool"):
        Config.validate_with_runtime(
            {**base_data, "tool_approval": {"rules": [{"match": "*demo*", "script": "check.py"}]}},
            runtime_paths,
        )
    Config.validate_with_runtime(
        {
            **base_data,
            "tool_approval": {
                "rules": [
                    {"match": "demo_action_tool", "script": "check.py"},
                    {"match": "*demo*", "script": "check.py"},
                ],
            },
        },
        runtime_paths,
    )


@pytest.mark.parametrize(
    ("tool_entry", "error_pattern"),
    [
        ({"atlassian": {"include_tools": ["jira_search_issues"]}}, r"atlassian\.include_tools.*jira_read"),
        ({"atlassian": {"exclude_tools": ["confluence_update_page"]}}, r"atlassian\.exclude_tools.*confluence_write"),
        ({"slack": {"include_tools": ["slack_inspect"]}}, r"slack\.include_tools.*slack_read"),
        ({"slack": {"exclude_tools": ["slack_inspect"]}}, r"slack\.exclude_tools.*slack_read"),
        ({"slack": {"include_tools": ["slack_post"]}}, r"slack\.include_tools.*slack_send"),
        ({"slack": {"exclude_tools": ["slack_download"]}}, r"slack\.exclude_tools.*slack_read"),
        ({"slack": {"exclude_tools": ["slack_watch"]}}, r"subscribe_event"),
        ({"atlassian": {"exclude_tools": ["jira_write"]}}, None),
        ({"slack": {"include_tools": ["slack_read"]}}, None),
    ],
)
def test_retired_atlassian_and_slack_include_exclude_overrides_fail_config_load(
    tmp_path: Path,
    tool_entry: dict[str, object],
    error_pattern: str | None,
) -> None:
    """include_tools/exclude_tools naming retired Atlassian/Slack functions fail fast with replacement guidance."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "agents": {"a": {"display_name": "A", "tools": [tool_entry], "include_default_tools": False}},
    }
    if error_pattern is not None:
        with pytest.raises(ConfigRuntimeValidationError, match=error_pattern):
            Config.validate_with_runtime(data, runtime_paths)
        return
    Config.validate_with_runtime(data, runtime_paths)


@pytest.mark.parametrize(
    ("overrides", "old", "new", "expected_effect"),
    [
        (
            {"exclude_tools": ["slack_post", "slack_delete"]},
            "slack_post",
            "slack_send",
            "removes all sibling actions in `slack_send`",
        ),
        (
            {"include_tools": ["slack_react"]},
            "slack_react",
            "slack_manage",
            "broadens to all sibling actions in `slack_manage`",
        ),
    ],
)
def test_retired_consolidated_filter_guidance_preserves_other_entries_and_names_approval_scripts(
    tmp_path: Path,
    overrides: dict[str, object],
    old: str,
    new: str,
    expected_effect: str,
) -> None:
    """Filter migration guidance never recommends dropping sibling entries and points per-action gating to scripts."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "agents": {"a": {"display_name": "A", "tools": [{"slack": overrides}], "include_default_tools": False}},
    }
    with pytest.raises(ConfigRuntimeValidationError) as exc_info:
        Config.validate_with_runtime(data, runtime_paths)
    message = str(exc_info.value)
    assert f"replace `{old}` with `{new}`" in message
    assert "preserving any other entries" in message
    assert expected_effect in message
    assert f"approval `script` matching `{new}`" in message
    assert f"[{new}]" not in message


@pytest.mark.parametrize(
    ("tools", "rules", "error_pattern"),
    [
        (
            ["matrix_user"],
            [{"match": "matrix_user_read", "arguments": {"action": "mark_read"}, "action": "require_approval"}],
            (
                r"mark_read moved from matrix_user_read to matrix_user_write; "
                r"move 'mark_read' to matrix_user_write \(preserving conditions and approval decision\) "
                r"and keep any remaining read actions on matrix_user_read\."
            ),
        ),
        (
            ["matrix_user"],
            [
                {
                    "match": "matrix_user_read",
                    "arguments": {"action": ["messages", "mark_read"]},
                    "action": "require_approval",
                },
            ],
            (
                r"mark_read moved from matrix_user_read to matrix_user_write; "
                r"move 'mark_read' to matrix_user_write \(preserving conditions and approval decision\) "
                r"and keep any remaining read actions on matrix_user_read\."
            ),
        ),
        (
            ["matrix_user"],
            [{"match": "matrix_user_write", "arguments": {"action": "mark_read"}, "action": "require_approval"}],
            None,
        ),
        (
            ["matrix_user"],
            [{"match": "*", "arguments": {"action": "mark_read"}, "action": "require_approval"}],
            None,
        ),
        (
            ["matrix_user"],
            [{"match": "matrix_user_*", "arguments": {"action": "mark_read"}, "action": "require_approval"}],
            None,
        ),
        (
            ["matrix_user", "gmail"],
            [{"match": "gmail_manage", "arguments": {"action": "mark_read"}, "action": "require_approval"}],
            None,
        ),
        (
            ["matrix_room"],
            [{"match": "matrix_room", "arguments": {"action": "room-info"}, "action": "require_approval"}],
            r"room-info.*room_info",
        ),
        (
            ["matrix_room"],
            [
                {
                    "match": "matrix_room",
                    "arguments": {"action": ["members", "room-info"]},
                    "action": "require_approval",
                },
            ],
            r"room-info.*room_info",
        ),
        (
            ["matrix_room"],
            [{"match": "*", "arguments": {"action": "room-info"}, "action": "require_approval"}],
            r"room-info.*room_info",
        ),
        (
            ["matrix_room"],
            [{"match": "matrix_room", "arguments": {"action": "room_info"}, "action": "require_approval"}],
            None,
        ),
        (
            ["matrix_room"],
            [{"match": "matrix_room", "arguments": {"action": "room*"}, "action": "require_approval"}],
            None,
        ),
    ],
)
def test_retired_action_argument_approval_rules_fail_closed(
    tmp_path: Path,
    tools: list[str],
    rules: list[dict[str, object]],
    error_pattern: str | None,
) -> None:
    """Approval rules targeting moved mark_read or renamed room-info action values fail closed."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": []},
        "agents": {"a": {"display_name": "A", "tools": tools, "include_default_tools": False}},
        "tool_approval": {"rules": rules},
    }
    if error_pattern is not None:
        with pytest.raises(ConfigRuntimeValidationError, match=error_pattern):
            Config.validate_with_runtime(data, runtime_paths)
    else:
        loaded = Config.validate_with_runtime(data, runtime_paths)
        assert len(loaded.tool_approval.rules) == len(rules)


@pytest.mark.parametrize(
    ("toolkit_name", "filter_key", "old_name", "new_name"),
    [
        ("google_docs", "include_tools", "google_docs_get_document", "google_docs_read"),
        ("google_docs", "exclude_tools", "google_docs_get_document", "google_docs_read"),
        ("twilio", "include_tools", "send_sms", "twilio_send"),
        ("twilio", "exclude_tools", "send_sms", "twilio_send"),
        ("contacts", "include_tools", "list_contacts", "contacts_read"),
        ("email_invite", "exclude_tools", "list_email_invites", "email_invite_read"),
        ("shared_directories", "include_tools", "list_shared_directories", "shared_directories_read"),
        ("claude_agent", "include_tools", "claude_agent_status", "claude_agent_read"),
        ("claude_agent", "exclude_tools", "claude_session_status", "claude_agent_read"),
    ],
)
def test_single_verb_renamed_filter_guidance_replaces_in_place(
    tmp_path: Path,
    toolkit_name: str,
    filter_key: str,
    old_name: str,
    new_name: str,
) -> None:
    """Single-verb 1-to-1 renames guide in-place filter replacement without claiming an action parameter or removal."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": []},
        "agents": {
            "a": {
                "display_name": "A",
                "tools": [{toolkit_name: {filter_key: [old_name]}}],
                "include_default_tools": False,
            },
        },
    }
    with pytest.raises(ConfigRuntimeValidationError) as exc_info:
        Config.validate_with_runtime(data, runtime_paths)
    message = str(exc_info.value)
    assert (
        f"replace `{old_name}` with `{new_name}` in `{toolkit_name}.{filter_key}` "
        "while preserving any other entries in the list"
    ) in message
    guidance = message.split("was removed; ", 1)[1]
    assert "action" not in guidance
    assert "remove" not in guidance


def test_triggers_write_watch_script_cannot_receive_timed_grant() -> None:
    """watch_script creates executable scripts and must never receive a timed auto-approve grant."""
    config = Config()
    registry = builtin_tool_registry()

    assert (
        grant_operation(
            config,
            "triggers_write",
            {"action": "watch_script"},
            tools=registry,
            toolkit_name="triggers",
        )
        is None
    )
    assert (
        grant_operation(
            config,
            "triggers_write",
            {"action": " watch_script "},
            tools=registry,
            toolkit_name="triggers",
        )
        is None
    )
    delete_op = grant_operation(
        config,
        "triggers_write",
        {"action": "delete"},
        tools=registry,
        toolkit_name="triggers",
    )
    assert delete_op is not None
    assert delete_op.mcp_tool_name == "delete"


@pytest.mark.parametrize(
    ("tools", "old_match", "expected_pattern", "desktop_pairing"),
    [
        (
            ["triggers"],
            "triggers",
            r"triggers was split into triggers_read and triggers_write.*rewrite the rules for triggers_read and triggers_write\.",
            "explicit_only",
        ),
        (
            ["desktop"],
            "desktop",
            r"desktop was split into desktop_read and desktop_control.*rewrite the rules for desktop_read and desktop_control.*run_desktop_shell",
            "explicit_only",
        ),
        (
            [],
            "desktop",
            r"desktop was split into desktop_read and desktop_control.*rewrite the rules for desktop_read and desktop_control.*run_desktop_shell",
            "self_service",
        ),
        (["browser"], "browser_control", r"read actions from browser_control moved to browser_read", "explicit_only"),
        (
            ["workflow"],
            "workflow",
            r"workflow was split into workflow_read and workflow_write.*rewrite the rules for workflow_read and workflow_write\.",
            "explicit_only",
        ),
        (
            ["matrix_api"],
            "matrix_api",
            r"matrix_api was split into matrix_api_read and matrix_api_write.*rewrite the rules for matrix_api_read and matrix_api_write\.",
            "explicit_only",
        ),
    ],
)
def test_mega_tool_split_approval_rules_fail_closed(
    tmp_path: Path,
    tools: list[str],
    old_match: str,
    expected_pattern: str,
    desktop_pairing: str,
) -> None:
    """Rules matching pre-split mega-tool function names fail closed and name all split replacements."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": desktop_pairing},
        "agents": {"a": {"display_name": "A", "tools": tools, "include_default_tools": False}},
        "tool_approval": {
            "default": "auto_approve",
            "rules": [{"match": old_match, "action": "require_approval"}],
        },
    }
    with pytest.raises(ConfigRuntimeValidationError, match=expected_pattern):
        Config.validate_with_runtime(data, runtime_paths)


def test_desktop_partial_and_script_split_approval_errors_name_all_replacements(tmp_path: Path) -> None:
    """Partial desktop migration and approval scripts still name both split functions and run_desktop_shell."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    script_path = tmp_path / "gate.py"
    script_path.write_text("def check(tool_name, arguments, agent_name):\n    return True\n", encoding="utf-8")

    # Covering desktop_read alone still rejects desktop_control and names all replacements.
    partial_data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "agents": {"a": {"display_name": "A", "tools": ["desktop"], "include_default_tools": False}},
        "tool_approval": {
            "default": "auto_approve",
            "rules": [
                {"match": "desktop", "action": "require_approval"},
                {"match": "desktop_read", "action": "require_approval"},
            ],
        },
    }
    with pytest.raises(
        ConfigRuntimeValidationError,
        match=r"desktop was split into desktop_read and desktop_control \(desktop_control now gets a different approval decision\).*run_desktop_shell",
    ):
        Config.validate_with_runtime(partial_data, runtime_paths)

    # Wildcard/exact approval script on retired `desktop` also names both replacements and run_desktop_shell.
    script_data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "defaults": {"tools": [], "desktop_pairing": "explicit_only"},
        "agents": {"a": {"display_name": "A", "tools": ["desktop"], "include_default_tools": False}},
        "tool_approval": {
            "default": "auto_approve",
            "rules": [{"match": "desktop*", "script": str(script_path)}],
        },
    }
    with pytest.raises(
        ConfigRuntimeValidationError,
        match=r"desktop was split into desktop_read and desktop_control; update the script and add explicit rules for desktop_read and desktop_control.*run_desktop_shell",
    ):
        Config.validate_with_runtime(script_data, runtime_paths)


def test_preset_and_alias_toolkits_preserve_action_scoped_grant_operations() -> None:
    """Tools resolved via presets like openclaw_compat or toolkit aliases keep distinct action-scoped grant keys."""
    config = Config()
    registry = builtin_tool_registry()

    open_preset = grant_operation(
        config,
        "browser_control",
        {"action": "open"},
        tools=registry,
        toolkit_name="openclaw_compat",
    )
    close_preset = grant_operation(
        config,
        "browser_control",
        {"action": "close"},
        tools=registry,
        toolkit_name="openclaw_compat",
    )
    open_direct = grant_operation(
        config,
        "browser_control",
        {"action": "open"},
        tools=registry,
        toolkit_name="browser",
    )
    assert open_preset is not None
    assert close_preset is not None
    assert open_direct is not None
    assert open_preset == open_direct
    assert open_preset.mcp_server_id == "browser"
    assert open_preset.mcp_tool_name == "open"
    assert close_preset.mcp_server_id == "browser"
    assert close_preset.mcp_tool_name == "close"
    assert open_preset.key != close_preset.key

    tabs_preset = grant_operation(
        config,
        "browser_read",
        {"action": "tabs"},
        tools=registry,
        toolkit_name="openclaw_compat",
    )
    shot_preset = grant_operation(
        config,
        "browser_read",
        {"action": "screenshot"},
        tools=registry,
        toolkit_name="openclaw_compat",
    )
    assert tabs_preset is not None
    assert shot_preset is not None
    assert tabs_preset.mcp_server_id == "browser"
    assert tabs_preset.mcp_tool_name == "tabs"
    assert shot_preset.mcp_tool_name == "screenshot"
    assert tabs_preset.key != shot_preset.key

    # Toolkit aliases also keep their canonical action-scoped grant owner and watch_script block.
    delete_alias = grant_operation(
        config,
        "triggers_write",
        {"action": "delete"},
        tools=registry,
        toolkit_name="external_trigger_manager",
    )
    assert delete_alias is not None
    assert delete_alias.mcp_server_id == "triggers"
    assert delete_alias.mcp_tool_name == "delete"
    assert (
        grant_operation(
            config,
            "triggers_write",
            {"action": "watch_script"},
            tools=registry,
            toolkit_name="external_trigger_manager",
        )
        is None
    )


@pytest.mark.parametrize("filter_key", ["include_tools", "exclude_tools"])
def test_workflow_retired_filter_guidance_recommends_removal_and_approval_rules(
    tmp_path: Path,
    filter_key: str,
) -> None:
    """Workflow has supports_toolkit_filters=False, so retired filter errors never suggest keeping the filter."""
    from mindroom.config.main import ConfigRuntimeValidationError  # noqa: PLC0415
    from mindroom.credentials.manager import get_runtime_credentials_manager  # noqa: PLC0415
    from mindroom.tool_system.catalog import ToolConfigOverrideError  # noqa: PLC0415
    from mindroom.tool_system.metadata import get_tool_by_name  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    data: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {
            "a": {
                "display_name": "A",
                "tools": [{"workflow": {filter_key: ["workflow"]}}],
                "include_default_tools": False,
            },
        },
    }
    expected = (
        rf"`workflow` was split into `workflow_read`, `workflow_write` and `workflow` does not support "
        rf"`{filter_key}`; remove `workflow\.{filter_key}` and use explicit `tool_approval\.rules` "
        r"matching `workflow_read`, `workflow_write` instead"
    )
    with pytest.raises(ConfigRuntimeValidationError, match=expected) as exc_info:
        Config.validate_with_runtime(data, runtime_paths)
    assert "replace `workflow`" not in str(exc_info.value)

    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("workflow", {"_source": "ui", filter_key: ["workflow"]})
    with pytest.raises(ToolConfigOverrideError, match=expected) as stored_exc:
        get_tool_by_name(
            "workflow",
            runtime_paths,
            tools=builtin_tool_registry(),
            credentials_manager=manager,
            worker_target=None,
        )
    assert "replace `workflow`" not in str(stored_exc.value)
