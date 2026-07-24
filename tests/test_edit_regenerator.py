"""Direct unit suite for the EditRegenerator edited-message replay workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.coalescing_batch import coalesced_prompt
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
from mindroom.response_runner import ResponseRequest
from mindroom.timestamp_formatting import format_timestamp_ms
from mindroom.turn_policy import IngressHookRunner
from mindroom.turn_store import TurnStore
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
    wait_for_turn_settled: AsyncMock
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
    redacted_source_event_ids: tuple[str, ...] = (),
    anchor_event_id: str | None = None,
    response_event_id: str | None = RESPONSE_EVENT_ID,
    source_event_prompts: dict[str, str] | None = None,
    source_event_metadata: dict[str, SourceEventMetadata] | None = None,
    response_owner: str | None = AGENT_NAME,
    thread_id: str | None = THREAD_ID,
) -> TurnRecord:
    anchor = anchor_event_id or source_event_ids[-1]
    return TurnRecord(
        anchor_event_id=anchor,
        source_event_ids=source_event_ids,
        redacted_source_event_ids=redacted_source_event_ids,
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


def _harness(tmp_path: Path, *, turn_record: TurnRecord | None) -> _Harness:
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
    current_turn_record = [turn_record]
    turn_store.load_turn.side_effect = lambda **_kwargs: current_turn_record[0]
    turn_store.get_turn_record.side_effect = lambda _event_id: current_turn_record[0]

    def record_turn(record: TurnRecord) -> None:
        current_turn_record[0] = record

    turn_store.record_turn.side_effect = record_turn
    turn_store.build_run_metadata.return_value = dict(RUN_METADATA)
    turn_store.prepare_response_for_redactions.return_value = False

    ingress_hook_runner = MagicMock(spec=IngressHookRunner)
    ingress_hook_runner.emit_message_received_hooks.return_value = False

    generate_response = AsyncMock(return_value=NEW_RESPONSE_EVENT_ID)
    response_lock = asyncio.Lock()

    async def run_locked_response(request: object) -> str | None:
        async with response_lock:
            assert isinstance(request, ResponseRequest)
            return await generate_response(request)

    wait_for_turn_settled = AsyncMock()
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
            generate_response=run_locked_response,
            wait_for_turn_settled=wait_for_turn_settled,
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
    harness.turn_store.record_turn.assert_not_called()


@pytest.mark.asyncio
async def test_simple_edit_regenerates_and_records_new_response(tmp_path: Path) -> None:
    """An edited single-message turn regenerates with the edited body and records the new outcome."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(new_body="what is 3+3?")

    await _handle_edit(harness, event, event_info)

    harness.generate_response.assert_awaited_once()
    request = harness.generate_response.await_args.args[0]
    assert request.prompt == "what is 3+3?"
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

    harness.turn_store.record_turn.assert_called_once()
    recorded = harness.turn_store.record_turn.call_args.args[0]
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

    on_lock_acquired = harness.generate_response.await_args.args[0].on_lifecycle_lock_acquired
    harness.turn_store.remove_stale_runs_for_edit.assert_not_called()
    on_lock_acquired()
    harness.turn_store.remove_stale_runs_for_edit.assert_called_once()
    removal_kwargs = harness.turn_store.remove_stale_runs_for_edit.call_args.kwargs
    assert removal_kwargs["requester_user_id"] == USER_ID
    assert removal_kwargs["turn_record"] == replace(
        record,
        source_event_revisions={
            ORIGINAL_EVENT_ID: (event.server_timestamp, event.event_id),
        },
    )


