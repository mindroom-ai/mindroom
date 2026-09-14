"""Edit revisions retain durable ownership through native approval pauses."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest
from agno.models.response import ToolExecution

from mindroom.approval_manager import initialize_approval_store
from mindroom.constants import MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import DeliveryStage, EventClass, EventKind
from mindroom.handled_turns import SourceEventMetadata, TurnRecord, TurnRecordCodec, _reset_handled_turn_ledger_runtime
from mindroom.history.types import HistoryScope
from mindroom.journal_dispatch import JournalDispatcher
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
from mindroom.response_runner import ResponseRunner, _DeliveryProgress
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE, shutdown_approval_runtime
from mindroom.turn_record import canonicalize_turn_record
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target
from tests.test_response_runner_focused import _admit_approval_source, _visible_event_response
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore, MatrixDelivery


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("requires_human", [False, True])
@pytest.mark.parametrize(
    "terminal_action",
    [
        "resume",
        "redact_revision",
        "redact_source",
        "stop",
        "stop_before_edit",
        "stop_with_newer_edit",
        "stop_after_final",
        "stop_retry",
        "stop_retry_with_newer_edit",
        "stop_all_owners",
        "stop_before_edit_with_ordinary_pause",
        "stop_before_edit_with_coalesced_pause",
    ],
)
async def test_edited_pause_survives_dispatch_and_restart(  # noqa: C901, PLR0912, PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    stopped: bool,
    requires_human: bool,
    terminal_action: str,
) -> None:
    """Settled originals stay settled; only active edits own and consume the resumed answer."""
    bot = _bot(tmp_path)
    room_id, source_id, edit_id, answer_id = "!room:localhost", "$source", "$edit", "$answer"
    room = nio.MatrixRoom(room_id, bot.matrix_id.full_id)
    room.users["@user:localhost"] = nio.MatrixUser("@user:localhost", "User")
    bot.client.rooms[room_id] = room
    bot.client.room_send.return_value = nio.RoomSendResponse("$answer-edit", room_id)
    target = MessageTarget.resolve(room_id, None, source_id, room_mode=True)
    store = await _store(journal_store, agent_name="general")
    store.deps = replace(store.deps, state_writer=bot._conversation_state_writer, resolver=bot._conversation_resolver)
    coalesced_pause = terminal_action == "stop_before_edit_with_coalesced_pause"
    ordinary_pause = terminal_action == "stop_before_edit_with_ordinary_pause" or coalesced_pause
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
        ):
            if ordinary_pause:
                original_request = replace(
                    _plain_request(target, source_event_id=source_id),
                    existing_event_id=answer_id,
                    source_handoff=asyncio.Event(),
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

        if terminal_action.startswith("redact"):
            redacts = edit_id if terminal_action == "redact_revision" else source_id
            redaction = nio.RedactionEvent.from_dict(
                {
                    "type": "m.room.redaction",
                    "event_id": "$redaction",
                    "sender": "@user:localhost",
                    "origin_server_ts": 30,
                    "redacts": redacts,
                    "content": {},
                },
            )
            await principal.admit(
                _inbound_event(room_id, redaction, EventKind.REDACTION, EventClass.CONTEXT_ONLY),
                _projected_event(room_id, redaction, EventKind.REDACTION, self_sender=bot.matrix_id.full_id),
            )
            await store.mark_source_redacted(redacts)
            assert await principal.is_pending(edit_id)
        if terminal_action.startswith("stop"):
            reconciler = UserStopReconciler(UserStopReconcilerDeps(store, runner, gateway))
            stop_order = 7 if terminal_action == "stop_all_owners" else 4
            if terminal_action == "stop_before_edit" or ordinary_pause:
                sends = bot.client.room_send.await_count
                assert await reconciler.finalize(answer_id, 2, AsyncMock())
                assert await principal.approval_continuation_for_source(edit_id) == continuation
                assert await principal.is_pending(edit_id)
                assert bot.client.room_send.await_count == sends
                assert await principal.approval_continuation_for_source(source_id) is None
                assert not await principal.is_pending(source_id)
                if coalesced_pause:
                    assert not await principal.is_pending("$early-source")
                return
            if terminal_action == "stop_retry_with_newer_edit":
                bot.client.room_send.side_effect = RuntimeError("Transport unavailable")
                with pytest.raises(RuntimeError, match="did not become durable"):
                    await reconciler.finalize(answer_id, 4, AsyncMock())
                bot.client.room_send.side_effect = None
            if terminal_action in {"stop_with_newer_edit", "stop_retry_with_newer_edit", "stop_all_owners"}:
                newer_event = nio.RoomMessageText.from_dict(
                    {**event.source, "event_id": "$newer-edit", "origin_server_ts": 30},
                )
                await principal.admit(
                    _inbound_event(room_id, newer_event, EventKind.MESSAGE, EventClass.ACTIONABLE),
                    _projected_event(room_id, newer_event, EventKind.MESSAGE, self_sender=bot.matrix_id.full_id),
                )
                regenerator.deps = replace(regenerator.deps, receipt_order=AsyncMock(return_value=6))
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
                    patch("mindroom.approval_response.evaluate_tool_approval", AsyncMock(return_value=(False, 60.0))),
                ):
                    assert await controller.handle_text_event(room, newer_event) is TurnDispatchOutcome.DEFERRED
                newer = await principal.approval_continuation_for_source("$newer-edit")
                assert newer is not None
                assert newer.prepared_edit_record.latest_edit_receipt_order == 6
                assert await principal.approval_continuation_for_source(edit_id) is not None
                sends = bot.client.room_send.await_count
            if terminal_action in {"stop_after_final", "stop_all_owners"}:
                if requires_human:
                    result = await manager.handle_card_response(
                        room_id=room_id,
                        sender_id="@user:localhost",
                        card_event_id="$approval-card",
                        status="approved",
                        reason=None,
                        authorize_responder=lambda _entity: True,
                    )
                    assert result.consumed
                claimed = await principal.claim_approval_continuation(
                    continuation.approval_id,
                    runtime_generation=runner.deps.approval_runtime_generation,
                )
                assert claimed is not None
                assert claimed.prepared_edit_record is not None
                await principal.enqueue_matrix_delivery(
                    delivery_id=edit_id,
                    stage=DeliveryStage.FINAL,
                    room_id=room_id,
                    thread_id=None,
                    payload={"body": "Edited answer", "formatted_body": "Edited answer"},
                    edits_event_id=answer_id,
                    result={"prepared_edit_record": TurnRecordCodec._to_ledger_record(claimed.prepared_edit_record)},
                )
                assert await principal.claim_matrix_delivery(delivery_id=edit_id, stage=DeliveryStage.FINAL) is not None
                with (
                    patch.object(type(gateway), "recover_deliveries", AsyncMock()),
                    pytest.raises(RuntimeError, match="did not become durable"),
                ):
                    await reconciler.finalize(answer_id, stop_order, AsyncMock())
                assert (await principal.approval_continuation(claimed.approval_id)).state == "claimed"
                if terminal_action == "stop_all_owners":
                    assert await principal.approval_continuation_for_source("$newer-edit") is None
                    assert not await principal.is_pending("$newer-edit")
                await principal.acknowledge_matrix_delivery(
                    delivery_id=edit_id,
                    stage=DeliveryStage.FINAL,
                    event_id="$final-edit",
                    delivered_projections=(),
                )
                assert await runner._recover_claimed_approval_lifecycle(claimed, target=target) == answer_id
                assert await principal.approval_continuation(claimed.approval_id) is None
                sends = bot.client.room_send.await_count
            if terminal_action == "stop_retry":
                bot.client.room_send.side_effect = RuntimeError("Transport unavailable")
                with pytest.raises(RuntimeError, match="did not become durable"):
                    await reconciler.finalize(answer_id, 4, AsyncMock())
                failing = await principal.approval_continuation(continuation.approval_id)
                assert failing is not None
                assert failing.state == "failing"
                assert await principal.is_pending(edit_id)
                assert store.get_turn_record(source_id).source_event_revisions is None
                bot.client.room_send.side_effect = None
            assert await reconciler.finalize(answer_id, stop_order, AsyncMock())
            assert await principal.approval_continuation_for_source(edit_id) is None
            assert not await principal.is_pending(edit_id)
            assert store.get_turn_record(source_id).source_event_revisions is None
            if terminal_action in {
                "stop_with_newer_edit",
                "stop_retry_with_newer_edit",
                "stop_after_final",
                "stop_all_owners",
            }:
                assert bot.client.room_send.await_count == sends
            if terminal_action in {"stop_with_newer_edit", "stop_retry_with_newer_edit"}:
                assert await principal.approval_continuation_for_source("$newer-edit") == newer
                assert await principal.is_pending("$newer-edit")
            if terminal_action == "stop_retry_with_newer_edit":
                final = await principal.load_matrix_delivery(delivery_id=edit_id, stage=DeliveryStage.FINAL)
                assert final is not None
                assert final.retired
                await gateway.recover_deliveries()
                assert bot.client.room_send.await_count == sends
            resumed = AsyncMock(side_effect=AssertionError("Stopped approval must not execute"))
            with patch.object(runner, "_continue_entity_call", resumed):
                await runner.handoff_approval_source(edit_id)
                await runner.wait_for_source_owned_inbox_responses()
            resumed.assert_not_awaited()
            return

        if requires_human:
            result = await manager.handle_card_response(
                room_id=room_id,
                sender_id="@user:localhost",
                card_event_id="$approval-card",
                status="approved",
                reason=None,
                authorize_responder=lambda _entity: True,
            )
            assert result.consumed

        _reset_handled_turn_ledger_runtime()
        reopened = await _store(journal_store, agent_name="general")
        gateway = replace(
            gateway,
            deps=replace(
                gateway.deps,
                terminal_turn_for=reopened.terminal_turn_record,
                terminal_turn_committed=reopened.publish_committed_response,
            ),
        )
        restarted = ResponseRunner(
            deps=replace(runner.deps, delivery_gateway=gateway, approval_runtime_generation="restarted"),
        )
        with patch.object(
            restarted,
            "_continue_entity_call",
            AsyncMock(return_value=CompletedApprovalRun(response_text="Edited answer", metadata_content={})),
        ):
            assert await restarted.handoff_approval_source(edit_id) is False
            await restarted.wait_for_source_owned_inbox_responses()
        assert await principal.approval_continuation_for_source(edit_id) is None
        assert not await principal.is_pending(edit_id)
        assert not await principal.is_pending(source_id)
        final = await principal.load_matrix_delivery(delivery_id=edit_id, stage=DeliveryStage.FINAL)
        assert final is not None
        assert final.acknowledged_event_id is not None
        assert final.edits_event_id == answer_id
        _reset_handled_turn_ledger_runtime()
        consumed = (await _store(journal_store, agent_name="general")).get_turn_record(source_id)
        assert consumed.source_event_ids == (source_id,)
        assert consumed is not None
        if terminal_action == "redact_revision":
            assert consumed.revision_replay[edit_id].redacted
            assert source_id not in (consumed.source_event_prompts or {})
        elif terminal_action == "redact_source":
            assert consumed.redacted_source_event_ids == (source_id,)
            assert source_id not in (consumed.source_event_prompts or {})
        else:
            assert consumed.source_event_revisions == {source_id: (20, edit_id)}
            assert consumed.revision_replay[edit_id].response_event_id == answer_id
    finally:
        await shutdown_approval_runtime()


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
    )
    lifecycle = runner._build_lifecycle(
        identity=runner._response_identity(request, response_kind="ai"),
        request=request,
    )
    progress = _DeliveryProgress(tracked_event_id="$waiting", stage_started=started)
    pause = ResponsePausedForApproval(PausedAttempt(session_id="session", run_id="run", tools=()))

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
