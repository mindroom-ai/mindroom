"""Startup and deleted-source cleanup share normal visible-delivery ownership."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom import reply_lifecycle as rl
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import DeliveryStage, EventClass, EventKind
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix_delivery import TurnHandoff
from mindroom.message_target import MessageTarget
from mindroom.response_payload_preparation import DispatchPayloadInputs
from mindroom.response_runner import ResponseRunner
from mindroom.turn_policy import PreparedDispatch, ResponseAction
from mindroom.turn_record import RevisionSnapshotChangedError
from mindroom.visible_response_reconciliation import VisibleResponseReconciler
from tests.conftest import (
    make_visible_message,
    patch_response_runner_module,
    unwrap_extracted_collaborator,
)
from tests.journal_helpers import admit_dispatch_event
from tests.matrix_room_events import (
    BOT_USER_ID,
)
from tests.response_runner_helpers import _bot, _envelope, _noop_typing
from tests.test_orderly_shutdown_recovery import _dispatcher
from tests.test_response_delivery_gateway import TestTurnDeliveryGoesThroughTheOutbox as _DeliveryTestHooks
from tests.test_response_delivery_gateway import _gateway
from tests.test_response_redaction_recovery import _message, _redaction
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import EventJournalStore, PrincipalStore
    from mindroom.turn_store import TurnStore

pytestmark = [pytest.mark.asyncio, pytest.mark.ledger_loads_from_disk]
SOURCE = "$deleted"
INITIAL = "$initial"


def _runner_on(
    bot: AgentBot,
    gateway: DeliveryGateway,
    principal: PrincipalStore,
    turn_store: TurnStore,
) -> ResponseRunner:
    """Return the bot's response runner, with its replies, deliveries, and turn ledger on the test's stores.

    The runner's gateway runs its replies' effects, as the bot wires them.
    """
    deps = unwrap_extracted_collaborator(bot._response_runner).deps
    assert deps.replies is not None
    replies = replace(deps.replies, store=principal, complete_turn=turn_store.publish_completed_turn)
    return ResponseRunner(
        replace(
            deps,
            delivery_gateway=replace(gateway, deps=replace(gateway.deps, reply_effects=replies.run_effects)),
            approval_store=principal,
            replies=replies,
        ),
    )


@pytest.mark.parametrize("scenario", ["retry", "setup_failure", "source_deleted", "deleted_after_model"])
async def test_preparation_outcomes_reach_controller_and_journal_owners(  # noqa: C901, PLR0915
    journal_store: EventJournalStore,
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    scenario: str,
) -> None:
    """Stale live context retries full preparation; deletion suppresses; real setup failure completes visibly."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store, agent_name="general")
    bot = _bot(tmp_path)
    gateway = _gateway(
        tmp_path,
        principal,
        terminal_turn_for=store.terminal_turn_record,
        terminal_turn_committed=store.publish_committed_response,
        turn_handoff=TurnHandoff(lambda _turn: (SOURCE,), lambda ids: dispatcher.release_delivered_turn_sources(ids)),
    )
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            agent_name="general",
            response_hooks=_DeliveryTestHooks._hooks(),
        ),
    )
    runner = _runner_on(bot, gateway, principal, store)
    controller = unwrap_extracted_collaborator(bot._turn_controller)
    gateway.deps.response_hooks.emit_cancelled_response = AsyncMock()

    async def settle_ignored(sources: tuple[str, ...]) -> None:
        for source in sources:
            await principal.settle(source)

    visible_responses = VisibleResponseReconciler(
        replace(
            controller.deps.visible_responses.deps,
            turn_store=store,
            delivery_gateway=gateway,
            settle_ignored_sources=settle_ignored,
        ),
    )
    controller.deps = replace(
        controller.deps,
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

    async def redact(*, event_id: str, **_kwargs: object) -> bool:
        visible.pop(event_id, None)
        return True

    gateway.deps.runtime.client.room_send.side_effect = transport
    gateway.deps.redact_message_event.side_effect = redact
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
        task = runner.track_inbox_response(
            controller._execute_response_action(
                callback_room,
                event,
                dispatch,
                ResponseAction(kind="individual"),
                DispatchPayloadInputs((), (), ()),
                processing_log="Processing",
                dispatch_started_at=0.0,
                handled_turn=store.get_turn_record(SOURCE),
            ),
            name="inbox_response:preparation",
            recovery_proof_ready=lambda: True,
            on_failure=lambda: dispatcher.retry_turn_sources(callback_room.room_id, (SOURCE,)),
            source_event_ids=(SOURCE,),
            room_id=callback_room.room_id,
        )
        tasks.append(task)
        response_started.set()
        return TurnDispatchOutcome.DEFERRED

    dispatcher = _dispatcher(principal, callback)
    model_requests = []

    async def model(*args: object, **kwargs: object) -> str:
        model_requests.append((args, kwargs))
        if scenario == "deleted_after_model":
            captured.set()
            await release.wait()
        return "survivor answer"

    await admit_dispatch_event(
        dispatcher,
        room,
        _message(body="surviving request"),
        EventKind.MESSAGE,
        EventClass.ACTIONABLE,
    )
    with patch_response_runner_module(
        ai_response=AsyncMock(side_effect=model),
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
            assert store.get_turn_record(SOURCE).response_event_id is None
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
            assert store.get_turn_record(SOURCE).completed
            assert not await principal.is_pending(SOURCE)
            assert len([content for content in sends if "m.new_content" not in content]) == 1
        elif scenario == "deleted_after_model":
            assert results == [None]
            assert len(model_requests) == 1
            assert visible == {}
            assert len(sends) == 1, "Deleted INITIAL received a late fallback edit"
            for reopened in (False, True):
                if reopened:
                    _reset_handled_turn_ledger_runtime()
                    journal_store = journal_database()
                    principal = journal_store.principal("agent@alice")
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
            assert store.get_turn_record(SOURCE).response_event_id is None
            assert not store.get_turn_record(SOURCE).completed
        else:
            assert results == [None]
            assert model_requests == []
            assert "history provider setup failed" in visible[INITIAL]
            assert store.get_turn_record(SOURCE).completed
