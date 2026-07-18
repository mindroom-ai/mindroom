"""Shared helpers for the threading behavior test modules."""

from __future__ import annotations

import asyncio
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest_asyncio
from nio.api import RelationshipType

import mindroom.matrix.cache as matrix_cache
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import STREAM_STATUS_KEY, STREAM_STATUS_STREAMING
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_history_result import thread_history_result as _thread_history_result_impl
from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.sync_tokens import load_sync_token_record, save_sync_token
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
)
from mindroom.matrix.users import AgentMatrixUser
from mindroom.runtime_support import (
    OwnedRuntimeSupport,
    StartupThreadPrewarmRegistry,
    close_owned_runtime_support,
    sync_owned_runtime_support,
)
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    make_event_cache_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
    from typing import Any

    from mindroom.matrix.cache import ThreadHistoryResult
    from mindroom.matrix.cache.event_cache import ThreadCacheState


async def _wait_for_room_cache_idle(coordinator: EventCacheWriteCoordinator) -> None:
    await wait_for_background_tasks(timeout=1.0, owner=coordinator.background_task_owner)


def _load_sync_token_value(storage_path: Path, agent_name: str) -> str | None:
    token_record = load_sync_token_record(storage_path, agent_name)
    if token_record is None:
        return None
    return token_record.token


def _runtime_bound_config(config: Config, runtime_root: Path) -> Config:
    """Return a runtime-bound config for threading tests."""
    return bind_runtime_paths(config, test_runtime_paths(runtime_root))


def _message(*, event_id: str, body: str, sender: str = "@user:localhost") -> ResolvedVisibleMessage:
    """Build one typed visible message for thread-history mocks."""
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=body,
        event_id=event_id,
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
    content: dict[str, object] = {
        "body": body,
        "msgtype": "m.text",
    }
    if replacement_of is not None:
        new_content: dict[str, object] = {
            "body": new_body or body.removeprefix("* ").strip() or body,
            "msgtype": "m.text",
        }
        if new_thread_id is not None:
            new_content["m.relates_to"] = {"rel_type": "m.thread", "event_id": new_thread_id}
        content["m.new_content"] = new_content
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": replacement_of}
    elif thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return cast(
        "nio.RoomMessageText",
        nio.RoomMessageText.from_dict(
            {
                "content": content,
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": server_timestamp,
                "room_id": room_id,
                "type": "m.room.message",
            },
        ),
    )


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


def _runtime_event_cache() -> AsyncMock:
    """Return a cache-shaped async mock for runtime-state tests."""
    return make_event_cache_mock()


def _runtime_write_coordinator() -> EventCacheWriteCoordinator:
    """Return one real coordinator for runtime-state tests."""
    return EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=object(),
    )


def _thread_mutation_cache_ops() -> tuple[ThreadMutationCacheOps, MagicMock, MagicMock]:
    """Return concrete thread cache ops backed by one async-mock event cache."""
    logger = MagicMock()
    event_cache = MagicMock()
    event_cache.append_event = AsyncMock(return_value=True)
    event_cache.disable = Mock()
    event_cache.invalidate_room_threads = AsyncMock()
    event_cache.invalidate_thread = AsyncMock()
    event_cache.mark_room_threads_stale = AsyncMock()
    event_cache.mark_thread_stale = AsyncMock()
    event_cache.redact_event = AsyncMock(return_value=True)
    event_cache.revalidate_thread_after_incremental_update = AsyncMock()
    runtime = MagicMock()
    runtime.event_cache = event_cache
    runtime.event_cache_write_coordinator = _runtime_write_coordinator()
    runtime.runtime_started_at = 1234567890.0
    return ThreadMutationCacheOps(logger_getter=lambda: logger, runtime=runtime), logger, event_cache


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


