"""Startup and deleted-source cleanup share normal visible-delivery ownership."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.cancellation import request_task_cancel
from mindroom.constants import ORIGINAL_SENDER_KEY, STREAM_STATUS_KEY
from mindroom.conversation_resolver import MessageContext
from mindroom.delivery_gateway import ResponseIdentity, _PlaceholderFailureUpdateRequest
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_scope
from mindroom.event_journal import DeliveryStage, EventClass, EventKind
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.matrix import stale_stream_cleanup as cleanup
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix_delivery import TurnHandoff
from mindroom.message_target import MessageTarget
from mindroom.response_delivery_recovery import ResponseDeliveryRecovery
from mindroom.response_payload_preparation import DispatchPayloadInputs
from mindroom.response_runner import ResponseRequest, ResponseRunner
from mindroom.turn_policy import PreparedDispatch, ResponseAction
from mindroom.turn_record import RevisionSnapshotChangedError
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from mindroom.visible_response_reconciliation import VisibleResponseReconciler
from tests.conftest import (
    make_relation_lookup,
    make_visible_message,
    patch_response_runner_module,
    runtime_paths_for,
    unwrap_extracted_collaborator,
)
from tests.journal_helpers import admit_dispatch_event
from tests.response_runner_helpers import _bot, _envelope, _noop_typing
from tests.test_orderly_shutdown_recovery import _dispatcher
from tests.test_response_delivery_gateway import TestTurnDeliveryGoesThroughTheOutbox as _DeliveryTestHooks
from tests.test_response_delivery_gateway import _gateway, _response_recovery_bot
from tests.test_response_redaction_recovery import _message, _redaction
from tests.test_stale_stream_cleanup import (
    BOT_USER_ID,
    NOW_MS,
    ROOM_ID,
    STALE_AGE_MS,
    USER_ID,
    _authoritative_history,
    _history_message,
    _make_client,
    _make_config,
    _make_message_event,
    _room_messages_response,
    _thread_reply_relation,
)
from tests.test_turn_controller_focused import _build_harness, _room_with_members, _text_event
from tests.test_turn_store import _store
from tests.test_user_stop_convergence import _SerializingRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import EventJournalStore, MatrixDelivery
    from mindroom.turn_store import TurnStore

pytestmark = [pytest.mark.asyncio, pytest.mark.ledger_loads_from_disk]
SOURCE = "$deleted"
INITIAL = "$initial"


async def test_fallback_edit_keeps_cleanup_behind_delivery_lock(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """Cleanup cannot remove INITIAL between fallback eligibility and its network edit."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    target = MessageTarget.resolve(ROOM_ID, "$thread", SOURCE)
    await store.record_pending_turn(TurnRecord.create([SOURCE], completed=False, conversation_target=target))
    dispatcher = _dispatcher(principal, AsyncMock())
    room = nio.MatrixRoom(ROOM_ID, BOT_USER_ID)
    await admit_dispatch_event(dispatcher, room, _message(), EventKind.MESSAGE, EventClass.ACTIONABLE)
    gateway = _gateway(tmp_path, principal)
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            response_recovery=ResponseDeliveryRecovery(principal, lambda: store, gateway.deps.redact_message_event),
        ),
    )
    visible = {}

    async def send(delivery: MatrixDelivery) -> str:
        visible[INITIAL] = delivery.payload["body"]
        return INITIAL

    await gateway._response_delivery(send, handoff=None).deliver(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id="$thread",
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    editing, release_edit, cleanup_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def edit(*_args: object, **kwargs: object) -> nio.RoomSendResponse:
        editing.set()
        await release_edit.wait()
        visible[INITIAL] = kwargs["content"]["m.new_content"]["body"]
        return nio.RoomSendResponse("$failure", ROOM_ID)

    async def redact(*, event_id: str, **_kwargs: object) -> bool:
        visible.pop(event_id, None)
        return True

    async def clean() -> bool:
        cleanup_started.set()
        return await gateway.cleanup_deleted_response(SOURCE)

    gateway.deps.runtime.client.room_send.side_effect = edit
    gateway.deps.runtime.client.rooms[ROOM_ID] = gateway.deps.runtime.client.rooms["!room:localhost"]
    gateway.deps.redact_message_event.side_effect = redact
    fallback = asyncio.create_task(
        gateway._finish_placeholder_delivery_failure(
            _PlaceholderFailureUpdateRequest(
                target,
                INITIAL,
                ResponseIdentity("agent", _envelope(target, source_event_id=SOURCE), SOURCE),
                "delivery_failed",
                None,
                None,
            ),
        ),
    )
    started = asyncio.create_task(editing.wait())
    await asyncio.wait((fallback, started), return_when=asyncio.FIRST_COMPLETED)
    started.cancel()
    if fallback.done():
        fallback.result()
    assert editing.is_set()
    fallback_owned_lock = gateway._recovery_worker()._delivery_lock(SOURCE).locked()
    await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    cleaning = asyncio.create_task(clean())
    await cleanup_started.wait()
    try:
        assert fallback_owned_lock, "Fallback transport started without the delivery lock"
        assert gateway._recovery_worker()._delivery_lock(SOURCE).locked()
        assert not cleaning.done()
        assert visible == {INITIAL: "Thinking..."}
    finally:
        release_edit.set()
        outcome = await fallback
        assert await cleaning
    assert outcome.is_visible_response
    assert visible == {}
    assert store.get_turn_record(SOURCE).response_event_id is None


@pytest.mark.parametrize("debt", ["recovered", "adopted", "unattempted", "lost_ack"])
async def test_recovered_initial_survives_same_requester_supersession(  # noqa: PLR0915
    journal_store: EventJournalStore,
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    debt: str,
) -> None:
    """Plain-reply replay finishes its recovered INITIAL despite a newer requester source."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store, agent_name="general")
    bot = _bot(tmp_path)
    room = _room_with_members(bot.config, "general", room_id="!room:localhost")
    source_id, thread_id = "$original", "$root:localhost"
    event = _text_event("original request", event_id=source_id)
    event.source["content"]["m.relates_to"] = {"m.in_reply_to": {"event_id": "$reply-target"}}
    target = MessageTarget.resolve(room.room_id, thread_id, source_id)
    original = TurnRecord.create(
        [source_id],
        completed=False,
        requester_id="@user:localhost",
        conversation_target=target,
    )
    await store.record_pending_turn(original)
    dispatcher = _dispatcher(principal, AsyncMock())
    await admit_dispatch_event(dispatcher, room, event, EventKind.MESSAGE, EventClass.ACTIONABLE)
    source = await principal.load_event(source_id)
    assert source is not None
    assert not source.thread_id
    await principal.enqueue_matrix_delivery(
        delivery_id=source_id,
        stage=DeliveryStage.INITIAL,
        room_id=room.room_id,
        thread_id=thread_id,
        payload={
            "msgtype": "m.text",
            "body": "Thinking...",
            "m.relates_to": _thread_reply_relation(thread_id, source_id),
        },
    )
    if debt != "unattempted":
        await principal.claim_matrix_delivery(
            delivery_id=source_id,
            stage=DeliveryStage.INITIAL,
            sending_device_id="CURRENT-DEVICE",
        )
    # Recreate runtime before the original send's normal adoption callback.
    _reset_handled_turn_ledger_runtime()
    journal_store = journal_database()
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store, agent_name="general")
    newer = _text_event(
        "newer requester message",
        event_id="$newer",
        thread_id=thread_id,
        origin_server_ts=2_000_000,
    )
    await admit_dispatch_event(dispatcher, room, newer, EventKind.MESSAGE, EventClass.ACTIONABLE)
    history = ThreadHistoryResult(
        [
            make_visible_message(
                event_id=source_id,
                sender="@user:localhost",
                body="original request",
                timestamp=1_000_000,
            ),
            make_visible_message(
                event_id="$newer",
                sender="@user:localhost",
                body="newer requester message",
                timestamp=2_000_000,
            ),
        ],
        is_full_history=True,
    )
    harness = _build_harness(bot.config, tmp_path, thread_history=history)
    controller = harness.controller
    relations = make_relation_lookup(threads={"$reply-target": thread_id})
    controller.deps.resolver.deps = replace(controller.deps.resolver.deps, relations=relations)
    gateway = _gateway(
        tmp_path,
        principal,
        terminal_turn_for=store.terminal_turn_record,
        terminal_turn_committed=store.publish_committed_response,
        turn_handoff=TurnHandoff(lambda _turn: (source_id,), dispatcher.release_delivered_turn_sources),
    )
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            agent_name="general",
            response_hooks=_DeliveryTestHooks._hooks(),
            response_recovery=ResponseDeliveryRecovery(principal, lambda: store, gateway.deps.redact_message_event),
        ),
    )
    visible, sends, model_requests = {}, [], []

    async def transport(*_args: object, **kwargs: object) -> nio.RoomSendResponse:
        content = kwargs["content"]
        sends.append(content)
        visible[INITIAL] = content.get("m.new_content", content)["body"]
        return nio.RoomSendResponse("$final" if "m.new_content" in content else INITIAL, room.room_id)

    gateway.deps.runtime.client.room_send.side_effect = transport
    if debt in {"recovered", "adopted"}:
        assert (await gateway.recover_deliveries()).complete
    elif debt == "lost_ack":
        visible[INITIAL] = "Thinking..."
    original_initial_id = INITIAL
    initial = await principal.load_matrix_delivery(delivery_id=source_id, stage=DeliveryStage.INITIAL)
    assert initial is not None
    assert initial.acknowledged_event_id == (original_initial_id if debt in {"recovered", "adopted"} else None)
    assert store.get_turn_record(source_id).response_event_id is None
    if debt == "adopted":
        await store.record_pending_turn(replace(original, response_event_id=original_initial_id))
    runner = ResponseRunner(
        replace(
            unwrap_extracted_collaborator(bot._response_runner).deps,
            delivery_gateway=gateway,
        ),
    )
    runner.deps.resolver.fetch_thread_history = AsyncMock(return_value=history)

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
    controller.deps.runtime.client.room_messages.return_value = _room_messages_response(
        *(
            []
            if debt in {"unattempted", "lost_ack"}
            else [
                _make_message_event(
                    event_id=INITIAL,
                    sender=controller.deps.matrix_id.full_id,
                    body="Thinking...",
                    timestamp_ms=NOW_MS,
                    relates_to=_thread_reply_relation(thread_id, source_id),
                    extra_content={STREAM_STATUS_KEY: "pending"},
                ),
            ]
        ),
    )
    controller.deps = replace(
        controller.deps,
        turn_store=store,
        pending_turns=principal,
        response_runner=runner,
        delivery_gateway=gateway,
        visible_responses=visible_responses,
        relations=relations,
        ingress=replace(controller.deps.ingress, deps=replace(controller.deps.ingress.deps, turn_store=store)),
    )

    async def model(*args: object, **kwargs: object) -> str:
        model_requests.append((args, kwargs))
        return "Original request finished with a substantive answer."

    with (
        turn_dispatch_recovery_scope(active=True),
        patch_response_runner_module(
            ai_response=AsyncMock(side_effect=model),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        await controller.handle_text_event(room, event)
        await harness.gate.drain_all()
        await runner.wait_for_source_owned_inbox_responses()
    response_record = store.get_turn_record(source_id)
    final = await principal.load_matrix_delivery(delivery_id=source_id, stage=DeliveryStage.FINAL)
    assert response_record.completed
    assert response_record.response_event_id == original_initial_id
    assert final is not None
    assert final.edits_event_id == original_initial_id
    assert not await principal.is_pending(source_id)
    assert len(model_requests) == 1
    assert visible == {INITIAL: "Original request finished with a substantive answer."}
    assert len([content for content in sends if "m.new_content" not in content]) == 1
    assert response_record.conversation_target.resolved_thread_id == thread_id


@pytest.mark.parametrize("repair_first", [False, True])
async def test_startup_snapshot_cannot_overwrite_completed_final(
    journal_store: EventJournalStore,
    tmp_path: Path,
    repair_first: bool,
) -> None:
    """FINAL ACK while room history is in flight invalidates stale restart repair."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    target = MessageTarget.resolve(ROOM_ID, "$thread", SOURCE)
    await store.record_pending_turn(TurnRecord.create([SOURCE], completed=False, conversation_target=target))
    gateway = _gateway(
        tmp_path,
        principal,
        terminal_turn_for=store.terminal_turn_record,
        terminal_turn_committed=store.publish_committed_response,
    )
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            response_recovery=ResponseDeliveryRecovery(
                principal,
                lambda store=store: store,
                gateway.deps.redact_message_event,
            ),
        ),
    )
    visible = {}

    async def send(delivery: MatrixDelivery) -> str:
        visible[INITIAL] = delivery.payload.get("m.new_content", delivery.payload)["body"]
        return INITIAL if delivery.stage is DeliveryStage.INITIAL else "$final"

    worker = gateway._response_delivery(send, handoff=None)
    await worker.deliver(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id="$thread",
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    config = _make_config(tmp_path)
    client = _make_client()
    snapshot = _room_messages_response(
        _make_message_event(
            event_id=INITIAL,
            body="Thinking...",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread", SOURCE),
            extra_content={STREAM_STATUS_KEY: "pending", ORIGINAL_SENDER_KEY: USER_ID},
        ),
    )
    captured, release = asyncio.Event(), asyncio.Event()
    editing, release_edit = asyncio.Event(), asyncio.Event()

    async def history(*_args: object, **_kwargs: object) -> nio.RoomMessagesResponse:
        captured.set()
        await release.wait()
        return snapshot

    async def edit(*_args: object, **kwargs: object) -> nio.RoomSendResponse:
        editing.set()
        await release_edit.wait()
        visible[INITIAL] = kwargs["content"]["m.new_content"]["body"]
        return nio.RoomSendResponse("$repair", ROOM_ID)

    final_queued = asyncio.Event()

    async def final() -> str | None:
        final_queued.set()
        return await worker.deliver(
            delivery_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM_ID,
            thread_id="$thread",
            edits_event_id=INITIAL,
            payload={"msgtype": "m.text", "body": "answer", "m.new_content": {"msgtype": "m.text", "body": "answer"}},
        )

    client.room_messages.side_effect = history
    client.room_send.side_effect = edit
    with patch.object(cleanup.time, "time", return_value=NOW_MS / 1000):
        scan = asyncio.create_task(
            cleanup._cleanup_stale_streaming_room(
                client,
                room_id=ROOM_ID,
                actors={BOT_USER_ID: client},
                bot_user_ids={BOT_USER_ID},
                config=config,
                runtime_paths=runtime_paths_for(config),
                response_recovery_scope=lambda _agent, room, event, gateway=gateway: gateway.response_recovery_scope(
                    room,
                    event,
                ),
            ),
        )
        await captured.wait()
        if repair_first:
            release.set()
            await editing.wait()
            pending_final = asyncio.create_task(final())
            await final_queued.wait()
            assert not pending_final.done()
            release_edit.set()
            await scan
            await pending_final
        else:
            release_edit.set()
            await final()
            release.set()
            await scan
    assert visible == {INITIAL: "answer"}
    assert store.get_turn_record(SOURCE).completed


@pytest.mark.parametrize("winner", ["final", "redaction", "journal"])
async def test_delayed_relay_loses_to_normal_final(
    journal_store: EventJournalStore,
    tmp_path: Path,
    winner: str,
) -> None:
    """Queued relay cannot create more work after original FINAL is acknowledged."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    await store.record_pending_turn(
        TurnRecord.create(
            [SOURCE],
            completed=False,
            response_event_id=INITIAL,
            conversation_target=MessageTarget.resolve(ROOM_ID, "$thread", SOURCE),
        ),
    )
    gateway = _gateway(
        tmp_path,
        principal,
        terminal_turn_for=store.terminal_turn_record,
        terminal_turn_committed=store.publish_committed_response,
    )
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            response_recovery=ResponseDeliveryRecovery(
                principal,
                lambda store=store: store,
                gateway.deps.redact_message_event,
            ),
        ),
    )

    async def send(delivery: MatrixDelivery) -> str:
        return INITIAL if delivery.stage is DeliveryStage.INITIAL else "$final"

    worker = gateway._response_delivery(
        send,
        handoff=TurnHandoff(lambda _turn: (SOURCE,), lambda _sources: None),
    )
    dispatcher = _dispatcher(principal, AsyncMock())
    room = nio.MatrixRoom(ROOM_ID, BOT_USER_ID)
    await admit_dispatch_event(dispatcher, room, _message(), EventKind.MESSAGE, EventClass.ACTIONABLE)
    await worker.deliver(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id="$thread",
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    candidate = cleanup._InterruptedThread(ROOM_ID, "$thread", INITIAL, "", "test_agent", USER_ID, 1)
    client = _make_client()
    relays = []
    captured, release = asyncio.Event(), asyncio.Event()

    async def history(*_args: object, **_kwargs: object) -> ThreadHistoryResult:
        captured.set()
        await release.wait()
        return _authoritative_history(_history_message(INITIAL, body="answer"))

    async def relay(*_args: object, **kwargs: object) -> nio.RoomSendResponse:
        relays.append(kwargs["content"])
        return nio.RoomSendResponse("$relay", ROOM_ID)

    def owned(_agent: str, room: str, event: str) -> AbstractAsyncContextManager[bool]:
        return gateway.response_recovery_scope(room, event)

    config = _make_config(tmp_path)
    client.room_send.side_effect = relay
    with patch.object(cleanup, "fetch_thread_messages_from_source", side_effect=history):
        pending = asyncio.create_task(
            cleanup._auto_resume_interrupted_threads(
                client,
                [candidate],
                response_recovery_scope=owned,
                config=config,
                runtime_paths=runtime_paths_for(config),
                delay=0,
            ),
        )
        await captured.wait()
        if winner == "final":
            await worker.deliver(
                delivery_id=SOURCE,
                stage=DeliveryStage.FINAL,
                room_id=ROOM_ID,
                thread_id="$thread",
                edits_event_id=INITIAL,
                payload={"msgtype": "m.text", "body": "answer"},
            )
            assert not await principal.is_pending(SOURCE)
        elif winner == "redaction":
            await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
        release.set()
        await pending
    assert relays == []


async def test_relay_publication_holds_normal_final_delivery_owner(  # noqa: PLR0915
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A publishing orphan relay keeps normal FINAL enqueue behind its delivery lock."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    await store.record_pending_turn(
        TurnRecord.create(
            [SOURCE],
            completed=False,
            response_event_id=INITIAL,
            conversation_target=MessageTarget.resolve(ROOM_ID, "$thread", SOURCE),
        ),
    )
    gateway = _gateway(
        tmp_path,
        principal,
        terminal_turn_for=store.terminal_turn_record,
        terminal_turn_committed=store.publish_committed_response,
    )
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            response_recovery=ResponseDeliveryRecovery(principal, lambda: store, gateway.deps.redact_message_event),
        ),
    )
    visible = {}
    final_sent = asyncio.Event()

    async def send(delivery: MatrixDelivery) -> str:
        visible[INITIAL] = delivery.payload.get("m.new_content", delivery.payload)["body"]
        if delivery.stage is DeliveryStage.FINAL:
            final_sent.set()
        return INITIAL if delivery.stage is DeliveryStage.INITIAL else "$final"

    worker = gateway._response_delivery(send, handoff=None)
    dispatcher = _dispatcher(principal, AsyncMock())
    await admit_dispatch_event(
        dispatcher,
        nio.MatrixRoom(ROOM_ID, BOT_USER_ID),
        _message(),
        EventKind.MESSAGE,
        EventClass.ACTIONABLE,
    )
    await worker.deliver(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id="$thread",
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    await principal.settle(SOURCE)
    assert not await principal.is_pending(SOURCE)
    candidate = cleanup._InterruptedThread(ROOM_ID, "$thread", INITIAL, "", "test_agent", USER_ID, 1)
    client, config = _make_client(), _make_config(tmp_path)
    publishing, release_relay, final_queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    relays = []

    async def relay(*_args: object, **kwargs: object) -> nio.RoomSendResponse:
        publishing.set()
        await release_relay.wait()
        relays.append(kwargs["content"])
        return nio.RoomSendResponse("$relay", ROOM_ID)

    async def final() -> str | None:
        final_queued.set()
        return await worker.deliver(
            delivery_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM_ID,
            thread_id="$thread",
            edits_event_id=INITIAL,
            payload={"msgtype": "m.text", "body": "answer"},
        )

    async def resume() -> int:
        return await cleanup._auto_resume_interrupted_threads(
            client,
            [candidate],
            response_recovery_scope=lambda _agent, room, event: gateway.response_recovery_scope(room, event),
            config=config,
            runtime_paths=runtime_paths_for(config),
            delay=0,
        )

    client.room_send.side_effect = relay
    with patch.object(
        cleanup,
        "fetch_thread_messages_from_source",
        return_value=_authoritative_history(_history_message(INITIAL, body="interrupted")),
    ):
        pending_relay = asyncio.create_task(resume())
        await publishing.wait()
        try:
            assert worker._delivery_lock(SOURCE).locked(), "Relay transport released normal delivery ownership"
            pending_final = asyncio.create_task(final())
            await final_queued.wait()
            assert not pending_final.done()
            assert not final_sent.is_set()
            assert await principal.load_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.FINAL) is None
        finally:
            release_relay.set()
            await pending_relay
        assert await pending_final == "$final"
        assert await resume() == 0
    assert len(relays) == 1
    assert visible == {INITIAL: "answer"}
    row = await principal.load_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.FINAL)
    assert row is not None
    assert row.delivery_id == SOURCE
    assert row.edits_event_id == INITIAL
    assert row.acknowledged_event_id == "$final"
    assert store.get_turn_record(SOURCE).completed


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_orphaned_relay_is_discovered_after_restart_before_it_finishes(
    journal_store: EventJournalStore,
    tmp_path: Path,
    lost_ack: bool,
) -> None:
    """A homeserver-accepted orphan relay revokes future startup candidates across restart."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    await store.record_pending_turn(
        TurnRecord.create(
            [SOURCE],
            completed=False,
            response_event_id=INITIAL,
            requester_id=USER_ID,
            response_owner="agent",
            conversation_target=MessageTarget.resolve(ROOM_ID, "$thread", SOURCE),
        ),
    )
    await principal.enqueue_matrix_delivery(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id="$thread",
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    await principal.claim_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.INITIAL)
    await principal.acknowledge_matrix_delivery(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        event_id=INITIAL,
        delivered_projections=(),
    )
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    client, router = _make_client(), _make_client()
    router.user_id = "@actual_router:localhost"
    for actor in (client, router):
        actor.joined_rooms.return_value = nio.JoinedRoomsResponse([ROOM_ID])
    events = [
        _make_message_event(
            event_id=INITIAL,
            body=cleanup.build_restart_interrupted_body("Partial answer"),
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread", SOURCE),
            extra_content={STREAM_STATUS_KEY: "error", ORIGINAL_SENDER_KEY: USER_ID},
        ),
    ]
    events.extend(
        [
            _make_message_event(
                event_id=SOURCE,
                body="original request",
                sender=USER_ID,
                timestamp_ms=NOW_MS - STALE_AGE_MS - 1,
                relates_to=_thread_reply_relation("$thread", "$thread"),
            ),
            _make_message_event(
                event_id="$thread",
                body="thread root",
                sender=USER_ID,
                timestamp_ms=NOW_MS - STALE_AGE_MS - 2,
            ),
        ],
    )
    client.room_messages.side_effect = lambda *_args, **_kwargs: _room_messages_response(*events)
    accepted = []

    async def send(*_args: object, **kwargs: object) -> nio.RoomSendResponse:
        content = kwargs["content"]
        accepted.append(content)
        events.append(
            _make_message_event(
                event_id="$relay",
                body=content["body"],
                timestamp_ms=NOW_MS,
                sender=router.user_id,
                relates_to=content["m.relates_to"],
                extra_content=content,
            ),
        )
        if lost_ack:
            message = "relay accepted before process stopped"
            raise ConnectionError(message)
        return nio.RoomSendResponse("$relay", ROOM_ID)

    router.room_send.side_effect = send
    for restart in range(2):
        _reset_handled_turn_ledger_runtime()
        store = await _store(journal_store)
        gateway = _gateway(tmp_path, principal)
        gateway = replace(
            gateway,
            deps=replace(
                gateway.deps,
                response_recovery=ResponseDeliveryRecovery(
                    principal,
                    lambda store=store: store,
                    gateway.deps.redact_message_event,
                ),
            ),
        )
        with (
            patch.object(cleanup.time, "time", return_value=NOW_MS / 1000),
            patch.object(
                cleanup,
                "fetch_thread_messages_from_source",
                return_value=_authoritative_history(_history_message(INITIAL, body=events[0].body)),
            ),
        ):
            result = await cleanup.recover_stale_streaming_messages(
                {BOT_USER_ID: client},
                resume_client=router,
                response_recovery_scope=lambda _agent, room, event, gateway=gateway: gateway.response_recovery_scope(
                    room,
                    event,
                ),
                config=config,
                runtime_paths=runtime_paths_for(config),
                startup_cutoff_ms=NOW_MS,
                scanned_room_ids=set(),
            )
        assert result.resumed_count == (1 if restart == 0 and not lost_ack else 0)
    assert len(accepted) == 1
    assert not store.get_turn_record(SOURCE).completed


