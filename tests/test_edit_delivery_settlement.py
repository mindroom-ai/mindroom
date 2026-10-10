"""A regeneration's edited text and answer survive a process boundary after its answer was delivered."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.cancellation import request_task_cancel
from mindroom.conversation_resolver import MessageContext
from mindroom.event_journal import DeliveryStage, EventClass, EventKind
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event
from mindroom.message_target import MessageTarget
from mindroom.reply_lifecycle import SpanSources
from tests.conftest import (
    journal_edit_regenerator_deps,
    patch_response_runner_module,
    unwrap_extracted_collaborator,
)
from tests.reply_span_helpers import seed_finished_reply
from tests.response_runner_helpers import _bot, _noop_typing
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("streaming", [False, True])
async def test_delivered_edit_survives_shutdown_during_post_response(  # noqa: PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    streaming: bool,
) -> None:
    """An edit's delivered answer and edited text stay recorded when post-response work never returns."""
    bot = _bot(tmp_path)
    room_id, source_id, edit_id, answer_id = "!room:localhost", "$source", "$edit", "$answer"
    bot.client.rooms[room_id] = nio.MatrixRoom(room_id, bot.matrix_id.full_id)
    bot.client.room_send.return_value = nio.RoomSendResponse("$answer-edit", room_id)

    async def read_delivered(_room_id: str, event_id: str) -> nio.RoomGetEventResponse:
        response = nio.RoomGetEventResponse()
        response.event = nio.RoomMessageText.from_dict(
            {
                "type": "m.room.message",
                "event_id": event_id,
                "sender": bot.matrix_id.full_id,
                "origin_server_ts": 30,
                "content": {"msgtype": "m.text", "body": "edited answer"},
            },
        )
        return response

    bot.client.room_get_event.side_effect = read_delivered
    target = MessageTarget.resolve(room_id, None, source_id, room_mode=True)
    store = await _store(journal_store, agent_name="general")
    store.deps = replace(store.deps, state_writer=bot._conversation_state_writer, resolver=bot._conversation_resolver)
    record = TurnRecord.create(
        [source_id],
        response_event_id=answer_id,
        completed=True,
        source_event_prompts={source_id: "original"},
        requester_id="@user:localhost",
        response_owner="general",
        conversation_target=target,
        history_scope=HistoryScope(kind="agent", scope_id="general"),
    )
    await store.record_responded_turn(record)
    bot._turn_store = store
    principal = journal_store.principal("general@@mindroom_general:localhost")
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
    await principal.admit(
        _inbound_event(room_id, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
        _projected_event(room_id, event, EventKind.MESSAGE, self_sender=bot.matrix_id.full_id),
    )
    gateway = unwrap_extracted_collaborator(bot._delivery_gateway)
    gateway = replace(
        gateway,
        deps=replace(
            gateway.deps,
            outbox=principal,
            terminal_turn_for=store.terminal_turn_record,
            terminal_turn_committed=store.publish_completed_turn,
        ),
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    assert runner.deps.replies is not None
    # Replies are recorded where their rows are delivered.
    runner.deps = replace(
        runner.deps,
        delivery_gateway=gateway,
        approval_store=principal,
        replies=replace(runner.deps.replies, store=principal, complete_turn=store.publish_completed_turn),
    )
    regenerator = unwrap_extracted_collaborator(bot._edit_regenerator)
    regenerator.deps = replace(
        regenerator.deps,
        turn_store=store,
        receipt_order=AsyncMock(return_value=1),
        **journal_edit_regenerator_deps(bot, principal),
    )
    await seed_finished_reply(
        principal,
        answer_id,
        sources=SpanSources(pending=(), logical=(source_id,)),
        room_id=room_id,
        thread_id=None,
        entity_name="general",
    )
    effects_started = asyncio.Event()
    never_finish = asyncio.Event()
    generated = []
    outcomes = []

    async def model(*_args: object, **_kwargs: object) -> str:
        generated.append("answer")
        recorder = _kwargs["turn_recorder"]
        assert isinstance(recorder, TurnRecorder)
        recorder.mark_completed()
        return "edited answer"

    async def stream_model(*args: object, **kwargs: object) -> AsyncIterator[str]:
        yield await model(*args, **kwargs)

    async def post_response(*_args: object, **_kwargs: object) -> None:
        outcomes.append(_args[0])
        effects_started.set()
        await never_finish.wait()

    context = MessageContext(False, False, None, [], [], False)
    resolver = unwrap_extracted_collaborator(bot._conversation_resolver)
    with (
        patch.object(resolver, "extract_message_context", AsyncMock(return_value=context)),
        patch_response_runner_module(
            typing_indicator=_noop_typing,
            should_use_streaming=AsyncMock(return_value=streaming),
            ai_response=model,
            stream_agent_response=stream_model,
            apply_post_response_effects=post_response,
        ),
    ):
        assert await regenerator.handle_message_edit(
            nio.MatrixRoom(room_id, bot.matrix_id.full_id),
            event,
            EventInfo.from_event(event.source),
            "@user:localhost",
        )
        (task,) = runner._inbox_response_tasks
        try:
            async with asyncio.timeout(10):
                await effects_started.wait()
            delivery = await principal.load_matrix_delivery(delivery_id=edit_id, stage=DeliveryStage.FINAL)
            assert delivery is not None, outcomes
            assert delivery.acknowledged_event_id is not None, outcomes
            assert delivery.edits_event_id == answer_id
            assert "selected edit" not in json.dumps(dict(delivery.payload))
            request_task_cancel(task, process_shutdown=True)
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                never_finish.set()
                request_task_cancel(task, process_shutdown=True)
                await asyncio.gather(task, return_exceptions=True)
    _reset_handled_turn_ledger_runtime()
    reopened = await _store(journal_store, agent_name="general")
    persisted = reopened.get_turn_record(source_id)
    assert persisted is not None
    assert persisted.completed
    assert persisted.source_event_revisions == {source_id: (20, edit_id)}
    assert persisted.source_event_prompts == {source_id: "selected edit"}
    assert persisted.response_event_id == answer_id
    assert persisted.source_event_ids == (source_id,)
    assert generated == ["answer"]
