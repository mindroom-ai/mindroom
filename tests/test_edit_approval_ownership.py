"""Edit revisions retain durable ownership through native approval pauses."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest
from agno.models.response import ToolExecution

from mindroom.approval_manager import initialize_approval_store
from mindroom.constants import STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import DeliveryStage, EventClass, EventKind
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.history.types import HistoryScope
from mindroom.journal_dispatch import JournalDispatcher
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
from mindroom.response_runner import ResponseRunner, _DeliveryProgress
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE, shutdown_approval_runtime
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
@pytest.mark.parametrize("terminal_action", ["resume", "redact_revision", "redact_source", "stop"])
async def test_edited_pause_survives_dispatch_and_restart(  # noqa: C901, PLR0915
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
    await store.record_responded_turn(
        TurnRecord.create(
            [source_id],
            response_event_id=answer_id,
            completed=True,
            source_event_prompts={source_id: "original"},
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
    for inbound in (original, event):
        await principal.admit(
            _inbound_event(room_id, inbound, EventKind.MESSAGE, EventClass.ACTIONABLE),
            _projected_event(room_id, inbound, EventKind.MESSAGE, self_sender=bot.matrix_id.full_id),
        )
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
            patch("mindroom.approval_response.evaluate_tool_approval", AsyncMock(return_value=(requires_human, 60.0))),
        ):
            await dispatcher.drain_once()
        model.assert_awaited_once()
        continuation = await principal.approval_continuation_for_source(edit_id)
        assert continuation is not None
        assert outcomes == [TurnDispatchOutcome.DEFERRED]
        assert continuation.source_event_ids == (edit_id,)
        assert continuation.state == ("waiting" if requires_human else "ready")
        assert await principal.is_pending(edit_id)
        assert not await principal.is_pending(source_id)
        assert await principal.approval_continuation_for_source(source_id) is None
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
        if terminal_action == "stop":

            async def finalize_stop(approval_settled: bool) -> bool:
                assert approval_settled
                await store.record_user_stopped_response(answer_id, 4)
                return True

            assert await runner.finalize_user_stop(answer_id, edit_id, target, 4, lambda: True, finalize_stop)
            assert await principal.approval_continuation_for_source(edit_id) is None
            assert not await principal.is_pending(edit_id)
            assert store.get_turn_record(source_id).source_event_revisions is None
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
@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("transport_fails", [False, True])
async def test_failed_pause_handoff_finalizes_visible_edited_response(
    tmp_path: Path,
    started: bool,
    transport_fails: bool,
) -> None:
    """Failure before continuation creation must replace the pending visible status."""
    bot = _bot(tmp_path)
    bot.client.room_send.return_value = nio.RoomSendResponse("$terminal-edit", "!room:localhost")
    if transport_fails:
        bot.client.room_send.side_effect = RuntimeError("Transport unavailable")
    bot.client.room_get_event.return_value = _visible_event_response(
        sender=bot.matrix_id.full_id,
        body="Partial edited answer",
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await _admit_approval_source(runner.deps.approval_store, event_id="$edit")
    request = replace(_plain_request(_target(), source_event_id="$edit"), existing_event_id="$waiting")
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
