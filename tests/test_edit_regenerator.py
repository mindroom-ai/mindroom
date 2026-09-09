"""Direct unit suite for the EditRegenerator edited-message replay workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import nio
import pytest
from agno.db.base import SessionType

from mindroom.agent_storage import create_state_storage
from mindroom.coalescing_batch import tagged_coalesced_prompt
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.conversation_resolver import ConversationResolver, ConversationResolverDeps, MessageContext
from mindroom.dispatch_source import EDIT_SOURCE_KIND
from mindroom.edit_regenerator import EditRegenerator, EditRegeneratorDeps
from mindroom.event_journal import (
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    IngestionBatchAdmission,
    IngestionRecordAdmission,
    IngestionRecordDisposition,
)
from mindroom.handled_turns import SourceEventMetadata, TurnRecord, TurnRecordCodec
from mindroom.history.types import HistoryScope
from mindroom.hooks.ingress import HookIngressPolicy
from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_hydration import ConversationHydrator
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event
from mindroom.message_target import MessageTarget
from mindroom.pending_event_worker import PendingEventWorker
from mindroom.response_runner import ResponseRequest
from mindroom.sync_restart_retry import InterruptedTurnRooms
from mindroom.timestamp_formatting import format_timestamp_ms
from mindroom.turn_policy import IngressHookRunner
from mindroom.turn_store import TurnStore, TurnStoreDeps
from tests.conftest import make_relation_lookup, make_visible_message, request_envelope
from tests.identity_helpers import entity_ids
from tests.test_response_delivery_gateway import _gateway

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import JournalEvent, MatrixDelivery
    from mindroom.hooks import MessageEnvelope

AGENT_NAME = "assistant"
ROOM_ID = "!room:example.org"
THREAD_ID = "$thread-root:example.org"
USER_ID = "@user:example.org"
ORIGINAL_EVENT_ID = "$original:example.org"
EDIT_EVENT_ID = "$edit:example.org"
RESPONSE_EVENT_ID = "$response:example.org"
NEW_RESPONSE_EVENT_ID = RESPONSE_EVENT_ID
RUN_METADATA = {"matrix_event_id": ORIGINAL_EVENT_ID}


@dataclass(frozen=True)
class _RuntimeStub:
    """Typed SupportsClientConfig stand-in for direct EditRegenerator tests."""

    client: nio.AsyncClient | None
    config: Config


@dataclass
class _Harness:
    """One fully wired EditRegenerator with mockable collaborators."""

    regenerator: EditRegenerator
    resolver: MagicMock
    turn_store: MagicMock
    ingress_hook_runner: MagicMock
    generate_response: AsyncMock
    wait_for_turn_settled: AsyncMock
    interrupted_turn_rooms: InterruptedTurnRooms
    config: Config
    runtime_paths: RuntimePaths
    room: nio.MatrixRoom
    context: MessageContext


def _message_context(*, thread_id: str | None = THREAD_ID) -> MessageContext:
    return MessageContext(
        am_i_mentioned=True,
        is_thread=thread_id is not None,
        thread_id=thread_id,
        thread_history=(make_visible_message(body="earlier message", thread_id=thread_id),),
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )


def _turn_record(
    *,
    source_event_ids: tuple[str, ...] = (ORIGINAL_EVENT_ID,),
    discovery_event_ids: tuple[str, ...] = (),
    redacted_source_event_ids: tuple[str, ...] = (),
    anchor_event_id: str | None = None,
    response_event_id: str | None = RESPONSE_EVENT_ID,
    source_event_prompts: dict[str, str] | None = None,
    source_event_metadata: dict[str, SourceEventMetadata] | None = None,
    response_owner: str | None = AGENT_NAME,
    requester_id: str | None = USER_ID,
    thread_id: str | None = THREAD_ID,
) -> TurnRecord:
    anchor = anchor_event_id or source_event_ids[-1]
    return TurnRecord(
        anchor_event_id=anchor,
        source_event_ids=source_event_ids,
        discovery_event_ids=discovery_event_ids,
        redacted_source_event_ids=redacted_source_event_ids,
        response_event_id=response_event_id,
        source_event_prompts=source_event_prompts,
        source_event_metadata=source_event_metadata,
        response_owner=response_owner,
        requester_id=requester_id,
        history_scope=HistoryScope(kind="agent", scope_id=AGENT_NAME),
        conversation_target=MessageTarget.resolve(ROOM_ID, thread_id, anchor),
    )


def _source_metadata(*source_event_ids: str) -> dict[str, SourceEventMetadata]:
    return {source_event_id: SourceEventMetadata(sender=USER_ID) for source_event_id in source_event_ids}


def _tagged_prompt(source_event_ids: tuple[str, ...], prompts: dict[str, str]) -> str:
    prompt = tagged_coalesced_prompt(
        source_event_ids,
        prompts,
        _source_metadata(*source_event_ids),
        timestamp_formatter=lambda _timestamp_ms: None,
        member_display_names={},
    )
    assert prompt is not None
    return prompt


def _edit_event(
    *,
    original_event_id: str | None = ORIGINAL_EVENT_ID,
    new_body: str = "what is 3+3?",
    sender: str = USER_ID,
    include_new_content: bool = True,
    event_id: str = EDIT_EVENT_ID,
    server_timestamp: int = 1_000_001,
) -> tuple[nio.RoomMessageText, EventInfo]:
    content: dict[str, object] = {
        "body": f"* {new_body}",
        "msgtype": "m.text",
    }
    if original_event_id is not None:
        content["m.relates_to"] = {"event_id": original_event_id, "rel_type": "m.replace"}
    if include_new_content:
        content["m.new_content"] = {"body": new_body, "msgtype": "m.text"}
    source = {
        "content": content,
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": server_timestamp,
        "type": "m.room.message",
        "room_id": ROOM_ID,
    }
    event = nio.RoomMessageText.from_dict(source)
    event.source = source
    return event, EventInfo.from_event(source)


def _harness(
    tmp_path: Path,
    *,
    turn_record: TurnRecord | None,
    receipt_order: int = 1,
    journal_store: EventJournalStore | None = None,
) -> _Harness:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    config = Config(agents={AGENT_NAME: AgentConfig(display_name="Assistant")})
    entity_ids(config, runtime_paths)

    context = _message_context()
    resolver = MagicMock(spec=ConversationResolver)
    resolver.canonical_source_requester.return_value = USER_ID
    resolver.extract_message_context.return_value = context
    resolver.build_message_envelope = MagicMock(
        return_value=request_envelope(
            room_id=ROOM_ID,
            reply_to_event_id=ORIGINAL_EVENT_ID,
            thread_id=THREAD_ID,
            user_id=USER_ID,
            agent_name=AGENT_NAME,
            source_kind=EDIT_SOURCE_KIND,
        ),
    )

    turn_store = MagicMock(spec=TurnStore)
    current_turn_record = [turn_record]
    turn_store.load_turn.side_effect = lambda **_kwargs: current_turn_record[0]
    turn_store.get_turn_record.side_effect = lambda _event_id: current_turn_record[0]
    turn_store.register_edit_revision.side_effect = lambda event_id, _revision: turn_store.get_turn_record(event_id)
    turn_store.is_revision_redacted.return_value = False

    def record_turn(record: TurnRecord) -> None:
        current_turn_record[0] = record

    turn_store.record_turn.side_effect = record_turn
    turn_store.record_responded_turn.side_effect = record_turn
    turn_store.publish_committed_response.side_effect = lambda _turn_id, _event_id, committed: record_turn(committed)
    turn_store.build_run_metadata.return_value = dict(RUN_METADATA)
    turn_store._prepare_edit_response_source.return_value = False

    async def prepare_edit_snapshot(
        *,
        record: TurnRecord,
        driving_revision_id: str,
        edit_receipt_order: int,
        consumed_revision_ids: tuple[str, ...],
        thread_history: object,
    ) -> bool:
        del driving_revision_id, consumed_revision_ids, thread_history
        return await turn_store._prepare_edit_response_source(
            target=record.conversation_target,
            source_event_ids=record.replay_source_event_ids,
            response_event_id=record.response_event_id,
            edit_receipt_order=edit_receipt_order,
        )

    turn_store.prepare_edit_snapshot.side_effect = prepare_edit_snapshot

    ingress_hook_runner = MagicMock(spec=IngressHookRunner)
    ingress_hook_runner.emit_message_received_hooks.return_value = False

    generate_response = AsyncMock(return_value=NEW_RESPONSE_EVENT_ID)
    response_lock = asyncio.Lock()

    async def run_locked_response(request: object) -> str | None:
        async with response_lock:
            assert isinstance(request, ResponseRequest)
            event_id = await generate_response(request)
            if event_id is not None and not interrupted_turn_rooms.contains(request.correlation_id or ""):
                await _acknowledge_test_edit(
                    tmp_path,
                    request,
                    event_id,
                    regenerator.deps.turn_store,
                    journal_store=journal_store,
                )
            return event_id

    wait_for_turn_settled = AsyncMock()
    interrupted_turn_rooms = InterruptedTurnRooms()
    regenerator = EditRegenerator(
        EditRegeneratorDeps(
            runtime=_RuntimeStub(client=AsyncMock(spec=nio.AsyncClient), config=config),
            runtime_paths=runtime_paths,
            agent_name=AGENT_NAME,
            resolver=resolver,
            turn_store=turn_store,
            ingress_hook_runner=ingress_hook_runner,
            generate_response=run_locked_response,
            wait_for_turn_settled=wait_for_turn_settled,
            receipt_order=AsyncMock(return_value=receipt_order),
            interrupted_turn_rooms=interrupted_turn_rooms,
            timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(timestamp_ms, timezone=config.timezone),
        ),
    )
    return _Harness(
        regenerator=regenerator,
        resolver=resolver,
        turn_store=turn_store,
        ingress_hook_runner=ingress_hook_runner,
        generate_response=generate_response,
        wait_for_turn_settled=wait_for_turn_settled,
        interrupted_turn_rooms=interrupted_turn_rooms,
        config=config,
        runtime_paths=runtime_paths,
        room=nio.MatrixRoom(room_id=ROOM_ID, own_user_id=f"@{AGENT_NAME}:example.org"),
        context=context,
    )


async def _handle_edit(harness: _Harness, event: nio.RoomMessageText, event_info: EventInfo) -> None:
    await harness.regenerator.handle_message_edit(harness.room, event, event_info, USER_ID)


async def _acknowledge_test_edit(
    tmp_path: Path,
    request: ResponseRequest,
    event_id: str,
    store: TurnStore,
    *,
    journal_store: EventJournalStore | None = None,
) -> None:
    """Model successful generation with an actual frozen outbox acknowledgement."""
    assert request.prepared_edit_record is not None
    journal = journal_store or EventJournalStore.open_sqlite(tmp_path / "edit-delivery.sqlite")
    gateway = _gateway(
        tmp_path,
        journal.principal("assistant@test"),
        terminal_turn_committed=store.publish_committed_response,
    )
    gateway = replace(gateway, deps=replace(gateway.deps, agent_name=AGENT_NAME))

    async def send(_delivery: MatrixDelivery) -> str:
        return event_id

    worker = replace(gateway._response_delivery(send, handoff=None), observe_delivered=None)
    try:
        await worker.deliver(
            delivery_id=request.correlation_id,
            stage=DeliveryStage.FINAL,
            room_id=ROOM_ID,
            thread_id=request.thread_id,
            edits_event_id=request.existing_event_id,
            payload={"msgtype": "m.text", "body": "generated answer"},
            result={"prepared_edit_record": TurnRecordCodec._to_ledger_record(request.prepared_edit_record)},
        )
    finally:
        if journal_store is None:
            await journal.close()


def _assert_no_regeneration(harness: _Harness) -> None:
    harness.generate_response.assert_not_awaited()
    harness.turn_store.record_turn.assert_not_called()


@pytest.mark.asyncio
async def test_simple_edit_regenerates_and_records_new_response(tmp_path: Path) -> None:
    """An edited single-message turn regenerates with the edited body and records the new outcome."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.room.add_member(USER_ID, "Banana Man", None)
    event, event_info = _edit_event(new_body="what is 3+3?")

    await _handle_edit(harness, event, event_info)

    harness.generate_response.assert_awaited_once()
    request = harness.generate_response.await_args.args[0]
    assert request.prompt == "what is 3+3?"
    assert request.member_display_names == {USER_ID: "Banana Man"}
    assert request.existing_event_id == RESPONSE_EVENT_ID
    assert request.existing_event_is_placeholder is False
    assert request.user_id == USER_ID
    assert request.correlation_id == EDIT_EVENT_ID
    assert request.matrix_run_metadata == RUN_METADATA
    assert request.current_timestamp_ms == float(event.server_timestamp)
    assert request.thread_history == harness.context.thread_history

    envelope_kwargs = harness.resolver.build_message_envelope.call_args.kwargs
    assert envelope_kwargs["body"] == "what is 3+3?"
    assert envelope_kwargs["source_kind"] == EDIT_SOURCE_KIND
    assert envelope_kwargs["target"] == record.conversation_target
    assert envelope_kwargs["requester_user_id"] == USER_ID

    metadata_kwargs = harness.turn_store.build_run_metadata.call_args.kwargs
    assert metadata_kwargs["additional_discovery_event_ids"] == ()

    harness.turn_store.publish_committed_response.assert_called_once()
    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.response_event_id == NEW_RESPONSE_EVENT_ID
    assert recorded.source_event_ids == (ORIGINAL_EVENT_ID,)
    assert recorded.anchor_event_id == ORIGINAL_EVENT_ID
    assert recorded.response_owner == AGENT_NAME
    assert recorded.history_scope == record.history_scope
    assert recorded.conversation_target == record.conversation_target


