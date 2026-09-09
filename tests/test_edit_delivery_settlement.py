"""Consumed edit proof follows durable final delivery across process boundaries."""

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
from mindroom.delivery_gateway import FinalDeliveryRequest
from mindroom.event_journal import DeliveryStage, EventClass, EventKind
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event
from mindroom.message_target import MessageTarget
from mindroom.turn_record import canonicalize_turn_record
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.journal_membership_helpers import admit_room_membership
from tests.response_runner_helpers import _bot, _noop_typing
from tests.test_response_delivery_gateway import TestTurnDeliveryGoesThroughTheOutbox as _DeliveryTests
from tests.test_response_delivery_gateway import _gateway, _identity
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
    from typing import Any

    from mindroom.event_journal import EventJournalStore
    from mindroom.event_journal.store import TurnRecordStore


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("completed", [False, True])
async def test_delivered_edit_survives_shutdown_during_post_response(  # noqa: PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    streaming: bool,
    completed: bool,
) -> None:
    """Successful final edit consumption cannot depend on post-response work returning."""
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
            terminal_turn_committed=store.publish_committed_response,
        ),
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.deps = replace(runner.deps, delivery_gateway=gateway)
    regenerator = unwrap_extracted_collaborator(bot._edit_regenerator)
    regenerator.deps = replace(
        regenerator.deps,
        turn_store=store,
        receipt_order=AsyncMock(return_value=1),
        generate_response=runner.generate_response,
    )
    effects_started = asyncio.Event()
    never_finish = asyncio.Event()
    generated = []
    outcomes = []

    async def model(*_args: object, **_kwargs: object) -> str:
        generated.append("answer")
        recorder = _kwargs["turn_recorder"]
        assert isinstance(recorder, TurnRecorder)
        if completed:
            recorder.mark_completed()
        else:
            recorder.mark_interrupted()
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
        task = asyncio.create_task(
            regenerator.handle_message_edit(
                nio.MatrixRoom(room_id, bot.matrix_id.full_id),
                event,
                EventInfo.from_event(event.source),
                "@user:localhost",
            ),
        )
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
    assert persisted.source_event_revisions == ({source_id: (20, edit_id)} if completed else None)
    assert persisted.revision_replay[edit_id].response_event_id == (answer_id if completed else None)
    assert persisted.source_event_prompts == {source_id: "selected edit" if completed else "original"}
    assert persisted.response_event_id == answer_id
    assert persisted.source_event_ids == (source_id,)
    assert generated == ["answer"]


