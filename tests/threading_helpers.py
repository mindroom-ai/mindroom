"""Shared helpers for the threading behavior test modules."""

from __future__ import annotations

import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest_asyncio
from nio.api import RelationshipType

from mindroom.bot import AgentBot
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.event_journal import (
    EventClass,
    EventKind,
    InboundEvent,
    ProjectedEvent,
    VisibleMessage,
)
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_history_result import thread_history_result as _thread_history_result_impl
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.sync_continuity_helpers import load_sync_checkpoint, save_sync_token

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from mindroom.matrix.thread_history_result import ThreadHistoryResult


def _load_sync_token_value(storage_path: Path, agent_name: str) -> str | None:
    checkpoint = load_sync_checkpoint(storage_path, agent_name)
    if checkpoint is None:
        return None
    return checkpoint.token


def _runtime_bound_config(config: Config, runtime_root: Path) -> Config:
    """Return a runtime-bound config for threading tests."""
    return bind_runtime_paths(config, test_runtime_paths(runtime_root))


def _message(
    *,
    event_id: str,
    body: str,
    sender: str = "@user:localhost",
    thread_id: str | None = None,
) -> ResolvedVisibleMessage:
    """Build one typed visible message matching what a real read returns.

    A message fetched as part of a thread carries that thread. The production
    read path has always populated it (`EventInfo.from_event(...).thread_id`),
    so a thread-history expectation that leaves it out asserts nothing about
    it -- which is invisible while a mock returns the very objects the test
    constructed.
    """
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=body,
        event_id=event_id,
        thread_id=thread_id,
    )


async def seed_thread_history(
    bot: AgentBot,
    *,
    room_id: str,
    thread_id: str | None,
    messages: Sequence[ResolvedVisibleMessage],
    hydrated: bool = True,
) -> None:
    """Put messages into the conversation projection a read will serve from.

    Writes straight to the journal store, because the projection is now the
    only thing a read serves from. Stubbing the reader instead would pin the
    projection's absence -- the test would pass whether or not anything ever
    reached it.

    Pass ``hydrated=False`` to seed content the projection holds without
    claiming it is all of it, which is what makes a dispatch read report
    itself degraded.
    """
    store = bot._journal_store.principal(bot._journal_principal_id)
    for ordinal, message in enumerate(messages, start=1):
        # `_message` builds expectations through `ResolvedVisibleMessage
        # .synthetic`, which stamps every one with timestamp 0. Seeding them
        # all at 0 makes the page order fall back to the event ID, which is
        # alphabetical rather than the order the conversation happened in.
        # Position in this list is that order, so it becomes the creation time
        # -- on the expectation objects too, since callers pass the very list
        # they then assert against.
        message.timestamp = ordinal
        # A thread root carries no `m.thread` relation of its own -- it becomes
        # a root only when someone replies to it -- so the journal records it
        # with no thread. Seeding it as a member of its own thread would make
        # relation resolution promote a plain room message into a thread.
        #
        # Only the journal column. The projection's own root handling is a
        # separate question, and page reads here still expect what they always
        # got.
        admitted_thread_id = None if message.event_id == thread_id else thread_id
        await store.admit(
            InboundEvent(
                event_id=message.event_id,
                room_id=room_id,
                thread_id=admitted_thread_id,
                kind=EventKind.MESSAGE,
                event_class=EventClass.ACTIONABLE,
                sender=message.sender,
                origin_server_ts=ordinal,
                source={"event_id": message.event_id, "content": dict(message.content)},
            ),
            ProjectedEvent(
                event_id=message.event_id,
                room_id=room_id,
                thread_id=thread_id,
                sender=message.sender,
                origin_server_ts=ordinal,
                content=dict(message.content),
                replaces_event_id=None,
                redacts_event_id=None,
            ),
        )
        await store.settle(message.event_id)
    if not hydrated:
        return
    # A strict read hydrates before it answers, and hydration talks to Matrix.
    # A bot under test has a mocked client, so without the marker every strict
    # read would try to fetch history from a mock and the turn would produce
    # nothing at all. Seeding a conversation means it is known, not merely
    # present.
    await store.install_hydrated_conversation(
        room_id=room_id,
        thread_id=thread_id,
        events=(),
        complete=True,
        expected_membership_epoch=await store.membership_epoch(room_id),
    )