@pytest.mark.parametrize(
    ("adopted", "terminal_write"),
    [
        (False, "none"),
        (True, "none"),
        (True, "after_retirement"),
        (True, "before_detachment"),
        (True, "stop_before_detachment"),
        (True, "stop_during_cleanup"),
    ],
)
async def test_deleted_acknowledged_initial_remains_cleanup_debt(
    journal_store: EventJournalStore,
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    adopted: bool,
    terminal_write: str,
) -> None:
    """Outbox recovery removes deleted INITIAL even when source callback already settled."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    gateway = _gateway(tmp_path, principal)
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            response_recovery=ResponseDeliveryRecovery(
                principal,
                lambda store=store: store,
                gateway.deps.redact_message_event,
            ),
        ),
    )
    dispatcher = _dispatcher(principal, AsyncMock())
    room = nio.MatrixRoom(ROOM_ID, BOT_USER_ID)
    await admit_dispatch_event(dispatcher, room, _message(), EventKind.MESSAGE, EventClass.ACTIONABLE)
    await store.record_pending_turn(
        TurnRecord.create(
            [SOURCE],
            completed=False,
            response_event_id=INITIAL if adopted else None,
            conversation_target=MessageTarget.resolve(ROOM_ID, "$thread", SOURCE),
        ),
    )
    await principal.enqueue_matrix_delivery(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id="$thread",
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    await principal.acknowledge_matrix_delivery(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        event_id=INITIAL,
        delivered_projections=(),
    )
    visible = {INITIAL: "Thinking..."}
    stop_task: asyncio.Task[bool] | None = None

    async def redact(*, event_id: str, **_kwargs: object) -> bool:
        nonlocal stop_task
        visible.pop(event_id, None)
        if terminal_write == "before_detachment":
            await store.record_responded_turn(replace(stale_record, response_event_id=INITIAL))
        elif terminal_write == "stop_before_detachment":
            stopped = await store.record_user_stopped_response(INITIAL, 20)
            assert stopped is not None
            assert stopped.completed
        elif terminal_write == "stop_during_cleanup":
            reconciler = UserStopReconciler(
                UserStopReconcilerDeps(store, cast("ResponseRunner", _SerializingRunner()), gateway),
            )
            stop_task = asyncio.create_task(reconciler.finalize(INITIAL, 20, AsyncMock()))
            await asyncio.sleep(0)
            assert not stop_task.done()
        return True

    gateway.deps.redact_message_event.side_effect = redact
    await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    await principal.settle("$redaction")
    assert not await principal.is_pending(SOURCE)
    stale_record = store.get_turn_record(SOURCE)
    await gateway.recover_deliveries()
    if stop_task is not None:
        assert await stop_task
    gateway.deps.runtime.client.room_send.assert_not_awaited()
    if terminal_write == "after_retirement":
        await store.record_responded_turn(replace(stale_record, response_event_id=INITIAL))
    assert visible == {}
    assert store.is_revision_redacted(SOURCE)
    assert store.get_turn_record(SOURCE).response_event_id is None
    assert store.get_turn_record(SOURCE).completed is terminal_write.startswith("stop_")
    _reset_handled_turn_ledger_runtime()
    reopened = await _store(journal_database())
    assert reopened.get_turn_record(SOURCE).response_event_id is None
    assert reopened.get_turn_record(SOURCE).completed is terminal_write.startswith("stop_")
    assert reopened.get_turn_record(SOURCE).redacted_source_event_ids == (SOURCE,)
    if terminal_write.startswith("stop_"):
        await _assert_removed_stop_replay(reopened, gateway)


async def _assert_removed_stop_replay(store: TurnStore, gateway: DeliveryGateway) -> None:
    """A reopened STOP settles its callback twice without editing the removed response."""
    record = store.get_turn_record(SOURCE)
    assert record.user_stop_receipt_order == 20
    assert record.user_stop_settled_receipt_order == 20
    recovery = gateway.deps.response_recovery
    assert recovery is not None
    gateway = replace(
        gateway,
        deps=replace(gateway.deps, response_recovery=replace(recovery, turn_store=lambda: store)),
    )
    finalized = AsyncMock()
    reconciler = UserStopReconciler(
        UserStopReconcilerDeps(store, cast("ResponseRunner", _SerializingRunner()), gateway),
    )
    with patch("mindroom.delivery_gateway.edit_message_outcome", new=AsyncMock()) as edit:
        assert await reconciler.finalize(INITIAL, 20, finalized)
        assert await reconciler.finalize(INITIAL, 20, finalized)
    assert finalized.await_count == 2
    edit.assert_not_awaited()
    assert store.get_turn_record(SOURCE) == record


@pytest.mark.parametrize("own_final", [False, True])
async def test_deleted_initial_cannot_demote_another_principals_eventless_turn(
    journal_store: EventJournalStore,
    journal_database: Callable[[], EventJournalStore],
    own_final: bool,
) -> None:
    """A's retired ACK provides no completion authority for B's record of the same source."""
    first = journal_store.principal("agent@alice")
    second = journal_store.principal("other@bob")
    first_store = await _store(journal_store)
    second_store = await _store(journal_store, agent_name="other")
    second_store.deps = replace(second_store.deps, redacted_event_ids=second.redacted_event_ids)
    target = MessageTarget.resolve(ROOM_ID, "$thread", SOURCE)
    room = nio.MatrixRoom(ROOM_ID, BOT_USER_ID)
    for principal in (first, second):
        dispatcher = _dispatcher(principal, AsyncMock())
        await admit_dispatch_event(dispatcher, room, _message(), EventKind.MESSAGE, EventClass.ACTIONABLE)
        await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    await first_store.record_pending_turn(TurnRecord.create([SOURCE], completed=False, conversation_target=target))
    await first.enqueue_matrix_delivery(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id="$thread",
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    await first.acknowledge_matrix_delivery(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        event_id=INITIAL,
        delivered_projections=(),
    )
    await first_store.mark_source_redacted(SOURCE)
    await first.retire_deleted_initial(delivery_id=SOURCE)
    if own_final:
        await second.enqueue_matrix_delivery(
            delivery_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM_ID,
            thread_id="$thread",
            payload={"msgtype": "m.text", "body": "B's own answer"},
        )
    await second_store.record_turn(TurnRecord.create([SOURCE], completed=True, conversation_target=target))
    await second_store.mark_source_redacted(SOURCE)
    record = second_store.get_turn_record(SOURCE)
    assert record.completed
    assert record.response_event_id is None
    assert record.redacted_source_event_ids == (SOURCE,)
    _reset_handled_turn_ledger_runtime()
    reopened = await _store(journal_database(), agent_name="other")
    assert reopened.get_turn_record(SOURCE) == record


@pytest.mark.parametrize(
    "gap",
    ["unattempted", "lost_ack", "unknown_device", "before_adoption", "adopted", "redacted", "failure"],
)
async def test_deleted_initial_crash_gaps_recover_from_existing_rows(  # noqa: C901, PLR0915
    journal_store: EventJournalStore,
    tmp_path: Path,
    gap: str,
) -> None:
    """Restart retains exact visible debt through transport, adoption and cleanup gaps."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    target = MessageTarget.resolve(ROOM_ID, "$thread", SOURCE)
    original = TurnRecord.create([SOURCE], completed=False, conversation_target=target)
    await store.record_pending_turn(original)
    gateway = _gateway(tmp_path, principal)
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            response_recovery=ResponseDeliveryRecovery(
                principal,
                lambda store=store: store,
                gateway.deps.redact_message_event,
            ),
        ),
    )
    dispatcher = _dispatcher(principal, AsyncMock())
    room = nio.MatrixRoom(ROOM_ID, BOT_USER_ID)
    await admit_dispatch_event(dispatcher, room, _message(), EventKind.MESSAGE, EventClass.ACTIONABLE)
    visible = {}
    transactions = []
    accepted, release = asyncio.Event(), asyncio.Event()

    async def transport(delivery: MatrixDelivery) -> str:
        transactions.append(delivery.transaction_id)
        visible.setdefault(INITIAL, "Thinking...")
        accepted.set()
        await release.wait()
        if gap in {"lost_ack", "unknown_device"} and len(transactions) == 1:
            message = "Matrix accepted but ACK was lost"
            raise ConnectionError(message)
        return INITIAL

    worker = gateway._response_delivery(transport, handoff=None)
    if gap == "unattempted":
        await principal.enqueue_matrix_delivery(
            delivery_id=SOURCE,
            stage=DeliveryStage.INITIAL,
            room_id=ROOM_ID,
            thread_id="$thread",
            payload={"msgtype": "m.text", "body": "Thinking..."},
        )
    else:
        pending = asyncio.create_task(
            worker.deliver(
                delivery_id=SOURCE,
                stage=DeliveryStage.INITIAL,
                room_id=ROOM_ID,
                thread_id="$thread",
                payload={"msgtype": "m.text", "body": "Thinking..."},
            ),
        )
        await accepted.wait()
        release.set()
        if gap in {"lost_ack", "unknown_device"}:
            with pytest.raises(ConnectionError, match="ACK was lost"):
                await pending
        else:
            assert await pending == INITIAL
    if gap in {"adopted", "redacted", "failure"}:
        await store.record_pending_turn(replace(original, response_event_id=INITIAL))
    await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    await principal.settle("$redaction")
    failures_enabled = gap == "failure"

    async def redact(*, event_id: str, **_kwargs: object) -> bool:
        if failures_enabled:
            return False
        visible.pop(event_id, None)
        if gap == "redacted" and not restarted:
            message = "crash after Matrix redaction"
            raise asyncio.CancelledError(message)
        return True

    restarted = False
    if gap == "unknown_device":
        gateway = replace(gateway, deps=replace(gateway.deps, sending_device_id=lambda: "NEW-DEVICE"))
        with patch("mindroom.delivery_gateway.find_outbox_delivery_event_id_via_room_messages", return_value=None):
            assert not (await gateway.recover_deliveries()).complete
        unknown = await principal.load_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.INITIAL)
        assert unknown is not None
        assert unknown.acknowledged_event_id is None
        assert not unknown.retired
        assert len(transactions) == 1
    gateway.deps.redact_message_event.side_effect = redact
    if gap == "redacted":
        with pytest.raises(asyncio.CancelledError, match="after Matrix redaction"):
            await gateway.recover_deliveries()
    elif gap == "failure":
        assert not (await gateway.recover_deliveries()).complete
    if gap in {"redacted", "failure"}:
        debt = await principal.deleted_initial_deliveries(agent_name="agent")
        assert len(debt) == 1
        assert debt[0].acknowledged_event_id == INITIAL
        assert not debt[0].retired
        assert store.get_turn_record(SOURCE).response_event_id == INITIAL
    _reset_handled_turn_ledger_runtime()
    store = await _store(journal_store)
    restarted, failures_enabled = True, False
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            response_recovery=ResponseDeliveryRecovery(principal, lambda: store, gateway.deps.redact_message_event),
        ),
    )

    async def resolve(_client: object, _room: str, **_kwargs: object) -> str | None:
        return INITIAL if INITIAL in visible else None

    with patch("mindroom.delivery_gateway.find_outbox_delivery_event_id_via_room_messages", side_effect=resolve):
        assert (await gateway.recover_deliveries()).complete
        assert (await gateway.recover_deliveries()).complete
    assert visible == {}
    assert await principal.deleted_initial_deliveries(agent_name="agent") == ()
    row = await principal.load_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.INITIAL)
    assert row is not None
    assert row.retired
    assert store.is_revision_redacted(SOURCE)
    assert store.get_turn_record(SOURCE).response_event_id is None
    assert not store.get_turn_record(SOURCE).completed
    # Late INITIAL adoption must not restore attribution after cleanup.
    if gap != "unattempted":
        await store.record_pending_turn(replace(original, response_event_id=INITIAL))
        assert store.get_turn_record(SOURCE).response_event_id is None
    assert (
        await gateway._response_delivery(transport, handoff=None).flush(
            delivery_id=SOURCE,
            stage=DeliveryStage.INITIAL,
        )
        is None
    )
    assert visible == {}


