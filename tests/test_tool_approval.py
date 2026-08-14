"""Tests for Matrix-backed tool approval state."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom import approval_transport
from mindroom.approval_events import PendingApproval, parse_approval_datetime
from mindroom.approval_manager import (
    _ApprovalManager,
    _build_event_arguments_preview,
    _build_full_event_arguments,
    get_approval_store,
    initialize_approval_store,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.config.models import ModelConfig
from mindroom.constants import DURABLE_FINAL_OUTCOME_KEY
from mindroom.entity_resolution import entity_identity_registry, mindroom_user_id
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalCardReservation,
    ApprovalContinuation,
    DeliveryAcknowledgement,
    DeliveryStage,
    DepartureSource,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    MatrixDelivery,
    UnreadableApprovalCard,
    delivery_transaction_id,
    unavailable_notice_delivery_id,
)
from mindroom.matrix.message_builder import build_message_content
from mindroom.tool_approval import (
    MatrixApprovalAction,
    ToolApprovalScriptError,
    evaluate_tool_approval,
    handle_matrix_approval_action,
    resolve_tool_approval_approver,
    shutdown_approval_runtime,
    tool_may_require_approval,
)
from mindroom.tools import approved_egress as _approved_egress  # noqa: F401 - registers the approval exemption
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


@pytest.fixture(autouse=True)
def reset_approval_store() -> Generator[None, None, None]:
    asyncio.run(shutdown_approval_runtime())
    yield
    asyncio.run(shutdown_approval_runtime())


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
        ),
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths, usernames={"router": "mindroom_router", "code": "mindroom_code"})
    return config


@pytest.mark.asyncio
async def test_terminal_approval_action_is_consumed_without_delivery_recovery(tmp_path: Path) -> None:
    """A terminal tombstone remains authoritative without consulting transport."""
    cards = MagicMock()
    cards.pending_approval_card = AsyncMock(return_value=None)
    cards.is_terminal_approval_card = AsyncMock(return_value=True)
    initialize_approval_store(test_runtime_paths(tmp_path), cards=cards, send_delivery=AsyncMock())
    before_consume = AsyncMock()

    result = await handle_matrix_approval_action(
        MatrixApprovalAction(
            room_id="!room:localhost",
            sender_id="@approver:localhost",
            card_event_id="$approval",
            status="approved",
            reason=None,
        ),
        before_consume=before_consume,
    )

    assert result.consumed is True
    before_consume.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_decided_card_action_is_consumed_before_transport_or_approver_validation(tmp_path: Path) -> None:
    """A duplicate click cannot escape while its deterministic terminal edit is still owed."""
    cards = MagicMock()
    cards.pending_approval_card = AsyncMock(return_value=MagicMock(resolution={"status": "approved"}))
    cards.is_terminal_approval_card = AsyncMock()
    manager = initialize_approval_store(test_runtime_paths(tmp_path), cards=cards, send_delivery=AsyncMock())
    before_consume = AsyncMock()

    try:
        result = await handle_matrix_approval_action(
            MatrixApprovalAction(
                room_id="!room:localhost",
                sender_id="@different-user:localhost",
                card_event_id="$approval",
                status="denied",
                reason=None,
            ),
            before_consume=before_consume,
        )
    finally:
        await manager.shutdown()

    assert result.consumed is True
    assert result.resolved is False
    before_consume.assert_awaited_once_with()
    cards.is_terminal_approval_card.assert_not_awaited()


def test_tool_approval_config_coerces_numeric_timeout_strings() -> None:
    """Pydantic should own normal numeric coercion for approval timeouts."""
    config = Config.model_validate(
        {
            "tool_approval": {
                "timeout_days": "7",
                "rules": [{"match": "read_*", "action": "require_approval", "timeout_days": "3"}],
            },
        },
    )

    assert config.tool_approval.timeout_days == 7.0
    assert config.tool_approval.rules[0].timeout_days == 3.0


@pytest.mark.asyncio
async def test_action_binds_its_exact_visible_card_after_changed_device_recovery_misses(tmp_path: Path) -> None:
    """An exact action target closes accepted-before-ack debt without a blind resend."""
    delivery = MatrixDelivery(
        delivery_id="approval-card-1",
        stage=DeliveryStage.INITIAL,
        room_id="!room:localhost",
        membership_epoch=0,
        thread_id="$thread",
        transaction_id="approval-transaction",
        payload={"approval_id": "approval-card-1"},
        edits_event_id=None,
        acknowledged_event_id=None,
        created_at_ns=1,
        event_type="io.mindroom.tool_approval",
        attempted=True,
        sending_device_id="OLD-DEVICE",
    )
    settled = MagicMock(resolution={"status": "expired"})
    cards = MagicMock()
    cards.pending_approval_card = AsyncMock(side_effect=(None, settled))
    cards.is_terminal_approval_card = AsyncMock(return_value=False)
    cards.load_matrix_delivery = AsyncMock(return_value=delivery)
    cards.acknowledge_matrix_delivery = AsyncMock(
        return_value=DeliveryAcknowledgement(settled_event_id="$approval", bound=True),
    )
    resolve_action = AsyncMock(return_value="approval-card-1")
    manager = _ApprovalManager(
        test_runtime_paths(tmp_path),
        send_delivery=AsyncMock(),
        resolve_delivery=AsyncMock(return_value=None),
        resolve_action_delivery=resolve_action,
        cards=cards,
        sending_device=lambda: "NEW-DEVICE",
    )
    before_consume = AsyncMock()

    try:
        result = await manager.handle_card_response(
            room_id="!room:localhost",
            sender_id="@approver:localhost",
            card_event_id="$approval",
            status="approved",
            reason=None,
            before_consume=before_consume,
        )
    finally:
        await manager.shutdown()

    assert result.consumed is True
    assert result.resolved is False
    resolve_action.assert_awaited_once_with("!room:localhost", "$approval")
    cards.acknowledge_matrix_delivery.assert_awaited_once_with(
        delivery_id="approval-card-1",
        stage=DeliveryStage.INITIAL,
        event_id="$approval",
        delivered_projections=(),
    )
    before_consume.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_action_delivery_resolver_reads_the_exact_router_card(tmp_path: Path) -> None:
    event = MagicMock(
        event_id="$approval",
        sender="@mindroom_router:localhost",
        source={
            "event_id": "$approval",
            "room_id": "!room:localhost",
            "type": "io.mindroom.tool_approval",
            "content": {"approval_id": "approval-card-1"},
        },
    )
    response = nio.RoomGetEventResponse()
    response.event = event
    client = MagicMock(
        user_id="@mindroom_router:localhost",
        room_get_event=AsyncMock(return_value=response),
    )
    router = MagicMock(
        agent_name="router",
        running=True,
        client=client,
        approval_room_ids=frozenset({"!room:localhost"}),
    )
    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda name: router if name == "router" else None,
        cards_provider=lambda: None,
    )

    assert await transport.resolve_approval_action_delivery("!room:localhost", "$approval") == "approval-card-1"
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$approval")


@pytest.mark.asyncio
async def test_action_delivery_resolver_retries_an_unreadable_exact_card(tmp_path: Path) -> None:
    client = MagicMock(
        user_id="@mindroom_router:localhost",
        room_get_event=AsyncMock(return_value=nio.RoomGetEventError("not found")),
    )
    router = MagicMock(
        agent_name="router",
        running=True,
        client=client,
        approval_room_ids=frozenset({"!room:localhost"}),
    )
    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda name: router if name == "router" else None,
        cards_provider=lambda: None,
    )

    with pytest.raises(approval_transport.ToolApprovalTransportError, match="could not verify"):
        await transport.resolve_approval_action_delivery("!room:localhost", "$approval")


@pytest.mark.asyncio
async def test_click_binds_a_card_accepted_before_its_acknowledgement(tmp_path: Path) -> None:
    """The first post-crash click binds its exact generic delivery before deciding."""
    journal = EventJournalStore.open_sqlite(tmp_path / "approval-click-before-ack.db")
    responder = journal.principal("agent@code")
    router = journal.principal("router@shared")
    room_id = "!room:localhost"
    source_event_id = "$source"
    card_event_id = "$approval"
    approval_id = "approval-1"
    card_delivery_id = "approval-card-1"
    await responder.admit(
        InboundEvent(
            event_id=source_event_id,
            room_id=room_id,
            thread_id="$thread",
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1_000,
            source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "run it"}},
        ),
    )
    continuation = ApprovalContinuation(
        approval_id=approval_id,
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="code",
        room_id=room_id,
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=(source_event_id,),
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="shell",
                invoking_agent="code",
                expires_at_ns=9_000_000_000_000_000_000,
            ),
        ),
        state="waiting",
        runtime_generation="runtime-a",
    )
    assert await responder.create_approval_continuation(continuation) == continuation
    requested_at = datetime.now(UTC)
    card_content = _ApprovalManager._pending_event_content(
        approval_id=card_delivery_id,
        tool_name="shell",
        arguments={"command": "true"},
        arguments_truncated=False,
        agent_name="code",
        thread_id="$thread",
        requester_id="@user:localhost",
        approver_user_id="@approver:localhost",
        requested_at=requested_at,
        expires_at=requested_at + timedelta(days=1),
        status="pending",
    )
    card_content.update(
        continuation_id=approval_id,
        continuation_generation=0,
        tool_call_id="call-1",
    )
    assert await router.reserve_approval_card_deliveries(
        continuation_principal_id=responder.principal_id,
        continuation_id=approval_id,
        expected_generation=0,
        cards=(
            ApprovalCardReservation(
                delivery_id=card_delivery_id,
                tool_call_id="call-1",
                event_type="io.mindroom.tool_approval",
                payload=card_content,
            ),
        ),
    )
    assert await router.claim_matrix_delivery(
        delivery_id=card_delivery_id,
        stage=DeliveryStage.INITIAL,
    )
    await router.record_matrix_delivery_device(
        delivery_id=card_delivery_id,
        stage=DeliveryStage.INITIAL,
        device_id="DEVICE",
    )
    sent: list[DeliveryStage] = []

    async def send(delivery: MatrixDelivery) -> str:
        sent.append(delivery.stage)
        return card_event_id if delivery.stage is DeliveryStage.INITIAL else "$terminal-edit"

    cards = MagicMock(wraps=router)
    cards.principal_id = router.principal_id
    manager = _ApprovalManager(
        test_runtime_paths(tmp_path),
        send_delivery=send,
        cards=cards,
        resolve_action_delivery=AsyncMock(return_value=card_delivery_id),
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: "DEVICE",
    )
    try:
        result = await manager.handle_card_response(
            room_id=room_id,
            sender_id="@approver:localhost",
            card_event_id=card_event_id,
            status="approved",
            reason=None,
        )

        assert result.consumed is True
        assert result.resolved is True
        assert sent == [DeliveryStage.FINAL]
        decided = await responder.approval_continuation(approval_id)
        assert decided is not None
        assert decided.calls[0].decision.value == "approved"
        assert await router.is_terminal_approval_card(room_id=room_id, card_event_id=card_event_id)
    finally:
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_continuation_decision_wakes_its_owning_bot_sources(tmp_path: Path) -> None:
    """Router-owned card decisions wake the entity journal that owns the paused run."""
    owner = MagicMock(running=True)
    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda name: owner if name == "code" else None,
        cards_provider=lambda: None,
    )

    await transport._wake_continuation_sources("code", ("$source-1", "$source-2"))

    owner.retry_approval_sources.assert_called_once_with(("$source-1", "$source-2"))


@pytest.mark.asyncio
async def test_startup_recovery_skips_a_malformed_expiry_without_starving_later_cards(tmp_path: Path) -> None:
    """One corrupt visible deadline cannot abort the room's remaining recovery page."""
    cards = MagicMock()
    cards.unacknowledged_matrix_deliveries = AsyncMock(return_value=())
    cards.pending_approval_room_ids = AsyncMock(return_value=("!room:localhost",))
    cards.pending_approval_cards = AsyncMock(
        return_value=(
            MagicMock(
                resolution=None,
                card={"content": {"expires_at": "not-a-datetime"}},
                created_at_ns=1,
                delivery_id="malformed-card",
            ),
            MagicMock(
                resolution={"status": "denied"},
                card={"content": {"expires_at": "2030-01-01T00:00:00+00:00"}},
                created_at_ns=2,
                delivery_id="recoverable-card",
            ),
        ),
    )
    manager = _ApprovalManager(
        test_runtime_paths(tmp_path),
        send_delivery=AsyncMock(),
        cards=cards,
        sending_device=lambda: "DEVICE",
    )

    try:
        with patch.object(manager, "_expire_stored", new=AsyncMock(return_value=True)) as expire:
            sweep = await manager.recover_cards_on_startup()

        assert sweep.scanned == 2
        assert sweep.discarded == 1
        assert sweep.failed == 1
        expire.assert_awaited_once()
        assert expire.await_args.args[1].delivery_id == "recoverable-card"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_startup_recovery_logs_a_deferred_terminal_flush(tmp_path: Path) -> None:
    """A retryable terminal delivery failure retains its exact recovery context in logs."""
    card_event_id = "$approval"
    room_id = "!room:localhost"
    stored = MagicMock(
        delivery_id="approval-card-1",
        card_event_id=card_event_id,
        card={},
        resolution={"status": "denied"},
    )
    manager = _ApprovalManager(
        test_runtime_paths(tmp_path),
        cards=MagicMock(),
        send_delivery=AsyncMock(),
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    worker = MagicMock(flush=AsyncMock(side_effect=RuntimeError("homeserver unavailable")))

    try:
        with (
            patch.object(manager, "_trusted_pending_from_card_event", return_value=MagicMock()),
            patch.object(manager, "_worker", return_value=worker),
            patch("mindroom.approval_manager.logger.warning") as warning,
        ):
            assert await manager._expire_stored(room_id, stored) is False

        warning.assert_called_once_with(
            "approval_terminal_delivery_deferred",
            delivery_id="approval-card-1",
            room_id=room_id,
            exc_info=True,
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_startup_recovery_counts_an_unreadable_card_as_failed_debt(tmp_path: Path) -> None:
    """A corrupt durable row keeps startup cleanup retryable instead of disappearing."""
    cards = MagicMock()
    cards.unacknowledged_matrix_deliveries = AsyncMock(return_value=())
    cards.pending_approval_room_ids = AsyncMock(return_value=("!room:localhost",))
    cards.pending_approval_cards = AsyncMock(
        return_value=(
            UnreadableApprovalCard(
                delivery_id="corrupt-card",
                created_at_ns=1,
                continuation_id="approval-1",
            ),
        ),
    )
    manager = _ApprovalManager(
        test_runtime_paths(tmp_path),
        cards=cards,
        send_delivery=AsyncMock(),
    )

    try:
        sweep = await manager.recover_cards_on_startup()

        assert sweep.scanned == 1
        assert sweep.failed == 1
        assert sweep.complete is False
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_startup_recovery_drops_a_transport_failure_settled_by_the_same_pass(tmp_path: Path) -> None:
    """A successful immediate retry must not report delivery debt that is gone."""
    delivery_id = "approval-card-1"
    cards = MagicMock()
    cards.pending_approval_room_ids = AsyncMock(return_value=("!room:localhost",))
    cards.pending_approval_cards = AsyncMock(
        return_value=(
            MagicMock(
                resolution={"status": "denied"},
                created_at_ns=1,
                delivery_id=delivery_id,
            ),
        ),
    )
    worker = MagicMock()
    worker.recover = AsyncMock(
        return_value=MagicMock(
            failed=1,
            failed_deliveries=frozenset({(delivery_id, DeliveryStage.FINAL)}),
        ),
    )
    manager = _ApprovalManager(
        test_runtime_paths(tmp_path),
        cards=cards,
        send_delivery=AsyncMock(),
    )

    try:
        with (
            patch.object(manager, "_worker", return_value=worker),
            patch.object(manager, "_expire_stored", new=AsyncMock(return_value=True)),
        ):
            sweep = await manager.recover_cards_on_startup()

        assert sweep.discarded == 1
        assert sweep.failed == 0
        assert sweep.complete is True
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_live_resolution_logs_room_context_when_terminal_flush_is_deferred(tmp_path: Path) -> None:
    """A live decision retains enough context to diagnose its retryable terminal debt."""
    room_id = "!room:localhost"
    cards = MagicMock()
    cards.resolve_continuation_approval_card = AsyncMock(
        return_value=MagicMock(resolution={"status": "denied"}, recorded=False, continuation_ready=False),
    )
    manager = _ApprovalManager(
        test_runtime_paths(tmp_path),
        cards=cards,
        send_delivery=AsyncMock(),
    )
    worker = MagicMock(flush=AsyncMock(side_effect=RuntimeError("homeserver unavailable")))
    pending = MagicMock(card_event_id="$approval", room_id=room_id)
    stored = MagicMock(delivery_id="approval-card-1")

    try:
        with (
            patch.object(manager, "_resolved_event_content", return_value={"status": "denied"}),
            patch.object(manager, "_worker", return_value=worker),
            patch("mindroom.approval_manager.logger.warning") as warning,
        ):
            assert not await manager._record_and_flush_resolution(
                pending,
                stored,
                status="denied",
                reason=None,
                resolved_by=None,
            )

        warning.assert_called_once_with(
            "approval_terminal_delivery_deferred",
            delivery_id="approval-card-1",
            room_id=room_id,
            exc_info=True,
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_deadline_sweep_expires_an_unacknowledged_card_and_wakes_its_continuation(tmp_path: Path) -> None:
    """An unknown Matrix outcome remains delivery debt without hiding the expired run."""
    recorded = MagicMock(
        recorded=True,
        continuation_ready=True,
        continuation_entity_name="code",
        source_event_ids=("$source",),
    )
    cards = MagicMock()
    cards.expire_unacknowledged_approval_card = AsyncMock(return_value=recorded)
    wake = AsyncMock()
    manager = _ApprovalManager(
        test_runtime_paths(tmp_path),
        cards=cards,
        continuation_ready=wake,
    )
    stored = MagicMock(
        delivery_id="approval-card-1",
        card_event_id=None,
        resolution=None,
    )

    assert await manager._expire_stored("!room:localhost", stored) is False

    cards.expire_unacknowledged_approval_card.assert_awaited_once_with(delivery_id="approval-card-1")
    wake.assert_awaited_once_with("code", ("$source",))


@pytest.mark.asyncio
async def test_transient_removed_owner_cleanup_rearms_startup_retry(tmp_path: Path) -> None:
    """A failed card edit cannot abandon a removed entity's fenced journal work."""
    continuation = MagicMock(
        approval_id="approval-removed",
        entity_name="removed",
        state="waiting",
        generation=0,
        runtime_generation=None,
    )
    failing = MagicMock(
        approval_id="approval-removed",
        entity_name="removed",
        state="failing",
        generation=0,
        runtime_generation=None,
    )
    principal = MagicMock(
        approval_continuation=AsyncMock(return_value=continuation),
        load_matrix_delivery=AsyncMock(return_value=None),
        request_approval_failure=AsyncMock(return_value=failing),
        discard_unavailable_approval_continuation=AsyncMock(),
    )
    journal = MagicMock(
        approval_continuations_for_entities=AsyncMock(return_value=(("agent@removed", continuation),)),
    )
    journal.principal.return_value = principal
    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda _name: None,
        cards_provider=lambda: None,
        journal_provider=lambda: journal,
        entity_configured=lambda name: name != "removed",
    )
    transport._startup_cleanup_done = True

    with (
        patch(
            "mindroom.approval_transport.approval_manager.get_approval_store",
            return_value=MagicMock(expire_continuation_cards=AsyncMock(return_value=False)),
        ),
        patch.object(transport, "_schedule_startup_cleanup_retry") as schedule_retry,
    ):
        await transport.reconcile_unavailable_entities({"removed"})

    assert transport._startup_cleanup_done is False
    schedule_retry.assert_called_once_with()
    principal.discard_unavailable_approval_continuation.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_unavailable_owner_cleanup_walks_cursor_pages(tmp_path: Path) -> None:
    """Startup cleanup cannot stop after one bounded continuation-owner page."""
    continuations = [MagicMock(approval_id=f"approval-{index}", entity_name="removed") for index in range(3)]
    journal = MagicMock(
        approval_continuations=AsyncMock(
            side_effect=(
                (("agent@removed", continuations[0]), ("agent@removed", continuations[1])),
                (("agent@removed", continuations[2]),),
            ),
        ),
    )
    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda _name: None,
        cards_provider=lambda: None,
        journal_provider=lambda: journal,
        entity_configured=lambda name: name != "removed",
    )

    with (
        patch.object(approval_transport, "_UNAVAILABLE_OWNER_SCAN_LIMIT", 2),
        patch.object(transport, "_discard_unavailable", new=AsyncMock(return_value=True)) as discard,
    ):
        assert await transport._reconcile_unavailable_owner_pages(None)

    assert journal.approval_continuations.await_args_list == [
        call(limit=2, after=None),
        call(limit=2, after=("removed", "approval-1")),
    ]
    assert [call.args[1].approval_id for call in discard.await_args_list] == [
        "approval-0",
        "approval-1",
        "approval-2",
    ]


@pytest.mark.asyncio
async def test_startup_unavailable_cleanup_scans_owners_while_card_recovery_remains_retryable(tmp_path: Path) -> None:
    """One stuck card does not prevent cleanup from settling unrelated continuations."""
    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda _name: None,
        cards_provider=lambda: None,
        journal_provider=lambda: None,
    )
    transport._startup_router_ready_for_cleanup = True
    transport._startup_runtime_support_ready_for_cleanup = True

    with (
        patch.object(
            transport,
            "_recover_approval_cards_on_startup",
            new=AsyncMock(return_value=False),
        ),
        patch.object(
            transport,
            "_reconcile_unavailable_owner_pages",
            new=AsyncMock(return_value=True),
        ) as reconcile,
        patch.object(transport, "_schedule_startup_cleanup_retry") as schedule_retry,
    ):
        await transport._run_startup_cleanup_if_ready()

    reconcile.assert_awaited_once_with(None)
    schedule_retry.assert_called_once_with()
    assert transport._startup_cleanup_done is False


