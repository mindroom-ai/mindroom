"""Direct unit suite for the EditRegenerator edited-message replay workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom import reply_lifecycle as rl
from mindroom.coalescing.batch import tagged_coalesced_prompt
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.conversation_resolver import ConversationResolver, MessageContext
from mindroom.dispatch_source import EDIT_SOURCE_KIND
from mindroom.edit_regenerator import EditRegenerator, EditRegeneratorDeps
from mindroom.entity_resolution import persisted_bot_user_ids
from mindroom.history.types import HistoryScope
from mindroom.hooks.ingress import HookIngressPolicy, IngressHookRunner
from mindroom.matrix.event_info import EventInfo
from mindroom.message_target import MessageTarget
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_sources import Owned, Unclaimed, UnclaimedReason
from mindroom.teams.formation import TeamIntent, TeamMemberStatus, TeamOutcome, TeamResolution, TeamResolutionMember
from mindroom.teams.spec import TeamMode
from mindroom.timestamp_formatting import format_timestamp_ms
from mindroom.turn_policy import ResponseAction
from mindroom.turn_record import SourceEventMetadata, TurnRecord, canonicalize_turn_record
from mindroom.turn_store import TurnStore
from tests.conftest import make_ingress_facts, make_visible_message, request_envelope
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.reply_turn.request import ResponseRequest
    from mindroom.response_runner import ResponseRunner
    from mindroom.response_sources import SourceFate
    from mindroom.turn_policy import TurnPolicy

AGENT_NAME = "assistant"
MEMBER_NAME = "member"
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
class _Runner:
    """ResponseRunner stand-in: each entry claims the reply under the conversation lock, then records its call.

    Each ``*_entry`` mock returns the event its reply's records name as the answer, or None when they name none; the
    runner methods return ``Owned()``, as a claimed reply owns its edit either way.
    """

    response_lock: asyncio.Lock
    # The newest reply answering the edited turn's sources, which an answer replaces.
    reply_for_sources: AsyncMock
    generate_response_entry: AsyncMock
    team_entry: AsyncMock = field(default_factory=lambda: AsyncMock(return_value=NEW_RESPONSE_EVENT_ID))
    empty_prompt_entry: AsyncMock = field(default_factory=lambda: AsyncMock(return_value=None))
    rejection_entry: AsyncMock = field(default_factory=lambda: AsyncMock(return_value=NEW_RESPONSE_EVENT_ID))
    regenerations: list[asyncio.Task[None]] = field(default_factory=list)

    async def _claimed(self, entry: AsyncMock, request: ResponseRequest, **kwargs: object) -> SourceFate:
        async with self.response_lock:
            # As the runner does once the regeneration claims its reply, before it runs.
            assert request.on_reply_claimed is not None
            await request.on_reply_claimed()
            event_id = await entry(request, **kwargs)
            if event_id is not None:
                # The reply records own the answer from then on, which is what a later edit regenerates.
                self.reply_for_sources.return_value = _reply(event_id=event_id)
            # The claimed reply's records own the edit, whether or not they answered it.
            return Owned()

    async def generate_response(self, request: ResponseRequest) -> SourceFate:
        return await self._claimed(self.generate_response_entry, request)

    async def generate_team_response_helper(self, request: ResponseRequest, **kwargs: object) -> SourceFate:
        return await self._claimed(self.team_entry, request, **kwargs)

    async def generate_response_for_empty_prompt(self, request: ResponseRequest, **kwargs: object) -> SourceFate:
        return await self._claimed(self.empty_prompt_entry, request, **kwargs)

    async def deliver_team_rejection(self, request: ResponseRequest, **kwargs: object) -> SourceFate:
        return await self._claimed(self.rejection_entry, request, **kwargs)

    @property
    def inbox(self) -> _Runner:
        """The inbox the regeneration runs on, which this stand-in plays itself."""
        return self

    def track_inbox_response(self, response: Coroutine[Any, Any, None], **_ownership: object) -> asyncio.Task[None]:
        task = asyncio.create_task(response)
        self.regenerations.append(task)
        return task


@dataclass
class _Policy:
    """TurnPolicy stand-in whose configured-team decision a test sets; an agent answers as itself."""

    configured_team_action: ResponseAction | None = None

    def effective_response_action(self, action: ResponseAction) -> ResponseAction:
        return self.configured_team_action or action


@dataclass
class _Harness:
    """One fully wired EditRegenerator with mockable collaborators."""

    regenerator: EditRegenerator
    resolver: MagicMock
    turn_store: MagicMock
    ingress_hook_runner: MagicMock
    generate_response: AsyncMock
    stop_reply: AsyncMock
    # The newest reply answering the edited turn's sources, from the reply records.
    reply_for_sources: AsyncMock
    later_message_exists: AsyncMock
    settle_sources: AsyncMock
    # The conversation lock the harness's regenerations take before they claim.
    response_lock: asyncio.Lock
    # The regenerations the regenerator started, which a test awaits before it checks their effects.
    regenerations: list[asyncio.Task[None]]
    # Sources of the turn the store reads as deleted.
    deleted: set[str]
    runner: _Runner
    policy: _Policy
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
    response_event_id: str | None = RESPONSE_EVENT_ID,
    source_event_prompts: dict[str, str] | None = None,
    source_event_metadata: dict[str, SourceEventMetadata] | None = None,
    response_owner: str | None = AGENT_NAME,
    requester_id: str | None = USER_ID,
    thread_id: str | None = THREAD_ID,
) -> TurnRecord:
    anchor = source_event_ids[-1]
    return TurnRecord(
        anchor_event_id=anchor,
        source_event_ids=source_event_ids,
        discovery_event_ids=discovery_event_ids,
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
    deleted: tuple[str, ...] = (),
    team: bool = False,
) -> _Harness:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    config = (
        Config(
            agents={MEMBER_NAME: AgentConfig(display_name="Member")},
            teams={AGENT_NAME: TeamConfig(display_name="Assistant", role="Answer together", agents=[MEMBER_NAME])},
        )
        if team
        else Config(agents={AGENT_NAME: AgentConfig(display_name="Assistant")})
    )
    entity_ids(config, runtime_paths)

    context = _message_context()
    resolver = MagicMock(spec=ConversationResolver)
    resolver.extract_message_context.return_value = context
    resolver.build_envelope = MagicMock(
        side_effect=lambda *, event, facts, target, body, **_kwargs: replace(
            request_envelope(
                target=target,
                prompt=body,
                user_id=facts.run_requester_id,
                agent_name=AGENT_NAME,
                source_kind=facts.source_kind,
            ),
            source_event_id=event.event_id,
        ),
    )

    turn_store = MagicMock(spec=TurnStore)
    current_turn_record = [turn_record]
    deleted_sources = set(deleted)
    turn_store.get_turn_record_after_writes.side_effect = lambda _original_event_id: current_turn_record[0]
    turn_store.get_turn_record.side_effect = lambda _event_id: current_turn_record[0]
    turn_store.deleted_sources.side_effect = lambda _record: frozenset(deleted_sources)
    turn_store.register_edit_revision.side_effect = lambda _record, _event_id, _revision: current_turn_record[0]
    turn_store.redacted_event_ids.return_value = frozenset()

    def record_turn(record: TurnRecord, **_kwargs: object) -> None:
        current_turn_record[0] = record

    turn_store.record_turn.side_effect = record_turn
    turn_store.record_responded_turn.side_effect = record_turn
    turn_store.record_edit.side_effect = record_turn
    turn_store.build_run_metadata.return_value = dict(RUN_METADATA)
    turn_store.prepare_edit_snapshot.return_value = False

    ingress_hook_runner = MagicMock(spec=IngressHookRunner)
    ingress_hook_runner.emit_message_received_hooks.return_value = False

    generate_response = AsyncMock(return_value=NEW_RESPONSE_EVENT_ID)
    reply_for_sources = AsyncMock(return_value=_reply(event_id=RESPONSE_EVENT_ID))
    store = MagicMock()
    store.replies.for_sources = reply_for_sources
    store.later_message_exists = AsyncMock(return_value=False)
    response_lock = asyncio.Lock()

    runner = _Runner(
        response_lock=response_lock,
        reply_for_sources=reply_for_sources,
        generate_response_entry=generate_response,
    )
    policy = _Policy()

    regenerator = EditRegenerator(
        EditRegeneratorDeps(
            runtime=_RuntimeStub(client=AsyncMock(spec=nio.AsyncClient), config=config),
            runtime_paths=runtime_paths,
            agent_name=AGENT_NAME,
            resolver=resolver,
            turn_store=turn_store,
            ingress_hook_runner=ingress_hook_runner,
            response_runner=cast("ResponseRunner", runner),
            turn_policy=cast("TurnPolicy", policy),
            settle_sources=AsyncMock(),
            stop_reply=AsyncMock(),
            receipt_order=AsyncMock(return_value=receipt_order),
            timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(timestamp_ms, timezone=config.timezone),
            store=store,
        ),
    )
    return _Harness(
        regenerator=regenerator,
        resolver=resolver,
        turn_store=turn_store,
        ingress_hook_runner=ingress_hook_runner,
        generate_response=generate_response,
        stop_reply=regenerator.deps.stop_reply,  # type: ignore[arg-type]
        reply_for_sources=reply_for_sources,
        later_message_exists=store.later_message_exists,
        settle_sources=regenerator.deps.settle_sources,  # type: ignore[arg-type]
        response_lock=response_lock,
        regenerations=runner.regenerations,
        runner=runner,
        policy=policy,
        deleted=deleted_sources,
        config=config,
        runtime_paths=runtime_paths,
        room=nio.MatrixRoom(room_id=ROOM_ID, own_user_id=f"@{AGENT_NAME}:example.org"),
        context=context,
    )


async def _handle_edit(harness: _Harness, event: nio.RoomMessageText, event_info: EventInfo) -> bool | None:
    handed_off = await harness.regenerator.handle_message_edit(
        harness.room,
        event,
        event_info,
        make_ingress_facts(event.sender, source_kind=EDIT_SOURCE_KIND, requester_user_id=USER_ID),
    )
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

    envelope_kwargs = harness.resolver.build_envelope.call_args.kwargs
    assert envelope_kwargs["body"] == "what is 3+3?"
    assert envelope_kwargs["facts"].source_kind == EDIT_SOURCE_KIND
    assert envelope_kwargs["target"] == record.conversation_target
    assert envelope_kwargs["facts"].run_requester_id == USER_ID

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
    assert (await prepare(request.thread_history)).invalid == ()
    harness.turn_store.remove_stale_runs_for_edit.assert_not_called()
    await request.on_reply_claimed()
    assert (await prepare(request.thread_history)).invalid == ()
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

    handled_turn = harness.turn_store.build_run_metadata.call_args.args[0]
    assert handled_turn.source_event_ids == (first_event_id, second_event_id)
    assert handled_turn.source_event_prompts == {
        first_event_id: "edited first message",
        second_event_id: "second message",
    }

    recorded = harness.turn_store.record_edit.call_args.args[0]
    assert recorded.response_event_id == NEW_RESPONSE_EVENT_ID
    assert recorded.source_event_prompts == {
        first_event_id: "edited first message",
        second_event_id: "second message",
    }


@pytest.mark.asyncio
async def test_coalesced_sibling_edit_excludes_redacted_source_prompt(tmp_path: Path) -> None:
    """Editing a sibling rebuilds without the deleted member's text, and the lock rechecks only what it ran with."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    record = _turn_record(
        source_event_ids=(first_event_id, second_event_id),
        source_event_prompts={second_event_id: "second message"},
        source_event_metadata=_source_metadata(first_event_id, second_event_id),
    )
    harness = _harness(tmp_path, turn_record=record, deleted=(first_event_id,))
    event, event_info = _edit_event(original_event_id=second_event_id, new_body="edited second message")

    await _handle_edit(harness, event, event_info)

    request = harness.generate_response.await_args.args[0]
    assert request.prompt == _tagged_prompt(
        (second_event_id,),
        {second_event_id: "edited second message"},
    )
    assert request.prepare_source_turn is not None
    assert (await request.prepare_source_turn(request.thread_history)).invalid == ()
    assert harness.turn_store.prepare_edit_snapshot.await_args.kwargs["live_source_event_ids"] == (second_event_id,)
    handled_turn = harness.turn_store.build_run_metadata.call_args.args[0]
    assert handled_turn.source_event_prompts == {second_event_id: "edited second message"}