@pytest.mark.asyncio
async def test_repeated_locked_preparation_removes_stale_runs_once(tmp_path: Path) -> None:
    """Repeated source gates prune one immutable edit snapshot only once."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    request = harness.generate_response.await_args.args[0]
    prepare = request.prepare_source_turn
    harness.turn_store.remove_stale_runs_for_edit.assert_not_called()
    assert await prepare(request.thread_history) is False
    assert await prepare(request.thread_history) is False
    harness.turn_store.remove_stale_runs_for_edit.assert_called_once()
    removal_kwargs = harness.turn_store.remove_stale_runs_for_edit.call_args.kwargs
    assert removal_kwargs["requester_user_id"] == USER_ID
    assert removal_kwargs["turn_record"] == replace(
        record,
        latest_edit_receipt_order=1,
        source_event_prompts={ORIGINAL_EVENT_ID: "what is 3+3?"},
        source_event_revisions={
            ORIGINAL_EVENT_ID: (event.server_timestamp, event.event_id),
        },
    )


@pytest.mark.asyncio
async def test_newer_same_source_edit_rejects_older_callback_during_generation(tmp_path: Path) -> None:
    """An older callback arriving during newer generation must never run or overwrite it."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.turn_store.try_claim_turn.return_value = True
    generation_started = asyncio.Event()
    release_generation = asyncio.Event()

    async def block_newer_generation(_request: ResponseRequest) -> str:
        generation_started.set()
        await release_generation.wait()
        return NEW_RESPONSE_EVENT_ID

    harness.generate_response.side_effect = block_newer_generation
    newer, newer_info = _edit_event(
        new_body="newest body",
        event_id="$edit-z:example.org",
        server_timestamp=1_000_010,
    )
    older, older_info = _edit_event(
        new_body="older body",
        event_id="$edit-a:example.org",
        server_timestamp=1_000_010,
    )

    newer_task = asyncio.create_task(_handle_edit(harness, newer, newer_info))
    await generation_started.wait()
    harness.turn_store.try_claim_turn.assert_called_once_with(record)
    harness.turn_store.release_pending_turn_claim.assert_not_called()
    await _handle_edit(harness, older, older_info)
    release_generation.set()
    await newer_task

    harness.generate_response.assert_awaited_once()
    assert harness.generate_response.await_args.args[0].prompt == "newest body"
    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.source_event_prompts == {ORIGINAL_EVENT_ID: "newest body"}
    assert recorded.source_event_revisions == {
        ORIGINAL_EVENT_ID: (1_000_010, "$edit-z:example.org"),
    }
    harness.turn_store.release_pending_turn_claim.assert_called_once_with(record)
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_older_same_source_preparation_finishing_last_is_rejected(tmp_path: Path) -> None:
    """A stale callback resolving after the newer revision commits must not regenerate."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    older_started = asyncio.Event()
    release_older = asyncio.Event()
    older, older_info = _edit_event(
        new_body="older body",
        event_id="$edit-old:example.org",
        server_timestamp=1_000_010,
    )
    newer, newer_info = _edit_event(
        new_body="newest body",
        event_id="$edit-new:example.org",
        server_timestamp=1_000_020,
    )

    async def resolve_body(source: dict[str, object], *_args: object, **_kwargs: object) -> tuple[str, None]:
        if source["event_id"] == older.event_id:
            older_started.set()
            await release_older.wait()
        content = source["content"]
        assert isinstance(content, dict)
        new_content = content["m.new_content"]
        assert isinstance(new_content, dict)
        body = new_content["body"]
        assert isinstance(body, str)
        return body, None

    with patch(
        "mindroom.edit_regenerator.extract_visible_edit_body",
        new=AsyncMock(side_effect=resolve_body),
    ):
        older_task = asyncio.create_task(_handle_edit(harness, older, older_info))
        await older_started.wait()
        await _handle_edit(harness, newer, newer_info)
        release_older.set()
        await older_task

    harness.generate_response.assert_awaited_once()
    assert harness.generate_response.await_args.args[0].prompt == "newest body"
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_concurrent_coalesced_sibling_edits_are_both_retained(tmp_path: Path) -> None:
    """Edits to two sources during one generation must converge to one combined durable prompt."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    harness = _harness(
        tmp_path,
        turn_record=_turn_record(
            source_event_ids=(first_event_id, second_event_id),
            source_event_prompts={
                first_event_id: "first base",
                second_event_id: "second base",
            },
            source_event_metadata=_source_metadata(first_event_id, second_event_id),
        ),
    )
    first_generation_started = asyncio.Event()
    release_first_generation = asyncio.Event()
    second_callback_loaded = asyncio.Event()
    generation_count = 0

    async def generate(_request: ResponseRequest) -> str:
        nonlocal generation_count
        generation_count += 1
        if generation_count == 1:
            first_generation_started.set()
            await release_first_generation.wait()
        return NEW_RESPONSE_EVENT_ID

    original_load_turn = harness.turn_store.load_turn.side_effect

    def load_turn(**kwargs: object) -> TurnRecord | None:
        if kwargs["original_event_id"] == second_event_id:
            second_callback_loaded.set()
        return original_load_turn(**kwargs)

    harness.turn_store.load_turn.side_effect = load_turn
    harness.generate_response.side_effect = generate
    first, first_info = _edit_event(
        original_event_id=first_event_id,
        new_body="first edited",
        event_id="$edit-first:example.org",
        server_timestamp=1_000_010,
    )
    second, second_info = _edit_event(
        original_event_id=second_event_id,
        new_body="second edited",
        event_id="$edit-second:example.org",
        server_timestamp=1_000_020,
    )

    first_task = asyncio.create_task(_handle_edit(harness, first, first_info))
    await first_generation_started.wait()
    second_task = asyncio.create_task(_handle_edit(harness, second, second_info))
    await second_callback_loaded.wait()
    release_first_generation.set()
    await asyncio.gather(first_task, second_task)

    assert [call.args[0].prompt for call in harness.generate_response.await_args_list] == [
        _tagged_prompt(
            (first_event_id, second_event_id),
            {first_event_id: "first edited", second_event_id: "second base"},
        ),
        _tagged_prompt(
            (first_event_id, second_event_id),
            {first_event_id: "first edited", second_event_id: "second edited"},
        ),
    ]
    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.source_event_prompts == {
        first_event_id: "first edited",
        second_event_id: "second edited",
    }
    assert recorded.source_event_revisions == {
        first_event_id: (1_000_010, "$edit-first:example.org"),
        second_event_id: (1_000_020, "$edit-second:example.org"),
    }
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_coalesced_sibling_edits_publish_the_latest_receipt_order(tmp_path: Path) -> None:
    """STOP ordering follows included callback admission, not Matrix revision order."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    harness = _harness(
        tmp_path,
        turn_record=_turn_record(
            source_event_ids=(first_event_id, second_event_id),
            source_event_prompts={first_event_id: "first base", second_event_id: "second base"},
            source_event_metadata=_source_metadata(first_event_id, second_event_id),
        ),
    )
    wait_started = asyncio.Event()
    release_wait = asyncio.Event()

    async def wait_for_turn_settled(_source_event_ids: tuple[str, ...]) -> None:
        wait_started.set()
        await release_wait.wait()

    harness.regenerator.deps.receipt_order.side_effect = [5, 4]
    harness.turn_store.try_claim_turn.side_effect = [False, True, True]
    harness.wait_for_turn_settled.side_effect = wait_for_turn_settled
    first, first_info = _edit_event(
        original_event_id=first_event_id,
        new_body="first edited",
        event_id="$edit-first:example.org",
        server_timestamp=1_000_010,
    )
    second, second_info = _edit_event(
        original_event_id=second_event_id,
        new_body="second edited",
        event_id="$edit-second:example.org",
        server_timestamp=1_000_020,
    )

    first_task = asyncio.create_task(_handle_edit(harness, first, first_info))
    await wait_started.wait()
    second_task = asyncio.create_task(_handle_edit(harness, second, second_info))
    await asyncio.sleep(0)
    assert len(next(iter(harness.regenerator._mailboxes.values())).pending) == 2
    release_wait.set()
    await asyncio.gather(first_task, second_task)

    request = harness.generate_response.await_args.args[0]
    assert request.prepare_source_turn is not None
    assert await request.prepare_source_turn(request.thread_history) is False
    harness.turn_store._prepare_edit_response_source.assert_called_once_with(
        target=MessageTarget.resolve(ROOM_ID, THREAD_ID, second_event_id),
        source_event_ids=(first_event_id, second_event_id),
        response_event_id=RESPONSE_EVENT_ID,
        edit_receipt_order=5,
    )


@pytest.mark.asyncio
async def test_suppressed_coalesced_edit_body_is_retained_for_later_sibling(tmp_path: Path) -> None:
    """Hook suppression skips its generation but not its durable body update."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    harness = _harness(
        tmp_path,
        turn_record=_turn_record(
            source_event_ids=(first_event_id, second_event_id),
            source_event_prompts={
                first_event_id: "first base",
                second_event_id: "second base",
            },
            source_event_metadata=_source_metadata(first_event_id, second_event_id),
        ),
    )
    harness.ingress_hook_runner.emit_message_received_hooks.side_effect = [True, False]
    first, first_info = _edit_event(
        original_event_id=first_event_id,
        new_body="first suppressed edit",
        event_id="$edit-first:example.org",
        server_timestamp=1_000_010,
    )
    second, second_info = _edit_event(
        original_event_id=second_event_id,
        new_body="second edit",
        event_id="$edit-second:example.org",
        server_timestamp=1_000_020,
    )

    await _handle_edit(harness, first, first_info)
    harness.generate_response.assert_not_awaited()
    suppressed_record = harness.turn_store.record_turn.call_args.args[0]
    assert suppressed_record.source_event_prompts == {
        first_event_id: "first suppressed edit",
        second_event_id: "second base",
    }

    await _handle_edit(harness, second, second_info)

    assert harness.generate_response.await_args.args[0].prompt == _tagged_prompt(
        (first_event_id, second_event_id),
        {first_event_id: "first suppressed edit", second_event_id: "second edit"},
    )
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_newer_edit_arriving_under_response_lock_is_drained(tmp_path: Path) -> None:
    """A newer edit queued while its older regeneration runs must trigger a final drain."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    first_generation_started = asyncio.Event()
    release_first_generation = asyncio.Event()
    newer_callback_loaded = asyncio.Event()
    generation_count = 0

    async def generate(_request: ResponseRequest) -> str:
        nonlocal generation_count
        generation_count += 1
        if generation_count == 1:
            first_generation_started.set()
            await release_first_generation.wait()
        return NEW_RESPONSE_EVENT_ID

    original_load_turn = harness.turn_store.load_turn.side_effect

    def load_turn(**kwargs: object) -> TurnRecord | None:
        if kwargs["original_event_id"] == ORIGINAL_EVENT_ID and generation_count == 1:
            newer_callback_loaded.set()
        return original_load_turn(**kwargs)

    harness.turn_store.load_turn.side_effect = load_turn
    harness.generate_response.side_effect = generate
    older, older_info = _edit_event(
        new_body="older body",
        event_id="$edit-old:example.org",
        server_timestamp=1_000_010,
    )
    newer, newer_info = _edit_event(
        new_body="newest body",
        event_id="$edit-new:example.org",
        server_timestamp=1_000_020,
    )

    older_task = asyncio.create_task(_handle_edit(harness, older, older_info))
    await first_generation_started.wait()
    newer_task = asyncio.create_task(_handle_edit(harness, newer, newer_info))
    await newer_callback_loaded.wait()
    release_first_generation.set()
    await asyncio.gather(older_task, newer_task)

    assert [call.args[0].prompt for call in harness.generate_response.await_args_list] == [
        "older body",
        "newest body",
    ]
    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.source_event_prompts == {ORIGINAL_EVENT_ID: "newest body"}
    assert recorded.source_event_revisions == {
        ORIGINAL_EVENT_ID: (1_000_020, "$edit-new:example.org"),
    }
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_cancelled_drain_is_retried_by_waiting_newer_edit(tmp_path: Path) -> None:
    """Cancellation must leave pending state for a waiting callback to retry safely."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    first_generation_started = asyncio.Event()
    release_cancellation = asyncio.Event()
    retry_callback_loaded = asyncio.Event()
    generation_count = 0

    async def generate(_request: ResponseRequest) -> str:
        nonlocal generation_count
        generation_count += 1
        if generation_count == 1:
            first_generation_started.set()
            await release_cancellation.wait()
            raise asyncio.CancelledError
        return NEW_RESPONSE_EVENT_ID

    original_load_turn = harness.turn_store.load_turn.side_effect

    def load_turn(**kwargs: object) -> TurnRecord | None:
        if generation_count == 1:
            retry_callback_loaded.set()
        return original_load_turn(**kwargs)

    harness.turn_store.load_turn.side_effect = load_turn
    harness.generate_response.side_effect = generate
    first, first_info = _edit_event(
        new_body="first body",
        event_id="$edit-first:example.org",
        server_timestamp=1_000_010,
    )
    retry, retry_info = _edit_event(
        new_body="retry body",
        event_id="$edit-retry:example.org",
        server_timestamp=1_000_020,
    )

    first_task = asyncio.create_task(_handle_edit(harness, first, first_info))
    await first_generation_started.wait()
    retry_task = asyncio.create_task(_handle_edit(harness, retry, retry_info))
    await retry_callback_loaded.wait()
    release_cancellation.set()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    await retry_task

    assert generation_count == 2
    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.source_event_revisions == {
        ORIGINAL_EVENT_ID: (1_000_020, "$edit-retry:example.org"),
    }
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_failed_generation_cleans_mailbox_and_allows_retry(tmp_path: Path) -> None:
    """A failed drain leaves durable revision state unchanged and a later retry succeeds."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    event, event_info = _edit_event(new_body="retry body")
    harness.generate_response.side_effect = RuntimeError("generation failed")

    with pytest.raises(RuntimeError, match="generation failed"):
        await _handle_edit(harness, event, event_info)

    assert harness.regenerator._mailboxes == {}
    harness.generate_response.reset_mock(side_effect=True)
    harness.generate_response.return_value = NEW_RESPONSE_EVENT_ID

    await _handle_edit(harness, event, event_info)

    assert harness.generate_response.await_args.args[0].prompt == "retry body"
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_persisted_revision_rejects_stale_edit_after_regenerator_restart(tmp_path: Path) -> None:
    """A new regenerator instance must not replay an older revision over durable state."""
    first_harness = _harness(tmp_path, turn_record=_turn_record())
    newer, newer_info = _edit_event(
        new_body="newest body",
        event_id="$edit-new:example.org",
        server_timestamp=1_000_020,
    )
    await _handle_edit(first_harness, newer, newer_info)
    persisted_record = first_harness.turn_store.publish_committed_response.call_args.args[2]

    restarted_harness = _harness(tmp_path, turn_record=persisted_record)
    older, older_info = _edit_event(
        new_body="older body",
        event_id="$edit-old:example.org",
        server_timestamp=1_000_010,
    )
    await _handle_edit(restarted_harness, older, older_info)

    _assert_no_regeneration(restarted_harness)
    assert restarted_harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_edit_waits_when_original_claim_precedes_pending_record(tmp_path: Path) -> None:
    """An edit must survive the gap between the original claim and pending record."""
    completed_record = _turn_record()
    harness = _harness(tmp_path, turn_record=None)
    original_settled = False
    wait_started = asyncio.Event()
    release_wait = asyncio.Event()

    def load_turn(**_kwargs: object) -> TurnRecord | None:
        return completed_record if original_settled else None

    async def wait_for_turn_settled(_source_event_ids: tuple[str, ...]) -> None:
        nonlocal original_settled
        wait_started.set()
        await release_wait.wait()
        original_settled = True

    harness.turn_store.load_turn.side_effect = load_turn
    harness.turn_store.get_turn_record.side_effect = lambda _event_id: completed_record if original_settled else None
    harness.wait_for_turn_settled.side_effect = wait_for_turn_settled
    event, event_info = _edit_event(new_body="edit during pending registration")

    task = asyncio.create_task(_handle_edit(harness, event, event_info))
    await wait_started.wait()
    assert not task.done()
    release_wait.set()
    await task

    harness.wait_for_turn_settled.assert_awaited_once_with((ORIGINAL_EVENT_ID,))
    request = harness.generate_response.await_args.args[0]
    assert request.prompt == "edit during pending registration"
    assert request.existing_event_id == RESPONSE_EVENT_ID
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_edit_waits_for_pending_original_response_then_reloads(tmp_path: Path) -> None:
    """An edit of a pending turn should wait, reload its response ID, and regenerate."""
    pending_record = _turn_record(response_event_id=None)
    completed_record = replace(pending_record, response_event_id=RESPONSE_EVENT_ID)
    harness = _harness(tmp_path, turn_record=pending_record)
    harness.turn_store.try_claim_turn.side_effect = [False, True]
    original_settled = False

    def load_turn(**_kwargs: object) -> TurnRecord:
        return completed_record if original_settled else pending_record

    async def wait_for_turn_settled(_source_event_ids: tuple[str, ...]) -> None:
        nonlocal original_settled
        original_settled = True

    harness.turn_store.load_turn.side_effect = load_turn
    harness.turn_store.get_turn_record.side_effect = lambda _event_id: (
        completed_record if original_settled else pending_record
    )
    harness.wait_for_turn_settled.side_effect = wait_for_turn_settled
    event, event_info = _edit_event(new_body="edit after pending")

    await _handle_edit(harness, event, event_info)

    harness.wait_for_turn_settled.assert_awaited_once_with((ORIGINAL_EVENT_ID,))
    request = harness.generate_response.await_args.args[0]
    assert request.prompt == "edit after pending"
    assert request.existing_event_id == RESPONSE_EVENT_ID
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_edit_waits_for_active_retry_then_uses_retried_response(tmp_path: Path) -> None:
    """An edit queued during normal retry must target the retry's new response."""
    interrupted_record = _turn_record(response_event_id="$interrupted:example.org")
    retried_record = replace(interrupted_record, response_event_id="$retried:example.org")
    harness = _harness(tmp_path, turn_record=interrupted_record)
    harness.turn_store.try_claim_turn.side_effect = [False, True]
    retry_settled = False
    wait_started = asyncio.Event()
    release_retry = asyncio.Event()

    def load_turn(**_kwargs: object) -> TurnRecord:
        return retried_record if retry_settled else interrupted_record

    async def wait_for_turn_settled(_source_event_ids: tuple[str, ...]) -> None:
        nonlocal retry_settled
        wait_started.set()
        await release_retry.wait()
        retry_settled = True

    harness.turn_store.load_turn.side_effect = load_turn
    harness.turn_store.get_turn_record.side_effect = lambda _event_id: (
        retried_record if retry_settled else interrupted_record
    )
    harness.wait_for_turn_settled.side_effect = wait_for_turn_settled
    event, event_info = _edit_event(new_body="edit during normal retry")

    task = asyncio.create_task(_handle_edit(harness, event, event_info))
    await asyncio.wait_for(wait_started.wait(), timeout=1)
    harness.generate_response.assert_not_awaited()
    release_retry.set()
    await task

    request = harness.generate_response.await_args.args[0]
    assert request.prompt == "edit during normal retry"
    assert request.existing_event_id == retried_record.response_event_id
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_edit_reloads_canonical_alias_owner_after_concurrent_claim(
    tmp_path: Path,
    journal_store: EventJournalStore,
) -> None:
    """A stale alias record must not spin after its replacement claim settles."""
    human_event_id = ORIGINAL_EVENT_ID
    old_relay_event_id = "$old-relay:example.org"
    new_relay_event_id = "$new-relay:example.org"
    old_record = _turn_record(
        source_event_ids=(old_relay_event_id,),
        discovery_event_ids=(human_event_id,),
        response_event_id="$old-response:example.org",
        source_event_prompts={old_relay_event_id: "old prompt"},
        source_event_metadata={
            old_relay_event_id: SourceEventMetadata(sender=USER_ID, discovery_event_id=human_event_id),
        },
    )
    new_record = _turn_record(
        source_event_ids=(new_relay_event_id,),
        discovery_event_ids=(human_event_id,),
        response_event_id="$new-response:example.org",
        source_event_prompts={new_relay_event_id: "new prompt"},
        source_event_metadata={
            new_relay_event_id: SourceEventMetadata(sender=USER_ID, discovery_event_id=human_event_id),
        },
    )
    harness = _harness(tmp_path, turn_record=None, journal_store=journal_store)
    state_writer = MagicMock()
    state_writer.supports_run_recovery.return_value = False
    real_store = TurnStore(
        TurnStoreDeps(
            agent_name=AGENT_NAME,
            turn_records=journal_store.turn_records(AGENT_NAME),
            redacted_event_ids=journal_store.principal("agent@alice").redacted_event_ids,
            legacy_responses_file=None,
            state_writer=state_writer,
            resolver=harness.resolver,
            tool_runtime=MagicMock(),
        ),
    )
    await real_store.warm()
    await real_store.record_pending_turn(old_record)
    claim_ready = asyncio.Event()
    wait_started = asyncio.Event()
    concurrent_claim: TurnRecord | None = None

    async def replace_alias_owner_and_claim(**_kwargs: object) -> bool:
        nonlocal concurrent_claim
        await real_store.record_turn(new_record)
        concurrent_claim = real_store.get_turn_record(new_relay_event_id)
        assert concurrent_claim is not None
        assert real_store.try_claim_turn(concurrent_claim) is True
        claim_ready.set()
        return False

    async def wait_for_turn_settled(event_ids: tuple[str, ...]) -> None:
        wait_started.set()
        await real_store.wait_for_turn_settled(event_ids)

    harness.ingress_hook_runner.emit_message_received_hooks.side_effect = replace_alias_owner_and_claim
    harness.regenerator.deps = replace(
        harness.regenerator.deps,
        turn_store=real_store,
        wait_for_turn_settled=wait_for_turn_settled,
    )
    event, event_info = _edit_event(
        original_event_id=human_event_id,
        new_body="latest edit",
    )

    task = asyncio.create_task(_handle_edit(harness, event, event_info))
    await asyncio.wait_for(claim_ready.wait(), timeout=1)
    await asyncio.wait_for(wait_started.wait(), timeout=1)
    assert not task.done()
    assert concurrent_claim is not None
    real_store.release_pending_turn_claim(concurrent_claim)
    await asyncio.wait_for(task, timeout=1)

    request = harness.generate_response.await_args.args[0]
    assert request.prompt == "latest edit"
    assert request.existing_event_id == new_record.response_event_id
    recorded = real_store.get_turn_record(human_event_id)
    assert recorded is not None
    assert recorded.source_event_ids == (new_relay_event_id,)
    assert recorded.source_event_revisions == {
        human_event_id: (event.server_timestamp, event.event_id),
    }
    assert real_store.try_claim_turn(recorded) is True
    real_store.release_pending_turn_claim(recorded)