@pytest.mark.asyncio
async def test_removed_owner_cleanup_sends_terminal_notice_before_releasing_sources(tmp_path: Path) -> None:
    """A removed entity's waiting response must not be the last visible lifecycle state."""
    continuation = MagicMock(
        approval_id="approval-removed",
        entity_name="removed",
        room_id="!room:localhost",
        thread_id="$thread",
        response_event_id="$waiting",
        state="waiting",
        generation=0,
        runtime_generation=None,
    )
    failing = MagicMock(
        approval_id="approval-removed",
        entity_name="removed",
        room_id="!room:localhost",
        thread_id="$thread",
        response_event_id="$waiting",
        state="failing",
        generation=0,
        runtime_generation=None,
    )
    frozen_notice: dict[str, object] = {}
    notice_turn_id = unavailable_notice_delivery_id("approval-removed", 0)
    transaction_id = delivery_transaction_id("router@localhost", notice_turn_id, DeliveryStage.FINAL.value)

    async def enqueue_notice(**kwargs: object) -> str:
        frozen_notice.update(kwargs)
        return notice_turn_id

    async def claim_notice(**_kwargs: object) -> MatrixDelivery:
        payload = frozen_notice["payload"]
        assert isinstance(payload, dict)
        return MatrixDelivery(
            delivery_id=notice_turn_id,
            stage=DeliveryStage.FINAL,
            room_id="!room:localhost",
            membership_epoch=0,
            thread_id="$thread",
            transaction_id=transaction_id,
            payload=payload,
            edits_event_id=None,
            acknowledged_event_id=None,
            created_at_ns=1,
            attempted=False,
            sending_device_id=None,
        )

    principal = MagicMock(
        approval_continuation=AsyncMock(return_value=continuation),
        load_matrix_delivery=AsyncMock(return_value=None),
        request_approval_failure=AsyncMock(return_value=failing),
        principal_id="agent@removed",
        discard_unavailable_approval_continuation=AsyncMock(return_value=True),
    )
    notice_store = MagicMock(
        principal_id="router@localhost",
        membership_epoch=AsyncMock(return_value=0),
        enqueue_unavailable_approval_notice=AsyncMock(side_effect=enqueue_notice),
        claim_matrix_delivery=AsyncMock(side_effect=claim_notice),
        record_matrix_delivery_device=AsyncMock(),
        acknowledge_matrix_delivery=AsyncMock(
            return_value=DeliveryAcknowledgement(settled_event_id="$notice", bound=True),
        ),
    )
    journal = MagicMock(
        approval_continuations_for_entities=AsyncMock(return_value=(("agent@removed", continuation),)),
    )
    journal.principal.return_value = principal
    client = MagicMock()
    client.user_id = "@mindroom_router:localhost"
    client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", client.user_id)}
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$notice", room_id="!room:localhost"))
    router = MagicMock(
        agent_name="router",
        running=True,
        client=client,
        approval_room_ids=frozenset({"!room:localhost"}),
        approval_store=notice_store,
    )
    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda name: router if name == "router" else None,
        cards_provider=lambda: notice_store,
        journal_provider=lambda: journal,
        entity_configured=lambda name: name != "removed",
    )

    with patch(
        "mindroom.approval_transport.approval_manager.get_approval_store",
        return_value=MagicMock(expire_continuation_cards=AsyncMock(return_value=True)),
    ):
        await transport.reconcile_unavailable_entities({"removed"})

    content = client.room_send.await_args.kwargs["content"]
    assert content["m.relates_to"]["m.in_reply_to"] == {"event_id": "$waiting"}
    assert "no longer available" in content["body"]
    assert client.room_send.await_args.kwargs["tx_id"] == transaction_id
    principal.discard_unavailable_approval_continuation.assert_awaited_once_with(
        "approval-removed",
        notice_principal_id="router@localhost",
    )