@pytest.mark.asyncio
async def test_coalesced_edit_rechecks_every_snapshotted_source_under_lock(tmp_path: Path) -> None:
    """A sibling deleted after prompt assembly suppresses the stale coalesced prompt; nothing runs with it."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    record = _turn_record(
        source_event_ids=(first_event_id, second_event_id),
        source_event_prompts={first_event_id: "first message", second_event_id: "second message"},
        source_event_metadata=_source_metadata(first_event_id, second_event_id),
    )
    harness = _harness(tmp_path, turn_record=record)

    async def prepare_edit_snapshot(*, live_source_event_ids: tuple[str, ...], **_kwargs: object) -> bool:
        # The sibling is deleted while the regeneration waited for the conversation.
        harness.deleted.add(first_event_id)
        return first_event_id in live_source_event_ids

    async def generate(request: ResponseRequest) -> str | None:
        assert request.prepare_source_turn is not None
        preparation = await request.prepare_source_turn(request.thread_history)
        return None if preparation.invalid else NEW_RESPONSE_EVENT_ID

    harness.turn_store.prepare_edit_snapshot.side_effect = prepare_edit_snapshot
    harness.generate_response.side_effect = generate
    event, event_info = _edit_event(original_event_id=second_event_id, new_body="edited second message")

    await _handle_edit(harness, event, event_info)

    assert [call.args[0].prompt for call in harness.generate_response.await_args_list] == [
        _tagged_prompt(
            (first_event_id, second_event_id),
            {first_event_id: "first message", second_event_id: "edited second message"},
        ),
    ]
    assert harness.turn_store.prepare_edit_snapshot.await_count == 1


def _reply(
    *,
    event_id: str | None,
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


def _not_regenerated_reasons(logs: list[dict[str, Any]]) -> list[object]:
    return [entry.get("reason") for entry in logs if entry["event"] == "edit_not_regenerated"]


@pytest.mark.asyncio
async def test_edit_regenerates_the_answer_its_reply_records_name(tmp_path: Path) -> None:
    """After a regeneration answered in a new reply, the next edit regenerates that reply, not the turn's old answer."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.reply_for_sources.return_value = _reply(event_id="$regenerated:example.org")
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    harness.reply_for_sources.assert_awaited_once_with(record.source_event_ids)
    request = harness.generate_response.await_args.args[0]
    assert request.existing_event_id == "$regenerated:example.org"
    assert (await request.prepare_source_turn(request.thread_history)).invalid == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_mode", ["thread", "room"])