async def seed_hydrated_conversation(
    bot: AgentBot,
    *,
    room_id: str,
    thread_id: str | None = None,
) -> None:
    """Record that a walk already ran for this conversation and reached the start of it.

    The counterpart of ``seed_unhydrated_room_event``. A dispatch-safe read can
    only call a conversation complete when hydration proves it, so a test whose
    subject is something else -- ingress ordering, command targeting -- has to
    say which conversations the bot already knows. Leaving the journal empty
    instead makes the read report itself degraded, which is honest but is not
    what those tests are about.
    """
    store = bot._journal_store.principal(bot._journal_principal_id)
    await store.install_hydrated_conversation(
        room_id=room_id,
        thread_id=thread_id,
        events=(),
        complete=True,
        expected_membership_epoch=await store.membership_epoch(room_id),
    )


async def seed_unhydrated_room_event(
    bot: AgentBot,
    *,
    room_id: str,
    event_id: str,
    body: str,
    sender: str = "@user:localhost",
    thread_id: str | None = None,
) -> None:
    """Admit one known room event without claiming its conversation is complete."""
    store = bot._journal_store.principal(bot._journal_principal_id)
    await store.admit(
        InboundEvent(
            event_id=event_id,
            room_id=room_id,
            thread_id=thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.CONTEXT_ONLY,
            sender=sender,
            origin_server_ts=1,
            source={"event_id": event_id, "content": {"body": body, "msgtype": "m.text"}},
        ),
        None,
    )


def thread_history_result(
    history: list[ResolvedVisibleMessage],
    *,
    is_full_history: bool,
    diagnostics: dict[str, str | int | float | bool] | None = None,
) -> ThreadHistoryResult:
    """Wrap history with hydration metadata for thread tests."""
    return _thread_history_result_impl(
        history,
        is_full_history=is_full_history,
        diagnostics=diagnostics,
    )


def _state_writer(bot: AgentBot) -> object:
    """Return the writer instance actually captured by the resolver."""
    return unwrap_extracted_collaborator(bot._conversation_state_writer)


def _make_client_mock(*, user_id: str = "@mindroom_general:localhost") -> AsyncMock:
    """Return one AsyncClient-shaped mock with sync-token support for bot tests."""
    client = make_matrix_client_mock(user_id=user_id)
    client.homeserver = "http://localhost:8008"
    return client


def _matrix_room(
    room_id: str = "!test:localhost",
    *,
    own_user_id: str = "@mindroom_general:localhost",
    name: str | None = None,
    members: tuple[str, ...] = (),
    members_synced: bool = True,
) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id=room_id, own_user_id=own_user_id)
    room.name = name
    for member_id in members:
        room.add_member(member_id, None, None)
    room.members_synced = members_synced
    return room


