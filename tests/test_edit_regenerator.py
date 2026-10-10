"""Direct unit suite for the EditRegenerator edited-message replay workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom import reply_lifecycle as rl
from mindroom.coalescing_batch import tagged_coalesced_prompt
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.conversation_resolver import ConversationResolver, MessageContext
from mindroom.dispatch_source import EDIT_SOURCE_KIND
from mindroom.edit_regenerator import EditRegenerator, EditRegeneratorDeps
from mindroom.handled_turns import SourceEventMetadata, TurnRecord
from mindroom.history.types import HistoryScope
from mindroom.hooks.ingress import HookIngressPolicy
from mindroom.matrix.event_info import EventInfo
from mindroom.message_target import MessageTarget
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.timestamp_formatting import format_timestamp_ms
from mindroom.turn_policy import IngressHookRunner
from mindroom.turn_store import TurnStore
from tests.conftest import make_visible_message, request_envelope
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.response_runner import ResponseRequest

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
    stop_reply: AsyncMock
    later_human_message: AsyncMock
    settle_sources: AsyncMock
    # The conversation lock the harness's regenerations take before they claim.
    response_lock: asyncio.Lock
    # The regenerations the regenerator started, which a test awaits before it checks their effects.
    regenerations: list[asyncio.Task[None]]
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
    resolver.extract_message_context.return_value = context
    resolver.build_message_envelope = MagicMock(
        side_effect=lambda *, event, target, body, requester_user_id, source_kind, **_kwargs: replace(
            request_envelope(
                target=target,
                prompt=body,
                user_id=requester_user_id,
                agent_name=AGENT_NAME,
                source_kind=source_kind,
            ),
            source_event_id=event.event_id,
        ),
    )

    turn_store = MagicMock(spec=TurnStore)
    current_turn_record = [turn_record]
    turn_store.load_turn.side_effect = lambda _original_event_id: current_turn_record[0]
    turn_store.get_turn_record.side_effect = lambda _event_id: current_turn_record[0]
    turn_store.register_edit_revision.side_effect = lambda event_id, _revision: turn_store.get_turn_record(event_id)
    turn_store._is_revision_redacted.return_value = False

    def record_turn(record: TurnRecord) -> None:
        current_turn_record[0] = record

    turn_store.record_turn.side_effect = record_turn
    turn_store.record_responded_turn.side_effect = record_turn
    turn_store.record_edit.side_effect = record_turn
    turn_store.build_run_metadata.return_value = dict(RUN_METADATA)
    turn_store._prepare_response_for_redactions.return_value = False

    async def prepare_edit_snapshot(
        *,
        record: TurnRecord,
        driving_revision_id: str,
        consumed_revision_ids: tuple[str, ...],
        thread_history: object,
    ) -> bool:
        del driving_revision_id, consumed_revision_ids, thread_history
        return await turn_store._prepare_response_for_redactions(
            target=record.conversation_target,
            source_event_ids=record.replay_source_event_ids,
        )

    turn_store.prepare_edit_snapshot.side_effect = prepare_edit_snapshot

    ingress_hook_runner = MagicMock(spec=IngressHookRunner)
    ingress_hook_runner.emit_message_received_hooks.return_value = False

    generate_response = AsyncMock(return_value=NEW_RESPONSE_EVENT_ID)
    reply_for_sources = AsyncMock(return_value=_reply(event_id=RESPONSE_EVENT_ID))
    response_lock = asyncio.Lock()

    regenerations: list[asyncio.Task[None]] = []

    async def regenerate(request: ResponseRequest) -> None:
        async with response_lock:
            # As the runner does once the regeneration claims its reply, before it runs.
            assert request.on_reply_claimed is not None
            await request.on_reply_claimed()
            event_id = await generate_response(request)
            if event_id is None:
                return
            # The reply records own the answer from then on, which is what a later edit regenerates.
            reply_for_sources.return_value = _reply(event_id=event_id)

    def track_inbox_response(response: Coroutine[Any, Any, None], **_ownership: object) -> asyncio.Task[None]:
        task = asyncio.create_task(response)
        regenerations.append(task)
        return task

    regenerator = EditRegenerator(
        EditRegeneratorDeps(
            runtime=_RuntimeStub(client=AsyncMock(spec=nio.AsyncClient), config=config),
            runtime_paths=runtime_paths,
            agent_name=AGENT_NAME,
            resolver=resolver,
            turn_store=turn_store,
            ingress_hook_runner=ingress_hook_runner,
            generate_response=regenerate,
            track_inbox_response=track_inbox_response,
            settle_sources=AsyncMock(),
            stop_reply=AsyncMock(),
            receipt_order=AsyncMock(return_value=receipt_order),
            timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(timestamp_ms, timezone=config.timezone),
            reply_for_sources=reply_for_sources,
            later_human_message=AsyncMock(return_value=False),
        ),
    )
    return _Harness(
        regenerator=regenerator,
        resolver=resolver,
        turn_store=turn_store,
        ingress_hook_runner=ingress_hook_runner,
        generate_response=generate_response,
        stop_reply=regenerator.deps.stop_reply,  # type: ignore[arg-type]
        later_human_message=regenerator.deps.later_human_message,  # type: ignore[arg-type]
        settle_sources=regenerator.deps.settle_sources,  # type: ignore[arg-type]
        response_lock=response_lock,
        regenerations=regenerations,
        config=config,
        runtime_paths=runtime_paths,
        room=nio.MatrixRoom(room_id=ROOM_ID, own_user_id=f"@{AGENT_NAME}:example.org"),
        context=context,
    )


async def _handle_edit(harness: _Harness, event: nio.RoomMessageText, event_info: EventInfo) -> bool | None:
    handed_off = await harness.regenerator.handle_message_edit(harness.room, event, event_info, USER_ID)
    await asyncio.gather(*harness.regenerations)
    return handed_off


def _assert_no_regeneration(harness: _Harness) -> None:
    harness.generate_response.assert_not_awaited()
    harness.turn_store.record_edit.assert_not_called()


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

    harness.turn_store.record_edit.assert_called_once()
    recorded = harness.turn_store.record_edit.call_args.args[0]
    assert recorded.response_event_id == NEW_RESPONSE_EVENT_ID
    assert recorded.source_event_ids == (ORIGINAL_EVENT_ID,)
    assert recorded.anchor_event_id == ORIGINAL_EVENT_ID
    assert recorded.response_owner == AGENT_NAME
    assert recorded.history_scope == record.history_scope
    assert recorded.conversation_target == record.conversation_target


@pytest.mark.asyncio
async def test_an_edit_prunes_the_history_it_replaces_only_once_its_reply_is_claimed(tmp_path: Path) -> None:
    """Source gates prune nothing; the claim of the reply does, once, so a refused claim leaves history whole."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    request = harness.generate_response.await_args.args[0]
    prepare = request.prepare_source_turn
    harness.turn_store.remove_stale_runs_for_edit.reset_mock()
    assert await prepare(request.thread_history) is False
    harness.turn_store.remove_stale_runs_for_edit.assert_not_called()
    await request.on_reply_claimed()
    assert await prepare(request.thread_history) is False
    harness.turn_store.remove_stale_runs_for_edit.assert_called_once()
    removal_kwargs = harness.turn_store.remove_stale_runs_for_edit.call_args.kwargs
    assert removal_kwargs["requester_user_id"] == USER_ID
    assert removal_kwargs["turn_record"] == replace(
        record,
        source_event_prompts={ORIGINAL_EVENT_ID: "what is 3+3?"},
        source_event_revisions={
            ORIGINAL_EVENT_ID: (event.server_timestamp, event.event_id),
        },
    )


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
    persisted_record = first_harness.turn_store.record_edit.call_args.args[0]

    restarted_harness = _harness(tmp_path, turn_record=persisted_record)
    older, older_info = _edit_event(
        new_body="older body",
        event_id="$edit-old:example.org",
        server_timestamp=1_000_010,
    )
    await _handle_edit(restarted_harness, older, older_info)

    _assert_no_regeneration(restarted_harness)


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

    recorded = harness.turn_store.record_edit.call_args.args[0]
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
    harness.turn_store._prepare_response_for_redactions.assert_called_once_with(
        target=record.conversation_target,
        source_event_ids=(second_event_id,),
    )
    handled_turn = harness.turn_store.build_run_metadata.call_args.args[0]
    assert handled_turn.redacted_source_event_ids == (first_event_id,)
    assert handled_turn.source_event_prompts == {second_event_id: "edited second message"}