@pytest.mark.asyncio
async def test_removed_owner_cleanup_recovers_frozen_success_through_original_owner(tmp_path: Path) -> None:
    """Unavailable cleanup must recover and finalize FINAL debt through its original principal."""
    journal = EventJournalStore.open_sqlite(tmp_path / "approval-frozen-success.db")
    principal = journal.principal("agent@removed")
    source_event_id = "$source"
    approval_id = "approval-frozen-success"
    await principal.admit(
        InboundEvent(
            event_id=source_event_id,
            room_id="!room:localhost",
            thread_id="$thread",
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1_000,
            source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "run it"}},
        ),
    )
    continuation = ApprovalContinuation(
        approval_id=approval_id,
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="removed",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=(source_event_id,),
        calls=(),
        state="claimed",
        runtime_generation="old-runtime",
    )
    assert await principal.create_approval_continuation(continuation) == continuation
    await principal.enqueue_matrix_delivery(
        delivery_id=source_event_id,
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={
            "msgtype": "m.text",
            "body": "finished",
            DURABLE_FINAL_OUTCOME_KEY: {"terminal_status": "completed"},
        },
    )
    assert await principal.claim_matrix_delivery(delivery_id=source_event_id, stage=DeliveryStage.FINAL) is not None

    recovered: list[tuple[str, str]] = []

    async def recover_final(principal_id: str, observed: ApprovalContinuation) -> bool:
        recovered.append((principal_id, observed.approval_id))
        await principal.acknowledge_matrix_delivery(
            delivery_id=source_event_id,
            stage=DeliveryStage.FINAL,
            event_id="$final-edit",
            delivered_projections=(),
        )
        return await principal.finish_approval_continuation(observed.approval_id)

    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda _name: None,
        cards_provider=lambda: None,
        journal_provider=lambda: journal,
        entity_configured=lambda name: name != "removed",
        recover_unavailable_final=recover_final,
    )

    try:
        expire_cards = AsyncMock()
        with patch(
            "mindroom.approval_transport.approval_manager.get_approval_store",
            return_value=MagicMock(expire_continuation_cards=expire_cards),
        ):
            assert await transport._discard_unavailable(
                "agent@removed",
                continuation,
                "Requesting agent 'removed' is no longer available.",
            )

        expire_cards.assert_not_awaited()
        assert recovered == [("agent@removed", approval_id)]
        assert await principal.approval_continuation(approval_id) is None
        assert not await principal.is_pending(source_event_id)
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_removed_owner_cleanup_adopts_notice_after_matrix_device_change(tmp_path: Path) -> None:
    """A crash after Matrix acceptance must not duplicate the notice after a re-login."""
    journal = EventJournalStore.open_sqlite(tmp_path / "approval-notice.db")
    principal = journal.principal("agent@removed")
    notice_store = journal.principal("router@localhost")
    reason = "Requesting agent 'removed' is no longer available."
    approval_id = "approval-device-change"
    notice_marker = "io.mindroom.approval_unavailable_id"
    source_event_id = "$source"
    waiting_event_id = "$waiting"
    notice_turn_id = unavailable_notice_delivery_id(approval_id, 0)
    await principal.admit(
        InboundEvent(
            event_id=source_event_id,
            room_id="!room:localhost",
            thread_id="$thread",
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1_000,
            source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "run it"}},
        ),
    )
    continuation = ApprovalContinuation(
        approval_id=approval_id,
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="removed",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id=waiting_event_id,
        source_event_ids=(source_event_id,),
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="shell",
                invoking_agent="removed",
                expires_at_ns=9_000_000_000_000_000_000,
            ),
        ),
        state="failing",
        failure_reason=reason,
    )
    assert await principal.create_approval_continuation(continuation) == continuation
    notice_content = build_message_content(
        reason,
        thread_event_id="$thread",
        reply_to_event_id=waiting_event_id,
        extra_content={"msgtype": "m.notice", notice_marker: approval_id},
    )
    await notice_store.enqueue_matrix_delivery(
        delivery_id=notice_turn_id,
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload=notice_content,
    )
    claimed_notice = await notice_store.claim_matrix_delivery(
        delivery_id=notice_turn_id,
        stage=DeliveryStage.FINAL,
    )
    assert claimed_notice is not None
    await notice_store.record_matrix_delivery_device(
        delivery_id=notice_turn_id,
        stage=DeliveryStage.FINAL,
        device_id="OLDDEVICE",
    )

    prior_notice = nio.Event.parse_event(
        {
            "event_id": "$notice-old-device",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 2_000,
            "type": "m.room.message",
            "content": dict(claimed_notice.payload),
        },
    )
    assert isinstance(prior_notice, nio.Event)
    client = MagicMock()
    client.user_id = "@mindroom_router:localhost"
    client.device_id = "NEWDEVICE"
    client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", client.user_id)}
    client.room_send = AsyncMock()
    client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!room:localhost",
            chunk=[prior_notice],
            start="start",
            end=None,
        ),
    )
    router = MagicMock(
        agent_name="router",
        running=True,
        client=client,
        approval_room_ids=frozenset({"!room:localhost"}),
        approval_store=notice_store,
    )
    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda name: router if name == "router" else None,
        cards_provider=lambda: None,
        journal_provider=lambda: journal,
        entity_configured=lambda name: name != "removed",
    )

    try:
        with patch(
            "mindroom.approval_transport.approval_manager.get_approval_store",
            return_value=MagicMock(expire_continuation_cards=AsyncMock(return_value=True)),
        ):
            assert await transport._discard_unavailable("agent@removed", continuation, reason)

        client.room_send.assert_not_awaited()
        client.room_messages.assert_awaited_once()
        delivered = await notice_store.load_matrix_delivery(delivery_id=notice_turn_id, stage=DeliveryStage.FINAL)
        assert delivered is not None
        assert delivered.acknowledged_event_id == "$notice-old-device"
        assert await principal.load_matrix_delivery(delivery_id=notice_turn_id, stage=DeliveryStage.FINAL) is None
        assert await principal.approval_continuation(approval_id) is None
        assert not await principal.is_pending(source_event_id)
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_removed_owner_cleanup_retries_a_stale_notice_in_current_membership(tmp_path: Path) -> None:
    """A stale notice attempt must not permanently strand its continuation."""
    journal = EventJournalStore.open_sqlite(tmp_path / "approval-stale-notice.db")
    principal = journal.principal("agent@removed")
    notice_store = journal.principal("router@localhost")
    approval_id = "approval-stale-notice"
    source_event_id = "$source"
    reason = "Requesting agent 'removed' is no longer available."
    await principal.admit(
        InboundEvent(
            event_id=source_event_id,
            room_id="!room:localhost",
            thread_id="$thread",
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1_000,
            source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "run it"}},
        ),
    )
    continuation = ApprovalContinuation(
        approval_id=approval_id,
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="removed",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=(source_event_id,),
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="shell",
                invoking_agent="removed",
                expires_at_ns=9_000_000_000_000_000_000,
            ),
        ),
        state="failing",
        failure_reason=reason,
    )
    assert await principal.create_approval_continuation(continuation) == continuation
    stale_delivery_id = await notice_store.enqueue_unavailable_approval_notice(
        approval_id=approval_id,
        room_id=continuation.room_id,
        thread_id=continuation.thread_id,
        payload={"msgtype": "m.notice", "body": reason},
    )
    assert stale_delivery_id == unavailable_notice_delivery_id(approval_id, 0)
    assert (
        await notice_store.claim_matrix_delivery(
            delivery_id=stale_delivery_id,
            stage=DeliveryStage.FINAL,
        )
        is not None
    )
    await notice_store.fence_departure(continuation.room_id, source=DepartureSource.LOCAL)
    await notice_store.note_membership_restarted(continuation.room_id)
    await notice_store.acknowledge_matrix_delivery(
        delivery_id=stale_delivery_id,
        stage=DeliveryStage.FINAL,
        event_id="$stale-notice",
        delivered_projections=(),
    )

    client = MagicMock()
    client.user_id = "@mindroom_router:localhost"
    client.device_id = "CURRENTDEVICE"
    client.rooms = {continuation.room_id: nio.MatrixRoom(continuation.room_id, client.user_id)}
    client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$current-notice", room_id=continuation.room_id),
    )
    router = MagicMock(
        agent_name="router",
        running=True,
        client=client,
        approval_room_ids=frozenset({continuation.room_id}),
        approval_store=notice_store,
    )
    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda name: router if name == "router" else None,
        cards_provider=lambda: None,
        journal_provider=lambda: journal,
        entity_configured=lambda name: name != "removed",
    )

    try:
        with patch(
            "mindroom.approval_transport.approval_manager.get_approval_store",
            return_value=MagicMock(expire_continuation_cards=AsyncMock(return_value=True)),
        ):
            assert await transport._discard_unavailable("agent@removed", continuation, reason)

        current_delivery_id = unavailable_notice_delivery_id(approval_id, 1)
        current = await notice_store.load_matrix_delivery(
            delivery_id=current_delivery_id,
            stage=DeliveryStage.FINAL,
        )
        assert current is not None
        assert current.acknowledged_event_id == "$current-notice"
        assert current_delivery_id != stale_delivery_id
        assert await principal.approval_continuation(approval_id) is None
        assert not await principal.is_pending(source_event_id)
        client.room_send.assert_awaited_once()
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_removed_owner_notice_refusal_remains_durable_and_rearms_retry(tmp_path: Path) -> None:
    """After a router rejoin, Matrix refusal leaves the owner and notice queued for retry."""
    journal = EventJournalStore.open_sqlite(tmp_path / "approval-notice-refusal.db")
    principal = journal.principal("agent@removed")
    notice_store = journal.principal("router@localhost")
    approval_id = "approval-refused-notice"
    source_event_id = "$refused-source"
    await principal.admit(
        InboundEvent(
            event_id=source_event_id,
            room_id="!room:localhost",
            thread_id="$thread",
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1_000,
            source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "run it"}},
        ),
    )
    continuation = ApprovalContinuation(
        approval_id=approval_id,
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="removed",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=(source_event_id,),
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="shell",
                invoking_agent="removed",
                expires_at_ns=9_000_000_000_000_000_000,
            ),
        ),
        state="failing",
        failure_reason="Requesting agent 'removed' is no longer available.",
    )
    assert await principal.create_approval_continuation(continuation) == continuation
    await notice_store.admit(
        InboundEvent(
            event_id=continuation.response_event_id,
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.CONTEXT_ONLY,
            sender="@mindroom_removed:localhost",
            origin_server_ts=2_000,
            source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "waiting"}},
        ),
    )
    await notice_store.fence_departure(continuation.room_id, source=DepartureSource.LOCAL)
    await notice_store.note_membership_restarted(continuation.room_id)
    client = MagicMock()
    client.user_id = "@mindroom_router:localhost"
    client.device_id = "DEVICE"
    client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", client.user_id)}
    client.room_send = AsyncMock(return_value=nio.RoomSendError(message="forbidden"))
    router = MagicMock(
        agent_name="router",
        running=True,
        client=client,
        approval_room_ids=frozenset({"!room:localhost"}),
        approval_store=notice_store,
    )
    transport = approval_transport.ApprovalMatrixTransport(
        runtime_paths=test_runtime_paths(tmp_path),
        bot_provider=lambda name: router if name == "router" else None,
        cards_provider=lambda: None,
        journal_provider=lambda: journal,
        entity_configured=lambda name: name != "removed",
    )
    transport._startup_cleanup_done = True

    try:
        with (
            patch(
                "mindroom.approval_transport.approval_manager.get_approval_store",
                return_value=MagicMock(expire_continuation_cards=AsyncMock(return_value=True)),
            ),
            patch.object(transport, "_schedule_startup_cleanup_retry") as schedule_retry,
        ):
            await transport.reconcile_unavailable_entities({"removed"})

        schedule_retry.assert_called_once_with()
        assert await principal.approval_continuation(approval_id) == continuation
        assert await principal.is_pending(source_event_id)
        delivery = await notice_store.load_matrix_delivery(
            delivery_id=unavailable_notice_delivery_id(approval_id, 1),
            stage=DeliveryStage.FINAL,
        )
        assert delivery is not None
        assert delivery.attempted is True
        assert delivery.acknowledged_event_id is None
        assert (
            await principal.load_matrix_delivery(
                delivery_id=unavailable_notice_delivery_id(approval_id, 1),
                stage=DeliveryStage.FINAL,
            )
            is None
        )
    finally:
        await journal.close()