async def test_an_edit_of_a_message_someone_answered_after_is_ignored(tmp_path: Path, thread_mode: str) -> None:
    """Later turns built on the old text, so editing an earlier message regenerates nothing."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.resolver.room_is_one_conversation.return_value = thread_mode == "room"
    harness.later_message_exists.return_value = True
    event, event_info = _edit_event()

    with capture_logs() as logs:
        assert await _handle_edit(harness, event, event_info) is None

    harness.later_message_exists.assert_awaited_once_with(
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        source_event_ids=record.source_event_ids,
        excluded_senders=persisted_bot_user_ids(harness.runtime_paths),
        whole_room=thread_mode == "room",
    )
    _assert_no_regeneration(harness)
    harness.turn_store.register_edit_revision.assert_not_called()
    assert _not_regenerated_reasons(logs) == ["later_human_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("running", "existing_event_id"),
    [
        # Stopping a reply an approval holds cancels its approval.
        pytest.param(
            replace(_reply(event_id=RESPONSE_EVENT_ID, state=rl.ReplyState.PAUSED), approval_id="approval-1"),
            RESPONSE_EVENT_ID,
            id="approval-held",
        ),
        pytest.param(
            replace(_reply(event_id=RESPONSE_EVENT_ID, state=rl.ReplyState.ACTIVE), current_span_id="span-running"),
            RESPONSE_EVENT_ID,
            id="streaming",
        ),
        # A reply whose first message Matrix has not acknowledged yet may already show; its claim reads the event
        # the create bound from the reply's records.
        pytest.param(
            replace(
                _reply(event_id=None, state=rl.ReplyState.ACTIVE),
                current_span_id="span-running",
                possibly_shown="Thinking...",
            ),
            None,
            id="unacknowledged-create",
        ),
    ],
)
async def test_an_edit_of_a_reply_that_still_runs_stops_it_and_regenerates_in_place(
    tmp_path: Path,
    running: rl.Reply,
    existing_event_id: str | None,
) -> None:
    """The edit stops the reply that still runs or waits for an approval; the regeneration takes its place."""
    harness = _harness(tmp_path, turn_record=_turn_record(), receipt_order=9)
    harness.reply_for_sources.return_value = running
    event, event_info = _edit_event(new_body="what is 4+4?")

    assert await _handle_edit(harness, event, event_info) is True

    harness.stop_reply.assert_awaited_once_with(running, 9)
    request = harness.generate_response.await_args.args[0]
    assert request.prompt == "what is 4+4?"
    assert request.existing_event_id == existing_event_id


@pytest.mark.asyncio
async def test_an_edit_of_a_reply_that_recorded_nothing_to_show_is_ignored(tmp_path: Path) -> None:
    """A turn that has not sent anything yet, as a participation turn may not have, answers the original text.

    Stopping it would post a cancel note where nothing showed, and its
    regeneration would answer without that participation decision.
    """
    harness = _harness(tmp_path, turn_record=_turn_record(), receipt_order=9)
    deciding = replace(_reply(event_id=None, state=rl.ReplyState.ACTIVE), current_span_id="span-running")
    harness.reply_for_sources.return_value = deciding
    event, event_info = _edit_event()

    with capture_logs() as logs:
        assert await _handle_edit(harness, event, event_info) is None

    harness.stop_reply.assert_not_awaited()
    _assert_no_regeneration(harness)
    assert _not_regenerated_reasons(logs) == ["reply_not_shown"]


@pytest.mark.asyncio
async def test_an_edit_whose_regeneration_runs_nothing_is_settled_by_it(tmp_path: Path) -> None:
    """A regeneration that opens no span settles the edit the room's lane handed it."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    event, event_info = _edit_event()

    # A refusal that runs nothing leaves the edit unclaimed.
    harness.runner.generate_response = AsyncMock(return_value=Unclaimed(UnclaimedReason.NOTHING_TO_RUN))  # type: ignore[method-assign]

    assert await _handle_edit(harness, event, event_info) is True
    harness.settle_sources.assert_awaited_once_with((event.event_id,))