@pytest.mark.asyncio
async def test_coalesced_edit_rechecks_every_snapshotted_source_under_lock(tmp_path: Path) -> None:
    """A sibling redacted after prompt assembly suppresses the stale coalesced prompt; nothing runs with it."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    record = _turn_record(
        source_event_ids=(first_event_id, second_event_id),
        source_event_prompts={first_event_id: "first message", second_event_id: "second message"},
        source_event_metadata=_source_metadata(first_event_id, second_event_id),
    )
    harness = _harness(tmp_path, turn_record=record)
    redaction_checks = 0

    async def _prepare_response_for_redactions(**_kwargs: object) -> bool:
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

    harness.turn_store._prepare_response_for_redactions.side_effect = _prepare_response_for_redactions
    harness.generate_response.side_effect = generate
    event, event_info = _edit_event(original_event_id=second_event_id, new_body="edited second message")

    await _handle_edit(harness, event, event_info)

    assert [call.args[0].prompt for call in harness.generate_response.await_args_list] == [
        _tagged_prompt(
            (first_event_id, second_event_id),
            {first_event_id: "first message", second_event_id: "edited second message"},
        ),
    ]
    assert harness.turn_store._prepare_response_for_redactions.call_count == 1
    assert harness.turn_store.record_turn.call_args.args[0].redacted_source_event_ids == (first_event_id,)


def _reply(
    *,
    event_id: str,
    state: rl.ReplyState = rl.ReplyState.COMPLETED,
    stop_receipt_order: int | None = None,
) -> rl.Reply:
    return rl.Reply(
        reply_id="reply-2",
        entity_name=AGENT_NAME,
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        membership_epoch=1,
        state=state,
        last_span_id="span-2",
        presentation="",
        revision=1,
        reply_sequence=2,
        created_at_ns=1,
        updated_at_ns=1,
        event_id=event_id,
        stop_receipt_order=stop_receipt_order,
    )


@pytest.mark.asyncio
async def test_edit_regenerates_the_answer_its_reply_records_name(tmp_path: Path) -> None:
    """After a regeneration answered in a new reply, the next edit regenerates that reply, not the turn's old answer."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.regenerator.deps.reply_for_sources.return_value = _reply(event_id="$regenerated:example.org")
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    harness.regenerator.deps.reply_for_sources.assert_awaited_once_with(record.source_event_ids)
    request = harness.generate_response.await_args.args[0]
    assert request.existing_event_id == "$regenerated:example.org"
    assert await request.prepare_source_turn(request.thread_history) is False