class _ProcessLost(BaseException):
    """Stop at one durable boundary without running response cleanup."""


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("anchor_event_id", ["$source", "$thread"])
async def test_stale_ledger_write_after_ack_cannot_erase_consumption_before_publication(
    tmp_path: Path,
    journal_store: EventJournalStore,
    anchor_event_id: str,
) -> None:
    """A cached pre-ACK mutation cannot overwrite proof before cache repair runs."""
    store = await _store(journal_store)
    principal = journal_store.principal("agent@alice")
    await store.record_responded_turn(
        TurnRecord.create(
            ["$source"],
            anchor_event_id=anchor_event_id,
            completed=True,
            response_event_id="$answer",
            source_event_prompts={"$source": "original"},
            latest_edit_receipt_order=1,
        ),
    )
    registered = await store.register_edit_revision("$source", (20, "$edit"))
    selected = canonicalize_turn_record(
        registered,
        source_event_prompts={"$source": "selected edit"},
        source_event_revisions={"$source": (20, "$edit")},
    )
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    publication_started = asyncio.Event()
    lose_process = asyncio.Event()
    upsert = type(store.deps.turn_records).upsert

    async def paused_upsert(owner: TurnRecordStore, **kwargs: object) -> str | None:
        write_started.set()
        await release_write.wait()
        return await upsert(owner, **kwargs)

    async def publish(_turn_id: str, _event_id: str, _committed: TurnRecord | None) -> None:
        publication_started.set()
        await lose_process.wait()
        raise _ProcessLost

    gateway = _gateway(
        tmp_path,
        principal,
        terminal_turn_for=store.terminal_turn_record,
        terminal_turn_committed=publish,
    )
    gateway.deps.response_hooks._apply_before_response = _DeliveryTests._hooks()._apply_before_response

    async def send(
        _client: nio.AsyncClient,
        _room: str,
        content: dict[str, Any],
        **_kwargs: object,
    ) -> DeliveredMatrixEvent:
        return DeliveredMatrixEvent("$physical-edit", content)

    with (
        patch.object(type(store.deps.turn_records), "upsert", paused_upsert),
        patch("mindroom.delivery_gateway.send_message_outcome", send),
    ):
        stale = asyncio.create_task(store.record_visible_echo("$source", "$echo"))
        await write_started.wait()
        final = asyncio.create_task(
            gateway.deliver_final(
                FinalDeliveryRequest(
                    target=MessageTarget.resolve("!room:localhost", None, "$source", room_mode=True),
                    existing_event_id="$answer",
                    response_text="answer",
                    identity=_identity("$edit"),
                    tool_trace=None,
                    extra_content=None,
                    prepared_edit_record=selected,
                ),
            ),
        )
        try:
            async with asyncio.timeout(5):
                await publication_started.wait()
            release_write.set()
            await stale
            _reset_handled_turn_ledger_runtime()
            restarted = (await _store(journal_store)).get_turn_record("$source")
        finally:
            release_write.set()
            lose_process.set()
            await stale
            with pytest.raises(_ProcessLost):
                await final
    assert restarted is not None
    assert restarted.visible_echo_event_id == "$echo"
    assert restarted.source_event_revisions == {"$source": (20, "$edit")}
    assert restarted.revision_replay["$edit"].response_event_id == "$answer"


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("boundary", ["enqueue", "ack", "failed", "losing", "retired"])
async def test_edit_delivery_process_boundaries(  # noqa: C901, PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    boundary: str,
) -> None:
    """Only the winning active acknowledgement can consume its frozen selected edit."""
    store = await _store(journal_store)
    principal = journal_store.principal("agent@alice")
    target = MessageTarget.resolve("!room:localhost", None, "$source", room_mode=True)
    original = TurnRecord.create(
        ["$source"],
        completed=True,
        response_event_id="$answer",
        source_event_prompts={"$source": "original"},
        latest_edit_receipt_order=1,
    )
    await store.record_responded_turn(original)
    registered = await store.register_edit_revision("$source", (20, "$edit"))
    assert registered is not None
    selected = canonicalize_turn_record(
        registered,
        source_event_prompts={"$source": "selected edit"},
        source_event_revisions={"$source": (20, "$edit")},
    )

    async def publish(turn_id: str, event_id: str, committed: TurnRecord | None) -> None:
        if boundary == "ack":
            raise _ProcessLost
        await store.publish_committed_response(turn_id, event_id, committed)

    gateway = _gateway(
        tmp_path,
        principal,
        terminal_turn_for=store.terminal_turn_record,
        terminal_turn_committed=publish,
    )
    gateway.deps.response_hooks._apply_before_response = _DeliveryTests._hooks()._apply_before_response

    async def send(
        _client: nio.AsyncClient,
        _room: str,
        content: dict[str, Any],
        **_kwargs: object,
    ) -> DeliveredMatrixEvent:
        if boundary == "enqueue":
            raise _ProcessLost
        if boundary == "failed":
            msg = "send failed"
            raise RuntimeError(msg)
        if boundary == "losing":
            await principal.acknowledge_matrix_delivery(
                delivery_id="$edit",
                stage=DeliveryStage.FINAL,
                event_id="$winner",
                delivered_projections=(),
            )
        if boundary == "retired":
            await admit_room_membership(principal, "!room:localhost", "leave")
        return DeliveredMatrixEvent("$physical-edit", content)

    request = FinalDeliveryRequest(
        target=target,
        existing_event_id="$answer",
        response_text="generated answer",
        identity=_identity("$edit"),
        tool_trace=None,
        extra_content=None,
        prepared_edit_record=selected,
    )
    with patch("mindroom.delivery_gateway.send_message_outcome", send):
        if boundary in {"enqueue", "ack"}:
            with pytest.raises(_ProcessLost):
                await gateway.deliver_final(request)
        elif boundary == "failed":
            with pytest.raises(RuntimeError, match="send failed"):
                await gateway.deliver_final(request)
        else:
            await gateway.deliver_final(request)
    _reset_handled_turn_ledger_runtime()
    reopened = await _store(journal_store)
    owner = reopened.get_turn_record("$source")
    assert owner is not None
    delivery = await principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
    assert delivery is not None
    assert "selected edit" not in json.dumps(dict(delivery.payload))
    assert delivery.result["prepared_edit_record"]["source_event_prompts"] == {"$source": "selected edit"}
    if boundary == "ack":
        assert owner.source_event_revisions == {"$source": (20, "$edit")}
        assert owner.revision_replay["$edit"].response_event_id == "$answer"
    else:
        assert owner.source_event_revisions is None
        assert owner.revision_replay["$edit"].response_event_id is None
    if boundary == "enqueue":
        # Newer live registration cannot replace the claimed row's generated snapshot.
        await reopened.register_edit_revision("$source", (30, "$newer"))
        gateway = _gateway(
            tmp_path,
            principal,
            terminal_turn_for=reopened.terminal_turn_record,
            terminal_turn_committed=reopened.publish_committed_response,
        )

        async def recovered_send(
            _client: nio.AsyncClient,
            _room: str,
            content: dict[str, Any],
            **_kwargs: object,
        ) -> DeliveredMatrixEvent:
            return DeliveredMatrixEvent("$recovered-physical-edit", content)

        with patch("mindroom.delivery_gateway.send_message_outcome", recovered_send):
            assert (await gateway.recover_deliveries()).recovered == 1
        _reset_handled_turn_ledger_runtime()
        final = (await _store(journal_store)).get_turn_record("$source")
        assert final.source_event_revisions == {"$source": (20, "$edit")}
        assert final.revision_replay["$edit"].response_event_id == "$answer"
        assert final.revision_replay["$newer"].response_event_id is None
        assert final.revision_watermark("$source") == (30, "$newer")


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
@pytest.mark.parametrize("mutation", ["revision_redaction", "source_redaction", "newer", "stop"])
@pytest.mark.parametrize("timing", ["before_ack", "publication"])
async def test_edit_acknowledgement_preserves_intervening_authority(  # noqa: C901
    tmp_path: Path,
    journal_store: EventJournalStore,
    mutation: str,
    timing: str,
) -> None:
    """Durable acknowledgement and cache publication preserve current revision and STOP owners."""
    store = await _store(journal_store)
    principal = journal_store.principal("agent@alice")
    await store.record_responded_turn(
        TurnRecord.create(
            ["$source"],
            completed=True,
            response_event_id="$answer",
            source_event_prompts={"$source": "original"},
            latest_edit_receipt_order=1,
        ),
    )
    registered = await store.register_edit_revision("$source", (20, "$edit"))
    selected = canonicalize_turn_record(
        registered,
        source_event_prompts={"$source": "selected edit"},
        source_event_revisions={"$source": (20, "$edit")},
    )

    async def mutate() -> None:
        if mutation == "revision_redaction":
            await store.mark_source_redacted("$edit")
        elif mutation == "source_redaction":
            await store.mark_source_redacted("$source")
        elif mutation == "newer":
            await store.register_edit_revision("$source", (30, "$newer"))
        else:
            await store.record_user_stopped_response("$answer", 2)

    async def publish(turn_id: str, event_id: str, committed: TurnRecord | None) -> None:
        if timing == "publication":
            await mutate()
        await store.publish_committed_response(turn_id, event_id, committed)

    gateway = _gateway(
        tmp_path,
        principal,
        terminal_turn_for=store.terminal_turn_record,
        terminal_turn_committed=publish,
    )
    gateway.deps.response_hooks._apply_before_response = _DeliveryTests._hooks()._apply_before_response

    async def send(
        _client: nio.AsyncClient,
        _room: str,
        content: dict[str, Any],
        **_kwargs: object,
    ) -> DeliveredMatrixEvent:
        if timing == "before_ack":
            await mutate()
        return DeliveredMatrixEvent("$physical-edit", content)

    with patch("mindroom.delivery_gateway.send_message_outcome", send):
        await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!room:localhost", None, "$source", room_mode=True),
                existing_event_id="$answer",
                response_text="answer",
                identity=_identity("$edit"),
                tool_trace=None,
                extra_content=None,
                prepared_edit_record=selected,
            ),
        )
    _reset_handled_turn_ledger_runtime()
    owner = (await _store(journal_store)).get_turn_record("$source")
    assert owner is not None
    assert owner.response_event_id == "$answer"
    if mutation == "revision_redaction":
        assert owner.revision_replay["$edit"].redacted
        assert "$source" not in (owner.source_event_prompts or {})
    elif mutation == "source_redaction":
        assert owner.redacted_source_event_ids == ("$source",)
        assert "$source" not in (owner.source_event_prompts or {})
    elif mutation == "newer":
        assert owner.revision_watermark("$source") == (30, "$newer")
        assert owner.revision_replay["$newer"].response_event_id is None
        assert owner.source_event_revisions == {"$source": (20, "$edit")}
    else:
        assert owner.user_stop_receipt_order == 2
    if timing == "publication" or mutation in {"newer", "revision_redaction"}:
        assert owner.revision_replay["$edit"].response_event_id == "$answer"
    else:
        assert owner.revision_replay["$edit"].response_event_id is None