@pytest.mark.asyncio
async def test_an_edit_whose_regeneration_a_replaced_runtime_refuses_stays_pending(tmp_path: Path) -> None:
    """A runtime being replaced refuses the regeneration without settling the edit, so the replacement answers it."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    event, event_info = _edit_event()
    harness.runner.generate_response = AsyncMock(side_effect=ResponseAdmissionRefusedError)  # type: ignore[method-assign]

    assert (
        await harness.regenerator.handle_message_edit(
            harness.room,
            event,
            event_info,
            make_ingress_facts(event.sender, source_kind=EDIT_SOURCE_KIND, requester_user_id=USER_ID),
        )
        is True
    )
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
        assert (
            await harness.regenerator.handle_message_edit(
                harness.room,
                event,
                event_info,
                make_ingress_facts(event.sender, source_kind=EDIT_SOURCE_KIND, requester_user_id=USER_ID),
            )
            is True
        )
        harness.generate_response.assert_not_awaited()
    await asyncio.gather(*harness.regenerations)

    harness.generate_response.assert_awaited_once()
    harness.turn_store.record_edit.assert_called_once()
    harness.settle_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_of_a_turn_whose_reply_is_gone_is_ignored(tmp_path: Path) -> None:
    """A reply whose answer is gone has nothing for an edit to regenerate."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    harness.reply_for_sources.return_value = _reply(
        event_id="$removed:example.org",
        state=rl.ReplyState.GONE,
    )
    event, event_info = _edit_event()

    with capture_logs() as logs:
        await _handle_edit(harness, event, event_info)

    harness.generate_response.assert_not_awaited()
    assert _not_regenerated_reasons(logs) == ["gone"]


