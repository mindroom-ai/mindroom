"""Edit revisions retain durable ownership through native approval pauses."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest
import pytest_asyncio
from agno.models.response import ToolExecution

from mindroom.approval_manager import initialize_approval_store
from mindroom.constants import MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import DeliveryStage, EventClass, EventKind, response_attempts
from mindroom.handled_turns import SourceEventMetadata, TurnRecord, TurnRecordCodec, _reset_handled_turn_ledger_runtime
from mindroom.history.types import HistoryScope
from mindroom.journal_dispatch import JournalDispatcher
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
from mindroom.response_runner import ResponseRunner, _DeliveryProgress
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE, shutdown_approval_runtime
from mindroom.turn_record import canonicalize_turn_record
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target
from tests.test_response_runner_focused import _admit_approval_source, _visible_event_response
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.approval_manager import ApprovalManager
    from mindroom.bot import AgentBot
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.edit_regenerator import EditRegenerator
    from mindroom.event_journal import ApprovalContinuation, EventJournalStore, MatrixDelivery, PrincipalStore
    from mindroom.event_journal.backend import Transaction
    from mindroom.turn_controller import TurnController
    from mindroom.turn_store import TurnStore


@dataclass
class _ApprovalCase:
    bot: AgentBot
    room: nio.MatrixRoom
    store: TurnStore
    principal: PrincipalStore
    gateway: DeliveryGateway
    runner: ResponseRunner
    regenerator: EditRegenerator
    controller: TurnController
    resolver: ConversationResolver
    manager: ApprovalManager
    target: MessageTarget
    event: nio.RoomMessageText
    approval: ApprovalContinuation
    requires_human: bool
    journal_store: EventJournalStore

    async def approve(self) -> None:
        if self.requires_human:
            result = await self.manager.handle_card_response(
                room_id=self.room.room_id,
                sender_id="@user:localhost",
                card_event_id="$approval-card",
                status="approved",
                reason=None,
                authorize_responder=lambda _entity: True,
            )
            assert result.consumed

    async def pause_newer(self) -> ApprovalContinuation:
        event = nio.RoomMessageText.from_dict({**self.event.source, "event_id": "$newer-edit", "origin_server_ts": 30})
        await self.principal.admit(
            _inbound_event(self.room.room_id, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            _projected_event(self.room.room_id, event, EventKind.MESSAGE, self_sender=self.bot.matrix_id.full_id),
        )
        self.regenerator.deps = replace(self.regenerator.deps, receipt_order=AsyncMock(return_value=6))
        pause = PausedAttempt(
            session_id=self.target.session_id,
            run_id="run-newer",
            tools=(
                ToolExecution(
                    tool_call_id="call-newer",
                    tool_name="read_document",
                    requires_confirmation=True,
                    approval_type=POLICY_CONFIRMATION_APPROVAL_TYPE,
                ),
            ),
            response_text="Reading updated document",
            toolkit_owners={("general", "read_document"): "test_toolkit"},
        )
        with (
            patch.object(
                self.resolver,
                "extract_message_context",
                AsyncMock(return_value=MessageContext(False, False, None, [], [], False)),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing,
                should_use_streaming=AsyncMock(return_value=False),
                ai_response=AsyncMock(side_effect=ResponsePausedForApproval(pause)),
            ),
            patch("mindroom.approval_response.evaluate_tool_approval", AsyncMock(return_value=(False, 60.0))),
            # Inspect the durable checkpoint before the test claims resumed execution.
            patch.object(type(self.principal), "claim_approval_continuation", AsyncMock(return_value=None)),
        ):
            assert await self.controller.handle_text_event(self.room, event) is TurnDispatchOutcome.DEFERRED
        newer = await self.principal.approval_continuation_for_source("$newer-edit")
        assert newer is not None
        assert newer.prepared_edit_record.latest_edit_receipt_order == 6
        assert await self.principal.approval_continuation_for_source("$edit") is not None
        return newer

    async def stop(self, order: int = 4) -> None:
        reconciler = UserStopReconciler(UserStopReconcilerDeps(self.store, self.runner, self.gateway))
        assert await reconciler.finalize("$answer", order, AsyncMock())

    async def failed_stop(self) -> None:
        self.bot.client.room_send.side_effect = RuntimeError("Transport unavailable")
        try:
            with pytest.raises(RuntimeError, match="did not become durable"):
                await self.stop()
        finally:
            self.bot.client.room_send.side_effect = None
        failing = await self.principal.approval_continuation(self.approval.approval_id)
        assert failing is not None
        assert failing.state == "failing"
        assert await self.principal.is_pending("$edit")
        assert self.store.get_turn_record("$source").source_event_revisions is None

    async def assert_stopped_edit_settled(self) -> None:
        assert await self.principal.approval_continuation_for_source("$edit") is None
        assert not await self.principal.is_pending("$edit")
        assert self.store.get_turn_record("$source").source_event_revisions is None
        resumed = AsyncMock(side_effect=AssertionError("Stopped approval must not execute"))
        with patch.object(self.runner, "_continue_entity_call", resumed):
            await self.runner.handoff_approval_source("$edit")
            await self.runner.wait_for_source_owned_inbox_responses()
        resumed.assert_not_awaited()

    async def freeze_final(self) -> ApprovalContinuation:
        await self.approve()
        claimed = await self.principal.claim_approval_continuation(
            self.approval.approval_id,
            runtime_generation=self.runner.deps.approval_runtime_generation,
        )
        assert claimed is not None
        assert claimed.prepared_edit_record is not None
        await self.principal.enqueue_matrix_delivery(
            delivery_id="$edit",
            stage=DeliveryStage.FINAL,
            room_id=self.room.room_id,
            thread_id=None,
            payload={"body": "Edited answer", "formatted_body": "Edited answer"},
            edits_event_id="$answer",
            result={"prepared_edit_record": TurnRecordCodec._to_ledger_record(claimed.prepared_edit_record)},
        )
        assert await self.principal.claim_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL) is not None
        return claimed

    async def recover_final(self, claimed: ApprovalContinuation) -> None:
        await self.principal.acknowledge_matrix_delivery(
            delivery_id="$edit",
            stage=DeliveryStage.FINAL,
            event_id="$final-edit",
            delivered_projections=(),
        )
        assert await self.runner._recover_claimed_approval_lifecycle(claimed, target=self.target) == "$answer"
        assert await self.principal.approval_continuation(claimed.approval_id) is None

    async def assert_stop_waits_for_final(self, claimed: ApprovalContinuation, *, order: int = 4) -> None:
        with (
            patch.object(type(self.gateway), "recover_deliveries", AsyncMock()),
            pytest.raises(RuntimeError, match="did not become durable"),
        ):
            await self.stop(order)
        assert (await self.principal.approval_continuation(claimed.approval_id)).state == "claimed"

    async def resume(self, event_id: str = "$edit", *, failure: str | None = None) -> None:
        result = (
            AsyncMock(side_effect=RuntimeError(failure))
            if failure
            else AsyncMock(return_value=CompletedApprovalRun(response_text="Edited answer", metadata_content={}))
        )
        with patch.object(self.runner, "_continue_entity_call", result):
            assert await self.runner.handoff_approval_source(event_id) is False
            await self.runner.wait_for_source_owned_inbox_responses()

    async def restart(self) -> None:
        _reset_handled_turn_ledger_runtime()
        self.store = await _store(self.journal_store, agent_name="general")
        self.gateway = replace(
            self.gateway,
            deps=replace(
                self.gateway.deps,
                terminal_turn_for=self.store.terminal_turn_record,
                terminal_turn_committed=self.store.publish_committed_response,
            ),
        )
        self.runner = ResponseRunner(
            deps=replace(self.runner.deps, delivery_gateway=self.gateway, approval_runtime_generation="restarted"),
        )


@asynccontextmanager
async def _paused_case(  # noqa: PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    *,
    stopped: bool,
    requires_human: bool,
    ordinary_pause: bool = False,
    coalesced_pause: bool = False,
) -> AsyncIterator[_ApprovalCase]:
    bot = _bot(tmp_path)
    room_id, source_id, edit_id, answer_id = "!room:localhost", "$source", "$edit", "$answer"
    room = nio.MatrixRoom(room_id, bot.matrix_id.full_id)
    room.users["@user:localhost"] = nio.MatrixUser("@user:localhost", "User")
    bot.client.rooms[room_id] = room
    bot.client.room_send.return_value = nio.RoomSendResponse("$answer-edit", room_id)
    target = MessageTarget.resolve(room_id, None, source_id, room_mode=True)
    store = await _store(journal_store, agent_name="general")
    store.deps = replace(store.deps, state_writer=bot._conversation_state_writer, resolver=bot._conversation_resolver)
    original_sources = ("$early-source", source_id) if coalesced_pause else (source_id,)
    record_original = store.record_pending_turn if ordinary_pause else store.record_responded_turn
    await record_original(
        TurnRecord.create(
            original_sources,
            response_event_id=answer_id,
            completed=True,
            source_event_prompts=dict.fromkeys(original_sources, "original"),
            source_event_metadata=(
                {event_id: SourceEventMetadata(sender="@user:localhost") for event_id in original_sources}
                if coalesced_pause
                else None
            ),
            requester_id="@user:localhost",
            response_owner="general",
            conversation_target=target,
            history_scope=HistoryScope(kind="agent", scope_id="general"),
        ),
    )
    if stopped:
        await store.record_user_stopped_response(answer_id, 1)
    principal = journal_store.principal("general@@mindroom_general:localhost")
    original = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": source_id,
            "sender": "@user:localhost",
            "origin_server_ts": 10,
            "content": {"msgtype": "m.text", "body": "original"},
        },
    )
    event = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": edit_id,
            "sender": "@user:localhost",
            "origin_server_ts": 20,
            "content": {
                "msgtype": "m.text",
                "body": "* selected edit",
                "m.new_content": {"msgtype": "m.text", "body": "selected edit"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": source_id},
            },
        },
    )
    inbound_events = [original, event]
    if coalesced_pause:
        inbound_events.insert(0, nio.RoomMessageText.from_dict({**original.source, "event_id": "$early-source"}))
    for inbound in inbound_events:
        await principal.admit(
            _inbound_event(room_id, inbound, EventKind.MESSAGE, EventClass.ACTIONABLE),
            _projected_event(room_id, inbound, EventKind.MESSAGE, self_sender=bot.matrix_id.full_id),
        )
    if not ordinary_pause:
        await principal.settle_many((source_id,))
    gateway = unwrap_extracted_collaborator(bot._delivery_gateway)
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            outbox=principal,
            terminal_turn_for=store.terminal_turn_record,
            terminal_turn_committed=store.publish_committed_response,
        ),
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.deps = replace(runner.deps, delivery_gateway=gateway, approval_store=principal)
    runner._approval_responses.store = principal
    runner._approval_responses.delivery_gateway = gateway
    regenerator = unwrap_extracted_collaborator(bot._edit_regenerator)
    regenerator.deps = replace(
        regenerator.deps,
        turn_store=store,
        receipt_order=AsyncMock(return_value=3),
        generate_response=runner.generate_response,
    )
    controller = unwrap_extracted_collaborator(bot._turn_controller)
    controller.deps = replace(controller.deps, edit_regenerator=regenerator)
    outcomes: list[TurnDispatchOutcome] = []

    async def dispatch_edit(room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        outcome = await controller.handle_text_event(room, event)
        outcomes.append(outcome)
        return outcome

    dispatcher = JournalDispatcher(
        store=principal,
        callbacks=replace(bot._journal_dispatcher.callbacks, on_message=dispatch_edit),
        room_for_id=lambda _room_id: room,
    )
    tool = ToolExecution(
        tool_call_id="call-1",
        tool_name="read_document",
        requires_confirmation=True,
        approval_type=POLICY_CONFIRMATION_APPROVAL_TYPE,
    )
    pause = PausedAttempt(
        session_id=target.session_id,
        run_id="run-edit",
        tools=(tool,),
        response_text="Reading document",
        toolkit_owners={("general", "read_document"): "test_toolkit"},
    )
    # Hide tool anchors to keep this test focused on durable revision ownership.
    bot.config.defaults.show_tool_calls = False

    async def prepare_event(_room: str, _thread: str | None, content: dict) -> dict:
        return content

    async def send_delivery(_delivery: MatrixDelivery) -> str:
        return "$approval-card"

    manager = initialize_approval_store(
        runner.deps.runtime_paths,
        prepare_event=prepare_event,
        send_delivery=send_delivery,
        resolve_delivery=AsyncMock(return_value=None),
        cards=journal_store.principal("router@shared"),
        transport_sender=lambda: "@router:localhost",
        sending_device=lambda: "DEVICE",
    )
    resolver = unwrap_extracted_collaborator(bot._conversation_resolver)
    model = AsyncMock(side_effect=ResponsePausedForApproval(pause))
    approval_evaluation = AsyncMock(return_value=(requires_human, 60.0))
    try:
        with (
            patch.object(
                resolver,
                "extract_message_context",
                AsyncMock(return_value=MessageContext(False, False, None, [], [], False)),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing,
                should_use_streaming=AsyncMock(return_value=False),
                ai_response=model,
            ),
            patch("mindroom.approval_response.evaluate_tool_approval", approval_evaluation),
            # Inspect the durable checkpoint before the test claims resumed execution.
            patch.object(type(principal), "claim_approval_continuation", AsyncMock(return_value=None)),
        ):
            if ordinary_pause:
                original_request = replace(
                    _plain_request(target, source_event_id=source_id),
                    existing_event_id=answer_id,
                    source_handoff=asyncio.Event(),
                    sources=ResponseSources(
                        pending_event_ids=tuple(
                            dict.fromkeys((source_id, *original_sources)),
                        ),
                        logical_source_event_ids=original_sources,
                    ),
                    matrix_run_metadata={MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: list(original_sources)},
                )
                assert await runner.generate_response(original_request) is None
                assert original_request.source_handoff.is_set()
                ordinary = await principal.approval_continuation_for_source(source_id)
                assert ordinary is not None
                assert ordinary.prepared_edit_record is None
                if coalesced_pause:
                    assert ordinary.source_event_ids == (source_id, "$early-source")
                approval_evaluation.return_value = (False, 60.0)
                assert await dispatch_edit(room, event) is TurnDispatchOutcome.DEFERRED
            else:
                await dispatcher.drain_once()
        assert model.await_count == (2 if ordinary_pause else 1)
        continuation = await principal.approval_continuation_for_source(edit_id)
        assert continuation is not None
        assert outcomes == [TurnDispatchOutcome.DEFERRED]
        assert continuation.source_event_ids == (edit_id,)
        assert continuation.state == ("waiting" if requires_human and not ordinary_pause else "ready")
        assert await principal.is_pending(edit_id)
        assert await principal.is_pending(source_id) is ordinary_pause
        assert (await principal.approval_continuation_for_source(source_id) is not None) is ordinary_pause
        assert store.get_turn_record(source_id).source_event_revisions is None

        yield _ApprovalCase(
            bot,
            room,
            store,
            principal,
            gateway,
            runner,
            regenerator,
            controller,
            resolver,
            manager,
            target,
            event,
            continuation,
            requires_human,
            journal_store,
        )
    finally:
        await shutdown_approval_runtime()


@pytest_asyncio.fixture
async def approval_case(
    tmp_path: Path,
    journal_store: EventJournalStore,
    stopped: bool,
    requires_human: bool,
) -> AsyncIterator[_ApprovalCase]:
    """Pause a real edited response with durable journal ownership."""
    async with _paused_case(tmp_path, journal_store, stopped=stopped, requires_human=requires_human) as case:
        yield case


@pytest_asyncio.fixture
async def ordinary_approval_case(
    tmp_path: Path,
    journal_store: EventJournalStore,
    stopped: bool,
    requires_human: bool,
    coalesced: bool,
) -> AsyncIterator[_ApprovalCase]:
    """Pause an original response, then its separately owned edit."""
    async with _paused_case(
        tmp_path,
        journal_store,
        stopped=stopped,
        requires_human=requires_human,
        ordinary_pause=True,
        coalesced_pause=coalesced,
    ) as case:
        yield case


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("requires_human", [False, True])
class TestEditApprovalOwnership:
    """Exercise independent pause, resume, and settlement contracts through real controllers."""

    @pytest.mark.parametrize("settlement", ["resume", "direct", "retry", "generic_retry"])
    async def test_old_failure_preserves_newer_answer(self, approval_case: _ApprovalCase, settlement: str) -> None:
        """An older missing run cannot replace an acknowledged newer edited answer."""
        case = approval_case
        reason = "Paused run is no longer available"
        if settlement in {"retry", "generic_retry"}:
            await case.approve()
            case.bot.client.room_send.side_effect = RuntimeError("Transport unavailable")
            await case.resume(failure=reason)
            case.bot.client.room_send.side_effect = None
            failure = await case.principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
            assert failure is not None
            assert failure.result is None
            assert failure.acknowledged_event_id is None
        await case.pause_newer()
        await case.resume("$newer-edit")
        newer = await case.principal.load_matrix_delivery(delivery_id="$newer-edit", stage=DeliveryStage.FINAL)
        assert newer is not None
        assert newer.acknowledged_event_id is not None
        assert case.store.get_turn_record("$source").source_event_revisions == {"$source": (30, "$newer-edit")}
        sends = case.bot.client.room_send.await_count
        if settlement == "direct":
            assert await case.runner._approval_responses.settle_failure(case.approval, reason)
        else:
            if settlement == "generic_retry":
                await case.gateway.recover_deliveries()
                assert case.bot.client.room_send.await_count == sends
            if settlement == "resume":
                await case.approve()
            await case.resume(failure=reason)
        assert case.bot.client.room_send.await_count == sends
        assert await case.principal.approval_continuation_for_source("$edit") is None
        assert not await case.principal.is_pending("$edit")
        assert not await case.manager.cards.pending_approval_cards(room_id=case.room.room_id, limit=10)
        failure = await case.principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
        assert failure is None or (failure.retired and failure.acknowledged_event_id is None)
        assert await case.principal.load_matrix_delivery(delivery_id="$newer-edit", stage=DeliveryStage.FINAL) == newer
        assert case.store.get_turn_record("$source").source_event_revisions == {"$source": (30, "$newer-edit")}

    @pytest.mark.parametrize("newer_outcome", ["pending", "failed", "unacknowledged"])
    async def test_unanswered_newer_edit_does_not_suppress_failure(
        self,
        approval_case: _ApprovalCase,
        newer_outcome: str,
    ) -> None:
        """Admission or a failed newer attempt does not prove a replacement answer."""
        case = approval_case
        await case.pause_newer()
        if newer_outcome == "failed":
            await case.resume("$newer-edit", failure="Newer run unavailable")
        elif newer_outcome == "unacknowledged":
            case.bot.client.room_send.side_effect = RuntimeError("Transport unavailable")
            await case.resume("$newer-edit")
            case.bot.client.room_send.side_effect = None
            newer = await case.principal.load_matrix_delivery(delivery_id="$newer-edit", stage=DeliveryStage.FINAL)
            assert newer is not None
            assert newer.result is not None
            assert newer.acknowledged_event_id is None
        await case.approve()
        await case.resume(failure="Paused run is no longer available")
        failure = await case.principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
        assert failure is not None
        assert failure.acknowledged_event_id is not None
        assert not failure.retired
        assert case.store.get_turn_record("$source").source_event_revisions is None

    async def test_newer_answer_preserves_frozen_success(self, approval_case: _ApprovalCase) -> None:
        """A result-bearing frozen FINAL remains owed even after a later edit answers."""
        case = approval_case
        claimed = await case.freeze_final()
        await case.pause_newer()
        await case.resume("$newer-edit")
        assert not await case.runner._approval_responses.settle_failure(claimed, "Paused run is no longer available")
        final = await case.principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
        assert final is not None
        assert final.result is not None
        assert not final.retired
        await case.recover_final(claimed)

    @pytest.mark.parametrize("coalesced", [False, True])
    async def test_ordinary_failure_preserves_newer_answer(
        self,
        ordinary_approval_case: _ApprovalCase,
        coalesced: bool,
    ) -> None:
        """Original source ownership, including coalesced order, yields to a newer answer."""
        case = ordinary_approval_case
        await case.resume()
        newer = await case.principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
        assert newer is not None
        assert newer.acknowledged_event_id is not None
        sends = case.bot.client.room_send.await_count
        await case.approve()
        await case.resume("$source", failure="Paused run is no longer available")
        assert case.bot.client.room_send.await_count == sends
        assert await case.principal.approval_continuation_for_source("$source") is None
        assert not await case.principal.is_pending("$source")
        if coalesced:
            assert not await case.principal.is_pending("$early-source")
        assert await case.principal.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is None

    @pytest.mark.parametrize("redaction", [None, "revision", "source"])
    async def test_resume_after_restart(self, approval_case: _ApprovalCase, redaction: str | None) -> None:
        """Durable edited ownership survives restart and either kind of source redaction."""
        case = approval_case
        if redaction is not None:
            redacts = "$edit" if redaction == "revision" else "$source"
            event = nio.RedactionEvent.from_dict(
                {
                    "type": "m.room.redaction",
                    "event_id": "$redaction",
                    "sender": "@user:localhost",
                    "origin_server_ts": 30,
                    "redacts": redacts,
                    "content": {},
                },
            )
            await case.principal.admit(
                _inbound_event(case.room.room_id, event, EventKind.REDACTION, EventClass.CONTEXT_ONLY),
                _projected_event(case.room.room_id, event, EventKind.REDACTION, self_sender=case.bot.matrix_id.full_id),
            )
            await case.store.mark_source_redacted(redacts)
            assert await case.principal.is_pending("$edit")
        await case.approve()
        await case.restart()
        await case.resume()
        assert await case.principal.approval_continuation_for_source("$edit") is None
        assert not await case.principal.is_pending("$edit")
        assert not await case.principal.is_pending("$source")
        final = await case.principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
        assert final is not None
        assert final.acknowledged_event_id is not None
        assert final.edits_event_id == "$answer"
        _reset_handled_turn_ledger_runtime()
        consumed = (await _store(case.journal_store, agent_name="general")).get_turn_record("$source")
        assert consumed is not None
        assert consumed.source_event_ids == ("$source",)
        if redaction == "revision":
            assert consumed.revision_replay["$edit"].redacted
            assert "$source" not in (consumed.source_event_prompts or {})
        elif redaction == "source":
            assert consumed.redacted_source_event_ids == ("$source",)
            assert "$source" not in (consumed.source_event_prompts or {})
        else:
            assert consumed.source_event_revisions == {"$source": (20, "$edit")}
            assert consumed.revision_replay["$edit"].response_event_id == "$answer"

    @pytest.mark.parametrize("transport_fails", [False, True])
    async def test_stop_settles_paused_edit(self, approval_case: _ApprovalCase, transport_fails: bool) -> None:
        """Real STOP reconciliation fences the edit, including after transport retry."""
        case = approval_case
        if transport_fails:
            await case.failed_stop()
        await case.stop()
        await case.assert_stopped_edit_settled()

    async def test_stop_before_edit_preserves_pause(self, approval_case: _ApprovalCase) -> None:
        """An earlier STOP cannot consume an edit selected after its receipt."""
        case = approval_case
        sends = case.bot.client.room_send.await_count
        await case.stop(2)
        assert await case.principal.approval_continuation_for_source("$edit") == case.approval
        assert await case.principal.is_pending("$edit")
        assert case.bot.client.room_send.await_count == sends

    @pytest.mark.parametrize("transport_fails", [False, True])
    async def test_delayed_stop_preserves_newer_pause(
        self,
        approval_case: _ApprovalCase,
        transport_fails: bool,
    ) -> None:
        """An old STOP settles its owner without changing a later approval bubble."""
        case = approval_case
        if transport_fails:
            await case.failed_stop()
        newer = await case.pause_newer()
        sends = case.bot.client.room_send.await_count
        await case.stop()
        await case.assert_stopped_edit_settled()
        assert case.bot.client.room_send.await_count == sends
        assert await case.principal.approval_continuation_for_source("$newer-edit") == newer
        assert await case.principal.is_pending("$newer-edit")
        if transport_fails:
            final = await case.principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
            assert final is not None
            assert final.retired
            await case.gateway.recover_deliveries()
            assert case.bot.client.room_send.await_count == sends

    async def test_stop_retries_after_source_worker_recovers_final(self, approval_case: _ApprovalCase) -> None:
        """STOP can finish after source recovery deletes its successful continuation."""
        case = approval_case
        claimed = await case.freeze_final()
        await case.assert_stop_waits_for_final(claimed)
        await case.recover_final(claimed)
        sends = case.bot.client.room_send.await_count
        await case.stop()
        await case.assert_stopped_edit_settled()
        assert case.bot.client.room_send.await_count == sends

    async def test_delayed_stop_after_selection_before_new_pause(self, approval_case: _ApprovalCase) -> None:
        """Current selection protects a newer run before it creates any attempt row."""
        case = approval_case
        await case.failed_stop()
        event = nio.RoomMessageText.from_dict({**case.event.source, "event_id": "$newer-edit", "origin_server_ts": 30})
        await case.principal.admit(
            _inbound_event(case.room.room_id, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            _projected_event(case.room.room_id, event, EventKind.MESSAGE, self_sender=case.bot.matrix_id.full_id),
        )
        assert not await case.store._prepare_edit_response_source(
            target=case.target,
            source_event_ids=case.approval.sources.logical_source_event_ids,
            response_event_id="$answer",
            edit_receipt_order=6,
        )

        # Ownership must remain available without reading historical snapshot routing fields.
        def remove_snapshot_routing(transaction: Transaction) -> None:
            row = transaction.fetchone(
                "SELECT context_json FROM approval_continuations WHERE approval_id = ?",
                (case.approval.approval_id,),
            )
            context = json.loads(row["context_json"])
            context.update(prepared_edit_record=None, room_id="!unrelated:localhost", response_event_id="$unrelated")
            transaction.execute(
                "UPDATE approval_continuations SET context_json = ? WHERE approval_id = ?",
                (json.dumps(context), case.approval.approval_id),
            )

        await case.journal_store.backend.write(remove_snapshot_routing)
        sends = case.bot.client.room_send.await_count
        await case.stop()
        await case.assert_stopped_edit_settled()
        assert case.bot.client.room_send.await_count == sends
        assert await case.principal.approval_continuation_for_source("$newer-edit") is None
        assert await case.principal.is_pending("$newer-edit")

    async def test_stop_fences_all_owners_while_final_is_unresolved(self, approval_case: _ApprovalCase) -> None:
        """Unresolved successful debt cannot leave another stopped approval executable."""
        case = approval_case
        await case.pause_newer()
        claimed = await case.freeze_final()
        await case.assert_stop_waits_for_final(claimed, order=7)
        assert await case.principal.approval_continuation_for_source("$newer-edit") is None
        assert not await case.principal.is_pending("$newer-edit")
        await case.recover_final(claimed)
        sends = case.bot.client.room_send.await_count
        await case.stop(7)
        await case.assert_stopped_edit_settled()
        assert case.bot.client.room_send.await_count == sends

    @pytest.mark.parametrize("coalesced", [False, True])
    async def test_stop_before_edit_retires_ordinary_pause(
        self,
        ordinary_approval_case: _ApprovalCase,
        coalesced: bool,
    ) -> None:
        """Ordinary and coalesced source owners can settle behind a later edit pause."""
        case = ordinary_approval_case
        sends = case.bot.client.room_send.await_count
        await case.stop(2)
        assert await case.principal.approval_continuation_for_source("$edit") == case.approval
        assert await case.principal.is_pending("$edit")
        assert case.bot.client.room_send.await_count == sends
        assert await case.principal.approval_continuation_for_source("$source") is None
        assert not await case.principal.is_pending("$source")
        if coalesced:
            assert not await case.principal.is_pending("$early-source")


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("transport_fails", [False, True])
async def test_failed_pause_handoff_finalizes_visible_edited_response(  # noqa: PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    started: bool,
    transport_fails: bool,
) -> None:
    """A visible error settles delivery without claiming the edited request was answered."""
    bot = _bot(tmp_path)
    bot.client.room_send.return_value = nio.RoomSendResponse("$terminal-edit", "!room:localhost")
    if transport_fails:
        bot.client.room_send.side_effect = RuntimeError("Transport unavailable")
    bot.client.room_get_event.return_value = _visible_event_response(
        sender=bot.matrix_id.full_id,
        body="Partial edited answer",
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    principal = journal_store.principal("general@@mindroom_general:localhost")
    await _admit_approval_source(principal, event_id="$source")
    await _admit_approval_source(principal, event_id="$edit")
    await principal.settle_many(("$source",))
    store = await _store(journal_store, agent_name="general")
    await store.record_responded_turn(
        TurnRecord.create(
            ["$source"],
            response_event_id="$waiting",
            completed=True,
            source_event_prompts={"$source": "original"},
        ),
    )
    registered = await store.register_edit_revision("$source", (20, "$edit"))
    assert registered is not None
    selected = canonicalize_turn_record(
        registered,
        source_event_prompts={"$source": "selected edit"},
        source_event_revisions={"$source": (20, "$edit")},
    )
    bot._turn_store = store
    gateway = unwrap_extracted_collaborator(runner.deps.delivery_gateway)
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            outbox=principal,
            terminal_turn_for=store.terminal_turn_record,
            terminal_turn_committed=store.publish_committed_response,
        ),
    )
    runner.deps = replace(runner.deps, approval_store=principal, delivery_gateway=gateway)
    request = replace(
        _plain_request(_target(), source_event_id="$edit"),
        existing_event_id="$waiting",
        prepared_edit_record=selected,
        sources=ResponseSources(("$edit",), ("$source",), edit_receipt_order=3),
    )
    lifecycle = runner._build_lifecycle(
        identity=runner._response_identity(request, response_kind="ai"),
        request=request,
    )
    progress = _DeliveryProgress(tracked_event_id="$waiting", stage_started=started)
    pause = ResponsePausedForApproval(PausedAttempt(session_id="session", run_id="run", tools=(), toolkit_owners={}))

    async def fail_handoff(_paused: PausedAttempt) -> None:
        message = "Approval handoff failed"
        raise RuntimeError(message)

    with patch.object(runner, "_run_cancellable_response", AsyncMock(side_effect=pause)):
        await runner._run_and_settle_locked_response(
            request,
            target=request.response_envelope.target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=AsyncMock(),
            user_id=request.user_id,
            run_id="run",
            build_post_response_outcome=lambda _outcome: ResponseOutcome(),
            post_response_deps=PostResponseEffectsDeps(logger=runner.deps.logger),
            approval_suspension_handler=fail_handoff,
            show_tool_calls=False,
        )
    assert progress.delivery_outcome is not None
    assert progress.delivery_outcome.terminal_status == "error"
    assert progress.delivery_outcome.is_visible_response
    final = await runner.deps.approval_store.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
    assert final is not None
    attempt = await journal_store.backend.read(
        lambda tx: response_attempts.load_response_attempt(
            tx,
            principal.principal_id,
            "$edit",
        ),
    )
    assert attempt is not None
    assert attempt.logical_source_event_ids == ("$source",)
    assert attempt.response_event_id == "$waiting"
    if transport_fails:
        assert final.acknowledged_event_id is None
        assert progress.delivery_outcome.delivery_kind is None
        assert progress.delivery_outcome.final_visible_body is None
        bot.client.room_send.side_effect = None
        assert (await runner.deps.delivery_gateway.recover_deliveries()).recovered == 1
        final = await runner.deps.approval_store.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
        assert final is not None
    assert final.acknowledged_event_id is not None
    assert final.payload["m.new_content"][STREAM_STATUS_KEY] == STREAM_STATUS_ERROR
    assert not await principal.is_pending("$edit")
    assert not await principal.is_pending("$source")
    _reset_handled_turn_ledger_runtime()
    persisted = (await _store(journal_store, agent_name="general")).get_turn_record("$source")
    assert persisted is not None
    assert persisted.response_event_id == "$waiting"
    assert persisted.source_event_prompts == {"$source": "original"}
    assert persisted.source_event_revisions is None
    assert persisted.revision_watermark("$source") == (20, "$edit")
    assert persisted.revision_replay["$edit"].response_event_id is None


@pytest.mark.asyncio
async def test_failed_pause_without_visible_response_keeps_no_event_outcome(tmp_path: Path) -> None:
    """A failure before any visible send retains the existing no-event terminal behavior."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target())
    outcome = await runner._finalize_failed_approval_handoff(
        target=request.response_envelope.target,
        request=request,
        progress=_DeliveryProgress(),
        failure_reason="failed",
    )
    assert outcome.terminal_status == "error"
    assert outcome.event_id is None
    assert not outcome.is_visible_response
