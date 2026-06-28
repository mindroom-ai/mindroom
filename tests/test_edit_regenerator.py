"""Direct unit suite for the EditRegenerator edited-message replay workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.coalescing_batch import coalesced_prompt
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.conversation_resolver import ConversationResolver, MessageContext
from mindroom.dispatch_source import EDIT_SOURCE_KIND
from mindroom.edit_regenerator import EditRegenerator, EditRegeneratorDeps
from mindroom.handled_turns import HandledTurnRecord, SourceEventMetadata
from mindroom.history.types import HistoryScope
from mindroom.hooks.ingress import HookIngressPolicy
from mindroom.matrix.event_info import EventInfo
from mindroom.message_target import MessageTarget
from mindroom.timestamp_formatting import format_timestamp_ms
from mindroom.turn_policy import IngressHookRunner
from mindroom.turn_store import TurnStore, _LoadedTurnRecord
from tests.conftest import make_visible_message, request_envelope
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

AGENT_NAME = "assistant"
ROOM_ID = "!room:example.org"
THREAD_ID = "$thread-root:example.org"
USER_ID = "@user:example.org"
ORIGINAL_EVENT_ID = "$original:example.org"
EDIT_EVENT_ID = "$edit:example.org"
RESPONSE_EVENT_ID = "$response:example.org"
NEW_RESPONSE_EVENT_ID = "$regenerated:example.org"
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
    logger: MagicMock
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
    anchor_event_id: str | None = None,
    response_event_id: str | None = RESPONSE_EVENT_ID,
    source_event_prompts: dict[str, str] | None = None,
    source_event_metadata: dict[str, SourceEventMetadata] | None = None,
    response_owner: str | None = AGENT_NAME,
    thread_id: str | None = THREAD_ID,
) -> HandledTurnRecord:
    anchor = anchor_event_id or source_event_ids[-1]
    return HandledTurnRecord(
        anchor_event_id=anchor,
        source_event_ids=source_event_ids,
        response_event_id=response_event_id,
        source_event_prompts=source_event_prompts,
        source_event_metadata=source_event_metadata,
        response_owner=response_owner,
        history_scope=HistoryScope(kind="agent", scope_id=AGENT_NAME),
        conversation_target=MessageTarget.resolve(ROOM_ID, thread_id, anchor),
    )


def _edit_event(
    *,
    original_event_id: str | None = ORIGINAL_EVENT_ID,
    new_body: str = "what is 3+3?",
    sender: str = USER_ID,
    include_new_content: bool = True,
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
        "event_id": EDIT_EVENT_ID,
        "sender": sender,
        "origin_server_ts": 1_000_001,
        "type": "m.room.message",
        "room_id": ROOM_ID,
    }
    event = nio.RoomMessageText.from_dict(source)
    event.source = source
    return event, EventInfo.from_event(source)


def _harness(tmp_path: Path, *, turn_record: HandledTurnRecord | None, requires_backfill: bool = False) -> _Harness:
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
    turn_store.load_turn.return_value = (
        None if turn_record is None else _LoadedTurnRecord(record=turn_record, requires_backfill=requires_backfill)
    )
    turn_store.build_run_metadata.return_value = dict(RUN_METADATA)

    ingress_hook_runner = MagicMock(spec=IngressHookRunner)
    ingress_hook_runner.emit_message_received_hooks.return_value = False

    generate_response = AsyncMock(return_value=NEW_RESPONSE_EVENT_ID)
    logger = MagicMock()
    regenerator = EditRegenerator(
        EditRegeneratorDeps(
            runtime=_RuntimeStub(client=AsyncMock(spec=nio.AsyncClient), config=config),
            get_logger=lambda: logger,
            runtime_paths=runtime_paths,
            agent_name=AGENT_NAME,
            resolver=resolver,
            turn_store=turn_store,
            ingress_hook_runner=ingress_hook_runner,
            generate_response=generate_response,
            timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(timestamp_ms, timezone=config.timezone),
        ),
    )
    return _Harness(
        regenerator=regenerator,
        resolver=resolver,
        turn_store=turn_store,
        ingress_hook_runner=ingress_hook_runner,
        generate_response=generate_response,
        logger=logger,
        config=config,
        runtime_paths=runtime_paths,
        room=nio.MatrixRoom(room_id=ROOM_ID, own_user_id=f"@{AGENT_NAME}:example.org"),
        context=context,
    )


async def _handle_edit(harness: _Harness, event: nio.RoomMessageText, event_info: EventInfo) -> None:
    await harness.regenerator.handle_message_edit(harness.room, event, event_info, USER_ID)


def _assert_no_regeneration(harness: _Harness) -> None:
    harness.generate_response.assert_not_awaited()
    harness.turn_store.record_turn_record.assert_not_called()


@pytest.mark.asyncio
async def test_simple_edit_regenerates_and_records_new_response(tmp_path: Path) -> None:
    """An edited single-message turn regenerates with the edited body and records the new outcome."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(new_body="what is 3+3?")

    await _handle_edit(harness, event, event_info)

    harness.generate_response.assert_awaited_once()
    call_kwargs = harness.generate_response.await_args.kwargs
    assert call_kwargs["prompt"] == "what is 3+3?"
    assert call_kwargs["existing_event_id"] == RESPONSE_EVENT_ID
    assert call_kwargs["existing_event_is_placeholder"] is False
    assert call_kwargs["user_id"] == USER_ID
    assert call_kwargs["correlation_id"] == EDIT_EVENT_ID
    assert call_kwargs["matrix_run_metadata"] == RUN_METADATA
    assert call_kwargs["current_timestamp_ms"] == float(event.server_timestamp)
    assert call_kwargs["thread_history"] == harness.context.thread_history

    envelope_kwargs = harness.resolver.build_message_envelope.call_args.kwargs
    assert envelope_kwargs["body"] == "what is 3+3?"
    assert envelope_kwargs["source_kind"] == EDIT_SOURCE_KIND
    assert envelope_kwargs["target"] == record.conversation_target
    assert envelope_kwargs["requester_user_id"] == USER_ID

    metadata_kwargs = harness.turn_store.build_run_metadata.call_args.kwargs
    assert metadata_kwargs["additional_source_event_ids"] == ()

    harness.turn_store.record_turn_record.assert_called_once()
    recorded = harness.turn_store.record_turn_record.call_args.args[0]
    assert recorded.response_event_id == NEW_RESPONSE_EVENT_ID
    assert recorded.source_event_ids == (ORIGINAL_EVENT_ID,)
    assert recorded.anchor_event_id == ORIGINAL_EVENT_ID
    assert recorded.response_owner == AGENT_NAME
    assert recorded.history_scope == record.history_scope
    assert recorded.conversation_target == record.conversation_target