@pytest.mark.asyncio
async def test_edit_aborts_when_physical_record_loses_discovery_alias(
    tmp_path: Path,
    journal_store: EventJournalStore,
) -> None:
    """A stale physical record with a reassigned alias must terminate without spinning."""
    physical_event_id = ORIGINAL_EVENT_ID
    discovery_event_id = "$human-alias:example.org"
    replacement_relay_event_id = "$replacement-relay:example.org"
    stale_record = _turn_record(
        source_event_ids=(physical_event_id,),
        discovery_event_ids=(discovery_event_id,),
        source_event_prompts={physical_event_id: "stale prompt"},
        source_event_metadata={
            physical_event_id: SourceEventMetadata(sender=USER_ID, discovery_event_id=discovery_event_id),
        },
    )
    replacement_record = _turn_record(
        source_event_ids=(replacement_relay_event_id,),
        discovery_event_ids=(discovery_event_id,),
        source_event_prompts={replacement_relay_event_id: "replacement prompt"},
        source_event_metadata={
            replacement_relay_event_id: SourceEventMetadata(
                sender=USER_ID,
                discovery_event_id=discovery_event_id,
            ),
        },
    )
    harness = _harness(tmp_path, turn_record=None)
    state_writer = MagicMock()
    state_writer.supports_run_recovery.return_value = False
    real_store = TurnStore(
        TurnStoreDeps(
            agent_name=AGENT_NAME,
            turn_records=journal_store.turn_records(AGENT_NAME),
            redacted_event_ids=journal_store.principal("agent@alice").redacted_event_ids,
            legacy_responses_file=None,
            state_writer=state_writer,
            resolver=harness.resolver,
            tool_runtime=MagicMock(),
        ),
    )
    await real_store.warm()
    await real_store.record_pending_turn(stale_record)
    wait_calls = 0

    async def replace_alias_owner(**_kwargs: object) -> bool:
        await real_store.record_turn(replacement_record)
        return False

    async def wait_for_turn_settled(event_ids: tuple[str, ...]) -> None:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls > 1:
            pytest.fail("stale physical claim retried without progress")
        await real_store.wait_for_turn_settled(event_ids)

    harness.ingress_hook_runner.emit_message_received_hooks.side_effect = replace_alias_owner
    harness.regenerator.deps = replace(
        harness.regenerator.deps,
        turn_store=real_store,
        wait_for_turn_settled=wait_for_turn_settled,
    )
    event, event_info = _edit_event(
        original_event_id=physical_event_id,
        new_body="must not apply",
    )

    await asyncio.wait_for(_handle_edit(harness, event, event_info), timeout=1)

    assert wait_calls == 1
    _assert_no_regeneration(harness)
    assert harness.regenerator._mailboxes == {}
    replacement = real_store.get_turn_record(discovery_event_id)
    assert replacement is not None
    assert replacement.source_event_ids == (replacement_relay_event_id,)
    assert real_store.try_claim_turn(replacement) is True
    real_store.release_pending_turn_claim(replacement)