@pytest.mark.asyncio
async def test_an_edit_of_a_message_someone_answered_after_is_ignored(tmp_path: Path) -> None:
    """Later turns built on the old text, so editing an earlier message regenerates nothing."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    harness.later_human_message.return_value = True
    event, event_info = _edit_event()

    assert await _handle_edit(harness, event, event_info) is None

    _assert_no_regeneration(harness)
    harness.turn_store.register_edit_revision.assert_not_called()


@pytest.mark.asyncio
async def test_an_edit_of_a_reply_an_approval_holds_cancels_the_approval_and_regenerates_in_place(
    tmp_path: Path,
) -> None:
    """The edit stops the held reply, which cancels its approval; the regeneration takes its place."""
    harness = _harness(tmp_path, turn_record=_turn_record(), receipt_order=9)
    held = replace(_reply(event_id=RESPONSE_EVENT_ID, state=rl.ReplyState.PAUSED), approval_id="approval-1")
    harness.regenerator.deps.reply_for_sources.return_value = held  # type: ignore[attr-defined]
    event, event_info = _edit_event(new_body="what is 4+4?")

    assert await _handle_edit(harness, event, event_info) is True

    harness.stop_reply.assert_awaited_once_with(held, 9)
    request = harness.generate_response.await_args.args[0]
    assert request.prompt == "what is 4+4?"
    assert request.existing_event_id == RESPONSE_EVENT_ID


@pytest.mark.asyncio
async def test_an_edit_of_a_reply_that_waits_for_background_work_stops_it_and_regenerates_in_place(
    tmp_path: Path,
) -> None:
    """The edit stops the waiting reply, which cancels the work it waits for; the regeneration takes its place."""
    harness = _harness(tmp_path, turn_record=_turn_record(), receipt_order=9)
    waiting = _reply(event_id=RESPONSE_EVENT_ID, state=rl.ReplyState.WAITING)
    harness.regenerator.deps.reply_for_sources.return_value = waiting  # type: ignore[attr-defined]
    event, event_info = _edit_event(new_body="what is 4+4?")

    assert await _handle_edit(harness, event, event_info) is True

    harness.stop_reply.assert_awaited_once_with(waiting, 9)
    request = harness.generate_response.await_args.args[0]
    assert request.existing_event_id == RESPONSE_EVENT_ID


@pytest.mark.asyncio
async def test_an_edit_of_a_reply_that_still_streams_stops_it_and_regenerates_in_place(tmp_path: Path) -> None:
    """The edit interrupts the running answer; the regeneration takes its place and owns the edit."""
    harness = _harness(tmp_path, turn_record=_turn_record(), receipt_order=9)
    running = replace(_reply(event_id=RESPONSE_EVENT_ID, state=rl.ReplyState.ACTIVE), current_span_id="span-running")
    harness.regenerator.deps.reply_for_sources.return_value = running  # type: ignore[attr-defined]
    event, event_info = _edit_event(new_body="what is 4+4?")

    assert await _handle_edit(harness, event, event_info) is True

    harness.stop_reply.assert_awaited_once_with(running, 9)
    request = harness.generate_response.await_args.args[0]
    assert request.prompt == "what is 4+4?"
    assert request.existing_event_id == RESPONSE_EVENT_ID


@pytest.mark.asyncio
async def test_an_edit_whose_regeneration_runs_nothing_is_settled_by_it(tmp_path: Path) -> None:
    """A regeneration that opens no span settles the edit the room's lane handed it."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    event, event_info = _edit_event()

    # A refusal is the response ending without signalling a claim.
    harness.regenerator.deps = replace(harness.regenerator.deps, generate_response=AsyncMock(return_value=None))

    assert await _handle_edit(harness, event, event_info) is True
    harness.settle_sources.assert_awaited_once_with((event.event_id,))


