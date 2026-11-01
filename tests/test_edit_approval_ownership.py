"""Edit revisions retain durable ownership through native approval pauses."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest
import pytest_asyncio

from mindroom import reply_lifecycle as rl
from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import ApprovalFailure, DeliveryStage, EventClass, EventKind
from mindroom.event_journal.replies import PreparedReplyRow, ReplyRowRequest
from mindroom.final_delivery import Failed, FailureCause, TurnProgress
from mindroom.history.types import HistoryScope
from mindroom.journal_dispatch import JournalDispatcher
from mindroom.llm.runs import RunRequirement, ToolExecution
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
from mindroom.reply_lifecycle import ApprovalFailureKind
from mindroom.reply_scope import ReplyRuntime, current_span
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.tool_system.tool_function import POLICY_CONFIRMATION_APPROVAL_TYPE
from mindroom.turn_record import TurnRecord
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from tests.approval_continuation_helpers import claim_continuation, dispatch_approval
from tests.conftest import (
    finish_edit_regenerations,
    handle_card_response_for_test,
    initialize_approval_store_for_test,
    patch_response_runner_module,
    replace_response_runner_deps,
    unwrap_extracted_collaborator,
    update_turn_owner_deps,
)
from tests.reply_span_helpers import reply_span, response_span, seed_finished_reply
from tests.reply_turn_helpers import runner_for
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target, streamed_answer
from tests.test_response_runner_focused import _admit_approval_source
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.approval.manager import ApprovalManager
    from mindroom.bot import AgentBot
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import ApprovalContinuation, EventJournalStore, MatrixDelivery, PrincipalStore
    from mindroom.response_runner import ResponseRunner
    from mindroom.turn_store import TurnStore
    from mindroom.turns.ingress import TurnIngress


@dataclass
class _ApprovalCase:
    bot: AgentBot
    room: nio.MatrixRoom
    store: TurnStore
    principal: PrincipalStore
    gateway: DeliveryGateway
    runner: ResponseRunner
    ingress: TurnIngress
    resolver: ConversationResolver
    manager: ApprovalManager
    target: MessageTarget
    event: nio.RoomMessageText
    approval: ApprovalContinuation
    requires_human: bool
    journal_store: EventJournalStore

    async def approve(self) -> None:
        if self.requires_human:
            result = await handle_card_response_for_test(
                self.manager,
                room_id=self.room.room_id,
                sender_id="@user:localhost",
                card_event_id="$approval-card",
                status="approved",
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
                stream_agent_response=streamed_answer(model),
            ),
            patch.object(self.bot, "_delivery_gateway", self.gateway),
        ):
            outcome = await self.ingress.handle_text_event(self.room, event)
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

    async def stop(self) -> None:
        reconciler = UserStopReconciler(UserStopReconcilerDeps(self.store, self.gateway))
        assert await reconciler.finalize("$answer", 4, room_id=self.room.room_id)
        # Run what the journal's worker runs for the approval source the Stop fenced and woke: its failure settlement.
        continuation = await self.principal.approvals.approval_continuation_for_source("$edit")
        if continuation is not None and continuation.state == "failing":
            await self.runner.handoff_approval_source("$edit")
        await self.runner.inbox.wait_for_source_owned_inbox_responses()

    async def failed_stop(self) -> None:
        """A Stop whose note cannot be sent yet still fences the approval; its settlement retries later."""
        self.bot.client.room_send.side_effect = RuntimeError("Transport unavailable")
        try:
            await self.stop()
        finally:
            self.bot.client.room_send.side_effect = None
        failing = await self.principal.approvals.approval_continuation(self.approval.approval_id)
        assert failing is not None
        assert failing.state == "failing"
        assert await self.principal.is_pending("$edit")
        assert (await self.store.get_turn_record("$source")).source_event_revisions == {"$source": (20, "$edit")}

    async def assert_stopped_edit_settled(self) -> None:
        assert await self.principal.approvals.approval_continuation_for_source("$edit") is None
        assert not await self.principal.is_pending("$edit")
        assert (await self.store.get_turn_record("$source")).source_event_revisions == {"$source": (20, "$edit")}
        resumed = AsyncMock(side_effect=AssertionError("Stopped approval must not execute"))
        with patch.object(self.runner.approvals, "_continue_entity_call", resumed):
            await self.runner.handoff_approval_source("$edit")
            await self.runner.inbox.wait_for_source_owned_inbox_responses()
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
        await dispatch_approval(self.runner.approvals, claimed, target=self.target)
        assert await self.principal.approvals.approval_continuation(claimed.approval_id) is None

    async def resume(self) -> None:
        result = AsyncMock(return_value=CompletedApprovalRun(response_text="Edited answer", metadata_content={}))
        with patch.object(self.runner.approvals, "_continue_entity_call", result):
            assert await self.runner.handoff_approval_source("$edit") is False
            await self.runner.inbox.wait_for_source_owned_inbox_responses()

    async def restart(self) -> None:
        self.store = await _store(self.journal_store, agent_name="general")
        self.gateway = replace(self.gateway)
        self.runner = runner_for(
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
    room_id, source_id, edit_id, answer_id = "!room:localhost", "$source", "$edit", "$answer"
    room = nio.MatrixRoom(room_id, bot.matrix_id.full_id)
    room.users["@user:localhost"] = nio.MatrixUser("@user:localhost", "User")
    bot.client.rooms[room_id] = room
    bot.client.room_send.return_value = nio.RoomSendResponse("$answer-edit", room_id)
    target = MessageTarget.resolve(room_id, None, source_id, room_mode=True)
    store = await _store(journal_store, agent_name="general")
    store.deps = replace(store.deps, state_writer=bot._conversation_state_writer)
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
        ),
    )
    replies = unwrap_extracted_collaborator(bot._response_runner).deps.replies
    runner = replace_response_runner_deps(
        bot,
        delivery_gateway=gateway,
        approval_store=principal.approvals,
        # These cases end a superseded approval by hand, as its source's worker
        # would after a crash cut its background cleanup short.
        replies=replace(replies, store=principal),
    )
    await runner.deps.replies.start()
    regenerator = unwrap_extracted_collaborator(bot._edit_regenerator)
    regenerator.deps = replace(
        regenerator.deps,
        turn_store=store,
        receipt_order=AsyncMock(return_value=3),
        store=principal,
    )
    await seed_finished_reply(
        principal,
        answer_id,
        sources=ResponseSources(pending_event_ids=(source_id,), logical_source_event_ids=(source_id,)),
        room_id=room_id,
        thread_id=None,
        entity_name="general",
    )
    ingress = bot._turn_ingress
    update_turn_owner_deps(bot, edit_regenerator=regenerator)
    outcomes: list[TurnDispatchOutcome] = []

    async def dispatch_edit(room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        outcome = await ingress.handle_text_event(room, event)
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
        requirements=(RunRequirement(tool),),
        response_text="Reading document",
        toolkit_owners={("general", "read_document"): "test_toolkit"},
    )
    # Hide tool anchors to keep this test focused on durable revision ownership.
    bot.config.defaults.show_tool_calls = False

    async def prepare_event(_room: str, _thread: str | None, content: dict) -> dict:
        return content

    async def send_delivery(_delivery: MatrixDelivery) -> str:
        return "$approval-card"

    manager = initialize_approval_store_for_test(
        runner.deps.runtime_paths,
        prepare_approval_event=prepare_event,
        send_approval_delivery=send_delivery,
        resolve_approval_delivery=AsyncMock(return_value=None),
        cards=journal_store.principal("router@shared"),
        transport_sender_id=lambda: "@router:localhost",
        transport_device_id=lambda: "DEVICE",
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
                stream_agent_response=streamed_answer(model),
            ),
            patch("mindroom.approval.response.evaluate_tool_approval", approval_evaluation),
            # Inspect the durable checkpoint before the test claims resumed execution.
            patch.object(ReplyRuntime, "claim_approval_resume", AsyncMock(return_value=(None, None))),
        ):
            await dispatcher.drain_once()
            await finish_edit_regenerations(bot)
        assert model.await_count == 1
        continuation = await principal.approvals.approval_continuation_for_source(edit_id)
        assert continuation is not None
        assert outcomes == [TurnDispatchOutcome.DEFERRED]
        assert continuation.source_event_ids == (edit_id,)
        assert continuation.state == ("waiting" if requires_human else "ready")
        assert await principal.is_pending(edit_id)
        assert not await principal.is_pending(source_id)
        assert await principal.approvals.approval_continuation_for_source(source_id) is None
        # The turn took the edit when its regeneration claimed the reply.
        assert (await store.get_turn_record(source_id)).source_event_revisions == {source_id: (20, edit_id)}

        yield _ApprovalCase(
            bot,
            room,
            store,
            principal,
            gateway,
            runner,
            ingress,
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
@pytest.mark.parametrize("requires_human", [False, True])
class TestEditApprovalOwnership:
    """Exercise independent pause, resume, and settlement contracts through real turn owners."""

    async def test_a_newer_edit_of_the_paused_reply_cancels_its_approval_and_regenerates(
        self,
        approval_case: _ApprovalCase,
    ) -> None:
        """The edit stops the held reply, which cancels its approval; the regeneration claims once the approval ended."""
        case = approval_case
        retried: list[tuple[str, ...]] = []
        case.runner.deps.replies.retry_sources = lambda _room_id, sources: retried.append(sources)
        await case.dispatch_newer()
        failing = await case.principal.approvals.approval_continuation(case.approval.approval_id)
        assert failing is not None
        assert failing.state == "failing"
        assert failing.failure == ApprovalFailure(ApprovalFailureKind.USER_STOP)
        # The regeneration waits for the approval's settlement, which the Stop woke.
        assert await case.principal.is_pending("$newer-edit")
        assert ("$newer-edit",) not in retried
        await case.runner.handoff_approval_source("$edit")
        await case.runner.inbox.wait_for_source_owned_inbox_responses()
        assert await case.principal.approvals.approval_continuation(case.approval.approval_id) is None
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
        assert await case.principal.approvals.approval_continuation_for_source("$newer-edit") is None
        assert not await case.runner.approvals._settle(
            claimed,
            ApprovalFailure(ApprovalFailureKind.ERROR, "Paused run is no longer available"),
        )
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
        assert await case.principal.approvals.approval_continuation_for_source("$edit") is None
        assert not await case.principal.is_pending("$edit")
        assert not await case.principal.is_pending("$source")
        final = await case.principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
        assert final is not None
        assert final.acknowledged_event_id is not None
        assert final.edits_event_id == "$answer"
        restarted = await _store(case.journal_store, agent_name="general")
        consumed = await restarted.get_turn_record("$source")
        assert consumed is not None
        assert consumed.source_event_ids == ("$source",)
        if redaction == "revision":
            assert await restarted.redacted_event_ids(case.room.room_id, ("$edit",)) == {"$edit"}
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
        failing = await case.principal.approvals.approval_continuation(case.approval.approval_id)
        assert failing is not None
        assert failing.state == "failing"
        assert failing.failure == ApprovalFailure(ApprovalFailureKind.USER_STOP)
        # An approval after the deletion runs nothing.
        await case.approve()
        resumed = AsyncMock(side_effect=AssertionError("A deleted message's approval must not execute"))
        with patch.object(case.runner.approvals, "_continue_entity_call", resumed):
            await case.runner.handoff_approval_source("$edit")
            await case.runner.inbox.wait_for_source_owned_inbox_responses()
        resumed.assert_not_awaited()
        assert await case.principal.approvals.approval_continuation(case.approval.approval_id) is None
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
        room_id="!room:localhost",
    )
    bot._turn_store = store
    gateway = unwrap_extracted_collaborator(runner.deps.delivery_gateway)
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            outbox=principal,
        ),
    )
    assert runner.deps.replies is not None
    runner = replace_response_runner_deps(
        bot,
        approval_store=principal.approvals,
        delivery_gateway=gateway,
        replies=replace(runner.deps.replies, store=principal),
    )
    request = replace(
        _plain_request(_target(), source_event_id="$edit"),
        existing_event_id="$waiting",
        edit_regeneration=True,
        sources=ResponseSources(("$edit",), ("$source",)),
    )
    lifecycle = runner.locked.build_lifecycle(request, response_kind="ai")
    progress = TurnProgress()
    pause = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session",
            run_id="run",
            requirements=tuple(map(RunRequirement, ())),
            toolkit_owners={},
        ),
    )

    async def fail_handoff(_pause: ResponsePausedForApproval) -> None:
        message = "Approval handoff failed"
        raise RuntimeError(message)

    with patch.object(type(runner.locked), "_run_cancellable_response", AsyncMock(side_effect=pause)):
        async with reply_span(
            principal,
            runtime=runner.deps.replies,
            source_event_id="$edit",
            room_id=request.room_id,
            thread_id=request.response_envelope.target.resolved_thread_id,
            logical_source_event_ids=("$source",),
            regenerated_event_id="$waiting",
        ):
            await runner.locked.run_and_settle_locked_response(
                request,
                target=request.response_envelope.target,
                lifecycle=lifecycle,
                progress=progress,
                response_function=AsyncMock(),
                build_post_response_outcome=lambda _outcome: ResponseOutcome(),
                post_response_deps=lambda: PostResponseEffectsDeps(logger=runner.deps.logger),
                approval_suspension_handler=fail_handoff,
            )
    assert progress.ending == Failed(FailureCause.APPROVAL_NOT_STARTED)
    # The regeneration wrote nothing, so its reply shows the answer it regenerated.
    assert await principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL) is None
    bot.client.room_send.assert_not_awaited()
    assert not await principal.is_pending("$edit")
    assert not await principal.is_pending("$source")
    persisted = await (await _store(journal_store, agent_name="general")).get_turn_record("$source")
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
            handle = current_span()
            assert handle is not None
            # The span's exit for an approval that never started, as the locked turn settles it.
            outcome = await runner.deps.delivery_gateway.end_interrupted_span(
                handle,
                request.response_envelope.target,
                ending=Failed(FailureCause.APPROVAL_NOT_STARTED),
                delivery_started=False,
                contact_turn=False,
            )
    assert outcome is not None
    assert outcome.ending == Failed(FailureCause.APPROVAL_NOT_STARTED)
    assert outcome.event_id == "$note"
    assert [content["body"] for content in sent] == ["Tool approval could not be started. Please try again."]