@pytest.mark.asyncio
async def test_pending_original_failure_releases_wait_without_regeneration(tmp_path: Path) -> None:
    """A settled original with no response ID should end the drain without hanging."""
    pending_record = _turn_record(response_event_id=None)
    harness = _harness(tmp_path, turn_record=pending_record)
    harness.turn_store.try_claim_turn.side_effect = [False, True]
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    harness.wait_for_turn_settled.assert_awaited_once_with((ORIGINAL_EVENT_ID,))
    harness.generate_response.assert_not_awaited()
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_original_cleans_mailbox(tmp_path: Path) -> None:
    """Cancelling the edit waiter must not leave an event-loop task or mailbox behind."""
    harness = _harness(tmp_path, turn_record=_turn_record(response_event_id=None))
    harness.turn_store.try_claim_turn.return_value = False
    wait_started = asyncio.Event()
    release_wait = asyncio.Event()

    async def wait_for_turn_settled(_source_event_ids: tuple[str, ...]) -> None:
        wait_started.set()
        await release_wait.wait()

    harness.wait_for_turn_settled.side_effect = wait_for_turn_settled
    event, event_info = _edit_event()
    task = asyncio.create_task(_handle_edit(harness, event, event_info))
    await wait_started.wait()
    task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert harness.regenerator._mailboxes == {}
    finally:
        release_wait.set()


@pytest.mark.asyncio
async def test_coalesced_edit_rebuilds_combined_prompt(tmp_path: Path) -> None:
    """Editing one member of a coalesced batch rebuilds the combined prompt and prompt map."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    record = _turn_record(
        source_event_ids=(first_event_id, second_event_id),
        source_event_prompts={first_event_id: "first message", second_event_id: "second message"},
        source_event_metadata=_source_metadata(first_event_id, second_event_id),
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id=first_event_id, new_body="edited first message")

    await _handle_edit(harness, event, event_info)

    expected_prompt = _tagged_prompt(
        (first_event_id, second_event_id),
        {first_event_id: "edited first message", second_event_id: "second message"},
    )
    assert harness.generate_response.await_args.args[0].prompt == expected_prompt

    metadata_call = harness.turn_store.build_run_metadata.call_args
    handled_turn = metadata_call.args[0]
    assert handled_turn.source_event_ids == (first_event_id, second_event_id)
    assert handled_turn.source_event_prompts == {
        first_event_id: "edited first message",
        second_event_id: "second message",
    }
    assert metadata_call.kwargs["additional_discovery_event_ids"] == ()

    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.response_event_id == NEW_RESPONSE_EVENT_ID
    assert recorded.source_event_prompts == {
        first_event_id: "edited first message",
        second_event_id: "second message",
    }


@pytest.mark.asyncio
async def test_coalesced_sibling_edit_excludes_redacted_source_prompt(tmp_path: Path) -> None:
    """Editing a sibling must rebuild without the tombstoned member's durable text."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    record = _turn_record(
        source_event_ids=(first_event_id, second_event_id),
        redacted_source_event_ids=(first_event_id,),
        source_event_prompts={first_event_id: "REDACTED_SECRET", second_event_id: "second message"},
        source_event_metadata=_source_metadata(first_event_id, second_event_id),
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id=second_event_id, new_body="edited second message")

    await _handle_edit(harness, event, event_info)

    request = harness.generate_response.await_args.args[0]
    assert request.prompt == _tagged_prompt(
        (second_event_id,),
        {second_event_id: "edited second message"},
    )
    assert "REDACTED_SECRET" not in request.prompt
    assert request.prepare_source_turn is not None
    assert await request.prepare_source_turn(request.thread_history) is False
    harness.turn_store._prepare_edit_response_source.assert_called_once_with(
        target=record.conversation_target,
        source_event_ids=(second_event_id,),
        response_event_id=record.response_event_id,
        edit_receipt_order=1,
    )
    handled_turn = harness.turn_store.build_run_metadata.call_args.args[0]
    assert handled_turn.redacted_source_event_ids == (first_event_id,)
    assert handled_turn.source_event_prompts == {second_event_id: "edited second message"}