@pytest.mark.asyncio
async def test_edit_of_redacted_coalesced_source_is_ignored(tmp_path: Path) -> None:
    """A later edit cannot reintroduce a source a redaction deleted."""
    first_event_id = "$m1:example.org"
    second_event_id = "$m2:example.org"
    record = _turn_record(
        source_event_ids=(first_event_id, second_event_id),
        source_event_prompts={second_event_id: "second message"},
        source_event_metadata=_source_metadata(first_event_id, second_event_id),
    )
    harness = _harness(tmp_path, turn_record=record, deleted=(first_event_id,))
    event, event_info = _edit_event(original_event_id=first_event_id, new_body="restore secret")

    await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)


@pytest.mark.asyncio
async def test_edit_request_rechecks_redaction_after_acquiring_response_lock(tmp_path: Path) -> None:
    """A redaction that wins the lifecycle lock race suppresses the stale regeneration."""
    record = _turn_record()
    harness = _harness(tmp_path, turn_record=record)
    harness.turn_store.prepare_edit_snapshot.return_value = True
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    request = harness.generate_response.await_args.args[0]
    assert request.prepare_source_turn is not None
    assert (await request.prepare_source_turn(request.thread_history)).invalid == (ORIGINAL_EVENT_ID,)
    snapshot = harness.turn_store.prepare_edit_snapshot.await_args.kwargs
    assert snapshot["record"].conversation_target == record.conversation_target
    assert snapshot["live_source_event_ids"] == (ORIGINAL_EVENT_ID,)


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
    await harness.regenerator.handle_message_edit(
        harness.room,
        event,
        event_info,
        make_ingress_facts(event.sender, source_kind=EDIT_SOURCE_KIND, requester_user_id=event.sender),
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
    event, event_info = _edit_event(
        original_event_id=original_event_id,
        sender=sender,
    )

    await harness.regenerator.handle_message_edit(
        harness.room,
        event,
        event_info,
        make_ingress_facts(event.sender, source_kind=EDIT_SOURCE_KIND, requester_user_id=sender),
    )
    await asyncio.gather(*harness.regenerations)

    _assert_no_regeneration(harness)
    harness.resolver.build_envelope.assert_not_called()


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
    await harness.regenerator.handle_message_edit(
        harness.room,
        event,
        event_info,
        make_ingress_facts(event.sender, source_kind=EDIT_SOURCE_KIND, requester_user_id=event.sender),
    )
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

    await harness.regenerator.handle_message_edit(
        harness.room,
        event,
        event_info,
        make_ingress_facts(event.sender, source_kind=EDIT_SOURCE_KIND, requester_user_id=event.sender),
    )
    await asyncio.gather(*harness.regenerations)

    _assert_no_regeneration(harness)
    harness.resolver.build_envelope.assert_not_called()


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
    # Keyed by the relay's physical source, as the turn rows store a selection.
    assert recorded.source_event_revisions == {first_relay: (event.server_timestamp, event.event_id)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_event_prompts",
    [None, {"$m1:example.org": "first message"}],
    ids=("absent", "incomplete"),
)
async def test_coalesced_edit_without_every_persisted_prompt_is_skipped(
    tmp_path: Path,
    source_event_prompts: dict[str, str] | None,
) -> None:
    """A coalesced turn whose prompt map is absent or misses a member cannot be rebuilt and is skipped."""
    record = _turn_record(
        source_event_ids=("$m1:example.org", "$m2:example.org"),
        source_event_prompts=source_event_prompts,
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
    harness.turn_store.get_turn_record_after_writes.assert_not_called()
    _assert_no_regeneration(harness)


@pytest.mark.asyncio
async def test_edit_without_turn_record_returns_early(tmp_path: Path) -> None:
    """An edit with no durable turn record does nothing else."""
    harness = _harness(tmp_path, turn_record=None)
    event, event_info = _edit_event()

    with capture_logs() as logs:
        await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)
    harness.resolver.build_envelope.assert_not_called()
    # As for a message still waiting for media or voice before it reaches its turn.
    assert _not_regenerated_reasons(logs) == ["no_turn_record"]


@pytest.mark.asyncio
async def test_replaying_a_committed_edit_of_a_relayed_message_runs_no_hooks_again(tmp_path: Path) -> None:
    """An edit of the human message a relay answered is committed on the relay's source, so its replay is no new text."""
    relay_event_id = "$relay:example.org"
    record = canonicalize_turn_record(
        _turn_record(
            source_event_ids=(relay_event_id,),
            discovery_event_ids=(ORIGINAL_EVENT_ID,),
            source_event_metadata={
                relay_event_id: SourceEventMetadata(sender=USER_ID, discovery_event_id=ORIGINAL_EVENT_ID),
            },
        ),
        # As the turn rows store a committed edit: keyed by the physical source it edits.
        source_event_revisions={relay_event_id: (1_000_001, EDIT_EVENT_ID)},
    )
    harness = _harness(tmp_path, turn_record=record)
    event, event_info = _edit_event(original_event_id=ORIGINAL_EVENT_ID, server_timestamp=1_000_001)

    await _handle_edit(harness, event, event_info)

    harness.ingress_hook_runner.emit_message_received_hooks.assert_not_awaited()


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

    await harness.regenerator.handle_message_edit(
        harness.room,
        event,
        event_info,
        make_ingress_facts(event.sender, source_kind=EDIT_SOURCE_KIND, requester_user_id=attacker_id),
    )
    await asyncio.gather(*harness.regenerations)

    _assert_no_regeneration(harness)
    harness.resolver.build_envelope.assert_not_called()


@pytest.mark.asyncio
async def test_an_edit_another_sender_made_for_the_requester_is_ignored(tmp_path: Path) -> None:
    """Another sender's edit that runs for the requester, as in a contact room, never revises the requester's message."""
    harness = _harness(tmp_path, turn_record=_turn_record())
    counterparty = "@counterparty:example.org"
    event, event_info = _edit_event(sender=counterparty)

    with capture_logs() as logs:
        handed_off = await harness.regenerator.handle_message_edit(
            harness.room,
            event,
            event_info,
            make_ingress_facts(
                event.sender,
                source_kind=EDIT_SOURCE_KIND,
                requester_user_id=USER_ID,
                acting_requester_id=USER_ID,
            ),
        )
    await asyncio.gather(*harness.regenerations)

    assert handed_off is None
    assert [log["reason"] for log in logs if log["event"] == "edit_not_regenerated"] == ["edited_by_another_sender"]
    _assert_no_regeneration(harness)
    harness.resolver.build_envelope.assert_not_called()


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
    harness.resolver.build_envelope.assert_not_called()


@pytest.mark.asyncio
async def test_edit_without_previous_response_event_is_skipped(tmp_path: Path) -> None:
    """A turn whose answer has no reply record, as one from before the upgrade, regenerates nothing."""
    record = _turn_record(response_event_id=None)
    harness = _harness(tmp_path, turn_record=record)
    harness.reply_for_sources.return_value = None
    event, event_info = _edit_event()

    with capture_logs() as logs:
        await _handle_edit(harness, event, event_info)

    _assert_no_regeneration(harness)
    assert _not_regenerated_reasons(logs) == ["no_reply"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded_thread_id", [THREAD_ID, None])
async def test_edit_context_is_read_in_the_recorded_thread(tmp_path: Path, recorded_thread_id: str | None) -> None:
    """An edit regenerates in the thread its turn was recorded in, never one resolved from the edit again."""
    record = _turn_record(thread_id=recorded_thread_id)
    harness = _harness(tmp_path, turn_record=record)
    recorded_context = _message_context(thread_id=recorded_thread_id)
    harness.resolver.extract_message_context.return_value = recorded_context
    event, event_info = _edit_event()

    await _handle_edit(harness, event, event_info)

    harness.resolver.extract_message_context.assert_awaited_once_with(
        harness.room,
        event,
        thread_id=recorded_thread_id,
    )
    harness.resolver.fetch_thread_history.assert_not_awaited()
    assert harness.generate_response.await_args.args[0].thread_history == recorded_context.thread_history


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


def _configured_team_action(harness: _Harness, outcome: TeamOutcome) -> ResponseAction:
    """Return the action the configured team ``AGENT_NAME`` takes now: answering, or rejected because its member is not."""
    member = TeamResolutionMember(
        agent=entity_ids(harness.config, harness.runtime_paths)[MEMBER_NAME],
        name=MEMBER_NAME,
        status=TeamMemberStatus.ELIGIBLE if outcome is TeamOutcome.TEAM else TeamMemberStatus.NOT_MATERIALIZABLE,
    )
    resolution = TeamResolution(
        intent=TeamIntent.CONFIGURED_TEAM,
        requested_members=[member.agent],
        member_statuses=[member],
        outcome=outcome,
        reason=None if outcome is TeamOutcome.TEAM else "Team 'assistant' cannot answer: member is unavailable.",
        mode=TeamMode.COORDINATE if outcome is TeamOutcome.TEAM else None,
    )
    if outcome is TeamOutcome.TEAM:
        return ResponseAction(kind="team", form_team=resolution)
    return ResponseAction(kind="reject", form_team=resolution, rejection_message=resolution.reason)


@pytest.mark.asyncio
async def test_a_regeneration_runs_as_the_configured_team_answers_now(tmp_path: Path) -> None:
    """A configured team's regeneration runs the team its current policy forms, with that team's mode."""
    harness = _harness(tmp_path, turn_record=_turn_record(), team=True)
    harness.policy.configured_team_action = _configured_team_action(harness, TeamOutcome.TEAM)
    event, event_info = _edit_event()

    assert await _handle_edit(harness, event, event_info) is True

    harness.runner.team_entry.assert_awaited_once()
    assert harness.runner.team_entry.await_args.kwargs == {"member_names": (MEMBER_NAME,), "team_mode": "coordinate"}
    assert harness.runner.team_entry.await_args.args[0].existing_event_id == RESPONSE_EVENT_ID
    harness.generate_response.assert_not_awaited()
    harness.runner.rejection_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_rejected_team_regeneration_shows_why_in_the_reply_it_regenerates(tmp_path: Path) -> None:
    """A configured team that cannot answer now delivers its rejection as the final of the reply the edit regenerates."""
    harness = _harness(tmp_path, turn_record=_turn_record(), team=True)
    harness.policy.configured_team_action = _configured_team_action(harness, TeamOutcome.REJECT)
    event, event_info = _edit_event()

    assert await _handle_edit(harness, event, event_info) is True

    harness.runner.rejection_entry.assert_awaited_once()
    request = harness.runner.rejection_entry.await_args.args[0]
    assert request.existing_event_id == RESPONSE_EVENT_ID
    assert harness.runner.rejection_entry.await_args.kwargs == {
        "member_names": [MEMBER_NAME],
        "team_mode": "coordinate",
        "rejection_message": "Team 'assistant' cannot answer: member is unavailable.",
    }
    # The claimed reply answers the edited text from then on.
    harness.turn_store.record_edit.assert_called_once()
    harness.generate_response.assert_not_awaited()
    harness.runner.team_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True])
async def test_an_empty_edit_finalizes_its_reply_before_any_team_decision(tmp_path: Path, team: bool) -> None:
    """An edit that empties its message ends the reply it regenerates empty, even for a team that would be rejected."""
    harness = _harness(tmp_path, turn_record=_turn_record(), team=team)
    if team:
        harness.policy.configured_team_action = _configured_team_action(harness, TeamOutcome.REJECT)
    event, event_info = _edit_event(new_body="   ")

    assert await _handle_edit(harness, event, event_info) is True

    harness.runner.empty_prompt_entry.assert_awaited_once()
    assert harness.runner.empty_prompt_entry.await_args.args[0].existing_event_id == RESPONSE_EVENT_ID
    assert harness.runner.empty_prompt_entry.await_args.kwargs == {"response_kind": "team" if team else "ai"}
    harness.runner.rejection_entry.assert_not_awaited()
    harness.runner.team_entry.assert_not_awaited()
    harness.generate_response.assert_not_awaited()