@pytest.mark.parametrize(
    ("tool_approval", "expected_location"),
    [
        ({"timeout_days": True}, ("tool_approval", "timeout_days")),
        (
            {"rules": [{"match": "read_*", "action": "require_approval", "timeout_days": False}]},
            ("tool_approval", "rules", 0, "timeout_days"),
        ),
    ],
)
def test_tool_approval_config_rejects_boolean_timeout_days_with_nested_location(
    tool_approval: dict[str, object],
    expected_location: tuple[object, ...],
) -> None:
    """Only the bool edge case needs custom validation around Pydantic numeric fields."""
    with pytest.raises(ValidationError) as exc_info:
        Config.model_validate({"tool_approval": tool_approval})

    assert expected_location in {tuple(error["loc"]) for error in exc_info.value.errors(include_context=False)}


def _approval_card(
    *,
    approval_id: str = "approval-1",
    event_id: str = "$approval",
    room_id: str = "!room:localhost",
    sender: str = "@mindroom_router:localhost",
    requester: str = "@requester:localhost",
    approver: str = "@user:localhost",
    status: str = "pending",
    origin_server_ts: int | None = None,
    arguments_truncated: bool = False,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    content: dict[str, Any] = {
        "msgtype": "io.mindroom.tool_approval",
        "body": "Approval required: read_file",
        "tool_name": "read_file",
        "tool_call_id": approval_id,
        "continuation_id": f"continuation-{approval_id}",
        "continuation_generation": 0,
        "approval_id": approval_id,
        "arguments": {"path": "notes.txt"},
        "status": status,
        "requester_id": requester,
        "approver_user_id": approver,
        "agent_name": "code",
        "thread_id": "$thread",
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    if arguments_truncated:
        content["arguments_truncated"] = True
    return {
        "event_id": event_id,
        "room_id": room_id,
        "sender": sender,
        "type": "io.mindroom.tool_approval",
        "origin_server_ts": origin_server_ts or int(now.timestamp() * 1000),
        "content": content,
    }


def test_pending_approval_from_card_event_requires_approver_user_id() -> None:
    card = _approval_card()
    card["content"].pop("approver_user_id")

    with pytest.raises(ValueError, match="missing required approval fields"):
        PendingApproval.from_card_event(card, room_id="!room:localhost")


def test_pending_approval_preserves_distinct_requester_and_approver() -> None:
    card = _approval_card(requester="@requester:localhost", approver="@approver:localhost")

    pending = PendingApproval.from_card_event(card, room_id="!room:localhost")

    assert pending.requester_id == "@requester:localhost"
    assert pending.approver_user_id == "@approver:localhost"


def test_parse_approval_datetime_preserves_approval_timestamp_contract() -> None:
    assert parse_approval_datetime(None) is None
    assert parse_approval_datetime("2030-01-01T10:00:00+02:00") == datetime.fromisoformat(
        "2030-01-01T10:00:00+02:00",
    )
    assert parse_approval_datetime("2030-01-01T10:00:00") == datetime(2030, 1, 1, 10, tzinfo=UTC)

    with pytest.raises(ValueError, match="Invalid isoformat string"):
        parse_approval_datetime("not-a-datetime")


def test_approval_arguments_preview_marks_sanitizer_truncation() -> None:
    arguments = {f"k{index}": index for index in range(30)}
    preview, truncated = _build_event_arguments_preview(arguments)

    assert preview["__truncated__"] == "5 more items"
    assert truncated is True

    card = _ApprovalManager._pending_event_content(
        approval_id="approval-1",
        tool_name="read_file",
        arguments=preview,
        arguments_truncated=truncated,
        agent_name="code",
        thread_id=None,
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        requested_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        status="pending",
    )

    assert card["arguments_truncated"] is True


def test_approval_arguments_preview_marks_nested_sanitizer_truncation() -> None:
    arguments = {"items": list(range(30))}
    preview, truncated = _build_event_arguments_preview(arguments)

    assert preview["items"][-1] == "... [truncated]"
    assert truncated is True


def test_approval_arguments_preview_does_not_mark_literal_truncation_marker() -> None:
    arguments = {"note": "literal marker ... [truncated]"}
    preview, truncated = _build_event_arguments_preview(arguments)

    assert preview == arguments
    assert truncated is False


def test_full_event_arguments_returns_complete_payload() -> None:
    arguments = {"content": "x" * 10_000, "path": "notes.txt"}

    assert _build_full_event_arguments(arguments) == arguments


def test_full_event_arguments_redacts_secrets_without_bypassing_truncation_checks() -> None:
    arguments = {"api_key": "sk-live-1234567890abcdef", "content": "x" * 5_000}

    full_arguments = _build_full_event_arguments(arguments)

    assert full_arguments is not None
    assert full_arguments["content"] == "x" * 5_000
    assert "sk-live-1234567890abcdef" not in json.dumps(full_arguments)


def test_full_event_arguments_rejects_payload_over_completeness_cap() -> None:
    assert _build_full_event_arguments({"content": "x" * 3_000_000}) is None


def test_full_event_arguments_accepts_sidecar_sized_payload() -> None:
    payload = {"content": "x" * 100_000}

    assert _build_full_event_arguments(payload) == payload


def test_full_event_arguments_budgets_utf8_bytes_not_characters() -> None:
    # 800k CJK chars stay under a character-based cap but encode to ~2.4MB, over the byte cap.
    assert _build_full_event_arguments({"content": "汉" * 800_000}) is None
    assert _build_full_event_arguments({"content": "汉" * 8_000}) == {"content": "汉" * 8_000}


def test_full_event_arguments_accepts_structurally_complex_payload_below_byte_cap() -> None:
    nested: object = "value"
    for _ in range(20):
        nested = {"nested": nested}
    arguments = {"items": list(range(60_000)), "nested": nested}

    assert _build_full_event_arguments(arguments) == arguments


def test_pending_approval_parses_full_arguments_availability() -> None:
    card = _approval_card(arguments_truncated=True)
    assert PendingApproval.from_card_event(card, room_id="!room:localhost").full_arguments_available is False

    card["content"]["full_arguments"] = {}
    assert PendingApproval.from_card_event(card, room_id="!room:localhost").full_arguments_available is False

    card["content"]["full_arguments"] = {"content": "x" * 10_000}
    assert PendingApproval.from_card_event(card, room_id="!room:localhost").full_arguments_available is True


def test_pending_approval_parses_sidecar_full_arguments_availability() -> None:
    url_card = _approval_card(arguments_truncated=True)
    url_card["content"]["full_arguments_url"] = "mxc://localhost/full-args"
    assert PendingApproval.from_card_event(url_card, room_id="!room:localhost").full_arguments_available is False

    url_card["content"]["full_arguments_info"] = {"size": 10_000, "mimetype": "application/json"}
    assert PendingApproval.from_card_event(url_card, room_id="!room:localhost").full_arguments_available is True

    file_card = _approval_card(arguments_truncated=True)
    file_card["content"]["full_arguments_file"] = {}
    assert PendingApproval.from_card_event(file_card, room_id="!room:localhost").full_arguments_available is False

    file_card["content"]["full_arguments_file"] = {
        "url": "mxc://localhost/full-args",
        "key": {"alg": "A256CTR", "k": "secret", "key_ops": ["encrypt", "decrypt"], "kty": "oct"},
        "iv": "iv-value",
        "hashes": {"sha256": "sha256-value"},
        "v": "v2",
        "size": 10_000,
        "mimetype": "application/json",
    }
    assert PendingApproval.from_card_event(file_card, room_id="!room:localhost").full_arguments_available is True


def test_pending_approval_defaults_missing_approvable_flag_to_true() -> None:
    card = _approval_card(arguments_truncated=True)

    assert PendingApproval.from_card_event(card, room_id="!room:localhost").approvable is True


@pytest.mark.parametrize(("value", "expected"), [(False, False), (True, True), (None, False), ("false", False)])
def test_pending_approval_parses_explicit_approvable_flag(value: object, expected: bool) -> None:
    card = _approval_card(arguments_truncated=True)
    card["content"]["approvable"] = value

    assert PendingApproval.from_card_event(card, room_id="!room:localhost").approvable is expected


def test_resolve_tool_approval_approver_rejects_internal_users(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding", rooms=["!room:localhost"])},
            bot_accounts=["@bridge_bot:localhost"],
            mindroom_user=MindRoomUserConfig(),
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
        ),
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths, usernames={"router": "actual_router", "code": "actual_code"})
    internal_user_id = mindroom_user_id(config, runtime_paths)
    assert internal_user_id is not None
    agent_user_id = entity_identity_registry(config, runtime_paths).current_id("code").full_id

    assert resolve_tool_approval_approver(config, runtime_paths, None) is None
    assert resolve_tool_approval_approver(config, runtime_paths, agent_user_id) is None
    assert resolve_tool_approval_approver(config, runtime_paths, internal_user_id) is None
    assert resolve_tool_approval_approver(config, runtime_paths, "@bridge_bot:localhost") is None
    assert resolve_tool_approval_approver(config, runtime_paths, "@user:localhost") == "@user:localhost"


@pytest.mark.asyncio
async def test_evaluate_tool_approval_rule_action_requires_approval(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={"rules": [{"match": "read_*", "action": "require_approval"}]},
        ),
        runtime_paths,
    )

    requires_approval, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "read_file",
        {"path": "notes.txt"},
        "code",
    )

    assert requires_approval is True
    assert timeout_seconds > 0