def _formatted_event_source(
    *,
    msgtype: str,
    event_id: str,
    body: str,
    sender: str,
    server_timestamp: int,
    room_id: str,
    thread_id: str | None,
    replacement_of: str | None,
    new_body: str | None,
    new_thread_id: str | None,
    extra_content: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return one `m.room.message` source for a msgtype that carries a body."""
    content: dict[str, object] = {
        "body": body,
        "msgtype": msgtype,
        **(extra_content or {}),
    }
    if replacement_of is not None:
        new_content: dict[str, object] = {
            "body": new_body or body.removeprefix("* ").strip() or body,
            "msgtype": msgtype,
            **(extra_content or {}),
        }
        if new_thread_id is not None:
            new_content["m.relates_to"] = {"rel_type": "m.thread", "event_id": new_thread_id}
        content["m.new_content"] = new_content
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": replacement_of}
    elif thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return {
        "content": content,
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": server_timestamp,
        "room_id": room_id,
        "type": "m.room.message",
    }


def _text_event(
    *,
    event_id: str,
    body: str,
    sender: str,
    server_timestamp: int,
    room_id: str = "!test:localhost",
    thread_id: str | None = None,
    replacement_of: str | None = None,
    new_body: str | None = None,
    new_thread_id: str | None = None,
) -> nio.RoomMessageText:
    """Build one Matrix text event with optional thread or edit relations."""
    return cast(
        "nio.RoomMessageText",
        nio.RoomMessageText.from_dict(
            _formatted_event_source(
                msgtype="m.text",
                event_id=event_id,
                body=body,
                sender=sender,
                server_timestamp=server_timestamp,
                room_id=room_id,
                thread_id=thread_id,
                replacement_of=replacement_of,
                new_body=new_body,
                new_thread_id=new_thread_id,
            ),
        ),
    )


def _emote_event(
    *,
    event_id: str,
    body: str,
    sender: str,
    server_timestamp: int,
    room_id: str = "!test:localhost",
    thread_id: str | None = None,
    replacement_of: str | None = None,
    new_body: str | None = None,
    new_thread_id: str | None = None,
) -> nio.RoomMessageEmote:
    """Build the same event as `_text_event`, sent as `/me`.

    Identical in every way a reader looks at except its msgtype, which is the
    point: the two are siblings under `RoomMessageFormatted` and every rule
    about visible messages has to treat them alike.
    """
    return cast(
        "nio.RoomMessageEmote",
        nio.RoomMessageEmote.from_dict(
            _formatted_event_source(
                msgtype="m.emote",
                event_id=event_id,
                body=body,
                sender=sender,
                server_timestamp=server_timestamp,
                room_id=room_id,
                thread_id=thread_id,
                replacement_of=replacement_of,
                new_body=new_body,
                new_thread_id=new_thread_id,
            ),
        ),
    )


_TEST_PICTURE: dict[str, object] = {
    "url": "mxc://localhost/picture",
    "info": {"mimetype": "image/png", "w": 8, "h": 8},
}


def _image_event(
    *,
    event_id: str,
    body: str,
    sender: str,
    server_timestamp: int,
    room_id: str = "!test:localhost",
    thread_id: str | None = None,
    replacement_of: str | None = None,
    new_body: str | None = None,
    new_thread_id: str | None = None,
) -> nio.RoomMessageImage:
    """Build the same event as `_text_event`, sent as a captioned picture.

    Parsed through nio's own msgtype dispatch rather than a named class, so a
    fixture cannot claim a parse the production reader would not get -- an
    `m.image` without a top-level `url` is a `BadEvent` to nio, and that is a
    real property of media replacements rather than a detail to fixture away.
    """
    event = nio.RoomMessage.parse_event(
        _formatted_event_source(
            msgtype="m.image",
            event_id=event_id,
            body=body,
            sender=sender,
            server_timestamp=server_timestamp,
            room_id=room_id,
            thread_id=thread_id,
            replacement_of=replacement_of,
            new_body=new_body,
            new_thread_id=new_thread_id,
            extra_content=dict(_TEST_PICTURE),
        ),
    )
    assert isinstance(event, nio.RoomMessageImage)
    return event


async def _event_iter(events: Sequence[nio.Event]) -> AsyncGenerator[nio.Event, None]:
    """Yield one concrete sequence as a Matrix relations iterator."""
    for event in events:
        yield event


def _make_room_get_event_response(event: nio.Event) -> nio.RoomGetEventResponse:
    """Wrap one nio event in a RoomGetEventResponse."""
    response = nio.RoomGetEventResponse()
    response.event = event
    return response


def _relations_client(
    *,
    root_event: nio.RoomMessageText,
    thread_events: Sequence[nio.Event],
    replacements_by_event_id: dict[str, Sequence[nio.Event]] | None = None,
    user_id: str = "@mindroom_general:localhost",
    next_batch: str = "s_test_token",
) -> AsyncMock:
    """Return one AsyncClient mock serving thread events through room history."""
    client = _make_client_mock(user_id=user_id)
    client.next_batch = next_batch
    replacement_map = replacements_by_event_id or {}

    def relation_events(event_id: str, rel_type: RelationshipType) -> Sequence[nio.Event]:
        if rel_type == RelationshipType.thread and event_id == root_event.event_id:
            return thread_events
        if rel_type == RelationshipType.replacement:
            return replacement_map.get(event_id, ())
        return ()

    client.room_get_event = AsyncMock(return_value=_make_room_get_event_response(root_event))

    def room_get_event_relations(
        _room_id: str,
        event_id: str,
        *,
        rel_type: RelationshipType,
        event_type: str | None = None,  # noqa: ARG001
        direction: nio.MessageDirection = nio.MessageDirection.back,  # noqa: ARG001
        limit: int | None = None,  # noqa: ARG001
        _event_type: str | None = None,
        _direction: nio.MessageDirection = nio.MessageDirection.back,
        _limit: int | None = None,
    ) -> AsyncGenerator[nio.Event, None]:
        return _event_iter(relation_events(event_id, rel_type))

    client.room_get_event_relations = MagicMock(side_effect=room_get_event_relations)
    room_scan_chunk = [
        *[event for events in replacement_map.values() for event in events],
        *thread_events,
        root_event,
    ]
    client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(room_id="!test:localhost", chunk=room_scan_chunk, start="", end=None),
    )
    return client


def _message_mutation_event_info(*, original_event_id: str = "$target:localhost") -> EventInfo:
    """Return one thread-affecting event info for direct mutation-helper tests."""
    return EventInfo.from_event(
        {
            "type": "m.room.message",
            "content": {
                "body": "* updated",
                "msgtype": "m.text",
                "m.new_content": {"body": "updated", "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
            },
        },
    )


class EmptyProjection:
    """A projection holding nothing, for tests that never point-look-up an event.

    Point lookups fall through to the homeserver on a projection miss, so a
    store that always misses gives these tests the client-only behavior they
    were written against. A test that means to prove something about the
    projection seeds a real store instead.
    """

    async def visible_message(self, *, room_id: str, logical_event_id: str) -> VisibleMessage | None:
        """Return nothing, so every point lookup reaches the homeserver."""
        del room_id, logical_event_id
        return None


def _conversation_runtime(*, client: nio.AsyncClient | None = None) -> BotRuntimeState:
    """Build one minimal live runtime state for conversation-read tests."""
    config = _conversation_runtime_config()
    return BotRuntimeState(
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        enable_streaming=True,
        orchestrator=None,
    )


def _conversation_runtime_config() -> Config:
    """Return one runtime-bound config for conversation-read tests."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp(prefix="mindroom-threading-runtime-")))
    return bind_runtime_paths(
        Config(agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])}),
        runtime_paths,
    )