@pytest.mark.asyncio
async def test_coalesced_edit_rechecks_every_snapshotted_source_under_lock(tmp_path: Path) -> None:
    """A sibling redacted after prompt assembly must suppress the stale coalesced prompt."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    record = _turn_record(
        source_event_ids=(first_event_id, second_event_id),
        source_event_prompts={first_event_id: "first message", second_event_id: "second message"},
        source_event_metadata=_source_metadata(first_event_id, second_event_id),
    )
    harness = _harness(tmp_path, turn_record=record)
    redaction_checks = 0

    async def _prepare_edit_response_source(**_kwargs: object) -> bool:
        nonlocal redaction_checks
        redaction_checks += 1
        if redaction_checks == 1:
            await harness.turn_store.record_turn(
                replace(record, redacted_source_event_ids=(first_event_id,)),
            )
            return True
        return False

    async def generate(request: ResponseRequest) -> str | None:
        assert request.prepare_source_turn is not None
        return None if await request.prepare_source_turn(request.thread_history) else NEW_RESPONSE_EVENT_ID

    harness.turn_store._prepare_edit_response_source.side_effect = _prepare_edit_response_source
    harness.generate_response.side_effect = generate
    event, event_info = _edit_event(original_event_id=second_event_id, new_body="edited second message")

    await _handle_edit(harness, event, event_info)

    assert [call.args[0].prompt for call in harness.generate_response.await_args_list] == [
        _tagged_prompt(
            (first_event_id, second_event_id),
            {first_event_id: "first message", second_event_id: "edited second message"},
        ),
        _tagged_prompt(
            (second_event_id,),
            {second_event_id: "edited second message"},
        ),
    ]
    assert harness.turn_store._prepare_edit_response_source.call_count == 2
    assert harness.turn_store.record_turn.call_args.args[0].redacted_source_event_ids == (first_event_id,)


@pytest.mark.asyncio
async def test_edit_of_redacted_coalesced_source_is_ignored(tmp_path: Path) -> None:
    """A later edit cannot reintroduce a source already tombstoned by redaction."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    record = _turn_record(
        source_event_ids=(first_event_id, second_event_id),
        redacted_source_event_ids=(first_event_id,),
        source_event_prompts={first_event_id: "REDACTED_SECRET", second_event_id: "second message"},
        source_event_metadata=_source_metadata(first_event_id, second_event_id),
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id=first_event_id, new_body="restore secret")

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)


@pytest.mark.asyncio
async def test_edit_request_rechecks_redaction_after_acquiring_response_lock(tmp_path: Path) -> None:
    """A redaction that wins the lifecycle lock race must suppress stale regeneration."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.turn_store._prepare_edit_response_source.return_value = True
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    request = harness.generate_response.await_args.args[0]
    assert request.prepare_source_turn is not None
    assert await request.prepare_source_turn(request.thread_history) is True
    harness.turn_store._prepare_edit_response_source.assert_called_once_with(
        target=record.conversation_target,
        source_event_ids=(ORIGINAL_EVENT_ID,),
        response_event_id=record.response_event_id,
        edit_receipt_order=1,
    )


@pytest.mark.asyncio
async def test_coalesced_edit_preserves_tagged_source_metadata(tmp_path: Path) -> None:
    """Edited coalesced turns should keep the model-facing per-message metadata shape."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    record = _turn_record(
        source_event_ids=(first_event_id, second_event_id),
        source_event_prompts={first_event_id: "first message", second_event_id: "second message"},
        source_event_metadata={
            first_event_id: SourceEventMetadata(sender="@alice:example.org", timestamp_ms=1_774_019_700_000),
            second_event_id: SourceEventMetadata(sender="@bob:example.org", timestamp_ms=1_774_019_760_000),
        },
    )
    harness = _harness(tmp_path, turn_record=record)
    harness.config.timezone = "America/Los_Angeles"
    event, event_info = _edit_event(
        original_event_id=first_event_id,
        new_body="edited ]]> first <message>",
        sender="@alice:example.org",
    )
    harness.resolver.build_message_envelope.return_value = request_envelope(
        room_id=ROOM_ID,
        reply_to_event_id=first_event_id,
        thread_id=THREAD_ID,
        user_id="@alice:example.org",
        agent_name=AGENT_NAME,
        source_kind=EDIT_SOURCE_KIND,
    )

    await harness.regenerator.handle_message_edit(
        harness.room,
        event,
        event_info,
        event.sender,
    )

    assert harness.generate_response.await_args.args[0].prompt == (
        "The user sent the following messages in quick succession. "
        "Treat them as one turn and respond once:\n\n"
        "<messages>\n"
        '<msg event_id="$m1:example.org" from="@alice:example.org" ts="2026-03-20 08:15 PDT">'
        "<![CDATA[edited ]]]]><![CDATA[> first <message>]]></msg>\n"
        '<msg event_id="$m2:example.org" from="@bob:example.org" ts="2026-03-20 08:16 PDT">'
        "<![CDATA[second message]]></msg>\n"
        "</messages>"
    )
    assert harness.generate_response.await_args.args[0].current_prompt_is_structured is True

    handled_turn = harness.turn_store.build_run_metadata.call_args.args[0]
    assert handled_turn.source_event_metadata == record.source_event_metadata
    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.source_event_metadata == record.source_event_metadata


@pytest.mark.parametrize(
    ("original_event_id", "sender", "allowed"),
    [
        ("$alice:example.org", "@alice:example.org", True),
        ("$bob:example.org", "@bob:example.org", True),
        ("$alice:example.org", "@bob:example.org", False),
        ("$bob:example.org", "@alice:example.org", False),
        ("$alice:example.org", "@attacker:example.org", False),
    ],
)
@pytest.mark.asyncio
async def test_multi_sender_coalesced_source_allows_only_its_sender_to_edit(
    tmp_path: Path,
    original_event_id: str,
    sender: str,
    allowed: bool,
) -> None:
    """Each coalesced source remains editable only by its persisted sender."""
    alice_event_id = "$alice:example.org"
    bob_event_id = "$bob:example.org"
    record = _turn_record(
        source_event_ids=(alice_event_id, bob_event_id),
        source_event_prompts={alice_event_id: "alice base", bob_event_id: "bob base"},
        source_event_metadata={
            alice_event_id: SourceEventMetadata(sender="@alice:example.org"),
            bob_event_id: SourceEventMetadata(sender="@bob:example.org"),
        },
        requester_id="@bob:example.org",
    )
    harness = _harness(tmp_path, turn_record=record)
    harness.resolver.build_message_envelope.return_value = request_envelope(
        room_id=ROOM_ID,
        reply_to_event_id=original_event_id,
        thread_id=THREAD_ID,
        user_id=sender,
        agent_name=AGENT_NAME,
        source_kind=EDIT_SOURCE_KIND,
    )
    event, event_info = _edit_event(
        original_event_id=original_event_id,
        sender=sender,
    )

    await harness.regenerator.handle_message_edit(harness.room, event, event_info, sender)

    if not allowed:
        _assert_no_regeneration(harness)
        harness.resolver.build_message_envelope.assert_not_called()
        return
    request = harness.generate_response.await_args.args[0]
    assert request.user_id == sender
    assert "what is 3+3?" in request.prompt
    assert harness.turn_store.publish_committed_response.call_args.args[2].source_event_revisions == {
        original_event_id: (event.server_timestamp, event.event_id),
    }