def _outbound_streaming_edit_content(
    *,
    body: str,
    original_event_id: str = "$stream-original:localhost",
    thread_id: str = "$thread:localhost",
    stream_status: str = STREAM_STATUS_STREAMING,
) -> dict[str, object]:
    """Return one outbound streaming edit content payload."""
    return {
        "body": f"* {body}",
        "msgtype": "m.text",
        "m.new_content": {
            "body": body,
            "msgtype": "m.text",
            STREAM_STATUS_KEY: stream_status,
            "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
    }


def _outbound_plain_edit_content(
    *,
    body: str,
    original_event_id: str = "$plain-original:localhost",
    thread_id: str = "$thread:localhost",
) -> dict[str, object]:
    """Return one outbound non-streaming edit content payload."""
    return {
        "body": f"* {body}",
        "msgtype": "m.text",
        "m.new_content": {
            "body": body,
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
    }


async def _reopen_event_cache(event_cache: SqliteEventCache) -> SqliteEventCache:
    """Close and reopen one SQLite cache against the same database file."""
    db_path = event_cache.db_path
    await event_cache.close()
    reopened_cache = SqliteEventCache(db_path)
    await reopened_cache.initialize()
    return reopened_cache


def _conversation_runtime(
    *,
    client: nio.AsyncClient | None = None,
    event_cache: SqliteEventCache | None = None,
    coordinator: EventCacheWriteCoordinator | None = None,
) -> BotRuntimeState:
    """Build one minimal live runtime state for conversation-cache tests."""
    config = _conversation_runtime_config()
    return BotRuntimeState(
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        enable_streaming=True,
        orchestrator=None,
        event_cache=event_cache or _runtime_event_cache(),
        event_cache_write_coordinator=coordinator or _runtime_write_coordinator(),
    )


def _conversation_runtime_config() -> Config:
    """Return one runtime-bound config for conversation-cache tests."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp(prefix="mindroom-threading-runtime-")))
    return bind_runtime_paths(
        Config(agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])}),
        runtime_paths,
    )


async def _assert_thread_read_guard_rejects_cache_when_unknown_live_mutation_races_fetch(  # noqa: PLR0915
    tmp_path: Path,
    *,
    read_thread: Callable[[MatrixConversationCache, str, str], Coroutine[Any, Any, ThreadHistoryResult]],
    force_refetch_reason: str,
    expected_full_history: bool,
) -> None:
    """Assert a blocked thread read does not validate cache after a racing UNKNOWN live mutation."""
    room_id = "!test:localhost"
    thread_id = "$thread:localhost"
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    root_event = _text_event(
        event_id=thread_id,
        body="Root",
        sender="@user:localhost",
        server_timestamp=1000,
        room_id=room_id,
    )
    old_reply = _text_event(
        event_id="$reply-old:localhost",
        body="Old reply",
        sender="@agent:localhost",
        server_timestamp=2000,
        room_id=room_id,
        thread_id=thread_id,
    )
    coordinator = _runtime_write_coordinator()
    client = _relations_client(
        root_event=root_event,
        thread_events=[old_reply],
        next_batch="s_initial",
    )
    cached_validated_at = time.time()
    await _replace_thread(
        event_cache,
        room_id,
        thread_id,
        [root_event.source, old_reply.source],
        validated_at=cached_validated_at,
    )
    await event_cache.mark_thread_stale(room_id, thread_id, reason=force_refetch_reason)
    room_messages_response = client.room_messages.return_value
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    room_invalidation_finished = asyncio.Event()
    thread_result: ThreadHistoryResult | None = None
    thread_state: ThreadCacheState | None = None
    live_task: asyncio.Task[None] | None = None

    async def blocking_room_messages(*_args: object, **_kwargs: object) -> nio.RoomMessagesResponse:
        fetch_started.set()
        await release_fetch.wait()
        return room_messages_response

    client.room_messages = AsyncMock(side_effect=blocking_room_messages)
    access = MatrixConversationCache(
        logger=MagicMock(),
        runtime=_conversation_runtime(
            client=client,
            event_cache=event_cache,
            coordinator=coordinator,
        ),
    )
    real_mark_room_threads_stale = event_cache.mark_room_threads_stale

    async def mark_room_threads_stale(room_id_arg: str, *, reason: str) -> None:
        assert room_id_arg == room_id
        assert reason == "live_thread_lookup_unavailable"
        await real_mark_room_threads_stale(room_id_arg, reason=reason)
        room_invalidation_finished.set()

    async def resolve_unknown_impact(*_args: object, **_kwargs: object) -> MutationThreadImpact:
        return MutationThreadImpact.unknown()

    event_cache.mark_room_threads_stale = AsyncMock(side_effect=mark_room_threads_stale)
    access._live._resolver.resolve_thread_impact_for_mutation = AsyncMock(side_effect=resolve_unknown_impact)
    unknown_event = _text_event(
        event_id="$unknown-edit:localhost",
        body="* Updated",
        sender="@agent:localhost",
        server_timestamp=3000,
        room_id=room_id,
        replacement_of="$missing:localhost",
        new_body="Updated",
    )
    read_task = asyncio.create_task(read_thread(access, room_id, thread_id))

    try:
        await asyncio.wait_for(fetch_started.wait(), timeout=1.0)
        await asyncio.sleep(0.01)
        live_task = asyncio.create_task(
            access.append_live_event(
                room_id,
                unknown_event,
                event_info=EventInfo.from_event(unknown_event.source),
            ),
        )
        await asyncio.wait_for(room_invalidation_finished.wait(), timeout=1.0)

        release_fetch.set()
        thread_result = await asyncio.wait_for(read_task, timeout=1.0)
        await asyncio.wait_for(live_task, timeout=1.0)
        await _wait_for_room_cache_idle(coordinator)
        thread_state = await event_cache.get_thread_cache_state(room_id, thread_id)
    finally:
        release_fetch.set()
        await asyncio.wait_for(
            asyncio.gather(
                read_task,
                *(task for task in [live_task] if task is not None),
                return_exceptions=True,
            ),
            timeout=1.0,
        )
        await _wait_for_room_cache_idle(coordinator)
        await event_cache.close()

    assert thread_result is not None
    assert thread_result.is_full_history is expected_full_history
    assert thread_result.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_HOMESERVER
    assert [message.body for message in thread_result] == ["Root", "Old reply"]
    assert thread_state is not None
    assert thread_state.validated_at is not None
    assert thread_state.room_invalidated_at is not None
    assert thread_state.room_invalidated_at > thread_state.validated_at
    assert matrix_cache.thread_cache_rejection_reason(thread_state) is not None
    client.room_messages.assert_awaited_once()


def _install_runtime_write_coordinator(bot: AgentBot) -> EventCacheWriteCoordinator:
    """Attach one explicit runtime write coordinator to a bot test double."""
    coordinator = EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=bot._runtime_view,
    )
    bot.event_cache_write_coordinator = coordinator
    return coordinator


async def _bind_owned_runtime_support(
    bot: AgentBot,
    *,
    db_path: Path | None = None,
) -> OwnedRuntimeSupport:
    """Build one real injected runtime-support bundle for a bot test."""
    support = await sync_owned_runtime_support(
        None,
        db_path=bot.config.cache.resolve_db_path(bot.runtime_paths) if db_path is None else db_path,
        logger=bot.logger,
        background_task_owner=bot._runtime_view,
        init_failure_reason_prefix="test_runtime_init_failed",
        log_db_path_change=False,
    )
    bot.event_cache = support.event_cache
    bot.event_cache_write_coordinator = support.event_cache_write_coordinator
    bot.startup_thread_prewarm_registry = support.startup_thread_prewarm_registry
    bot._runtime_view.mark_runtime_started()
    return support


async def _close_bound_runtime_support(bot: AgentBot, support: OwnedRuntimeSupport) -> None:
    """Close one test-owned runtime-support bundle."""
    await close_owned_runtime_support(support, logger=bot.logger)


def _save_certified_sync_token(
    bot: AgentBot,
    token: str,
) -> None:
    """Persist one cache-bound certified sync token for bot lifecycle tests."""
    save_sync_token(
        bot.storage_path,
        bot.agent_name,
        token,
        cache_generation=bot.event_cache.cache_generation,
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
        bot.event_cache = _runtime_event_cache()
        bot.event_cache_write_coordinator = _install_runtime_write_coordinator(bot)
        bot.startup_thread_prewarm_registry = StartupThreadPrewarmRegistry()

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
    def _sync_response(joined_rooms: object) -> MagicMock:
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = joined_rooms
        return sync_response

    async def _run_sync_response_without_startup_side_effects(
        self,
        bot: AgentBot,
        sync_response: nio.SyncResponse,
    ) -> None:
        if bot.client is not None and not isinstance(sync_response.next_batch, str):
            sync_response.next_batch = bot.client.next_batch
        orchestrator = bot.orchestrator
        bot_ready_context = (
            patch.object(orchestrator, "handle_bot_ready", AsyncMock()) if orchestrator is not None else nullcontext()
        )
        with (
            patch.object(bot, "_emit_agent_lifecycle_event", AsyncMock()),
            patch.object(bot, "_maybe_start_startup_thread_prewarm"),
            patch.object(bot, "_maybe_start_deferred_overdue_task_drain"),
            bot_ready_context,
        ):
            await bot._on_sync_response(sync_response)