@pytest.mark.parametrize(
    ("hostnames", "expected"),
    [
        (["docs.example.com"], False),
        (["docs.example.com", "api.example.com"], False),
        (["docs.example.com", "docs.other.test"], True),
        (["docs.other.test"], True),
        ([123], True),
        (["https://docs.example.com"], True),
        ("docs.example.com", True),
        ([], True),
    ],
)
@pytest.mark.asyncio
async def test_evaluate_tool_approval_honors_tool_approval_exemption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hostnames: object,
    expected: bool,
) -> None:
    """request_network_access calls where every hostname is statically allowlisted need no approval."""
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_ALLOWLIST", ".example.com")
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={"rules": [{"match": "request_network_access", "action": "require_approval"}]},
        ),
        runtime_paths,
    )

    requires_approval, _ = await evaluate_tool_approval(
        config,
        runtime_paths,
        "request_network_access",
        {"hostnames": hostnames, "ttl_minutes": 5, "reason": "Need docs."},
        "code",
    )

    assert requires_approval is expected


@pytest.mark.asyncio
async def test_tool_approval_rule_matching_uses_first_matching_action_for_listing(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={
                "default": "auto_approve",
                "rules": [
                    {"match": "read_*", "action": "auto_approve", "timeout_days": 2},
                    {"match": "read_file", "action": "require_approval", "timeout_days": 9},
                ],
            },
        ),
        runtime_paths,
    )

    requires_approval, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "read_file",
        {"path": "notes.txt"},
        "code",
    )

    assert requires_approval is False
    assert timeout_seconds == 2 * 24 * 60 * 60
    assert tool_may_require_approval(config, "read_file") is False