@pytest.mark.asyncio
async def test_physical_source_edit_outranks_colliding_discovery_alias(tmp_path: Path) -> None:
    """A physical event remains owned and edited directly when a relay aliases the same ID."""
    relay_event_id = "$relay:example.org"
    human_event_id = "$human:example.org"
    record = _turn_record(
        source_event_ids=(relay_event_id, human_event_id),
        source_event_prompts={
            relay_event_id: "relay base",
            human_event_id: "human base",
        },
        source_event_metadata={
            relay_event_id: SourceEventMetadata(
                sender="@bob:example.org",
                discovery_event_id=human_event_id,
            ),
            human_event_id: SourceEventMetadata(sender="@alice:example.org"),
        },
        requester_id="@bob:example.org",
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(
        original_event_id=human_event_id,
        new_body="human edited",
        sender="@alice:example.org",
    )
    harness.resolver.build_message_envelope.return_value = request_envelope(
        room_id=ROOM_ID,
        reply_to_event_id=human_event_id,
        thread_id=THREAD_ID,
        user_id="@alice:example.org",
        agent_name=AGENT_NAME,
        source_kind=EDIT_SOURCE_KIND,
    )

    await harness.regenerator.handle_message_edit(harness.room, event, event_info, event.sender)

    request = harness.generate_response.await_args.args[0]
    assert "human edited" in request.prompt
    assert "human base" not in request.prompt
    assert "relay base" in request.prompt
    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.source_event_prompts == {
        relay_event_id: "relay base",
        human_event_id: "human edited",
    }


@pytest.mark.asyncio
async def test_partial_coalesced_metadata_rejects_anchor_sender_editing_sibling(tmp_path: Path) -> None:
    """Missing exact-source ownership must fail closed for a coalesced turn."""
    alice_event_id = "$alice:example.org"
    bob_event_id = "$bob:example.org"
    record = _turn_record(
        source_event_ids=(alice_event_id, bob_event_id),
        source_event_prompts={alice_event_id: "alice base", bob_event_id: "bob base"},
        source_event_metadata={
            bob_event_id: SourceEventMetadata(sender="@bob:example.org"),
        },
        requester_id="@bob:example.org",
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(
        original_event_id=alice_event_id,
        sender="@bob:example.org",
    )

    await harness.regenerator.handle_message_edit(harness.room, event, event_info, event.sender)

    _assert_no_regeneration(harness)
    harness.resolver.build_message_envelope.assert_not_called()


@pytest.mark.asyncio
async def test_coalesced_routed_alias_edit_updates_owned_relay_prompt(tmp_path: Path) -> None:
    """A human edit routed through a relay must replace that relay's prompt."""
    first_relay = "$relay-one:example.org"
    second_relay = "$relay-two:example.org"
    first_human = "$human-one:example.org"
    second_human = "$human-two:example.org"
    record = _turn_record(
        source_event_ids=(first_relay, second_relay),
        discovery_event_ids=(first_human, second_human),
        source_event_prompts={first_relay: "first base", second_relay: "second base"},
        source_event_metadata={
            first_relay: SourceEventMetadata(sender=USER_ID, discovery_event_id=first_human),
            second_relay: SourceEventMetadata(sender=USER_ID, discovery_event_id=second_human),
        },
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(
        original_event_id=first_human,
        new_body="first edited",
        event_id="$edit-first:example.org",
    )

    await _handle_edit(harness, event, event_info)

    request = harness.generate_response.await_args.args[0]
    assert "first edited" in request.prompt
    assert "first base" not in request.prompt
    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.source_event_prompts == {first_relay: "first edited", second_relay: "second base"}
    assert recorded.source_event_revisions == {first_human: (event.server_timestamp, event.event_id)}


@pytest.mark.asyncio
async def test_coalesced_edit_without_persisted_prompts_is_skipped(tmp_path: Path) -> None:
    """A coalesced turn without a persisted prompt map cannot be rebuilt and is skipped."""
    record = _turn_record(
        source_event_ids=("$m1:example.org", "$m2:example.org"),
        source_event_prompts=None,
        source_event_metadata=_source_metadata("$m1:example.org", "$m2:example.org"),
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id="$m1:example.org")

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)


@pytest.mark.asyncio
async def test_coalesced_edit_with_incomplete_prompt_map_is_skipped(tmp_path: Path) -> None:
    """A prompt map missing one coalesced member aborts regeneration without recording."""
    record = _turn_record(
        source_event_ids=("$m1:example.org", "$m2:example.org"),
        source_event_prompts={"$m1:example.org": "first message"},
        source_event_metadata=_source_metadata("$m1:example.org", "$m2:example.org"),
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id="$m1:example.org")

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)


@pytest.mark.asyncio
async def test_edit_without_original_event_id_returns_early(tmp_path: Path) -> None:
    """An event without an m.replace relation never reaches context extraction or turn lookup."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    event, event_info = _edit_event(original_event_id=None)
    assert event_info.original_event_id is None

    await _handle_edit(harness, event, event_info)

    harness.resolver.extract_message_context.assert_not_awaited()
    harness.turn_store.load_turn.assert_not_called()
    _assert_no_regeneration(harness)


@pytest.mark.asyncio
async def test_edit_without_turn_record_returns_early(tmp_path: Path) -> None:
    """An edit with no durable turn record does nothing else."""
    harness = _harness(tmp_path, turn_record=None)
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)
    harness.resolver.build_message_envelope.assert_not_called()


@pytest.mark.asyncio
async def test_hook_suppression_records_turn_without_regeneration(tmp_path: Path) -> None:
    """Suppressing ingress hooks persists edit facts without regenerating."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.ingress_hook_runner.emit_message_received_hooks.return_value = True
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    hook_kwargs = harness.ingress_hook_runner.emit_message_received_hooks.await_args.kwargs
    assert hook_kwargs["correlation_id"] == EDIT_EVENT_ID
    assert hook_kwargs["policy"] == HookIngressPolicy()

    harness.generate_response.assert_not_awaited()
    harness.turn_store.record_turn.assert_called_once()
    recorded = harness.turn_store.record_turn.call_args.args[0]
    assert recorded.response_event_id == RESPONSE_EVENT_ID
    assert recorded.source_event_ids == (ORIGINAL_EVENT_ID,)
    assert recorded.source_event_prompts == {ORIGINAL_EVENT_ID: "what is 3+3?"}
    assert recorded.source_event_revisions == {
        ORIGINAL_EVENT_ID: (event.server_timestamp, event.event_id),
    }
    assert recorded.suppressed_source_event_revisions == {
        ORIGINAL_EVENT_ID: (event.server_timestamp, event.event_id),
    }


@pytest.mark.asyncio
async def test_hook_suppressed_edit_replay_stays_suppressed(tmp_path: Path) -> None:
    """Matrix replay must not turn a durably suppressed edit into a retry."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    harness.ingress_hook_runner.emit_message_received_hooks.return_value = True
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)
    await _handle_edit(harness, event, event_info)

    harness.ingress_hook_runner.emit_message_received_hooks.assert_awaited_once()
    harness.generate_response.assert_not_awaited()
    recorded = harness.turn_store.record_turn.call_args.args[0]
    assert recorded.suppressed_source_event_revisions == {
        ORIGINAL_EVENT_ID: (event.server_timestamp, event.event_id),
    }


@pytest.mark.asyncio
async def test_concurrent_duplicate_edit_runs_ingress_hook_once(tmp_path: Path) -> None:
    """Mailbox reservation must deduplicate an edit before its async hook runs."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    hook_started = asyncio.Event()
    release_hook = asyncio.Event()

    async def hook(**_kwargs: object) -> bool:
        hook_started.set()
        await release_hook.wait()
        return False

    harness.ingress_hook_runner.emit_message_received_hooks.side_effect = hook
    event, event_info = _edit_event()

    first = asyncio.create_task(_handle_edit(harness, event, event_info))
    await hook_started.wait()
    second = asyncio.create_task(_handle_edit(harness, event, event_info))
    await asyncio.sleep(0)
    harness.ingress_hook_runner.emit_message_received_hooks.assert_awaited_once()

    release_hook.set()
    await asyncio.gather(first, second)

    harness.ingress_hook_runner.emit_message_received_hooks.assert_awaited_once()
    harness.generate_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_response_failure_propagates_without_recording(tmp_path: Path) -> None:
    """A raising generate_response propagates and leaves the turn record untouched."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    harness.generate_response.side_effect = RuntimeError("model unavailable")
    event, event_info = _edit_event()

    with pytest.raises(RuntimeError, match="model unavailable"):
        await _handle_edit(harness, event, event_info)

    harness.turn_store.record_turn.assert_not_called()


@pytest.mark.asyncio
async def test_sync_restart_cancellation_leaves_interrupted_edit_uncommitted(tmp_path: Path) -> None:
    """A replacement interruption must leave its revision for room-scoped recovery."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    attempts = 0

    async def interrupt(request: ResponseRequest) -> str:
        nonlocal attempts
        attempts += 1
        assert request.prepare_source_turn is not None
        assert await request.prepare_source_turn(request.thread_history) is False
        assert request.on_interrupted_response_recoverable is not None
        assert request.on_deferred_outcome_handled is not None
        request.on_interrupted_response_recoverable()
        await request.on_deferred_outcome_handled("$interrupted:example.org")
        raise asyncio.CancelledError

    harness.generate_response.side_effect = interrupt
    event, event_info = _edit_event(new_body="latest after restart")

    with pytest.raises(asyncio.CancelledError):
        await _handle_edit(harness, event, event_info)

    assert attempts == 1
    assert harness.interrupted_turn_rooms.pending_room_ids == {ROOM_ID}
    harness.turn_store.record_turn.assert_not_called()
    assert harness.regenerator._mailboxes == {}
    expected_record = replace(
        record,
        latest_edit_receipt_order=1,
        source_event_prompts={ORIGINAL_EVENT_ID: "latest after restart"},
        source_event_revisions={
            ORIGINAL_EVENT_ID: (event.server_timestamp, event.event_id),
        },
    )
    harness.turn_store.remove_stale_runs_for_edit.assert_called_once_with(
        turn_record=expected_record,
        requester_user_id=USER_ID,
    )


@pytest.mark.asyncio
async def test_user_stop_durably_commits_the_edit_revision_before_generation_returns(tmp_path: Path) -> None:
    """A stopped edit must not become eligible for regeneration after a crash."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)

    async def stop(request: ResponseRequest) -> str:
        assert request.on_user_stop_handled is not None
        await request.on_user_stop_handled(NEW_RESPONSE_EVENT_ID, 2)
        return NEW_RESPONSE_EVENT_ID

    harness.generate_response.side_effect = stop
    event, event_info = _edit_event(new_body="stop this revision")

    await _handle_edit(harness, event, event_info)

    harness.turn_store.record_turn.assert_awaited_once()
    stopped_record = harness.turn_store.record_turn.call_args.args[0]
    assert stopped_record.response_event_id == NEW_RESPONSE_EVENT_ID
    assert stopped_record.source_event_revisions == {
        ORIGINAL_EVENT_ID: (event.server_timestamp, event.event_id),
    }
    assert stopped_record.user_stop_receipt_order == 2
    assert stopped_record.user_stop_settled_receipt_order == 2


@pytest.mark.asyncio
async def test_durable_user_stop_suppresses_preceding_edit_recovery(tmp_path: Path) -> None:
    """An edit accepted before STOP must not regenerate after the process restarts."""
    record = replace(
        _turn_record(),
        user_stop_receipt_order=2,
        user_stop_settled_receipt_order=2,
    )
    harness = _harness(tmp_path, turn_record=record, receipt_order=1)
    event, event_info = _edit_event(
        new_body="edit before stop",
        event_id="$z-edit-before-stop:example.org",
        server_timestamp=1_000_020,
    )

    await _handle_edit(harness, event, event_info)

    harness.generate_response.assert_not_awaited()
    recorded = harness.turn_store.record_turn.call_args.args[0]
    assert recorded.source_event_revisions == {
        ORIGINAL_EVENT_ID: (event.server_timestamp, event.event_id),
    }
    assert recorded.user_stop_settled_receipt_order == 2


@pytest.mark.asyncio
async def test_edit_after_durable_user_stop_can_regenerate(tmp_path: Path) -> None:
    """STOP is a cutoff, not a permanent ban on later user edits."""
    record = replace(
        _turn_record(),
        user_stop_receipt_order=2,
        user_stop_settled_receipt_order=2,
    )
    harness = _harness(tmp_path, turn_record=record, receipt_order=3)
    event, event_info = _edit_event(
        new_body="edit after stop",
        event_id="$a-edit-after-stop:example.org",
        server_timestamp=1_000_020,
    )

    await _handle_edit(harness, event, event_info)

    harness.generate_response.assert_awaited_once()
    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.user_stop_settled_receipt_order == 2


@pytest.mark.asyncio
async def test_restart_replays_durably_committed_interrupted_edit(tmp_path: Path) -> None:
    """Matrix replay must recheck a committed edit whose generation was interrupted."""
    event, event_info = _edit_event(new_body="latest after process restart")
    revision = (event.server_timestamp, event.event_id)
    record = replace(
        _turn_record(),
        source_event_prompts={ORIGINAL_EVENT_ID: "latest after process restart"},
        source_event_revisions={ORIGINAL_EVENT_ID: revision},
    )
    harness = _harness(tmp_path, turn_record=record)

    await _handle_edit(harness, event, event_info)

    harness.generate_response.assert_awaited_once()
    harness.ingress_hook_runner.emit_message_received_hooks.assert_not_awaited()
    request = harness.generate_response.await_args.args[0]
    assert request.prompt == "latest after process restart"
    assert request.sync_restart_retry_source_event_id == ORIGINAL_EVENT_ID
    recorded = harness.turn_store.publish_committed_response.call_args.args[2]
    assert recorded.source_event_revisions == {ORIGINAL_EVENT_ID: revision}


@pytest.mark.asyncio
async def test_swallowed_sync_restart_leaves_edit_uncommitted(tmp_path: Path) -> None:
    """A runner-returned interruption marker must not consume the queued edit."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    attempts = 0

    async def interrupt(request: ResponseRequest) -> str:
        nonlocal attempts
        attempts += 1
        assert request.on_interrupted_response_recoverable is not None
        request.on_interrupted_response_recoverable()
        return "$interrupted:example.org"

    harness.generate_response.side_effect = interrupt
    event, event_info = _edit_event(new_body="latest after restart")

    await _handle_edit(harness, event, event_info)

    assert attempts == 1
    assert harness.interrupted_turn_rooms.pending_room_ids == {ROOM_ID}
    harness.turn_store.record_turn.assert_not_called()
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_sync_restart_leaves_every_waiting_coalesced_source_uncommitted(tmp_path: Path) -> None:
    """Restart cancellation must leave every source in the mailbox for recovery."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    harness = _harness(
        tmp_path,
        turn_record=_turn_record(
            source_event_ids=(first_event_id, second_event_id),
            source_event_prompts={
                first_event_id: "first base",
                second_event_id: "second base",
            },
            source_event_metadata=_source_metadata(first_event_id, second_event_id),
        ),
    )
    generation_started = asyncio.Event()
    cancel_generation = asyncio.Event()
    sibling_hook_finished = asyncio.Event()
    attempts = 0
    hook_calls = 0

    async def hook(*, envelope: MessageEnvelope, **_kwargs: object) -> bool:
        nonlocal hook_calls
        assert envelope.requester_id == USER_ID
        hook_calls += 1
        if hook_calls == 2:
            sibling_hook_finished.set()
        return False

    async def interrupt(request: ResponseRequest) -> str:
        nonlocal attempts
        attempts += 1
        generation_started.set()
        await cancel_generation.wait()
        assert request.on_interrupted_response_recoverable is not None
        assert request.on_deferred_outcome_handled is not None
        request.on_interrupted_response_recoverable()
        await request.on_deferred_outcome_handled("$interrupted:example.org")
        raise asyncio.CancelledError

    harness.ingress_hook_runner.emit_message_received_hooks.side_effect = hook
    harness.generate_response.side_effect = interrupt
    first, first_info = _edit_event(
        original_event_id=first_event_id,
        new_body="first edited",
        event_id="$edit-first:example.org",
        server_timestamp=1_000_010,
    )
    second, second_info = _edit_event(
        original_event_id=second_event_id,
        new_body="second edited",
        event_id="$edit-second:example.org",
        server_timestamp=1_000_020,
    )

    first_task = asyncio.create_task(_handle_edit(harness, first, first_info))
    await asyncio.wait_for(generation_started.wait(), timeout=1)
    second_task = asyncio.create_task(_handle_edit(harness, second, second_info))
    await asyncio.wait_for(sibling_hook_finished.wait(), timeout=1)
    second_task.cancel()
    cancel_generation.set()
    with pytest.raises(asyncio.CancelledError):
        await second_task
    with pytest.raises(asyncio.CancelledError):
        await first_task

    assert attempts == 1
    assert hook_calls == 2
    assert harness.interrupted_turn_rooms.pending_room_ids == {ROOM_ID}
    # Neither the driving edit nor its waiting sibling may commit, so replacement
    # recovery re-drives the whole coalesced turn.
    harness.turn_store.record_turn.assert_not_called()
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_edit_from_non_owning_requester_is_ignored(tmp_path: Path) -> None:
    """A requester cannot regenerate another requester's durable response."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    attacker_id = "@attacker:example.org"
    event, event_info = _edit_event(sender=attacker_id)

    await harness.regenerator.handle_message_edit(harness.room, event, event_info, attacker_id)

    _assert_no_regeneration(harness)
    harness.resolver.build_message_envelope.assert_not_called()


@pytest.mark.asyncio
async def test_suppressed_regeneration_needs_no_caller_owned_backfill(tmp_path: Path) -> None:
    """TurnStore repairs during load, so suppression needs no regenerator backfill branch."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.generate_response.return_value = None
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    harness.generate_response.assert_awaited_once()
    harness.turn_store.record_turn.assert_not_called()


@pytest.mark.asyncio
async def test_edit_owned_by_other_entity_is_ignored(tmp_path: Path) -> None:
    """A turn owned by another entity is left alone entirely."""
    record = _turn_record(response_owner="other_agent")
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)
    harness.resolver.build_message_envelope.assert_not_called()


@pytest.mark.asyncio
async def test_edit_without_previous_response_event_is_skipped(tmp_path: Path) -> None:
    """A turn record with no previous response event cannot anchor a regeneration."""
    record = _turn_record(response_event_id=None)
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)


@pytest.mark.asyncio
async def test_edit_from_managed_agent_is_ignored(tmp_path: Path) -> None:
    """Edits sent by a managed entity never reach turn lookup."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    agent_user_id = entity_ids(harness.config, harness.runtime_paths)[AGENT_NAME].full_id
    event, event_info = _edit_event(sender=agent_user_id)

    await _handle_edit(harness, event, event_info)

    harness.resolver.extract_message_context.assert_not_awaited()
    harness.turn_store.load_turn.assert_not_called()
    _assert_no_regeneration(harness)


@pytest.mark.asyncio
async def test_edit_context_realigned_to_recorded_thread_root(tmp_path: Path) -> None:
    """An edit resolved outside the recorded thread refetches history for the recorded root."""
    record = _turn_record(thread_id=THREAD_ID)
    harness = _harness(tmp_path, turn_record=record)
    harness.resolver.extract_message_context.return_value = _message_context(thread_id=None)
    refetched_history = [make_visible_message(body="recorded thread message", thread_id=THREAD_ID)]
    harness.resolver.fetch_thread_history.return_value = refetched_history
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    harness.resolver.fetch_thread_history.assert_awaited_once_with(
        ROOM_ID,
        THREAD_ID,
    )
    assert harness.generate_response.await_args.args[0].thread_history == refetched_history


@pytest.mark.asyncio
async def test_non_coalesced_anchor_mismatch_adds_run_discovery_alias(tmp_path: Path) -> None:
    """A non-coalesced turn anchored to another event keeps the edited event discoverable."""
    anchor_event_id = "$question:example.org"
    record = _turn_record(source_event_ids=(anchor_event_id,), anchor_event_id=anchor_event_id)
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id=ORIGINAL_EVENT_ID)

    await _handle_edit(harness, event, event_info)

    metadata_kwargs = harness.turn_store.build_run_metadata.call_args.kwargs
    assert metadata_kwargs["additional_discovery_event_ids"] == (ORIGINAL_EVENT_ID,)