@pytest.mark.asyncio
async def test_lifecycle_lock_callback_removes_stale_runs(tmp_path: Path) -> None:
    """The lock-acquired callback prunes stale persisted runs for the regeneration record."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    on_lock_acquired = harness.generate_response.await_args.kwargs["on_lifecycle_lock_acquired"]
    harness.turn_store.remove_stale_runs_for_edit.assert_not_called()
    on_lock_acquired()
    harness.turn_store.remove_stale_runs_for_edit.assert_called_once()
    removal_kwargs = harness.turn_store.remove_stale_runs_for_edit.call_args.kwargs
    assert removal_kwargs["requester_user_id"] == USER_ID
    assert removal_kwargs["loaded_turn"].record == record


@pytest.mark.asyncio
async def test_coalesced_edit_rebuilds_combined_prompt(tmp_path: Path) -> None:
    """Editing one member of a coalesced batch rebuilds the combined prompt and prompt map."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    record = _turn_record(
        source_event_ids=(first_event_id, second_event_id),
        source_event_prompts={first_event_id: "first message", second_event_id: "second message"},
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id=first_event_id, new_body="edited first message")

    await _handle_edit(harness, event, event_info)

    expected_prompt = coalesced_prompt(["edited first message", "second message"])
    assert harness.generate_response.await_args.kwargs["prompt"] == expected_prompt

    metadata_call = harness.turn_store.build_run_metadata.call_args
    handled_turn = metadata_call.args[0]
    assert handled_turn.source_event_ids == (first_event_id, second_event_id)
    assert handled_turn.source_event_prompts == {
        first_event_id: "edited first message",
        second_event_id: "second message",
    }
    assert metadata_call.kwargs["additional_source_event_ids"] == ()

    recorded = harness.turn_store.record_turn_record.call_args.args[0]
    assert recorded.response_event_id == NEW_RESPONSE_EVENT_ID
    assert recorded.source_event_prompts == {
        first_event_id: "edited first message",
        second_event_id: "second message",
    }


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
    event, event_info = _edit_event(original_event_id=first_event_id, new_body="edited ]]> first <message>")

    await _handle_edit(harness, event, event_info)

    assert harness.generate_response.await_args.kwargs["prompt"] == (
        "The user sent the following messages in quick succession. "
        "Treat them as one turn and respond once:\n\n"
        "<messages>\n"
        '<msg event_id="$m1:example.org" from="@alice:example.org" ts="2026-03-20 08:15 PDT">'
        "<![CDATA[edited ]]]]><![CDATA[> first <message>]]></msg>\n"
        '<msg event_id="$m2:example.org" from="@bob:example.org" ts="2026-03-20 08:16 PDT">'
        "<![CDATA[second message]]></msg>\n"
        "</messages>"
    )
    assert harness.generate_response.await_args.kwargs["current_prompt_is_structured"] is True

    handled_turn = harness.turn_store.build_run_metadata.call_args.args[0]
    assert handled_turn.source_event_metadata == record.source_event_metadata
    recorded = harness.turn_store.record_turn_record.call_args.args[0]
    assert recorded.source_event_metadata == record.source_event_metadata