@pytest.mark.asyncio
async def test_newer_same_source_edit_rejects_older_callback_during_generation(tmp_path: Path) -> None:
    """An older callback arriving during newer generation must never run or overwrite it."""
    harness = _harness(tmp_path, turn_record=_turn_record())
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
    await _handle_edit(harness, older, older_info)
    release_generation.set()
    await newer_task

    harness.generate_response.assert_awaited_once()
    assert harness.generate_response.await_args.args[0].prompt == "newest body"
    recorded = harness.turn_store.record_turn.call_args.args[0]
    assert recorded.source_event_revisions == {
        ORIGINAL_EVENT_ID: (1_000_010, "$edit-z:example.org"),
    }
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
        coalesced_prompt(["first edited", "second base"]),
        coalesced_prompt(["first edited", "second edited"]),
    ]
    recorded = harness.turn_store.record_turn.call_args.args[0]
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

    assert harness.generate_response.await_args.args[0].prompt == coalesced_prompt(
        ["first suppressed edit", "second edit"],
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
    recorded = harness.turn_store.record_turn.call_args.args[0]
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
    recorded = harness.turn_store.record_turn.call_args.args[0]
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
    persisted_record = first_harness.turn_store.record_turn.call_args.args[0]

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
async def test_pending_original_failure_releases_wait_without_regeneration(tmp_path: Path) -> None:
    """A settled original with no response ID should end the drain without hanging."""
    pending_record = _turn_record(response_event_id=None)
    harness = _harness(tmp_path, turn_record=pending_record)
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    harness.wait_for_turn_settled.assert_awaited_once_with((ORIGINAL_EVENT_ID,))
    harness.generate_response.assert_not_awaited()
    harness.logger.debug.assert_any_call(
        "Skipping edit regeneration after durable turn reload",
        original_event_id=ORIGINAL_EVENT_ID,
    )
    assert harness.regenerator._mailboxes == {}


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_original_cleans_mailbox(tmp_path: Path) -> None:
    """Cancelling the edit waiter must not leave an event-loop task or mailbox behind."""
    harness = _harness(tmp_path, turn_record=_turn_record(response_event_id=None))
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
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id=first_event_id, new_body="edited first message")

    await _handle_edit(harness, event, event_info)

    expected_prompt = coalesced_prompt(["edited first message", "second message"])
    assert harness.generate_response.await_args.args[0].prompt == expected_prompt

    metadata_call = harness.turn_store.build_run_metadata.call_args
    handled_turn = metadata_call.args[0]
    assert handled_turn.source_event_ids == (first_event_id, second_event_id)
    assert handled_turn.source_event_prompts == {
        first_event_id: "edited first message",
        second_event_id: "second message",
    }
    assert metadata_call.kwargs["additional_discovery_event_ids"] == ()

    recorded = harness.turn_store.record_turn.call_args.args[0]
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
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id=second_event_id, new_body="edited second message")

    await _handle_edit(harness, event, event_info)

    request = harness.generate_response.await_args.args[0]
    assert request.prompt == coalesced_prompt(["edited second message"])
    assert "REDACTED_SECRET" not in request.prompt
    assert request.prepare_source_turn is not None
    assert request.prepare_source_turn() is False
    harness.turn_store.prepare_response_for_redactions.assert_called_once_with(
        target=record.conversation_target,
        source_event_ids=(second_event_id,),
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
    )
    harness = _harness(tmp_path, turn_record=record)
    redaction_checks = 0

    def prepare_response_for_redactions(**_kwargs: object) -> bool:
        nonlocal redaction_checks
        redaction_checks += 1
        if redaction_checks == 1:
            harness.turn_store.record_turn(
                replace(record, redacted_source_event_ids=(first_event_id,)),
            )
            return True
        return False

    async def generate(request: ResponseRequest) -> str | None:
        assert request.prepare_source_turn is not None
        return None if request.prepare_source_turn() else NEW_RESPONSE_EVENT_ID

    harness.turn_store.prepare_response_for_redactions.side_effect = prepare_response_for_redactions
    harness.generate_response.side_effect = generate
    event, event_info = _edit_event(original_event_id=second_event_id, new_body="edited second message")

    await _handle_edit(harness, event, event_info)

    assert [call.args[0].prompt for call in harness.generate_response.await_args_list] == [
        coalesced_prompt(["first message", "edited second message"]),
        coalesced_prompt(["edited second message"]),
    ]
    assert harness.turn_store.prepare_response_for_redactions.call_count == 2
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
    harness.turn_store.prepare_response_for_redactions.return_value = True
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    request = harness.generate_response.await_args.args[0]
    assert request.prepare_source_turn is not None
    assert request.prepare_source_turn() is True
    harness.turn_store.prepare_response_for_redactions.assert_called_once_with(
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
            second_event_id: SourceEventMetadata(sender="@bob:example.org", timestamp_ms=1_774_019_760_000),
        },
    )
    harness = _harness(tmp_path, turn_record=record)
    harness.config.timezone = "America/Los_Angeles"
    event, event_info = _edit_event(original_event_id=first_event_id, new_body="edited ]]> first <message>")

    await _handle_edit(harness, event, event_info)

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
    recorded = harness.turn_store.record_turn.call_args.args[0]
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
    harness.turn_store.record_turn.assert_called_once()
    recorded = harness.turn_store.record_turn.call_args.args[0]
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

    harness.turn_store.record_turn.assert_not_called()


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
        caller_label="edit_regeneration_context",
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
