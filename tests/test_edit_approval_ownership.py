"""Edit revisions retain durable ownership through native approval pauses."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio
from agno.models.response import ToolExecution

from mindroom import reply_lifecycle as rl
from mindroom.approval_manager import initialize_approval_store
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import DeliveryStage, EventClass, EventKind
from mindroom.event_journal.replies import PreparedReplyRow, ReplyRowRequest
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.history.types import HistoryScope
from mindroom.journal_dispatch import JournalDispatcher
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
from mindroom.reply_scope import ReplyRuntime
from mindroom.response_runner import ResponseRunner, _DeliveryProgress
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE, shutdown_approval_runtime
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from tests.approval_continuation_helpers import claim_continuation
from tests.conftest import (
    finish_edit_regenerations,
    journal_edit_regenerator_deps,
    patch_response_runner_module,
    unwrap_extracted_collaborator,
)
from tests.reply_span_helpers import reply_span, response_span, seed_finished_reply
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target
from tests.test_response_runner_focused import _admit_approval_source
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

    async def dispatch_newer(self, model: AsyncMock | None = None) -> TurnDispatchOutcome:
        """Dispatch a newer edit of the message whose reply the approval holds; it stops that reply first.

        Without a model it admits the edit, whose regeneration must not run yet;
        with one it dispatches the admitted edit again, as its retry does.
        """
        event = nio.RoomMessageText.from_dict({**self.event.source, "event_id": "$newer-edit", "origin_server_ts": 30})
        if model is None:
            model = AsyncMock(side_effect=AssertionError("A held reply regenerates only once its approval ended"))
            await self.principal.admit(
                _inbound_event(self.room.room_id, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
                _projected_event(self.room.room_id, event, EventKind.MESSAGE, self_sender=self.bot.matrix_id.full_id),
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
                ai_response=model,
            ),
            patch.object(
                self.bot,
                "_user_stop_reconciler",
                UserStopReconciler(UserStopReconcilerDeps(self.store, self.gateway)),
            ),
        ):
            outcome = await self.controller.handle_text_event(self.room, event)
            await finish_edit_regenerations(self.bot)
        return outcome

    async def redact(self, redacts: str) -> None:
        """Admit the user's redaction of one event, as the journal records a deletion."""
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
        await self.principal.admit(
            _inbound_event(self.room.room_id, event, EventKind.REDACTION, EventClass.CONTEXT_ONLY),
            _projected_event(self.room.room_id, event, EventKind.REDACTION, self_sender=self.bot.matrix_id.full_id),
        )
        await self.store.mark_source_redacted(redacts, room_id=self.room.room_id)

    async def stop(self) -> None:
        reconciler = UserStopReconciler(UserStopReconcilerDeps(self.store, self.gateway))
        assert await reconciler.finalize("$answer", 4, room_id=self.room.room_id)
        # Run what the journal's worker runs for the approval source the Stop fenced and woke: its failure settlement.
        continuation = await self.principal.approval_continuation_for_source("$edit")
        if continuation is not None and continuation.state == "failing":
            await self.runner.handoff_approval_source("$edit")
        await self.runner.wait_for_source_owned_inbox_responses()

    async def failed_stop(self) -> None:
        """A Stop whose note cannot be sent yet still fences the approval; its settlement retries later."""
        self.bot.client.room_send.side_effect = RuntimeError("Transport unavailable")
        try:
            await self.stop()
        finally:
            self.bot.client.room_send.side_effect = None
        failing = await self.principal.approval_continuation(self.approval.approval_id)
        assert failing is not None
        assert failing.state == "failing"
        assert await self.principal.is_pending("$edit")
        assert self.store.get_turn_record("$source").source_event_revisions == {"$source": (20, "$edit")}

    async def assert_stopped_edit_settled(self) -> None:
        assert await self.principal.approval_continuation_for_source("$edit") is None
        assert not await self.principal.is_pending("$edit")
        assert self.store.get_turn_record("$source").source_event_revisions == {"$source": (20, "$edit")}
        resumed = AsyncMock(side_effect=AssertionError("Stopped approval must not execute"))
        with patch.object(self.runner, "_continue_entity_call", resumed):
            await self.runner.handoff_approval_source("$edit")
            await self.runner.wait_for_source_owned_inbox_responses()
        resumed.assert_not_awaited()

    async def freeze_final(self) -> ApprovalContinuation:
        await self.approve()
        claimed = await claim_continuation(
            self.principal,
            self.approval.approval_id,
            runtime_generation=self.runner.deps.approval_runtime_generation,
        )
        assert claimed is not None
        assert claimed.claim_span_id is not None
        resume = await self.principal.replies.span(claimed.claim_span_id)
        assert resume is not None
        # The resume's answer ends its span, as a resumed run's FINAL does.
        await self.principal.enqueue_reply_row(
            ReplyRowRequest(
                reply_id=resume.reply_id,
                span_id=resume.span_id,
                decide=lambda reply, span: rl.finish(
                    reply,
                    span,
                    rl.TerminalWrite(
                        shown=reply.presentation,
                        prepared_revision=reply.revision,
                        state=rl.ReplyState.COMPLETED,
                    ),
                    now_ns=time.time_ns(),
                ),
                room_id=self.room.room_id,
                thread_id=None,
            ),
            PreparedReplyRow(payload={"body": "Edited answer", "formatted_body": "Edited answer"}),
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

    async def resume(self) -> None:
        result = AsyncMock(return_value=CompletedApprovalRun(response_text="Edited answer", metadata_content={}))
        with patch.object(self.runner, "_continue_entity_call", result):
            assert await self.runner.handoff_approval_source("$edit") is False
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
    requires_human: bool,
) -> AsyncIterator[_ApprovalCase]:
    bot = _bot(tmp_path)
    # Ownership checks must not start background embedding requests after a resume.
    bot.config.agents["general"].memory_backend = "none"
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
            (source_id,),
            response_event_id=answer_id,
            completed=True,
            source_event_prompts={source_id: "original"},
            requester_id="@user:localhost",
            response_owner="general",
            conversation_target=target,
            history_scope=HistoryScope(kind="agent", scope_id="general"),
        ),
    )
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
    runner.deps = replace(
        runner.deps,
        delivery_gateway=gateway,
        approval_store=principal,
        # These cases end a superseded approval by hand, as its source's worker
        # would after a crash cut its background cleanup short.
        replies=replace(
            runner.deps.replies,
            store=principal,
            complete_turn=store.publish_completed_turn,
        ),
    )
    await runner.deps.replies.start()
    runner._approval_responses.store = principal
    runner._approval_responses.delivery_gateway = gateway
    runner._approval_responses.finish_approval = runner.deps.replies.finish_approval
    regenerator = unwrap_extracted_collaborator(bot._edit_regenerator)
    regenerator.deps = replace(
        regenerator.deps,
        turn_store=store,
        receipt_order=AsyncMock(return_value=3),
        **journal_edit_regenerator_deps(bot, principal),
    )
    await seed_finished_reply(
        principal,
        answer_id,
        sources=rl.SpanSources(pending=(), logical=(source_id,)),
        room_id=room_id,
        thread_id=None,
        entity_name="general",
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
            patch.object(ReplyRuntime, "claim_approval_resume", AsyncMock(return_value=(None, None))),
            patch.object(
                runner,
                "deps",
                replace(
                    runner.deps,
                    approval_store=MagicMock(
                        spec=type(principal),
                        wraps=principal,
                    ),
                ),
            ),
        ):
            await dispatcher.drain_once()
            await finish_edit_regenerations(bot)
        assert model.await_count == 1
        continuation = await principal.approval_continuation_for_source(edit_id)
        assert continuation is not None
        assert outcomes == [TurnDispatchOutcome.DEFERRED]
        assert continuation.source_event_ids == (edit_id,)
        assert continuation.state == ("waiting" if requires_human else "ready")
        assert await principal.is_pending(edit_id)
        assert not await principal.is_pending(source_id)
        assert await principal.approval_continuation_for_source(source_id) is None
        # The turn took the edit when its regeneration claimed the reply.
        assert store.get_turn_record(source_id).source_event_revisions == {source_id: (20, edit_id)}

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
    requires_human: bool,
) -> AsyncIterator[_ApprovalCase]:
    """Pause a real edited response with durable journal ownership."""
    async with _paused_case(tmp_path, journal_store, requires_human=requires_human) as case:
        yield case


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("requires_human", [False, True])
class TestEditApprovalOwnership:
    """Exercise independent pause, resume, and settlement contracts through real controllers."""

    async def test_a_newer_edit_of_the_paused_reply_cancels_its_approval_and_regenerates(
        self,
        approval_case: _ApprovalCase,
    ) -> None:
        """The edit stops the held reply, which cancels its approval; the regeneration claims once the approval ended."""
        case = approval_case
        retried: list[tuple[str, ...]] = []
        case.runner.deps.replies.retry_sources = lambda _room_id, sources: retried.append(sources)
        await case.dispatch_newer()
        failing = await case.principal.approval_continuation(case.approval.approval_id)
        assert failing is not None
        assert failing.state == "failing"
        assert failing.failure_reason == "cancelled_by_user"
        # The regeneration waits for the approval's settlement, which the Stop woke.
        assert await case.principal.is_pending("$newer-edit")
        assert ("$newer-edit",) not in retried
        await case.runner.handoff_approval_source("$edit")
        await case.runner.wait_for_source_owned_inbox_responses()
        assert await case.principal.approval_continuation(case.approval.approval_id) is None
        assert ("$newer-edit",) in retried
        # The retried edit regenerates the reply in place from the newer text.
        case.bot.client.room_send.reset_mock()
        model = AsyncMock(return_value="Newer answer")
        await case.dispatch_newer(model)
        model.assert_awaited_once()
        assert not await case.principal.is_pending("$newer-edit")
        edits = [
            call.kwargs["content"]
            for call in case.bot.client.room_send.await_args_list
            if call.kwargs["content"].get("m.relates_to", {}).get("event_id") == "$answer"
        ]
        assert edits
        assert edits[-1]["m.new_content"]["body"].startswith("Newer answer")

    async def test_a_newer_edit_leaves_a_frozen_success_to_deliver(self, approval_case: _ApprovalCase) -> None:
        """A newer edit waits while the approved run's frozen FINAL is owed, which still delivers, then regenerates."""
        case = approval_case
        claimed = await case.freeze_final()
        retried: list[tuple[str, ...]] = []
        case.runner.deps.replies.retry_sources = lambda _room_id, sources: retried.append(sources)
        await case.dispatch_newer()
        assert await case.principal.approval_continuation_for_source("$newer-edit") is None
        assert not await case.runner._approval_responses.settle_failure(claimed, "Paused run is no longer available")
        final = await case.principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
        assert final is not None
        assert final.permanent_failure_reason is None
        assert not final.retired
        assert ("$newer-edit",) not in retried
        await case.recover_final(claimed)
        assert ("$newer-edit",) in retried

    @pytest.mark.parametrize("redaction", [None, "revision"])
    async def test_resume_after_restart(self, approval_case: _ApprovalCase, redaction: str | None) -> None:
        """Durable edited ownership survives restart and the redaction of the edit revision."""
        case = approval_case
        if redaction is not None:
            await case.redact("$edit")
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
        else:
            assert consumed.source_event_revisions == {"$source": (20, "$edit")}

    async def test_deleting_the_message_cancels_the_approval_and_removes_the_reply(
        self,
        approval_case: _ApprovalCase,
    ) -> None:
        """Deleting the message the held reply answers cancels its approval as a Stop would, and the reply goes."""
        case = approval_case
        await case.redact("$source")
        failing = await case.principal.approval_continuation(case.approval.approval_id)
        assert failing is not None
        assert failing.state == "failing"
        assert failing.failure_reason == "cancelled_by_user"
        # An approval after the deletion runs nothing.
        await case.approve()
        resumed = AsyncMock(side_effect=AssertionError("A deleted message's approval must not execute"))
        with patch.object(case.runner, "_continue_entity_call", resumed):
            await case.runner.handoff_approval_source("$edit")
            await case.runner.wait_for_source_owned_inbox_responses()
        resumed.assert_not_awaited()
        assert await case.principal.approval_continuation(case.approval.approval_id) is None
        assert not await case.principal.is_pending("$edit")
        reply = await case.principal.replies.for_event("$answer")
        assert reply is not None
        assert reply.state is rl.ReplyState.GONE
        assert await case.principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL) is None

    @pytest.mark.parametrize("transport_fails", [False, True])
    async def test_stop_settles_paused_edit(self, approval_case: _ApprovalCase, transport_fails: bool) -> None:
        """Real STOP reconciliation fences the edit, including after transport retry."""
        case = approval_case
        if transport_fails:
            await case.failed_stop()
        await case.stop()
        await case.assert_stopped_edit_settled()


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
async def test_failed_pause_handoff_keeps_the_regenerated_answer(
    tmp_path: Path,
    journal_store: EventJournalStore,
) -> None:
    """A regeneration whose pause fails keeps the answer it regenerated."""
    bot = _bot(tmp_path)
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
    assert runner.deps.replies is not None
    runner.deps = replace(
        runner.deps,
        approval_store=principal,
        delivery_gateway=gateway,
        replies=replace(runner.deps.replies, store=principal, complete_turn=store.publish_completed_turn),
    )
    request = replace(
        _plain_request(_target(), source_event_id="$edit"),
        existing_event_id="$waiting",
        edit_regeneration=True,
        sources=ResponseSources(("$edit",), ("$source",)),
    )
    lifecycle = runner._build_lifecycle(
        identity=runner._response_identity(request, response_kind="ai"),
        request=request,
    )
    progress = _DeliveryProgress()
    pause = ResponsePausedForApproval(PausedAttempt(session_id="session", run_id="run", tools=(), toolkit_owners={}))

    async def fail_handoff(_paused: PausedAttempt) -> None:
        message = "Approval handoff failed"
        raise RuntimeError(message)

    with patch.object(runner, "_run_cancellable_response", AsyncMock(side_effect=pause)):
        async with reply_span(
            principal,
            runtime=runner.deps.replies,
            source_event_id="$edit",
            room_id=request.room_id,
            thread_id=request.response_envelope.target.resolved_thread_id,
            logical_source_event_ids=("$source",),
            regenerated_event_id="$waiting",
        ):
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
    assert progress.delivery_outcome.event_id == "$waiting"
    # The regeneration wrote nothing, so its reply shows the answer it regenerated.
    assert await principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL) is None
    bot.client.room_send.assert_not_awaited()
    assert not await principal.is_pending("$edit")
    assert not await principal.is_pending("$source")
    _reset_handled_turn_ledger_runtime()
    persisted = (await _store(journal_store, agent_name="general")).get_turn_record("$source")
    assert persisted is not None
    assert persisted.response_event_id == "$waiting"


@pytest.mark.asyncio
async def test_failed_pause_without_visible_response_shows_the_approval_failure(tmp_path: Path) -> None:
    """A pause that fails before anything of the reply is visible tells the user in a new message."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target())
    sent: list[dict[str, Any]] = []

    async def send(_client: object, _room_id: str, content: dict[str, Any], **_kwargs: object) -> DeliveredMatrixEvent:
        sent.append(content)
        return DeliveredMatrixEvent("$note", content)

    with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(side_effect=send)):
        async with response_span(runner, request):
            outcome = await runner._finalize_failed_approval_handoff(
                target=request.response_envelope.target,
                request=request,
                failure_reason="failed",
            )
    assert outcome.terminal_status == "error"
    assert outcome.event_id == "$note"
    assert outcome.failure_reason == "failed"
    assert [content["body"] for content in sent] == ["Tool approval could not be started. Please try again."]