@pytest.mark.asyncio
async def test_coalesced_edit_without_persisted_prompts_is_skipped(tmp_path: Path) -> None:
    """A coalesced turn without a persisted prompt map cannot be rebuilt and is skipped."""
    record = _turn_record(
        source_event_ids=("$m1:example.org", "$m2:example.org"),
        source_event_prompts=None,
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
    harness.logger.debug.assert_any_call("Edit event has no original event ID")


@pytest.mark.asyncio
async def test_edit_without_turn_record_returns_early(tmp_path: Path) -> None:
    """An edit with no durable turn record logs the debug path and does nothing else."""
    harness = _harness(tmp_path, turn_record=None)
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)
    harness.resolver.build_message_envelope.assert_not_called()
    harness.logger.debug.assert_any_call(
        "No handled turn record found for edited message",
        original_event_id=ORIGINAL_EVENT_ID,
    )


@pytest.mark.asyncio
async def test_hook_suppression_records_turn_without_regeneration(tmp_path: Path) -> None:
    """Suppressing ingress hooks records the unchanged turn record and skips regeneration."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.ingress_hook_runner.emit_message_received_hooks.return_value = True
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    hook_kwargs = harness.ingress_hook_runner.emit_message_received_hooks.await_args.kwargs
    assert hook_kwargs["correlation_id"] == EDIT_EVENT_ID
    assert hook_kwargs["policy"] == HookIngressPolicy()

    harness.generate_response.assert_not_awaited()
    harness.turn_store.record_turn_record.assert_called_once()
    recorded = harness.turn_store.record_turn_record.call_args.args[0]
    assert recorded.response_event_id == RESPONSE_EVENT_ID
    assert recorded.source_event_ids == (ORIGINAL_EVENT_ID,)


@pytest.mark.asyncio
async def test_generate_response_failure_propagates_without_recording(tmp_path: Path) -> None:
    """A raising generate_response propagates and leaves the turn record untouched."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    harness.generate_response.side_effect = RuntimeError("model unavailable")
    event, event_info = _edit_event()

    with pytest.raises(RuntimeError, match="model unavailable"):
        await _handle_edit(harness, event, event_info)

    harness.turn_store.record_turn_record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("requires_backfill", [True, False])
async def test_suppressed_regeneration_backfills_only_when_required(
    tmp_path: Path,
    requires_backfill: bool,
) -> None:
    """A None regeneration result records the merged turn only when backfill is pending."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record, requires_backfill=requires_backfill)
    harness.generate_response.return_value = None
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    harness.generate_response.assert_awaited_once()
    if requires_backfill:
        harness.turn_store.record_turn_record.assert_called_once()
        recorded = harness.turn_store.record_turn_record.call_args.args[0]
        assert recorded.response_event_id == RESPONSE_EVENT_ID
    else:
        harness.turn_store.record_turn_record.assert_not_called()


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
        caller_label="edit_regeneration_context",
    )
    assert harness.generate_response.await_args.kwargs["thread_history"] == refetched_history


@pytest.mark.asyncio
async def test_non_coalesced_anchor_mismatch_extends_run_metadata_sources(tmp_path: Path) -> None:
    """A non-coalesced turn anchored to another event keeps the edited event discoverable."""
    anchor_event_id = "$question:example.org"
    record = _turn_record(source_event_ids=(anchor_event_id,), anchor_event_id=anchor_event_id)
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id=ORIGINAL_EVENT_ID)

    await _handle_edit(harness, event, event_info)

    metadata_kwargs = harness.turn_store.build_run_metadata.call_args.kwargs
    assert metadata_kwargs["additional_source_event_ids"] == (ORIGINAL_EVENT_ID,)


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
    record = HandledTurnRecord(
        anchor_event_id=ORIGINAL_EVENT_ID,
        source_event_ids=(ORIGINAL_EVENT_ID,),
        response_event_id=RESPONSE_EVENT_ID,
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)