@pytest.mark.parametrize("owner", ["pending", "live", "owed_final", "completed_final", "stop", "orphan", "mixed"])
async def test_recovery_respects_existing_source_and_final_owners(  # noqa: C901, PLR0915
    journal_store: EventJournalStore,
    tmp_path: Path,
    owner: str,
) -> None:
    """Transport ownership never replaces pending generation, FINAL or explicit STOP."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    target = MessageTarget.resolve(ROOM_ID, "$thread", SOURCE)
    sources = ("$survivor", SOURCE) if owner == "mixed" else (SOURCE,)
    record = TurnRecord.create(
        sources,
        completed=False,
        response_event_id=INITIAL,
        conversation_target=target,
        requester_id=USER_ID,
        response_owner="agent",
    )
    await store.record_pending_turn(record)
    dispatcher = _dispatcher(principal, AsyncMock())
    room = nio.MatrixRoom(ROOM_ID, BOT_USER_ID)
    for source in sources:
        await admit_dispatch_event(dispatcher, room, _message(source), EventKind.MESSAGE, EventClass.ACTIONABLE)
    gateway = _gateway(
        tmp_path,
        principal,
        terminal_turn_for=store.terminal_turn_record,
        terminal_turn_committed=store.publish_committed_response,
    )
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            response_recovery=ResponseDeliveryRecovery(
                principal,
                lambda store=store: store,
                gateway.deps.redact_message_event,
            ),
        ),
    )
    visible = {}

    async def send(delivery: MatrixDelivery) -> str:
        visible[INITIAL] = delivery.payload["body"]
        return INITIAL if delivery.stage is DeliveryStage.INITIAL else "$final"

    worker = gateway._response_delivery(send, handoff=None)
    await worker.deliver(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id="$thread",
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    if owner not in {"pending", "mixed"}:
        await principal.settle(SOURCE)
    if owner == "live":
        assert store.try_claim_turn(record)
    if owner in {"owed_final", "completed_final"}:
        await principal.enqueue_matrix_delivery(
            delivery_id=SOURCE,
            stage=DeliveryStage.FINAL,
            room_id=ROOM_ID,
            thread_id="$thread",
            edits_event_id=INITIAL,
            payload={"msgtype": "m.text", "body": "answer"},
        )
        if owner == "completed_final":
            await worker.flush(delivery_id=SOURCE, stage=DeliveryStage.FINAL)
    if owner == "stop":
        await store.record_user_stopped_response(INITIAL, 20, delivery_settled=True)
        visible[INITIAL] = "Stopped by user"
        await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
        await store.mark_source_redacted(SOURCE)
    if owner in {"owed_final", "completed_final", "mixed"}:
        await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    try:
        if owner in {"owed_final", "completed_final"}:
            outcome = await gateway._finish_placeholder_delivery_failure(
                _PlaceholderFailureUpdateRequest(
                    target,
                    INITIAL,
                    ResponseIdentity("agent", _envelope(target, source_event_id=SOURCE), SOURCE),
                    "delivery_failed",
                    None,
                    None,
                ),
            )
            assert outcome.terminal_status == "suspended"
            assert visible[INITIAL] == ("answer" if owner == "completed_final" else "Thinking...")
        async with gateway.supersession_scope(SOURCE, ROOM_ID) as allowed:
            assert allowed is (owner in {"owed_final", "completed_final", "stop"})
        async with gateway.response_recovery_scope(ROOM_ID, INITIAL) as allowed:
            assert allowed is (owner == "orphan")
        await gateway.cleanup_deleted_response(SOURCE)
        assert INITIAL in visible
        initial = await principal.load_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.INITIAL)
        assert initial is not None
        assert not initial.retired
        assert store.get_turn_record(SOURCE).response_event_id == INITIAL
        if owner == "mixed":
            assert await principal.is_pending("$survivor")
            await worker.deliver(
                delivery_id=SOURCE,
                stage=DeliveryStage.FINAL,
                room_id=ROOM_ID,
                thread_id="$thread",
                edits_event_id=INITIAL,
                payload={"msgtype": "m.text", "body": "survivor answer"},
            )
            assert visible == {INITIAL: "survivor answer"}
        elif owner == "owed_final":
            await worker.flush(delivery_id=SOURCE, stage=DeliveryStage.FINAL)
            assert visible == {INITIAL: "answer"}
        elif owner == "stop":
            assert visible == {INITIAL: "Stopped by user"}
    finally:
        store.release_pending_turn_claim(record)


@pytest.mark.parametrize("callback_first", [False, True])
@pytest.mark.parametrize("shutdown", [False, True])
async def test_source_redaction_at_second_preparation_gate_suppresses_visible_initial(
    journal_store: EventJournalStore,
    tmp_path: Path,
    callback_first: bool,
    shutdown: bool,
) -> None:
    """Refreshed stale source history cannot turn terminal deletion into a setup error."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    bot = _bot(tmp_path)
    gateway = _gateway(tmp_path, principal)
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            response_recovery=ResponseDeliveryRecovery(
                principal,
                lambda store=store: store,
                gateway.deps.redact_message_event,
            ),
        ),
    )
    runner = ResponseRunner(replace(unwrap_extracted_collaborator(bot._response_runner).deps, delivery_gateway=gateway))
    target = MessageTarget.resolve(ROOM_ID, "$thread", SOURCE)
    await store.record_pending_turn(
        TurnRecord.create([SOURCE], completed=False, response_event_id=INITIAL, conversation_target=target),
    )
    await principal.enqueue_matrix_delivery(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM_ID,
        thread_id="$thread",
        payload={"msgtype": "m.text", "body": "Thinking..."},
    )
    await principal.acknowledge_matrix_delivery(
        delivery_id=SOURCE,
        stage=DeliveryStage.INITIAL,
        event_id=INITIAL,
        delivered_projections=(),
    )
    dispatcher = _dispatcher(principal, AsyncMock())
    dispatcher.callbacks = replace(
        dispatcher.callbacks,
        on_redaction=_response_recovery_bot(journal_store, store)._on_redaction,
    )
    room = nio.MatrixRoom(ROOM_ID, BOT_USER_ID)
    await admit_dispatch_event(dispatcher, room, _message(), EventKind.MESSAGE, EventClass.ACTIONABLE)
    history_ready, release = asyncio.Event(), asyncio.Event()

    async def history(*_args: object, **_kwargs: object) -> ThreadHistoryResult:
        snapshot = ThreadHistoryResult(
            [make_visible_message(event_id=SOURCE, body="deleted prompt")],
            is_full_history=True,
        )
        history_ready.set()
        await release.wait()
        return snapshot

    async def prepare(history: object) -> bool:
        return await store.prepare_pending_response_source(
            target=target,
            source_event_ids=(SOURCE,),
            terminal_source_event_ids=(SOURCE,),
            thread_history=history,
        )

    runner.deps.resolver.fetch_thread_history = AsyncMock(side_effect=history)
    visible = {INITIAL: "Thinking..."}

    async def redact(*, event_id: str, **_kwargs: object) -> bool:
        visible.pop(event_id, None)
        return True

    gateway.deps.redact_message_event.side_effect = redact
    request = ResponseRequest(
        thread_history=[],
        prompt="deleted prompt",
        user_id=USER_ID,
        response_envelope=_envelope(target, source_event_id=SOURCE),
        existing_event_id=INITIAL,
        existing_event_is_placeholder=True,
        prepare_source_turn=prepare,
    )
    task = asyncio.create_task(
        runner._begin_locked_turn(
            request,
            resolved_target=target,
            history_scope=runner.deps.state_writer.history_scope(),
            execution_identity=runner.deps.tool_runtime.build_execution_identity(target=target, user_id=USER_ID),
        ),
    )
    await history_ready.wait()
    await admit_dispatch_event(dispatcher, room, _redaction(), EventKind.REDACTION, EventClass.ACTIONABLE)
    if callback_first:
        await dispatcher.drain_once()
    if shutdown:
        request_task_cancel(task, process_shutdown=True)
    release.set()
    if shutdown:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await gateway.recover_deliveries()).complete
    else:
        assert await task is None
    assert visible == {}
    assert store.get_turn_record(SOURCE).response_event_id is None


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
            response_recovery=ResponseDeliveryRecovery(
                principal,
                lambda store=store: store,
                gateway.deps.redact_message_event,
            ),
        ),
    )
    runner = ResponseRunner(replace(unwrap_extracted_collaborator(bot._response_runner).deps, delivery_gateway=gateway))
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
            on_failure=lambda: dispatcher.retry_turn_sources((SOURCE,)),
            source_event_ids=(SOURCE,),
        )
        tasks.append(task)
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
            assert store.get_turn_record(SOURCE).response_event_id == INITIAL
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
            assert store.get_turn_record(SOURCE).response_event_id == INITIAL
            assert model_requests == []
            await dispatcher.drain_once()
            await tasks[-1]
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
                record = store.get_turn_record(SOURCE)
                assert record.response_event_id is None
                assert not record.completed
                assert record.redacted_source_event_ids == (SOURCE,)
                initial = await principal.load_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.INITIAL)
                final = await principal.load_matrix_delivery(delivery_id=SOURCE, stage=DeliveryStage.FINAL)
                assert initial is not None
                assert initial.retired
                assert final is None
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