@pytest.mark.asyncio
async def test_edit_without_resolved_body_is_skipped(tmp_path: Path) -> None:
    """An edit whose m.new_content has no resolvable body aborts before regeneration."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    event, event_info = _edit_event(include_new_content=False)

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)


@pytest.mark.asyncio
async def test_record_without_persisted_response_context_is_skipped(tmp_path: Path) -> None:
    """A turn record missing persisted response context cannot be regenerated."""
    record = TurnRecord(
        anchor_event_id=ORIGINAL_EVENT_ID,
        source_event_ids=(ORIGINAL_EVENT_ID,),
        response_event_id=RESPONSE_EVENT_ID,
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("deletion", ["canonical", "absent", "other_principal", "other_room", "edit_only"])
async def test_projection_deletion_unblocks_edit_before_redaction_callback(  # noqa: PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    deletion: str,
) -> None:
    """Projection tombstones unblock FIFO refill only for this exact canonical source."""
    first, second = "$first", "$second"
    record = replace(
        _turn_record(
            source_event_ids=(first, second),
            source_event_prompts={first: "DELETED_EDIT", second: "second"},
            source_event_metadata=_source_metadata(first, second),
        ),
        source_event_revisions={first: (10, "$deleted-edit")},
    )
    harness = _harness(tmp_path, turn_record=record)
    principal = journal_store.principal("@assistant:example.org")
    runtime = harness.regenerator.deps.runtime
    reader = ConversationReader(principal, ConversationHydrator(principal, runtime, "@assistant:example.org"))
    resolver = ConversationResolver(
        ConversationResolverDeps(
            runtime=runtime,
            runtime_paths=harness.runtime_paths,
            logger=get_logger(__name__),
            agent_name=AGENT_NAME,
            matrix_id="@assistant:example.org",
            relations=make_relation_lookup(),
            conversation_reader=reader,
        ),
    )
    # Context extraction is independent of the strict exact-source read being exercised.
    harness.resolver.resolve_exact_source.side_effect = resolver.resolve_exact_source
    writer = MagicMock()
    writer.supports_run_recovery.return_value = False
    writer.history_scope.return_value = record.history_scope
    writer.session_type_for_scope.return_value = SessionType.AGENT
    writer.create_storage.side_effect = lambda *_args, **_kwargs: create_state_storage(
        AGENT_NAME,
        tmp_path,
        subdir="sessions",
        session_table="edit_sessions",
    )
    store = TurnStore(
        TurnStoreDeps(
            agent_name=AGENT_NAME,
            turn_records=journal_store.turn_records(AGENT_NAME),
            redacted_event_ids=principal.redacted_event_ids,
            legacy_responses_file=None,
            state_writer=writer,
            resolver=harness.resolver,
            tool_runtime=MagicMock(),
        ),
    )
    await store.warm()
    await store.record_responded_turn(record)
    await store.mark_source_redacted("$deleted-edit")
    original = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": first,
            "sender": USER_ID,
            "origin_server_ts": 1,
            "content": {
                "msgtype": "m.text",
                "body": "LIVE_ORIGINAL",
                "m.relates_to": {"rel_type": "m.thread", "event_id": THREAD_ID},
            },
        },
    )
    await principal.install_hydrated_conversation(
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        complete=True,
        expected_membership_epoch=await principal.membership_epoch(ROOM_ID),
        events=(_projected_event(ROOM_ID, original, EventKind.MESSAGE, self_sender="@assistant:example.org"),)
        if deletion in {"canonical", "edit_only"}
        else (),
    )
    edit, info = _edit_event(original_event_id=second, new_body="LIVE_SIBLING")
    redaction = nio.RedactionEvent.from_dict(
        {
            "type": "m.room.redaction",
            "event_id": "$redaction",
            "sender": USER_ID,
            "origin_server_ts": 20,
            "redacts": "$deleted-edit" if deletion == "edit_only" else first,
            "content": {},
        },
    )
    redaction_room = "!other:example.org" if deletion == "other_room" else ROOM_ID
    redaction_record = IngestionRecordAdmission(
        IngestionRecordDisposition.SEMANTIC_EVENT,
        event=_inbound_event(redaction_room, redaction, EventKind.REDACTION, EventClass.ACTIONABLE),
        projected=_projected_event(
            redaction_room,
            redaction,
            EventKind.REDACTION,
            self_sender="@assistant:example.org",
        ),
    )
    if deletion == "other_principal":
        await journal_store.principal("@other:example.org").admit(redaction_record.event, redaction_record.projected)
    stream = uuid4()
    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=stream)
    batch = [
        IngestionRecordAdmission(
            IngestionRecordDisposition.SEMANTIC_EVENT,
            event=_inbound_event(ROOM_ID, edit, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected=_projected_event(ROOM_ID, edit, EventKind.MESSAGE, self_sender="@assistant:example.org"),
        ),
    ]
    if deletion not in {"absent", "other_principal"}:
        batch.append(redaction_record)
    await principal.admit_ingestion_batch(IngestionBatchAdmission(stream, 1, tuple(batch)))
    prompts = []

    async def generate(request: ResponseRequest) -> str | None:
        assert request.prepare_source_turn is not None
        assert await request.prepare_source_turn(request.thread_history) is False
        assert not store.get_turn_record(first).pending_redaction_cleanup_event_ids
        prompts.append(request.prompt)
        await _acknowledge_test_edit(tmp_path, request, RESPONSE_EVENT_ID, store, journal_store=journal_store)
        return RESPONSE_EVENT_ID

    harness.regenerator.deps = replace(harness.regenerator.deps, turn_store=store, generate_response=generate)

    async def handle(event: JournalEvent) -> bool:
        if event.event_id == edit.event_id:
            await _handle_edit(harness, edit, info)
        elif event.room_id == ROOM_ID:
            await store.mark_source_redacted(redaction.redacts)
        return True

    worker = PendingEventWorker(store=principal, handle=handle)
    try:
        await worker.drain_once()
    finally:
        await worker.stop()
    owner = store.get_turn_record(first)
    assert owner is not None
    if deletion in {"canonical", "edit_only"}:
        assert len(prompts) == 1
        assert "LIVE_SIBLING" in prompts[0]
        assert ("LIVE_ORIGINAL" in prompts[0]) == (deletion == "edit_only")
        assert (first in owner.redacted_source_event_ids) == (deletion == "canonical")
        assert not await principal.is_pending(edit.event_id)
        assert not await principal.is_pending(redaction.event_id)
    else:
        assert prompts == []
        assert first not in owner.redacted_source_event_ids
        assert await principal.is_pending(edit.event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("deletion_phase", ["before", "locked", "preparation"])
@pytest.mark.parametrize("aliased_requester", [False, True])
async def test_deleted_coalesced_revision_refills_and_rebuilds_without_losing_edit(  # noqa: PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    deletion_phase: str,
    aliased_requester: bool,
) -> None:
    """A sibling deletion rebuilds exact canonical text and preserves the driving callback."""
    first, second = "$first", "$second"
    edited = replace(
        _turn_record(
            source_event_ids=(first, second),
            source_event_prompts={first: "DELETED_EDIT_MARKER", second: "second original"},
            source_event_metadata=_source_metadata(first, second),
        ),
        source_event_revisions={first: (10, "$deleted-edit")},
    )
    harness = _harness(tmp_path, turn_record=edited)
    writer = MagicMock()
    writer.supports_run_recovery.return_value = False
    writer.history_scope.return_value = edited.history_scope
    writer.session_type_for_scope.return_value = SessionType.AGENT
    writer.create_storage.side_effect = lambda *_args, **_kwargs: create_state_storage(
        AGENT_NAME,
        tmp_path,
        subdir="sessions",
        session_table="edit_sessions",
    )
    real_store = TurnStore(
        TurnStoreDeps(
            agent_name=AGENT_NAME,
            turn_records=journal_store.turn_records(AGENT_NAME),
            redacted_event_ids=journal_store.principal("agent@alice").redacted_event_ids,
            legacy_responses_file=None,
            state_writer=writer,
            resolver=harness.resolver,
            tool_runtime=MagicMock(),
        ),
    )
    await real_store.warm()
    await real_store.record_responded_turn(edited)
    surviving = make_visible_message(
        event_id=first,
        sender="@bridge:test" if aliased_requester else USER_ID,
        body="SURVIVING_ORIGINAL",
        thread_id=THREAD_ID,
        timestamp=1,
    )
    harness.resolver.resolve_exact_source.return_value = surviving
    attempts: list[ResponseRequest] = []
    generated: list[str] = []

    original_prepare = real_store._prepare_edit_response_source
    changed_during_preparation = False

    async def late_prepare(**kwargs: object) -> bool:
        nonlocal changed_during_preparation
        if not changed_during_preparation:
            changed_during_preparation = True
            await real_store.mark_source_redacted("$deleted-edit")
        return await original_prepare(**kwargs)

    if deletion_phase == "preparation":
        real_store._prepare_edit_response_source = late_prepare

    async def generate(request: ResponseRequest) -> str | None:
        attempts.append(request)
        if deletion_phase == "locked" and len(attempts) == 1:
            await real_store.mark_source_redacted("$deleted-edit")
        assert request.prepare_source_turn is not None
        if await request.prepare_source_turn(request.thread_history):
            return None
        generated.append(request.prompt)
        await _acknowledge_test_edit(
            tmp_path,
            request,
            RESPONSE_EVENT_ID,
            harness.regenerator.deps.turn_store,
            journal_store=journal_store,
        )
        return RESPONSE_EVENT_ID

    harness.regenerator.deps = replace(harness.regenerator.deps, turn_store=real_store, generate_response=generate)
    if deletion_phase == "before":
        await real_store.mark_source_redacted("$deleted-edit")
    event, info = _edit_event(original_event_id=second, new_body="LIVE_SIBLING_EDIT")
    await _handle_edit(harness, event, info)
    assert len(attempts) == (1 if deletion_phase == "before" else 2)
    assert len(generated) == 1
    assert "DELETED_EDIT_MARKER" not in generated[0]
    assert "SURVIVING_ORIGINAL" in generated[0]
    assert "LIVE_SIBLING_EDIT" in generated[0]
    owner = real_store.get_turn_record(second)
    assert owner is not None
    assert owner.completed
    assert owner.response_event_id == RESPONSE_EVENT_ID
    assert owner.source_event_revisions[first] == (1, first)
    assert owner.source_event_revisions[second] == (event.server_timestamp, event.event_id)
    assert owner.revision_watermark(first) == (10, "$deleted-edit")
    assert owner.revision_replay["$deleted-edit"].response_event_id == RESPONSE_EVENT_ID
    assert not owner.revision_replay["$deleted-edit"].cleanup_pending
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("late_preparation", [False, True])
async def test_redacted_driving_edit_retires_only_its_own_pending_revision(  # noqa: PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    late_preparation: bool,
) -> None:
    """The sibling's admitted edit survives suppression of the newer driving edit."""
    first, second = "$first", "$second"
    original = _turn_record(
        source_event_ids=(first, second),
        source_event_prompts={first: "first original", second: "SECOND_ORIGINAL"},
        source_event_metadata=_source_metadata(first, second),
    )
    harness = _harness(tmp_path, turn_record=original)
    writer = MagicMock()
    writer.supports_run_recovery.return_value = False
    writer.history_scope.return_value = original.history_scope
    writer.session_type_for_scope.return_value = SessionType.AGENT
    writer.create_storage.side_effect = lambda *_args, **_kwargs: create_state_storage(
        AGENT_NAME,
        tmp_path,
        subdir="sessions",
        session_table="driving_edit_sessions",
    )
    store = TurnStore(
        TurnStoreDeps(
            agent_name=AGENT_NAME,
            turn_records=journal_store.turn_records(AGENT_NAME),
            redacted_event_ids=journal_store.principal("agent@alice").redacted_event_ids,
            legacy_responses_file=None,
            state_writer=writer,
            resolver=harness.resolver,
            tool_runtime=MagicMock(),
        ),
    )
    await store.warm()
    await store.record_responded_turn(original)
    assert store.try_claim_turn(original)
    waiting = asyncio.Event()
    generated: list[str] = []
    attempts = 0

    async def wait(event_ids: tuple[str, ...]) -> None:
        waiting.set()
        await store.wait_for_turn_settled(event_ids)

    original_prepare = store._prepare_edit_response_source
    changed_during_preparation = False

    async def late_prepare(**kwargs: object) -> bool:
        nonlocal changed_during_preparation
        if not changed_during_preparation:
            changed_during_preparation = True
            await store.mark_source_redacted("$driving-edit")
        return await original_prepare(**kwargs)

    if late_preparation:
        store._prepare_edit_response_source = late_prepare

    async def generate(request: ResponseRequest) -> str | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1 and not late_preparation:
            await store.mark_source_redacted("$driving-edit")
        assert request.prepare_source_turn is not None
        if await request.prepare_source_turn(request.thread_history):
            return None
        generated.append(request.prompt)
        await _acknowledge_test_edit(
            tmp_path,
            request,
            RESPONSE_EVENT_ID,
            harness.regenerator.deps.turn_store,
            journal_store=journal_store,
        )
        return RESPONSE_EVENT_ID

    harness.resolver.resolve_exact_source.return_value = make_visible_message(
        event_id=second,
        body="SECOND_ORIGINAL",
        sender=USER_ID,
        timestamp=1,
        thread_id=THREAD_ID,
    )
    harness.regenerator.deps = replace(
        harness.regenerator.deps,
        turn_store=store,
        wait_for_turn_settled=wait,
        generate_response=generate,
    )
    first_event, first_info = _edit_event(
        original_event_id=first,
        new_body="LIVE_FIRST_EDIT",
        event_id="$first-edit",
        server_timestamp=10,
    )
    second_event, second_info = _edit_event(
        original_event_id=second,
        new_body="DELETED_DRIVER",
        event_id="$driving-edit",
        server_timestamp=20,
    )
    first_task = asyncio.create_task(_handle_edit(harness, first_event, first_info))
    await asyncio.wait_for(waiting.wait(), timeout=1)
    second_received = asyncio.Event()

    async def received(**_kwargs: object) -> bool:
        second_received.set()
        return False

    harness.ingress_hook_runner.emit_message_received_hooks.side_effect = received
    second_task = asyncio.create_task(_handle_edit(harness, second_event, second_info))
    await asyncio.wait_for(second_received.wait(), timeout=1)
    assert next(iter(harness.regenerator._mailboxes.values())).pending.get(second) is not None
    store.release_pending_turn_claim(original)
    await asyncio.gather(first_task, second_task)
    assert attempts == 2
    assert len(generated) == 1
    assert "LIVE_FIRST_EDIT" in generated[0]
    assert "SECOND_ORIGINAL" in generated[0]
    assert "DELETED_DRIVER" not in generated[0]
    owner = store.get_turn_record(first)
    assert owner is not None
    assert owner.source_event_revisions[first] == (10, "$first-edit")
    assert owner.revision_replay["$driving-edit"].response_event_id is None