@pytest.mark.asyncio
async def test_tool_approval_script_rule_listing_requires_approval_but_evaluation_runs_script(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    script_path = tmp_path / "approval.py"
    script_path.write_text(
        "def check(tool_name, arguments, agent_name):\n    return arguments['requires_approval']\n",
        encoding="utf-8",
    )
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={
                "default": "auto_approve",
                "timeout_days": 4,
                "rules": [{"match": "write_*", "script": str(script_path), "timeout_days": 1}],
            },
        ),
        runtime_paths,
    )

    requires_approval, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "write_file",
        {"requires_approval": False},
        "code",
    )

    assert requires_approval is False
    assert timeout_seconds == 24 * 60 * 60
    assert tool_may_require_approval(config, "write_file") is True


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        ("auto_approve", False),
        ("require_approval", True),
    ],
)
@pytest.mark.asyncio
async def test_tool_approval_rule_matching_falls_back_to_default_for_listing(
    tmp_path: Path,
    default: str,
    expected: bool,
) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={
                "default": default,
                "rules": [{"match": "write_*", "action": "require_approval"}],
            },
        ),
        runtime_paths,
    )

    requires_approval, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "read_file",
        {"path": "notes.txt"},
        "code",
    )

    assert requires_approval is expected
    assert timeout_seconds == 7 * 24 * 60 * 60
    assert tool_may_require_approval(config, "read_file") is expected


@pytest.mark.asyncio
async def test_evaluate_tool_approval_script_error_is_sanitized(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    script_path = tmp_path / "approval.py"
    script_path.write_text(
        "def check(tool_name, arguments, agent_name):\n    raise ValueError('boom')\n",
        encoding="utf-8",
    )
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={"rules": [{"match": "read_file", "script": str(script_path)}]},
        ),
        runtime_paths,
    )

    with pytest.raises(ToolApprovalScriptError, match="failed with ValueError"):
        await evaluate_tool_approval(config, runtime_paths, "read_file", {"path": "notes.txt"}, "code")


def test_get_approval_store_returns_initialized_store(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)

    store = initialize_approval_store(runtime_paths)

    assert get_approval_store() is store