def _save_certified_sync_token(
    bot: AgentBot,
    token: str,
) -> None:
    """Persist one certified sync token for bot lifecycle tests.

    Certified by the event journal: the token has to name the store that
    consumed the events it covers.
    """
    save_sync_token(
        bot.storage_path,
        bot.agent_name,
        token,
        store_generation=bot._sync_checkpoint_trust.store_generation,
    )


class ThreadingBehaviorTestBase:
    """Shared fixtures and helpers for the split TestThreadingBehavior modules."""

    @pytest_asyncio.fixture
    async def bot(self, tmp_path: Path) -> AsyncGenerator[AgentBot, None]:
        """Create an AgentBot for testing."""
        agent_user = AgentMatrixUser(
            user_id="@mindroom_general:localhost",
            password=TEST_PASSWORD,
            display_name="GeneralAgent",
            agent_name="general",
        )

        config = _runtime_bound_config(
            Config(
                agents={"general": AgentConfig(display_name="GeneralAgent", rooms=["!test:localhost"])},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,  # Disable streaming for simpler testing
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        wrap_extracted_collaborators(bot)

        # Mock the orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        mock_orchestrator.handle_bot_ready = AsyncMock()
        mock_orchestrator.send_approval_notice = AsyncMock()
        bot.orchestrator = mock_orchestrator

        # Create a mock client
        bot.client = _make_client_mock(user_id="@mindroom_general:localhost")
        # Sync checkpoints are certified by the event journal. Pinned so a test
        # that saves one and restarts exercises the token logic rather than the
        # first-open mint, which would rightly reject it.
        bot._sync_checkpoint_trust.store_generation = "test-store-generation"

        # Initialize components that depend on client

        # Mock the agent to return a response
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "I can help you with that!"

        # Make the agent's arun method return the response
        async def mock_arun(*_args: object, **_kwargs: object) -> MagicMock:
            return mock_response

        mock_agent.arun = mock_arun

        # Mock create_agent to return our mock agent
        with patch("mindroom.bot.create_agent", return_value=mock_agent):
            yield bot

        # No cleanup needed since we're using mocks

    @staticmethod
    def _sync_response(joined_rooms: dict[str, nio.RoomInfo]) -> nio.SyncResponse:
        return nio.SyncResponse(
            next_batch="",
            rooms=nio.Rooms(invite={}, join=joined_rooms, leave={}),
            device_key_count=nio.DeviceOneTimeKeyCount(
                curve25519=None,
                signed_curve25519=None,
            ),
            device_list=nio.DeviceList(changed=[], left=[]),
            to_device_events=[],
            presence_events=[],
        )

    async def _run_sync_response_without_startup_side_effects(
        self,
        bot: AgentBot,
        sync_response: nio.SyncResponse,
    ) -> None:
        if bot.client is not None and not sync_response.next_batch:
            sync_response.next_batch = bot.client.next_batch
        orchestrator = bot.orchestrator
        bot_ready_context = (
            patch.object(orchestrator, "handle_bot_ready", AsyncMock()) if orchestrator is not None else nullcontext()
        )
        with (
            patch.object(bot, "_emit_agent_lifecycle_event", AsyncMock()),
            patch.object(bot, "_maybe_start_deferred_overdue_task_drain"),
            bot_ready_context,
        ):
            await bot._on_sync_response(sync_response)