@pytest.mark.asyncio
async def test_an_edit_whose_regeneration_a_replaced_runtime_refuses_stays_pending(tmp_path: Path) -> None:
    """A runtime being replaced refuses the regeneration without settling the edit, so the replacement answers it."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    event, event_info = _edit_event()
    harness.regenerator.deps = replace(
        harness.regenerator.deps,
        generate_response=AsyncMock(side_effect=ResponseAdmissionRefusedError),
    )

    assert await harness.regenerator.handle_message_edit(harness.room, event, event_info, USER_ID) is True
    (refused,) = await asyncio.gather(*harness.regenerations, return_exceptions=True)

    assert isinstance(refused, ResponseAdmissionRefusedError)
    harness.settle_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_edit_does_not_hold_the_rooms_lane_while_another_reply_holds_the_conversation(
    tmp_path: Path,
) -> None:
    """The lane hands the edit off at once; its regeneration claims once the reply holding the conversation ends."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    event, event_info = _edit_event()

    async with harness.response_lock, asyncio.timeout(5):
        # Another reply of this agent in the same conversation runs and holds it.
        assert await harness.regenerator.handle_message_edit(harness.room, event, event_info, USER_ID) is True
        harness.generate_response.assert_not_awaited()
    await asyncio.gather(*harness.regenerations)

    harness.generate_response.assert_awaited_once()
    harness.turn_store.record_edit.assert_called_once()
    harness.settle_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_of_a_turn_whose_reply_is_gone_is_ignored(tmp_path: Path) -> None:
    """A reply whose answer is gone has nothing for an edit to regenerate."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    harness.regenerator.deps.reply_for_sources.return_value = _reply(
        event_id="$removed:example.org",
        state=rl.ReplyState.GONE,
    )
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    harness.generate_response.assert_not_awaited()


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
    harness.turn_store._prepare_response_for_redactions.return_value = True
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    request = harness.generate_response.await_args.args[0]
    assert request.prepare_source_turn is not None
    assert await request.prepare_source_turn(request.thread_history) is True
    harness.turn_store._prepare_response_for_redactions.assert_called_once_with(
        target=record.conversation_target,
        source_event_ids=(ORIGINAL_EVENT_ID,),
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
            second_event_id: SourceEventMetadata(sender="@alice:example.org", timestamp_ms=1_774_019_760_000),
        },
        requester_id="@alice:example.org",
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
    await asyncio.gather(*harness.regenerations)

    assert harness.generate_response.await_args.args[0].prompt == (
        "The user sent the following messages in quick succession. "
        "Treat them as one turn and respond once:\n\n"
        "<messages>\n"
        '<msg event_id="$m1:example.org" from="@alice:example.org" ts="2026-03-20 08:15 PDT">'
        "<![CDATA[edited ]]]]><![CDATA[> first <message>]]></msg>\n"
        '<msg event_id="$m2:example.org" from="@alice:example.org" ts="2026-03-20 08:16 PDT">'
        "<![CDATA[second message]]></msg>\n"
        "</messages>"
    )
    assert harness.generate_response.await_args.args[0].current_prompt_is_structured is True

    handled_turn = harness.turn_store.build_run_metadata.call_args.args[0]
    assert handled_turn.source_event_metadata == record.source_event_metadata
    recorded = harness.turn_store.record_edit.call_args.args[0]
    assert recorded.source_event_metadata == record.source_event_metadata


@pytest.mark.parametrize(
    ("original_event_id", "sender"),
    [
        ("$alice:example.org", "@alice:example.org"),
        ("$bob:example.org", "@bob:example.org"),
        ("$alice:example.org", "@bob:example.org"),
        ("$bob:example.org", "@alice:example.org"),
        ("$alice:example.org", "@attacker:example.org"),
    ],
)
@pytest.mark.asyncio
async def test_multi_sender_coalesced_record_never_regenerates_as_one_sender(
    tmp_path: Path,
    original_event_id: str,
    sender: str,
) -> None:
    """Regeneration would run every sender's source as the editor, so mixed records never regenerate."""
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
    await asyncio.gather(*harness.regenerations)

    _assert_no_regeneration(harness)
    harness.resolver.build_message_envelope.assert_not_called()


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
                sender="@alice:example.org",
                discovery_event_id=human_event_id,
            ),
            human_event_id: SourceEventMetadata(sender="@alice:example.org"),
        },
        requester_id="@alice:example.org",
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
    await asyncio.gather(*harness.regenerations)

    request = harness.generate_response.await_args.args[0]
    assert "human edited" in request.prompt
    assert "human base" not in request.prompt
    assert "relay base" in request.prompt
    recorded = harness.turn_store.record_edit.call_args.args[0]
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
    await asyncio.gather(*harness.regenerations)

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
    recorded = harness.turn_store.record_edit.call_args.args[0]
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
async def test_hook_suppression_ignores_the_edit(tmp_path: Path) -> None:
    """An ingress hook that suppresses the edit leaves the turn and its answer as they were."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.ingress_hook_runner.emit_message_received_hooks.return_value = True
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    hook_kwargs = harness.ingress_hook_runner.emit_message_received_hooks.await_args.kwargs
    assert hook_kwargs["correlation_id"] == EDIT_EVENT_ID
    assert hook_kwargs["policy"] == HookIngressPolicy()

    harness.generate_response.assert_not_awaited()
    harness.turn_store.record_turn.assert_not_called()


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
async def test_edit_from_non_owning_requester_is_ignored(tmp_path: Path) -> None:
    """A requester cannot regenerate another requester's durable response."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    attacker_id = "@attacker:example.org"
    event, event_info = _edit_event(sender=attacker_id)

    await harness.regenerator.handle_message_edit(harness.room, event, event_info, attacker_id)
    await asyncio.gather(*harness.regenerations)

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
    """A turn whose answer has no reply record, as one from before the upgrade, regenerates nothing."""
    record = _turn_record(response_event_id=None)
    harness = _harness(tmp_path, turn_record=record)
    harness.regenerator.deps.reply_for_sources.return_value = None  # type: ignore[attr-defined]
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
