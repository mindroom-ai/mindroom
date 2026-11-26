"""Startup and deleted-source cleanup share normal visible-delivery ownership."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import reply_lifecycle as rl
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.bot_runtime_view import RunPhase
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import DeliveryStage, EventClass, EventKind
from mindroom.event_journal.replies import PreparedReplyRow, ReplyRowRequest
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.message_target import MessageTarget
from mindroom.reply_scope import ReplyRuntime
from mindroom.reply_turn.inbox import InboxResponses
from mindroom.response_payload_preparation import DispatchPayloadInputs
from mindroom.response_sources import ResponseSources
from mindroom.turn_policy import PreparedDispatch, ResponseAction
from mindroom.turn_record import RevisionSnapshotChangedError, TurnRecord
from mindroom.visible_response_reconciliation import VisibleResponseReconciler
from tests.conftest import (
    make_visible_message,
    patch_response_runner_module,
    unwrap_extracted_collaborator,
    update_turn_owner_deps,
)
from tests.journal_helpers import admit_dispatch_event
from tests.matrix_room_events import (
    BOT_USER_ID,
)
from tests.reply_span_helpers import no_reply_jobs
from tests.reply_turn_helpers import runner_for
from tests.response_runner_helpers import _bot, _envelope, _noop_typing
from tests.test_orderly_shutdown_recovery import _dispatcher
from tests.test_response_delivery_gateway import TestTurnDeliveryGoesThroughTheOutbox as _DeliveryTestHooks
from tests.test_response_delivery_gateway import _gateway
from tests.test_response_redaction_recovery import _message, _redaction
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import EventJournalStore, PrincipalStore
    from mindroom.response_runner import ResponseRunner

pytestmark = pytest.mark.asyncio
SOURCE = "$deleted"
INITIAL = "$initial"


def _runner_on(
    bot: AgentBot,
    gateway: DeliveryGateway,
    principal: PrincipalStore,
) -> ResponseRunner:
    """Return the bot's response runner, with its replies and deliveries on the test's stores.

    The runner's gateway runs its replies' effects, as the bot wires them.
    """
    deps = unwrap_extracted_collaborator(bot._response_runner).deps
    replies = replace(deps.replies, store=principal)
    return runner_for(
        replace(
            deps,
            delivery_gateway=replace(gateway, deps=replace(gateway.deps, reply_effects=replies.run_effects)),
            approval_store=principal.approvals,
            replies=replies,
        ),
    )


@pytest.mark.parametrize("scenario", ["retry", "setup_failure", "source_deleted", "deleted_after_model"])
async def test_preparation_outcomes_reach_dispatch_and_journal_owners(  # noqa: C901, PLR0915
    journal_store: EventJournalStore,
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    scenario: str,
) -> None:
    """Stale live context retries full preparation; deletion suppresses; real setup failure completes visibly."""
    principal = journal_store.principal("general@alice")
    store = await _store(journal_store, agent_name="general")
    bot = _bot(tmp_path)
    gateway = _gateway(
        tmp_path,
        principal,
        turn_handoff=lambda ids: dispatcher.release_delivered_turn_sources(ids),
    )
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            agent_name="general",
            response_hooks=_DeliveryTestHooks._hooks(),
        ),
    )
    runner = _runner_on(bot, gateway, principal)
    turn_dispatcher = bot._turn_dispatcher
    gateway.deps.response_hooks._emit_cancelled_response = AsyncMock()

    async def settle_ignored(sources: tuple[str, ...]) -> None:
        for source in sources:
            await principal.settle(source)

    visible_responses = VisibleResponseReconciler(
        replace(
            turn_dispatcher.deps.visible_responses.deps,
            turn_store=store,
            delivery_gateway=gateway,
            settle_ignored_sources=settle_ignored,
        ),
    )
    update_turn_owner_deps(
        bot,
        response_runner=runner,
        delivery_gateway=gateway,
        turn_store=store,
        visible_responses=visible_responses,
    )
    room_id = "!room:localhost"
    room = nio.MatrixRoom(room_id, BOT_USER_ID)
    target = MessageTarget.resolve(room_id, "$thread", SOURCE)
    record = TurnRecord.create([SOURCE], completed=False, conversation_target=target)
    await store.record_pending_turn(record)
    await store.record_pending_turn(TurnRecord.create(["$context"], completed=False, conversation_target=target))
    visible = {}
    sends = []

    async def transport(*_args: object, **kwargs: object) -> nio.RoomSendResponse:
        content = kwargs["content"]
        sends.append(content)
        body = content.get("m.new_content", content)["body"]
        visible[INITIAL] = body
        return nio.RoomSendResponse("$final" if "m.new_content" in content else INITIAL, room_id)

    async def redact(_room_id: str, event_id: str, **_kwargs: object) -> nio.RoomRedactResponse:
        visible.pop(event_id, None)
        return nio.RoomRedactResponse("$redaction", room_id)

    gateway.deps.runtime.client.room_send.side_effect = transport
    gateway.deps.runtime.client.room_redact.side_effect = redact
    captured, release = asyncio.Event(), asyncio.Event()
    reads = 0

    async def history(*_args: object, **_kwargs: object) -> ThreadHistoryResult:
        nonlocal reads
        reads += 1
        if scenario == "deleted_after_model":
            return ThreadHistoryResult(
                [make_visible_message(event_id=SOURCE, body="surviving request")],
                is_full_history=True,
            )
        if reads == 1:
            snapshot = ThreadHistoryResult(
                [
                    make_visible_message(event_id="$context", body="PRIVATE_CONTEXT_TO_DELETE"),
                    make_visible_message(event_id=SOURCE, body="surviving request"),
                ],
                is_full_history=True,
            )
            captured.set()
            await release.wait()
            if scenario == "setup_failure":
                runner.deps.request_preparer.normalizer.build_dispatch_payload_with_attachments = AsyncMock(
                    side_effect=RuntimeError("history provider setup failed"),
                )
            return snapshot
        return ThreadHistoryResult(
            [make_visible_message(event_id=SOURCE, body="surviving request")],
            is_full_history=True,
        )

    runner.deps.resolver.fetch_thread_history = AsyncMock(side_effect=history)
    tasks = []
    response_started = asyncio.Event()

    async def callback(callback_room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=True,
                thread_id="$thread",
                thread_history=(),
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
            target=target,
            correlation_id=SOURCE,
            envelope=_envelope(target, source_event_id=SOURCE),
        )
        task = runner.inbox.track_inbox_response(
            turn_dispatcher._execute_response_action(
                callback_room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                dispatch_started_at=0.0,
                handled_turn=await store.get_turn_record(SOURCE),
            ),
            name="inbox_response:preparation",
            recovery_proof_ready=lambda: True,
            source_event_ids=(SOURCE,),
            room_id=callback_room.room_id,
        )
        tasks.append(task)
        response_started.set()
        return TurnDispatchOutcome.DEFERRED

    dispatcher = _dispatcher(principal, callback)
    # A finished inbox task hands its sources back to this test's journal dispatcher.
    runner.inbox.retry_approval_sources = dispatcher.retry_turn_sources
    model_requests = []

    def model(*args: object, **kwargs: object) -> AsyncIterator[str]:
        model_requests.append((args, kwargs))

        async def answer() -> AsyncIterator[str]:
            if scenario == "deleted_after_model":
                captured.set()
                await release.wait()
            yield "survivor answer"

        return answer()

    await admit_dispatch_event(
        dispatcher,
        room,
        _message(body="surviving request"),
        EventKind.MESSAGE,
        EventClass.ACTIONABLE,
    )
    with patch_response_runner_module(
        stream_agent_response=model,
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing,
        apply_post_response_effects=AsyncMock(),
    ):
        assert await dispatcher.drain_once() == 1
        await captured.wait()
        if scenario != "setup_failure":
            deleted = SOURCE if scenario in {"source_deleted", "deleted_after_model"} else "$context"
            await admit_dispatch_event(
                dispatcher,
                room,
                _redaction(deleted),
                EventKind.REDACTION,
                EventClass.ACTIONABLE,
            )
        if scenario == "deleted_after_model":
            assert (await principal.replies.for_sources((SOURCE,))).event_id == INITIAL
            assert (await gateway.recover_deliveries()).complete
            assert visible == {}
            assert (await store.get_turn_record(SOURCE)).response_event_id is None
        release.set()
        results = await asyncio.gather(tasks[-1], return_exceptions=True)
        if scenario != "retry":
            tasks[-1].result()
        if scenario == "retry":
            assert isinstance(results[0], RevisionSnapshotChangedError)
            assert await principal.is_pending(SOURCE)
            assert await principal.load_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.FINAL) is None
            assert (await principal.replies.for_sources((SOURCE,))).event_id == INITIAL
            assert model_requests == []
            response_started.clear()
            await dispatcher.drain_once()
            dispatcher.start()
            try:
                await asyncio.wait_for(response_started.wait(), timeout=5)
                await tasks[-1]
            finally:
                await dispatcher.stop()
            assert len(model_requests) == 1
            assert "PRIVATE_CONTEXT_TO_DELETE" not in str(model_requests)
            assert visible == {INITIAL: "survivor answer"}
            assert (await store.get_turn_record(SOURCE)).completed
            assert not await principal.is_pending(SOURCE)
            assert len([content for content in sends if "m.new_content" not in content]) == 1
        elif scenario == "deleted_after_model":
            assert results == [None]
            assert len(model_requests) == 1
            assert visible == {}
            assert len(sends) == 1, "Deleted INITIAL received a late fallback edit"
            for reopened in (False, True):
                if reopened:
                    journal_store = journal_database()
                    principal = journal_store.principal("general@alice")
                    store = await _store(journal_store, agent_name="general")
                # The deletion ended the reply gone and its placeholder was removed; no answer row exists.
                reply = await principal.replies.for_sources((SOURCE,))
                assert reply is not None
                assert reply.state is rl.ReplyState.GONE
                assert not reply.redaction_pending
                assert await principal.load_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.FINAL) is None
        elif scenario == "source_deleted":
            assert results == [None]
            assert model_requests == []
            assert visible == {}
            assert (await store.get_turn_record(SOURCE)).response_event_id is None
            assert not (await store.get_turn_record(SOURCE)).completed
        else:
            assert results == [None]
            assert model_requests == []
            assert "history provider setup failed" in visible[INITIAL]
            assert (await store.get_turn_record(SOURCE)).completed


@pytest.mark.parametrize(
    ("error", "retriable"),
    [
        (RevisionSnapshotChangedError("Conversation revision changed before locked response preparation"), True),
        (ReplyMembershipPendingError(), True),
        (RuntimeError("unexpected inbox failure"), False),
    ],
    ids=["revision_snapshot_changed", "reply_membership_pending", "unexpected_runtime_error"],
)
async def test_inbox_response_task_logs_retriable_handoff_errors_at_info(
    error: Exception,
    retriable: bool,
) -> None:
    """Retriable handoff exceptions log info and retry sources without logging inbox_response_task_failed."""
    logger = MagicMock()
    replies = MagicMock()
    replies.waits_to_claim.return_value = False
    retried: list[tuple[str, tuple[str, ...]]] = []
    claim = MagicMock()
    inbox = InboxResponses(
        logger=logger,
        replies=replies,
        retry_approval_sources=lambda room_id, event_ids: retried.append((room_id, event_ids)),
        runtime=MagicMock(phase=RunPhase.ACCEPTING),
    )

    async def failing_response() -> None:
        raise error

    task = inbox.track_inbox_response(
        failing_response(),
        name="inbox_response:test",
        room_id="!room:localhost",
        recovery_proof_ready=lambda: True,
        claim=claim,
        source_event_ids=("$event",),
    )

    results = await asyncio.gather(task, return_exceptions=True)

    assert results == [error]
    claim.release.assert_called_once_with()
    assert retried == [("!room:localhost", ("$event",))]
    if retriable:
        logger.info.assert_called_once_with(
            "inbox_response_task_retrying",
            task_name="inbox_response:test",
            exception_type=error.__class__.__name__,
            error=str(error),
        )
        logger.error.assert_not_called()
    else:
        logger.error.assert_called_once_with(
            "inbox_response_task_failed",
            task_name="inbox_response:test",
            exception_type=error.__class__.__name__,
            error=str(error),
        )
        logger.info.assert_not_called()


async def test_supersede_replay_releases_deferred_sources_without_lost_owner_warning(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """Superseding an interrupted replay releases its deferred sources before a lost-owner sweep runs."""
    principal = journal_store.principal("general@alice")
    room_id = "!room:localhost"
    room = nio.MatrixRoom(room_id, BOT_USER_ID)
    source_ids = ("$event-1", "$event-2")
    sources = ResponseSources(pending_event_ids=source_ids, logical_source_event_ids=source_ids)
    owned: set[str] = set()

    async def defer_replay(_callback_room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        owned.add(event.event_id)
        return TurnDispatchOutcome.DEFERRED

    dispatcher = _dispatcher(principal, defer_replay, source_has_live_owner=owned.__contains__)
    replies = ReplyRuntime(
        store=principal,
        entity_name="general",
        generation="gen-2",
        retry_sources=dispatcher.retry_turn_sources,
        approval_ended=lambda _reply_id: None,
        reply_stopped=no_reply_jobs,
        release_sources=dispatcher.release_delivered_turn_sources,
    )
    gateway = _gateway(
        tmp_path,
        principal,
        turn_handoff=dispatcher.release_delivered_turn_sources,
    )
    gateway = replace(
        gateway,
        deps=replace(gateway.deps, agent_name="general", reply_effects=replies.run_effects),
    )
    gateway.deps.runtime.client.room_redact = AsyncMock(return_value=nio.RoomRedactResponse("$redaction", room_id))

    for event_id in source_ids:
        await admit_dispatch_event(
            dispatcher,
            room,
            _message(event_id=event_id, body=f"prompt {event_id}"),
            EventKind.MESSAGE,
            EventClass.ACTIONABLE,
        )

    # An earlier bot generation claimed a reply and acknowledged a placeholder before restarting.
    await principal.replies.write_generation("gen-1")
    claimed = (
        await principal.replies.claim(
            rl.ClaimRequest(
                span_id="span-1",
                delivery_id=sources.delivery_id,
                sources=sources,
                bot_generation="gen-1",
                now_ns=10,
                membership_epoch=await principal.membership_epoch(room_id),
                new_reply_id="reply-1",
                entity_name="general",
                room_id=room_id,
                thread_id="$thread",
                empty_presentation="{}",
            ),
        )
    ).transition
    assert claimed.reply is not None
    assert claimed.claimed is not None
    await principal.enqueue_reply_row(
        ReplyRowRequest(
            reply_id=claimed.reply.reply_id,
            span_id=claimed.claimed.span_id,
            decide=lambda reply, span: rl.enqueue_initial(
                reply,
                span,
                shown="placeholder",
                placeholder_only=True,
                prepared_revision=reply.revision,
                now_ns=20,
            ),
            placeholder_only=True,
        ),
        PreparedReplyRow(payload={"body": "Thinking..."}),
    )
    assert await principal.claim_matrix_delivery(delivery_id=sources.delivery_id, stage=DeliveryStage.INITIAL)
    await principal.acknowledge_matrix_delivery(
        delivery_id=sources.delivery_id,
        stage=DeliveryStage.INITIAL,
        event_id="$reply",
        delivered_projections=(),
    )

    # Restart takes ownership (losing the interrupted span), and startup replay defers both sources.
    await replies.start()
    assert await dispatcher.drain_once() == len(source_ids)
    assert set(dispatcher._worker._deferred) == set(source_ids)

    # Batch 1 supersedes the interrupted replay and drops its in-memory ownership.
    owned.clear()
    assert await gateway.supersede_replay(source_ids) is True
    for event_id in source_ids:
        assert not await principal.is_pending(event_id)
    assert dispatcher._worker._deferred == {}
    gateway.deps.runtime.client.room_redact.assert_awaited_once()

    with patch("mindroom.pending_event_worker.logger.warning") as mock_warning:
        dispatcher._worker._queue_lost_owners()
        assert await dispatcher.drain_once() == 0

    lost_owner_warnings = [
        call
        for call in mock_warning.call_args_list
        if call.args and call.args[0] == "pending_event_deferral_owner_lost"
    ]
    assert lost_owner_warnings == []
