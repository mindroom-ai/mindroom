"""Behavioral coverage for durable timed thread approvals."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal
from unittest.mock import MagicMock

import nio
import pytest
from agno.models.response import ToolExecution

from mindroom.approval_inbound import parse_approval_response_event
from mindroom.approval_manager import ApprovalActionResult, _ApprovalManager
from mindroom.approval_receipt import build_approval_receipt
from mindroom.approval_response import ApprovalResponseCoordinator
from mindroom.approval_transport import _approval_delivery_content
from mindroom.config.approval import ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalDecisionMetadata,
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    MatrixDelivery,
)
from mindroom.matrix.large_messages import content_fits_normal_event
from mindroom.mcp.config import MCPServerConfig
from mindroom.message_target import MessageTarget
from mindroom.tool_approval_grants import ApprovalOperation, grant_operation
from tests.conftest import test_runtime_paths
from tests.journal_membership_helpers import admit_room_membership

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


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

    manager.send_delivery = send
    try:
        card = await _card(journal, manager, "first")
        await _approve(manager, card)
        grant = await journal.principal("router@shared").approval_grant_for_card(
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

    manager.send_delivery = send
    try:
        card = await _card(journal, manager, "first")
        monkeypatch.setattr("time.time_ns", lambda: 9_000_000_000_000_000_000)
        await _approve(manager, card)
        final = next(delivery for delivery in sent if delivery.stage is DeliveryStage.FINAL)
        assert final.payload["status"] == "expired"
        assert "auto_approval" not in final.payload
        assert (
            await journal.principal("router@shared").approval_grant_for_card(room_id="!room:test", card_event_id=card)
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
    monkeypatch.setattr("mindroom.approval_manager._MANAGER", manager)
    responder = journal.principal("agent@code")
    config = Config(tool_approval=ToolApprovalConfig(default="require_approval"))
    coordinator = ApprovalResponseCoordinator(
        config=lambda: config,
        runtime_paths=test_runtime_paths(tmp_path),
        store=responder,
        delivery_gateway=MagicMock(spec=DeliveryGateway),
        retry_sources=lambda _room, _sources: None,
    )
    tool = ToolExecution(
        tool_call_id="call-authored",
        tool_name="shell",
        tool_args={"command": "true"},
        requires_confirmation=True,
        approval_type=approval_type,
    )
    plan = await coordinator.plan_pause(((tool, "call-authored", "shell", "code"),), requester_id="@human:test")
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
    continuation = ApprovalContinuation(
        approval_id="authored",
        run_id="run",
        session_id="session",
        entity_kind="agent",
        entity_name="code",
        room_id="!room:test",
        thread_id="$thread",
        requester_id="@human:test",
        response_event_id="$waiting",
        source_event_ids=("$source-authored",),
        calls=plan.calls,
        state="waiting",
        runtime_generation="runtime",
    )
    assert await responder.create_approval_continuation(continuation) is not None
    try:
        await coordinator.publish_generation(
            continuation,
            plan,
            target=MessageTarget(
                room_id="!room:test",
                source_thread_id="$thread",
                resolved_thread_id="$thread",
                reply_to_event_id=None,
                session_id="session",
            ),
            failure_reason="publication failed",
        )
        card = await journal.principal("router@shared").pending_approval_card(
            room_id="!room:test",
            card_event_id="$authored-0-0",
        )
        assert card is not None
        if approval_type == "mindroom_policy":
            assert card.card["content"]["auto_approve_options"] == [300, 600, 1800]
        else:
            assert "auto_approve_options" not in card.card["content"]
            await _approve(manager, "$authored-0-0")
            current = await responder.approval_continuation("authored")
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
    monkeypatch.setattr("mindroom.approval_manager._MANAGER", manager)
    responder = journal.principal("agent@code")
    config = Config(tool_approval=ToolApprovalConfig(default="require_approval"))
    coordinator = ApprovalResponseCoordinator(
        config=lambda: config,
        runtime_paths=test_runtime_paths(tmp_path),
        store=responder,
        delivery_gateway=MagicMock(spec=DeliveryGateway),
        retry_sources=lambda _room, _sources: None,
    )
    sent = []

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery)
        return "$" + delivery.delivery_id

    manager.send_delivery = send
    try:
        for name in ("origin", "reuse"):
            tool = ToolExecution(
                tool_call_id="call-" + name,
                tool_name="shell",
                tool_args={"command": name},
                requires_confirmation=True,
                approval_type="mindroom_policy",
            )
            plan = await coordinator.plan_pause(((tool, "call-" + name, "shell", "code"),), requester_id="@human:test")
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
            continuation = await coordinator.create(
                ApprovalContinuation(
                    approval_id=name,
                    run_id="run-" + name,
                    session_id="session",
                    entity_kind="agent",
                    entity_name="code",
                    room_id="!room:test",
                    thread_id="$thread",
                    requester_id="@human:test",
                    response_event_id="$waiting-" + name,
                    source_event_ids=("$source-" + name,),
                    calls=plan.calls,
                    state="waiting",
                    runtime_generation="runtime",
                ),
            )
            await coordinator.publish_generation(
                continuation,
                plan,
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
                stored = await responder.approval_continuation(name)
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

    manager.send_delivery = send
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

    manager.send_delivery = send
    try:
        first = await _card(journal, manager, "first")
        await _approve(manager, first)
        sent.clear()
        await _card(journal, manager, "subsequent")
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
        assert (
            await journal.principal("router@shared").pending_approval_card(
                room_id="!room:test",
                card_event_id="$card-subsequent",
            )
            is None
        )
        continuation = await journal.principal("agent@code").approval_continuation("subsequent")
        assert continuation is not None
        assert continuation.state == "ready"
        assert continuation.calls[0].decision.value == "approved"
        audit = await journal.backend.read(
            lambda transaction: transaction.fetchone(
                "SELECT continuation_id, tool_call_id, grant_id FROM approval_grant_cards WHERE delivery_id = ?",
                ("card-subsequent",),
            ),
        )
        assert audit is not None
        assert audit["continuation_id"] == "subsequent"
        assert audit["tool_call_id"] == "call-subsequent"
        assert audit["grant_id"]
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

    manager.send_delivery = send
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
    manager.sending_device = lambda: "test-device"
    delivered: list[MatrixDelivery] = []
    try:
        first = await _card(journal, manager, "first")
        await _approve(manager, first)

        async def fail_send(_delivery: MatrixDelivery) -> str:
            msg = "Matrix is temporarily unavailable"
            raise TimeoutError(msg)

        manager.send_delivery = fail_send
        await _card(journal, manager, "deferred", command="original command")
        await manager.cards.maintain_approval_grants()
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

    manager.send_delivery = recover_send
    manager.sending_device = lambda: recovery_device

    async def no_receipt_in_history(_delivery: MatrixDelivery) -> str | None:
        return None

    manager.resolve_delivery = no_receipt_in_history
    try:
        await manager.recover_cards_on_startup()
        assert len(delivered) == 1
        assert delivered[0].payload["arguments"] == {"command": "original command"}
        assert (
            await manager.cards.load_matrix_delivery(delivery_id="card-deferred", stage=DeliveryStage.INITIAL) is None
        )
        assert await manager.cards.is_terminal_approval_card(
            room_id="!room:test",
            card_event_id="$receipt-deferred",
        )
        await manager.recover_cards_on_startup()
        assert len(delivered) == 1
        continuation = await journal.principal("agent@code").approval_continuation("deferred")
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

        async def refuse_arguments(_room: str, _thread: str | None, content: dict) -> dict:
            return {**content, "approvable": False}

        manager.prepare_event = refuse_arguments
        await _card(journal, manager, "refused")
        continuation = await journal.principal("agent@code").approval_continuation("refused")
        assert continuation is not None
        assert continuation.calls[0].decision is None
        stored = await journal.principal("router@shared").pending_approval_card(
            room_id="!room:test",
            card_event_id="$card-refused",
        )
        assert stored is not None
        assert "auto_approve_options" not in stored.card["content"]
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
        continuation = await journal.principal("agent@code").approval_continuation("binding-changed")
        assert continuation is not None
        assert continuation.calls[0].decision is None
        await admit_room_membership(journal.principal("agent@code"), "!room:test", "leave")
        await admit_room_membership(journal.principal("agent@code"), "!room:test", "join")
        await _card(journal, manager, "rejoined")
        continuation = await journal.principal("agent@code").approval_continuation("rejoined")
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
            await responder.request_approval_failure(
                "second",
                "execution binding changed",
                expected_state="waiting",
                expected_generation=0,
                expected_runtime_generation=None,
            )
        else:
            await journal.principal("router@shared").resolve_continuation_approval_card(
                card_event_id=second,
                requested_status=terminal,
                reason="human declined",
                metadata=ApprovalDecisionMetadata(),
            )
        await _approve(manager, first)
        continuation = await responder.approval_continuation("second")
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
        await manager.handle_card_response(
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=card,
            status="approved",
            reason=None,
            auto_approve_seconds=600,
            current_binding="new-binding",
            authorize_responder=lambda _agent: True,
        )
        continuation = await journal.principal("agent@code").approval_continuation("first")
        assert continuation is not None
        assert continuation.calls[0].decision is None
        assert (
            await journal.principal("router@shared").approval_grant_for_card(room_id="!room:test", card_event_id=card)
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

    manager.send_delivery = send
    try:
        card = await _card(journal, manager, "first")
        await _approve(manager, card)
        grant = await manager.cards.approval_grant_for_card(room_id="!room:test", card_event_id=card)
        assert grant is not None
        await manager.handle_grant_revocation(
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=card,
            grant_id=grant.grant_id,
            authorize_responder=lambda _agent: True,
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

    manager.send_delivery = recovered_send
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
async def test_matching_card_does_not_advertise_a_revoke_target_it_does_not_own(tmp_path: Path) -> None:
    """Only the origin card can revoke; automatic decisions still retain audit references."""
    journal = EventJournalStore.open_sqlite(tmp_path / "origin.db")
    manager = _manager(journal, tmp_path)
    sent = []

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery)
        return "$" + delivery.delivery_id

    manager.send_delivery = send
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
        continuation = await journal.principal("agent@code").approval_continuation("racing")
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
        await manager.handle_card_response(
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=card,
            status=status,
            reason=None,
            authorize_responder=lambda _agent: True,
        )
        await manager.recover_cards_on_startup()
        scopes = await journal.backend.read(
            lambda transaction: transaction.fetchall(
                "SELECT delivery_id FROM approval_grant_cards WHERE principal_id = ?",
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
        grant = await owner.approval_grant_for_card(room_id="!room:test", card_event_id=card)
        assert grant is not None
        await manager.recover_cards_on_startup()
        assert '"command": "first"' in await _grant_payload(journal, grant.grant_id)
        if terminal == "expiry":
            monkeypatch.setattr("time.time_ns", lambda: 2_000_000_600_000_000_000)
        else:
            await admit_room_membership(journal.principal(terminal), "!room:test", "leave")
        await manager.recover_cards_on_startup()
        assert await _grant_payload(journal, grant.grant_id) == ""
        assert await owner.approval_grant_for_card(room_id="!room:test", card_event_id=card) == grant
        assert (
            await owner.revoke_approval_grant(
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

    manager.send_delivery = send
    try:
        card = await _card(journal, manager, "first")
        await _approve(manager, card)
        grant = await owner.approval_grant_for_card(room_id="!room:test", card_event_id=card)
        assert grant is not None
        delivery_id = await owner.revoke_approval_grant(
            room_id="!room:test",
            card_event_id=card,
            sender_id="@human:test",
            grant_id=grant.grant_id,
        )
        assert delivery_id is not None
        monkeypatch.setattr("time.time_ns", lambda: 2_000_000_600_000_000_000)
        await manager.recover_cards_on_startup()
        assert await _grant_payload(journal, grant.grant_id)
        pending = await owner.load_matrix_delivery(delivery_id=delivery_id, stage=DeliveryStage.FINAL)
        assert pending is not None
        assert pending.acknowledged_event_id is None
        fail_revocation = False
        await manager.recover_cards_on_startup()
        await manager.recover_cards_on_startup()
        assert await _grant_payload(journal, grant.grant_id) == ""
        assert await owner.load_matrix_delivery(delivery_id=delivery_id, stage=DeliveryStage.FINAL) is None
        sent_count = len(delivered)
        assert (
            await owner.revoke_approval_grant(
                room_id="!room:test",
                card_event_id=card,
                sender_id="@human:test",
                grant_id=grant.grant_id,
            )
            == delivery_id
        )
        await manager.recover_cards_on_startup()
        assert len(delivered) == sent_count
        retained = await owner.approval_grant_for_card(room_id="!room:test", card_event_id=card)
        assert retained is not None
        assert retained.revoked_at_ns is not None
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
    manager: _ApprovalManager,
    name: str,
    *,
    agent: str = "code",
    requester: str = "@human:test",
    thread: str | None = "$thread",
    operation: str | None = "binding:shell",
    approvable: bool = True,
    command: str | None = None,
) -> str:
    responder = journal.principal("agent@" + agent)
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
    continuation = ApprovalContinuation(
        approval_id=name,
        run_id="run-" + name,
        session_id="session-" + name,
        entity_kind="agent",
        entity_name=agent,
        room_id="!room:test",
        thread_id=thread,
        requester_id=requester,
        response_event_id="$waiting-" + name,
        source_event_ids=("$source-" + name,),
        calls=(
            ApprovalCall(
                tool_call_id="call-" + name,
                tool_name="shell",
                invoking_agent=agent,
                expires_at_ns=9_000_000_000_000_000_000,
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
        tool_name="shell",
        arguments={"command": name if command is None else command} if approvable else {"command": "x" * 300000},
        room_id="!room:test",
        requester_id=requester,
        approver_user_id=requester,
        expires_at_ns=9_000_000_000_000_000_000,
        agent_name=agent,
        thread_id=thread,
        grant_operation=ApprovalOperation(operation, "shell") if operation is not None else None,
    )
    assert card is not None
    assert await manager.reserve_and_publish(
        continuation_principal_id=responder.principal_id,
        continuation_id=name,
        continuation_generation=0,
        cards=(card,),
    )
    return "$card-" + name


def _manager(journal: EventJournalStore, tmp_path: Path) -> _ApprovalManager:
    async def prepare(_room: str, _thread: str | None, content: dict) -> dict:
        return content

    async def send(delivery: MatrixDelivery) -> str:
        return "$" + delivery.delivery_id + ("-edit" if delivery.stage is DeliveryStage.FINAL else "")

    return _ApprovalManager(
        test_runtime_paths(tmp_path),
        cards=journal.principal("router@shared"),
        prepare_event=prepare,
        send_delivery=send,
        transport_sender=lambda: "@router:test",
    )


async def _approve(
    manager: _ApprovalManager,
    card: str,
    *,
    seconds: int = 600,
    sender: str = "@human:test",
) -> ApprovalActionResult:
    return await manager.handle_card_response(
        room_id="!room:test",
        sender_id=sender,
        card_event_id=card,
        status="approved",
        reason=None,
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
            continuation = await journal.principal("agent@code").approval_continuation(name)
            assert continuation is not None
            assert continuation.calls[0].decision.value == "approved"
        for name, agent in (("other-agent", "helper"), ("other-operation", "code"), ("other-requester", "code")):
            continuation = await journal.principal("agent@" + agent).approval_continuation(name)
            assert continuation is not None
            assert continuation.calls[0].decision is None
        grant = await manager.cards.approval_grant_for_card(room_id="!room:test", card_event_id=first)
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
        continuation = await journal.principal("agent@code").approval_continuation("later")
        assert continuation is not None
        assert continuation.calls[0].decision.value == "approved"
        await _approve(manager, first)
        monkeypatch.setattr("time.time_ns", lambda: 2_000_000_600_000_000_000)
        await _card(journal, manager, "expired")
        continuation = await journal.principal("agent@code").approval_continuation("expired")
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
        grant = await manager.cards.approval_grant_for_card(room_id="!room:test", card_event_id=first)
        assert grant is not None
        result = await manager.handle_grant_revocation(
            room_id="!room:test",
            sender_id="@other:test",
            card_event_id=first,
            grant_id=grant.grant_id,
            authorize_responder=lambda _agent: True,
        )
        assert not result.resolved
        result = await manager.handle_grant_revocation(
            room_id="!room:test",
            sender_id="@human:test",
            card_event_id=first,
            grant_id=grant.grant_id,
            authorize_responder=lambda _agent: True,
        )
        assert result.resolved
        await _approve(manager, first)
        await _card(journal, manager, "after")
        continuation = await journal.principal("agent@code").approval_continuation("after")
        assert continuation is not None
        assert continuation.calls[0].decision is None
        grant = await manager.cards.approval_grant_for_card(room_id="!room:test", card_event_id=first)
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
        continuation = await journal.principal("agent@code").approval_continuation("first")
        assert continuation is not None
        assert continuation.calls[0].decision is None
    finally:
        await manager.shutdown()
        await journal.close()
