"""Test threading behavior to reproduce and fix the threading error.

This test verifies that:
1. Agents always respond in threads (never in main room)
2. Commands that are replies don't cause threading errors
3. The bot handles various message relation scenarios correctly
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio
from nio.api import RelationshipType

from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.hooks import EVENT_AGENT_STARTED
from mindroom.matrix.cache.event_cache import ThreadCacheState, _EventCache
from mindroom.matrix.cache.thread_history_result import (
    THREAD_HISTORY_SOURCE_CACHE,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
)
from mindroom.matrix.cache.thread_history_result import (
    thread_history_result as _thread_history_result_impl,
)
from mindroom.matrix.cache.write_coordinator import _EventCacheWriteCoordinator
from mindroom.matrix.client import (
    DeliveredMatrixEvent,
    PermanentMatrixStartupError,
    ResolvedVisibleMessage,
    ThreadHistoryResult,
)
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import _clear_mxc_cache
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_generate_response_mock,
    make_event_cache_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from pathlib import Path


def _runtime_bound_config(config: Config, runtime_root: Path) -> Config:
    """Return a runtime-bound config for threading tests."""
    return bind_runtime_paths(config, test_runtime_paths(runtime_root))


def test_plain_reply_event_info_has_no_safe_thread_root_for_routing() -> None:
    """Plain replies should not populate a synthetic safe thread root."""
    event_info = EventInfo.from_event(
        {
            "content": {
                "body": "plain reply",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$target:localhost"}},
            },
            "event_id": "$reply:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "room_id": "!test:localhost",
            "type": "m.room.message",
        },
    )

    assert event_info.is_reply is True
    assert event_info.reply_to_event_id == "$target:localhost"
    assert event_info.safe_thread_root is None


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


def _runtime_write_coordinator() -> _EventCacheWriteCoordinator:
    """Return one real coordinator for runtime-state tests."""
    return _EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=object(),
    )


async def _reopen_event_cache(event_cache: _EventCache) -> _EventCache:
    """Close and reopen one SQLite cache against the same database file."""
    db_path = event_cache.db_path
    await event_cache.close()
    reopened_cache = _EventCache(db_path)
    await reopened_cache.initialize()
    return reopened_cache


def _conversation_runtime(
    *,
    client: nio.AsyncClient | None = None,
    event_cache: _EventCache | None = None,
    coordinator: _EventCacheWriteCoordinator | None = None,
) -> BotRuntimeState:
    """Build one minimal live runtime state for conversation-cache tests."""
    return BotRuntimeState(
        client=client,
        config=MagicMock(spec=Config),
        enable_streaming=True,
        orchestrator=None,
        event_cache=event_cache or _runtime_event_cache(),
        event_cache_write_coordinator=coordinator or _runtime_write_coordinator(),
    )


def _install_runtime_write_coordinator(bot: AgentBot) -> _EventCacheWriteCoordinator:
    """Attach one explicit runtime write coordinator to a bot test double."""
    coordinator = _EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=bot._runtime_view,
    )
    bot.event_cache_write_coordinator = coordinator
    return coordinator


class TestMatrixConversationCacheThreadReads:
    """Targeted read-path tests for invalidate-and-refetch behavior."""

    @pytest.mark.asyncio
    async def test_record_outbound_message_swallows_internal_write_failure(self) -> None:
        """The public outbound write-through boundary must fail open."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access._writes.record_outbound_message = AsyncMock(side_effect=RuntimeError("cache write failed"))

        await access.record_outbound_message(
            "!room:localhost",
            "$event:localhost",
            {"body": "hello", "msgtype": "m.text"},
        )

    @pytest.mark.asyncio
    async def test_invalidate_known_thread_fails_closed_when_stale_marker_write_fails(self) -> None:
        """Thread invalidation must delete cached rows when the stale marker cannot be persisted."""
        event_cache = _runtime_event_cache()
        event_cache.mark_thread_stale = AsyncMock(side_effect=RuntimeError("sqlite write failed"))
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
        )

        await access._writes._invalidate_known_thread(
            "!room:localhost",
            "$thread:localhost",
            reason="test_failure",
        )

        event_cache.invalidate_thread.assert_awaited_once_with("!room:localhost", "$thread:localhost")

    @pytest.mark.asyncio
    async def test_invalidate_room_threads_fails_closed_when_stale_marker_write_fails(self) -> None:
        """Room invalidation must delete cached room rows when the stale marker cannot be persisted."""
        event_cache = _runtime_event_cache()
        event_cache.mark_room_threads_stale = AsyncMock(side_effect=RuntimeError("sqlite write failed"))
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
        )

        await access._writes._invalidate_room_threads(
            "!room:localhost",
            reason="test_failure",
        )

        event_cache.invalidate_room_threads.assert_awaited_once_with("!room:localhost")

    @pytest.mark.asyncio
    async def test_lookup_miss_invalidation_survives_restart_and_refetches_next_read(self, tmp_path: Path) -> None:
        """Lookup-miss mutations should leave a durable marker that the next runtime observes."""
        event_cache = _EventCache(tmp_path / "event_cache.db")
        await event_cache.initialize()
        root_event = {
            "event_id": "$thread:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {"body": "Root", "msgtype": "m.text"},
        }
        stale_reply_event = {
            "event_id": "$reply:localhost",
            "sender": "@agent:localhost",
            "origin_server_ts": 2000,
            "type": "m.room.message",
            "content": {
                "body": "Stale reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread:localhost"},
            },
        }

        def room_get_event_relations(
            _room_id: str,
            event_id: str,
            *,
            rel_type: RelationshipType | None = None,
            event_type: str | None = None,
            direction: nio.MessageDirection = nio.MessageDirection.back,
            limit: int | None = None,
        ) -> object:
            assert rel_type is not None
            assert event_type is not None

            async def iterator() -> object:
                if (event_id, rel_type, event_type, direction, limit) == (
                    "$thread:localhost",
                    RelationshipType.thread,
                    "m.room.message",
                    nio.MessageDirection.back,
                    None,
                ):
                    yield nio.RoomMessageText.from_dict(
                        {
                            "content": {
                                "body": "Fresh reply",
                                "msgtype": "m.text",
                                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread:localhost"},
                            },
                            "event_id": "$reply:localhost",
                            "sender": "@agent:localhost",
                            "origin_server_ts": 3000,
                            "room_id": "!test:localhost",
                            "type": "m.room.message",
                        },
                    )

            return iterator()

        outbound_client = _make_client_mock(user_id="@mindroom_general:localhost")
        outbound_client.next_batch = "s1"
        reader_client = _make_client_mock(user_id="@mindroom_general:localhost")
        reader_client.next_batch = "s1"
        reader_client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {"body": "Root", "msgtype": "m.text"},
                    "event_id": "$thread:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1000,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )
        reader_client.room_get_event_relations = MagicMock(side_effect=room_get_event_relations)
        reader_client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(
                room_id="!test:localhost",
                chunk=[
                    nio.RoomMessageText.from_dict(
                        {
                            "content": {
                                "body": "Fresh reply",
                                "msgtype": "m.text",
                                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread:localhost"},
                            },
                            "event_id": "$reply:localhost",
                            "sender": "@agent:localhost",
                            "origin_server_ts": 3000,
                            "room_id": "!test:localhost",
                            "type": "m.room.message",
                        },
                    ),
                    nio.RoomMessageText.from_dict(
                        {
                            "content": {"body": "Root", "msgtype": "m.text"},
                            "event_id": "$thread:localhost",
                            "sender": "@user:localhost",
                            "origin_server_ts": 1000,
                            "room_id": "!test:localhost",
                            "type": "m.room.message",
                        },
                    ),
                ],
                start="",
                end=None,
            ),
        )

        first_access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=outbound_client, event_cache=event_cache),
        )

        try:
            await event_cache.replace_thread(
                "!test:localhost",
                "$thread:localhost",
                [root_event, stale_reply_event],
                validated_at=time.time(),
            )
            await first_access.record_outbound_message(
                "!test:localhost",
                "$edit:localhost",
                {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$missing:localhost"},
                },
            )

            event_cache = await _reopen_event_cache(event_cache)
            second_access = MatrixConversationCache(
                logger=MagicMock(),
                runtime=_conversation_runtime(client=reader_client, event_cache=event_cache),
            )

            history = await second_access.get_thread_history("!test:localhost", "$thread:localhost")
        finally:
            await event_cache.close()

        assert [message.body for message in history] == ["Root", "Fresh reply"]
        reader_client.room_messages.assert_awaited_once()


class TestThreadingBehavior:
    """Test that agents correctly handle threading in various scenarios."""

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
        bot.orchestrator = mock_orchestrator

        # Create a mock client
        bot.client = _make_client_mock(user_id="@mindroom_general:localhost")
        bot.event_cache = _runtime_event_cache()
        bot.event_cache_write_coordinator = _install_runtime_write_coordinator(bot)

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

    @pytest.mark.asyncio
    async def test_start_and_stop_manage_persistent_event_cache(self, bot: AgentBot) -> None:
        """Startup should wire standalone runtime support services onto the live runtime."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        start_client = _make_client_mock(user_id="@mindroom_general:localhost")
        start_client.add_event_callback = MagicMock()
        start_client.add_response_callback = MagicMock()
        start_client.close = AsyncMock()

        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=start_client)),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
            patch.object(bot, "_emit_agent_lifecycle_event", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
            patch("mindroom.bot.wait_for_background_tasks", AsyncMock()),
        ):
            await bot.start()
            assert bot.client is start_client

            await bot.stop(reason="test")

        assert bot._standalone_runtime_support is None
        assert bot._runtime_view.event_cache is None
        assert bot._runtime_view.event_cache_write_coordinator is None
        start_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_injected_shared_event_cache_stays_open_for_other_bots(self, bot: AgentBot, tmp_path: Path) -> None:
        """A non-owned injected cache should stay open until its explicit owner closes it."""
        other_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password=TEST_PASSWORD,
            display_name="RouterAgent",
            agent_name="router",
        )
        other_bot = AgentBot(
            agent_user=other_user,
            storage_path=tmp_path,
            rooms=["!test:localhost"],
            enable_streaming=False,
            config=bot.config,
            runtime_paths=bot.runtime_paths,
        )

        shared_cache = _EventCache(bot.config.cache.resolve_db_path(bot.runtime_paths))
        shared_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )
        await shared_cache.initialize()
        bot.event_cache = shared_cache
        bot.event_cache_write_coordinator = shared_coordinator
        other_bot.event_cache = shared_cache
        other_bot.event_cache_write_coordinator = shared_coordinator

        try:
            await shared_cache.store_event(
                "$shared-event",
                "!test:localhost",
                {
                    "event_id": "$shared-event",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "type": "m.room.message",
                    "content": {"body": "shared cache", "msgtype": "m.text"},
                },
            )
            await bot._close_runtime_support_services()
            assert bot._standalone_runtime_support is None
            assert bot.event_cache is shared_cache
            assert other_bot.event_cache is shared_cache

            cached_event = await other_bot.event_cache.get_event("!test:localhost", "$shared-event")
        finally:
            await other_bot._close_runtime_support_services()
            await shared_cache.close()

        assert cached_event is not None
        assert cached_event["event_id"] == "$shared-event"

    @pytest.mark.asyncio
    async def test_partial_runtime_support_injection_fails_fast(self, bot: AgentBot) -> None:
        """Standalone runtime ownership requires all support services to be injected together."""
        bot.event_cache_write_coordinator = None
        bot.event_cache = _runtime_event_cache()

        with pytest.raises(
            RuntimeError,
            match="Runtime support services must be injected all together or not at all",
        ):
            await bot._initialize_runtime_support_services()

    @pytest.mark.asyncio
    async def test_try_start_partial_runtime_support_injection_fails_before_login(self, bot: AgentBot) -> None:
        """Mixed runtime support injection should stop startup before any login side effects."""
        bot.client = None
        bot.event_cache_write_coordinator = None
        bot.event_cache = _runtime_event_cache()

        with (
            patch.object(bot, "ensure_user_account", AsyncMock()) as ensure_user_account,
            patch("mindroom.bot.login_agent_user", AsyncMock()) as login_agent_user,
            pytest.raises(
                PermanentMatrixStartupError,
                match="Runtime support services must be injected all together or not at all",
            ),
        ):
            await bot.try_start()

        ensure_user_account.assert_not_awaited()
        login_agent_user.assert_not_awaited()
        assert bot.client is None

    @pytest.mark.asyncio
    async def test_standalone_runtime_support_degrades_when_event_cache_init_fails(self, bot: AgentBot) -> None:
        """Standalone startup should keep running without cache when SQLite init fails."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None

        with patch("mindroom.runtime_support._EventCache.initialize", AsyncMock(side_effect=RuntimeError("boom"))):
            await bot._initialize_runtime_support_services()

        assert bot._standalone_runtime_support is not None
        assert bot._runtime_view.event_cache is bot._standalone_runtime_support.event_cache
        assert (
            bot._runtime_view.event_cache_write_coordinator
            is bot._standalone_runtime_support.event_cache_write_coordinator
        )
        assert bot.event_cache.is_initialized is False

    @pytest.mark.asyncio
    async def test_start_keeps_running_when_runtime_support_init_fails(self, bot: AgentBot) -> None:
        """Startup should keep the logged-in client when cache init degrades to no-cache."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        start_client = _make_client_mock(user_id="@mindroom_general:localhost")
        start_client.add_event_callback = MagicMock()
        start_client.add_response_callback = MagicMock()
        start_client.close = AsyncMock()

        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=start_client)),
            patch(
                "mindroom.runtime_support._EventCache.initialize",
                AsyncMock(side_effect=RuntimeError("cache init failed")),
            ),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
            patch.object(bot, "_emit_agent_lifecycle_event", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
        ):
            await bot.start()

        assert bot.client is start_client
        assert bot.running is True
        assert bot.event_cache.is_initialized is False
        start_client.close.assert_not_awaited()
        await bot.stop(reason="test")
        start_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_resets_running_flag_when_agent_started_hooks_fail(self, bot: AgentBot) -> None:
        """Startup cleanup should clear running state if EVENT_AGENT_STARTED emission fails."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        start_client = _make_client_mock(user_id="@mindroom_general:localhost")
        start_client.add_event_callback = MagicMock()
        start_client.add_response_callback = MagicMock()
        start_client.close = AsyncMock()
        bot.hook_registry = MagicMock()
        bot.hook_registry.has_hooks.side_effect = lambda event_name: event_name == EVENT_AGENT_STARTED

        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=start_client)),
            patch.object(bot, "_initialize_runtime_support_services", AsyncMock()),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
            patch("mindroom.bot.emit", AsyncMock(side_effect=RuntimeError("hook boom"))),
            pytest.raises(RuntimeError, match="hook boom"),
        ):
            await bot.start()

        start_client.close.assert_awaited_once()
        assert bot.running is False
        assert bot.client is None

    @pytest.mark.asyncio
    async def test_standalone_runtime_support_rebuilds_after_close_when_db_path_changes(
        self,
        bot: AgentBot,
        tmp_path: Path,
    ) -> None:
        """Standalone restart should rebuild support from the latest configured cache path."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        bot.config.cache.db_path = str(tmp_path / "event-cache-first.db")

        await bot._initialize_runtime_support_services()
        first_support = bot._standalone_runtime_support
        assert first_support is not None
        assert first_support.event_cache.db_path == tmp_path / "event-cache-first.db"
        await bot._close_runtime_support_services()

        assert bot._standalone_runtime_support is None
        assert bot._runtime_view.event_cache is None
        assert bot._runtime_view.event_cache_write_coordinator is None

        bot.config.cache.db_path = str(tmp_path / "event-cache-second.db")
        await bot._initialize_runtime_support_services()
        second_support = bot._standalone_runtime_support
        assert second_support is not None
        assert second_support is not first_support
        assert second_support.event_cache.db_path == tmp_path / "event-cache-second.db"

        await bot._close_runtime_support_services()

    @pytest.mark.asyncio
    async def test_sync_response_caches_timeline_events_for_point_lookups(self, bot: AgentBot) -> None:
        """Sync-response handling should persist timeline events into SQLite-backed lookups."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        await bot._initialize_runtime_support_services()
        assert bot.event_cache

        try:
            message_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "Thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$thread_msg:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )
            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {
                "!test:localhost": MagicMock(timeline=MagicMock(events=[message_event])),
            }
            bot._first_sync_done = True

            await bot._on_sync_response(sync_response)
            await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)
            cached_event = await bot.event_cache.get_event("!test:localhost", "$thread_msg:localhost")
        finally:
            await bot._close_runtime_support_services()

        assert cached_event is not None
        assert cached_event["event_id"] == "$thread_msg:localhost"
        assert cached_event["content"]["body"] == "Thread reply"

    @pytest.mark.asyncio
    async def test_sync_error_keeps_watchdog_clock_on_latest_activity(self, bot: AgentBot) -> None:
        """Sync errors should keep the watchdog alive using the latest observed sync activity."""
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock(join={})
        sync_error = MagicMock(spec=nio.SyncError)
        bot._first_sync_done = True

        with patch("mindroom.bot.time.monotonic", side_effect=[100.0, 200.0]):
            await bot._on_sync_response(sync_response)
            await bot._on_sync_error(sync_error)

        assert bot._last_sync_monotonic == 200.0

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_schedules_background_write(self, bot: AgentBot) -> None:
        """Sync timeline caching should return before a slow cache write finishes."""
        store_started = asyncio.Event()
        allow_store_finish = asyncio.Event()

        async def slow_store_events_batch(_events: object) -> None:
            store_started.set()
            await allow_store_finish.wait()

        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock(side_effect=slow_store_events_batch)
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.redact_event = AsyncMock()
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        message_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Thread reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$thread_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[message_event])),
        }

        bot._conversation_cache.cache_sync_timeline(sync_response)
        await asyncio.wait_for(store_started.wait(), timeout=1.0)

        allow_store_finish.set()
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        event_cache.store_events_batch.assert_awaited_once()
        event_cache.append_event.assert_awaited_once()
        event_cache.redact_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_appends_thread_events_to_cached_threads(self, bot: AgentBot) -> None:
        """Sync timeline writes should append direct thread events through the thread-cache helper."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.redact_event = AsyncMock()
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        message_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Thread reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$thread_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[message_event])),
        }

        bot._conversation_cache.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        event_cache.append_event.assert_awaited_once()
        append_args = event_cache.append_event.await_args.args
        assert append_args[0] == "!test:localhost"
        assert append_args[1] == "$thread_root:localhost"
        assert append_args[2]["event_id"] == "$thread_msg:localhost"

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_appends_threaded_edits_to_cached_threads(self, bot: AgentBot) -> None:
        """Sync timeline writes should append threaded edits using the thread root from m.new_content."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.redact_event = AsyncMock()
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* Updated thread reply",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "Updated thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_msg:localhost"},
                },
                "event_id": "$thread_edit:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567891,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[edit_event])),
        }

        bot._conversation_cache.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        event_cache.append_event.assert_awaited_once()
        append_args = event_cache.append_event.await_args.args
        assert append_args[0] == "!test:localhost"
        assert append_args[1] == "$thread_root:localhost"
        assert append_args[2]["event_id"] == "$thread_edit:localhost"

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_appends_edits_via_cached_thread_lookup(self, bot: AgentBot) -> None:
        """Sync timeline writes should append edits using cached thread membership when m.new_content lacks it."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        await bot._initialize_runtime_support_services()
        assert bot.event_cache

        try:
            await bot.event_cache.replace_thread(
                "!test:localhost",
                "$thread_root:localhost",
                [
                    {
                        "event_id": "$thread_root:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567889,
                        "type": "m.room.message",
                        "content": {"body": "Root message", "msgtype": "m.text"},
                    },
                    {
                        "event_id": "$thread_msg:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567890,
                        "type": "m.room.message",
                        "content": {
                            "body": "Thread reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                        },
                    },
                ],
            )

            edit_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "* Updated thread reply",
                        "msgtype": "m.text",
                        "m.new_content": {
                            "body": "Updated thread reply",
                            "msgtype": "m.text",
                        },
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_msg:localhost"},
                    },
                    "event_id": "$thread_edit:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567891,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )
            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {
                "!test:localhost": MagicMock(timeline=MagicMock(events=[edit_event])),
            }

            bot._conversation_cache.cache_sync_timeline(sync_response)
            await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

            cached_thread_events = await bot.event_cache.get_thread_events(
                "!test:localhost",
                "$thread_root:localhost",
            )
            cached_thread_id = await bot.event_cache.get_thread_id_for_event(
                "!test:localhost",
                "$thread_edit:localhost",
            )
        finally:
            await bot._close_runtime_support_services()

        assert cached_thread_events is not None
        assert [event["event_id"] for event in cached_thread_events] == [
            "$thread_root:localhost",
            "$thread_msg:localhost",
            "$thread_edit:localhost",
        ]
        assert cached_thread_id == "$thread_root:localhost"

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_does_not_append_room_level_events(self, bot: AgentBot) -> None:
        """Sync timeline writes should not append non-threaded events into thread cache state."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.redact_event = AsyncMock()
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        message_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Room reply",
                    "msgtype": "m.text",
                },
                "event_id": "$room_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[message_event])),
        }

        bot._conversation_cache.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        event_cache.append_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_serializes_same_room_updates_in_order(self, bot: AgentBot) -> None:
        """Later sync updates for one room should wait for earlier queued cache writes."""
        store_started = asyncio.Event()
        allow_store_finish = asyncio.Event()
        call_order: list[str] = []

        async def slow_store_events_batch(_events: object) -> None:
            call_order.append("store-start")
            store_started.set()
            await allow_store_finish.wait()
            call_order.append("store-finish")

        async def record_redaction(*_args: object, **_kwargs: object) -> bool:
            call_order.append("redact")
            return True

        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock(side_effect=slow_store_events_batch)
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.redact_event = AsyncMock(side_effect=record_redaction)
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        message_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Thread reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$thread_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$thread_msg:localhost"
        redaction_event.sender = "@user:localhost"
        redaction_event.server_timestamp = 1234567891
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$thread_msg:localhost",
            "room_id": "!test:localhost",
            "type": "m.room.redaction",
        }

        first_sync_response = MagicMock()
        first_sync_response.__class__ = nio.SyncResponse
        first_sync_response.rooms = MagicMock()
        first_sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[message_event])),
        }

        second_sync_response = MagicMock()
        second_sync_response.__class__ = nio.SyncResponse
        second_sync_response.rooms = MagicMock()
        second_sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[redaction_event])),
        }

        bot._conversation_cache.cache_sync_timeline(first_sync_response)
        await asyncio.wait_for(store_started.wait(), timeout=1.0)

        bot._conversation_cache.cache_sync_timeline(second_sync_response)
        await asyncio.sleep(0)
        event_cache.redact_event.assert_not_awaited()

        allow_store_finish.set()
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        assert call_order == ["store-start", "store-finish", "redact"]

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_redactions_continue_after_thread_append_failure(self, bot: AgentBot) -> None:
        """A failed thread append should not stop later redactions in the same sync batch."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.append_event = AsyncMock(side_effect=RuntimeError("append failed"))
        event_cache.redact_event = AsyncMock(return_value=True)
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        message_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Thread reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$thread_msg_new:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$thread_msg_old:localhost"
        redaction_event.sender = "@user:localhost"
        redaction_event.server_timestamp = 1234567891
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$thread_msg_old:localhost",
            "room_id": "!test:localhost",
            "type": "m.room.redaction",
        }
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[message_event, redaction_event])),
        }

        bot._conversation_cache.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        event_cache.append_event.assert_awaited_once()
        event_cache.redact_event.assert_awaited_once_with("!test:localhost", "$thread_msg_old:localhost")

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_keeps_room_updates_isolated(self, bot: AgentBot) -> None:
        """One room's queued cache write should not block another room's write."""
        room_a_started = asyncio.Event()
        release_room_a = asyncio.Event()
        room_b_finished = asyncio.Event()

        async def store_events_batch(events: list[tuple[str, str, dict[str, object]]]) -> None:
            room_id = events[0][1]
            if room_id == "!room-a:localhost":
                room_a_started.set()
                await release_room_a.wait()
                return
            if room_id == "!room-b:localhost":
                room_b_finished.set()
                return
            msg = f"Unexpected room_id {room_id}"
            raise AssertionError(msg)

        def sync_response_for(room_id: str, event_id: str) -> nio.SyncResponse:
            message_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": f"Thread reply for {room_id}",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )
            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {room_id: MagicMock(timeline=MagicMock(events=[message_event]))}
            return sync_response

        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock(side_effect=store_events_batch)
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.redact_event = AsyncMock()
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        bot._conversation_cache.cache_sync_timeline(
            sync_response_for("!room-a:localhost", "$room_a_msg:localhost"),
        )
        await asyncio.wait_for(room_a_started.wait(), timeout=1.0)

        bot._conversation_cache.cache_sync_timeline(
            sync_response_for("!room-b:localhost", "$room_b_msg:localhost"),
        )
        await asyncio.wait_for(room_b_finished.wait(), timeout=1.0)

        release_room_a.set()
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        assert event_cache.store_events_batch.await_count == 2

    @pytest.mark.asyncio
    async def test_live_redaction_callback_removes_persisted_lookup_event(self, bot: AgentBot) -> None:
        """Live redaction callbacks should remove point-lookup cache entries."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        await bot._initialize_runtime_support_services()
        assert bot.event_cache

        try:
            await bot.event_cache.store_event(
                "$thread_msg:localhost",
                "!test:localhost",
                {
                    "event_id": "$thread_msg:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "type": "m.room.message",
                    "content": {
                        "body": "Thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                },
            )
            room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
            redaction_event = MagicMock(spec=nio.RedactionEvent)
            redaction_event.event_id = "$redaction:localhost"
            redaction_event.redacts = "$thread_msg:localhost"
            redaction_event.sender = "@user:localhost"
            redaction_event.server_timestamp = 1234567891
            redaction_event.source = {
                "content": {},
                "event_id": "$redaction:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567891,
                "redacts": "$thread_msg:localhost",
                "room_id": "!test:localhost",
                "type": "m.room.redaction",
            }

            await bot._on_redaction(room, redaction_event)
            cached_event = await bot.event_cache.get_event("!test:localhost", "$thread_msg:localhost")
        finally:
            await bot._close_runtime_support_services()

        assert cached_event is None

    @pytest.mark.asyncio
    async def test_sync_timeline_redaction_does_not_resurrect_point_lookup_cache(self, bot: AgentBot) -> None:
        """A sync batch that contains both a message and its redaction must leave no cached lookup entry."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        await bot._initialize_runtime_support_services()
        assert bot.event_cache

        try:
            await bot.event_cache.replace_thread(
                "!test:localhost",
                "$thread_root:localhost",
                [
                    {
                        "event_id": "$thread_root:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567889,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                        "content": {"body": "Root message", "msgtype": "m.text"},
                    },
                ],
            )
            message_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "Redacted reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$thread_msg:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )
            redaction_event = MagicMock(spec=nio.RedactionEvent)
            redaction_event.event_id = "$redaction:localhost"
            redaction_event.redacts = "$thread_msg:localhost"
            redaction_event.sender = "@user:localhost"
            redaction_event.server_timestamp = 1234567891
            redaction_event.source = {
                "content": {},
                "event_id": "$redaction:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567891,
                "redacts": "$thread_msg:localhost",
                "room_id": "!test:localhost",
                "type": "m.room.redaction",
            }
            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {
                "!test:localhost": MagicMock(timeline=MagicMock(events=[message_event, redaction_event])),
            }

            bot._conversation_cache.cache_sync_timeline(sync_response)
            await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)
            cached_event = await bot.event_cache.get_event("!test:localhost", "$thread_msg:localhost")
            cached_thread_events = await bot.event_cache.get_thread_events(
                "!test:localhost",
                "$thread_root:localhost",
            )
        finally:
            await bot._close_runtime_support_services()

        assert cached_event is None
        assert cached_thread_events is not None
        assert [event["event_id"] for event in cached_thread_events] == ["$thread_root:localhost"]

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_skips_thread_appends_after_store_failure(self, bot: AgentBot) -> None:
        """Failed point-lookup writes must not leave split thread cache state."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock(side_effect=RuntimeError("store failed"))
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.redact_event = AsyncMock(return_value=True)
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        message_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Thread reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$thread_msg_new:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$thread_msg_old:localhost"
        redaction_event.sender = "@user:localhost"
        redaction_event.server_timestamp = 1234567891
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$thread_msg_old:localhost",
            "room_id": "!test:localhost",
            "type": "m.room.redaction",
        }
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[message_event, redaction_event])),
        }

        bot._conversation_cache.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        event_cache.store_events_batch.assert_awaited_once()
        event_cache.append_event.assert_awaited_once()
        event_cache.redact_event.assert_awaited_once_with("!test:localhost", "$thread_msg_old:localhost")

    @pytest.mark.asyncio
    async def test_wait_for_background_tasks_owner_scope_isolated(self, bot: AgentBot) -> None:
        """Scoped waits should not block on background tasks owned by another bot."""
        other_owner = object()
        other_task_started = asyncio.Event()
        release_other_task = asyncio.Event()

        async def other_owner_task() -> None:
            other_task_started.set()
            await release_other_task.wait()

        other_task = create_background_task(
            other_owner_task(),
            name="other_owner_task",
            owner=other_owner,
        )

        await asyncio.wait_for(other_task_started.wait(), timeout=1.0)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)
        assert not other_task.done()

        release_other_task.set()
        await wait_for_background_tasks(timeout=1.0, owner=other_owner)
        assert other_task.done()

    @pytest.mark.asyncio
    async def test_wait_for_background_tasks_drains_child_tasks_created_during_wait(self) -> None:
        """Owner-scoped draining should keep waiting for child tasks spawned by awaited tasks."""
        owner = object()
        parent_started = asyncio.Event()
        release_parent = asyncio.Event()
        child_started = asyncio.Event()
        release_child = asyncio.Event()
        child_finished = asyncio.Event()

        async def child_task() -> None:
            child_started.set()
            await release_child.wait()
            child_finished.set()

        async def parent_task() -> None:
            parent_started.set()
            await release_parent.wait()
            create_background_task(child_task(), name="child_task", owner=owner)

        parent = create_background_task(parent_task(), name="parent_task", owner=owner)
        await asyncio.wait_for(parent_started.wait(), timeout=1.0)

        drain_task = asyncio.create_task(wait_for_background_tasks(timeout=1.0, owner=owner))
        await asyncio.sleep(0)

        release_parent.set()
        await asyncio.wait_for(child_started.wait(), timeout=1.0)
        assert drain_task.done() is False

        release_child.set()
        await drain_task

        assert parent.done()
        assert child_finished.is_set()

    @pytest.mark.asyncio
    async def test_wait_for_background_tasks_timeout_stops_after_bounded_cancel_rounds(self) -> None:
        """Timed-out draining should return even if cancelled tasks keep spawning replacements."""
        owner = object()
        respawned_count = 0
        respawned_replacement = asyncio.Event()
        allow_respawn = True

        async def respawning_task() -> None:
            nonlocal respawned_count
            try:
                await asyncio.Future()
            finally:
                if allow_respawn:
                    respawned_count += 1
                    respawned_replacement.set()
                    create_background_task(
                        respawning_task(),
                        name=f"respawning_task_{respawned_count}",
                        owner=owner,
                    )

        create_background_task(respawning_task(), name="respawning_task_root", owner=owner)

        try:
            await asyncio.wait_for(wait_for_background_tasks(timeout=0.01, owner=owner), timeout=0.5)
            await asyncio.wait_for(respawned_replacement.wait(), timeout=0.5)
            assert respawned_count >= 1
        finally:
            allow_respawn = False
            await wait_for_background_tasks(timeout=0.05, owner=owner)

    @pytest.mark.asyncio
    async def test_live_edit_cache_lookup_failure_does_not_raise(self, bot: AgentBot) -> None:
        """Live edit caching should degrade cleanly when SQLite lookup fails."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(side_effect=RuntimeError("database is locked"))
        event_cache.append_event = AsyncMock()
        bot.event_cache = event_cache

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_msg:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        await bot._conversation_cache.append_live_event(
            "!test:localhost",
            edit_event,
            event_info=EventInfo.from_event(edit_event.source),
        )

        event_cache.get_thread_id_for_event.assert_awaited_once_with("!test:localhost", "$thread_msg:localhost")
        event_cache.append_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_event_queues_persistent_cache_fill_through_room_write_barrier(self) -> None:
        """Point-event cache fills should use the same room-ordered coordinator as other durable writes."""
        event_cache = _runtime_event_cache()
        event_cache.get_event = AsyncMock(return_value=None)
        event_cache.store_event = AsyncMock()
        coordinator = _runtime_write_coordinator()
        client = _make_client_mock()
        client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {"body": "hello", "msgtype": "m.text"},
                    "event_id": "$event:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=client,
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )

        with patch.object(coordinator, "queue_room_update", wraps=coordinator.queue_room_update) as mock_queue:
            await access.get_event("!test:localhost", "  $event:localhost  ")

        event_cache.store_event.assert_awaited_once()
        stored_event_id, stored_room_id, stored_event_source = event_cache.store_event.await_args.args
        assert stored_event_id == "$event:localhost"
        assert stored_room_id == "!test:localhost"
        assert stored_event_source["event_id"] == "$event:localhost"
        mock_queue.assert_called_once()

    @pytest.mark.asyncio
    async def test_local_bot_redaction_ignores_cache_failure_after_successful_redact(self, bot: AgentBot) -> None:
        """A successful local redact should delegate advisory bookkeeping through the cache facade."""
        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.room_redact = AsyncMock(
            return_value=nio.RoomRedactResponse(
                event_id="$redaction:localhost",
                room_id="!test:localhost",
            ),
        )
        bot._conversation_cache.record_outbound_redaction = AsyncMock()

        result = await bot._redact_message_event(
            room_id="!test:localhost",
            event_id="$target:localhost",
            reason="cleanup",
        )

        assert result is True
        bot.client.room_redact.assert_awaited_once_with(
            "!test:localhost",
            "$target:localhost",
            reason="cleanup",
        )
        bot._conversation_cache.record_outbound_redaction.assert_awaited_once_with(
            "!test:localhost",
            "$target:localhost",
        )

    @pytest.mark.asyncio
    async def test_wait_for_room_idle_returns_after_completed_tail_task(self) -> None:
        """Room-idle waiting should not livelock on a tail task that already finished."""
        coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )

        try:
            completed_task = asyncio.create_task(asyncio.sleep(0, result=object()))
            await completed_task
            coordinator._room_update_tasks["!room:localhost"] = completed_task
            await asyncio.wait_for(coordinator.wait_for_room_idle("!room:localhost"), timeout=0.2)
            assert coordinator._room_update_tasks.get("!room:localhost") is None
        finally:
            await coordinator.close()

    @pytest.mark.asyncio
    async def test_sync_edit_marks_cached_thread_stale_and_next_read_refetches(
        self,
        tmp_path: Path,
    ) -> None:
        """A synced thread edit should force the next read to refetch from Matrix, even after a restart."""
        event_cache = _EventCache(tmp_path / "event_cache.db")
        await event_cache.initialize()
        root_event = _text_event(
            event_id="$thread_root:localhost",
            body="Root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        reply_event = _text_event(
            event_id="$reply:localhost",
            body="Original reply",
            sender="@agent:localhost",
            server_timestamp=2000,
            thread_id="$thread_root:localhost",
        )
        reply_edit = _text_event(
            event_id="$reply_edit:localhost",
            body="* Edited reply",
            sender="@agent:localhost",
            server_timestamp=3000,
            replacement_of="$reply:localhost",
            new_body="Edited reply",
            new_thread_id="$thread_root:localhost",
        )
        initial_client = _relations_client(
            root_event=root_event,
            thread_events=[reply_event],
            next_batch="s_initial",
        )
        restarted_client = _relations_client(
            root_event=root_event,
            thread_events=[reply_event],
            replacements_by_event_id={"$reply:localhost": [reply_edit]},
            next_batch="s_after_edit",
        )

        try:
            access = MatrixConversationCache(
                logger=MagicMock(),
                runtime=_conversation_runtime(client=initial_client, event_cache=event_cache),
            )
            initial_history = await access.get_thread_history("!test:localhost", "$thread_root:localhost")

            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {
                "!test:localhost": MagicMock(timeline=MagicMock(events=[reply_edit])),
            }
            access.cache_sync_timeline(sync_response)
            await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")
            event_cache = await _reopen_event_cache(event_cache)

            restarted_access = MatrixConversationCache(
                logger=MagicMock(),
                runtime=_conversation_runtime(client=restarted_client, event_cache=event_cache),
            )
            refreshed_history = await restarted_access.get_thread_history("!test:localhost", "$thread_root:localhost")
            restarted_client.room_messages.reset_mock()
            cached_history = await restarted_access.get_thread_history("!test:localhost", "$thread_root:localhost")
        finally:
            await event_cache.close()

        assert [message.body for message in initial_history] == ["Root", "Original reply"]
        assert [message.body for message in refreshed_history] == ["Root", "Edited reply"]
        assert [message.body for message in cached_history] == ["Root", "Edited reply"]
        assert cached_history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        restarted_client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lookup_miss_sync_edit_marks_room_stale_durably_and_forces_refetch(
        self,
        tmp_path: Path,
    ) -> None:
        """Ambiguous sync edits should invalidate every cached thread in the room durably across restarts."""
        event_cache = _EventCache(tmp_path / "event_cache.db")
        await event_cache.initialize()
        root_event = _text_event(
            event_id="$thread_root:localhost",
            body="Root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        original_reply = _text_event(
            event_id="$reply:localhost",
            body="Original reply",
            sender="@agent:localhost",
            server_timestamp=2000,
            thread_id="$thread_root:localhost",
        )
        refreshed_reply = _text_event(
            event_id="$reply:localhost",
            body="Refetched reply",
            sender="@agent:localhost",
            server_timestamp=2500,
            thread_id="$thread_root:localhost",
        )
        ambiguous_edit = _text_event(
            event_id="$unknown_edit:localhost",
            body="* Unknown edit",
            sender="@agent:localhost",
            server_timestamp=3000,
            replacement_of="$missing:localhost",
            new_body="Unknown edit",
        )
        initial_client = _relations_client(
            root_event=root_event,
            thread_events=[original_reply],
            next_batch="s_initial",
        )
        restarted_client = _relations_client(
            root_event=root_event,
            thread_events=[refreshed_reply],
            next_batch="s_refetch",
        )

        try:
            access = MatrixConversationCache(
                logger=MagicMock(),
                runtime=_conversation_runtime(client=initial_client, event_cache=event_cache),
            )
            await access.get_thread_history("!test:localhost", "$thread_root:localhost")

            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {
                "!test:localhost": MagicMock(timeline=MagicMock(events=[ambiguous_edit])),
            }
            access.cache_sync_timeline(sync_response)
            await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")
            event_cache = await _reopen_event_cache(event_cache)

            cache_state = await event_cache.get_thread_cache_state("!test:localhost", "$thread_root:localhost")
            restarted_access = MatrixConversationCache(
                logger=MagicMock(),
                runtime=_conversation_runtime(client=restarted_client, event_cache=event_cache),
            )
            refreshed_history = await restarted_access.get_thread_history("!test:localhost", "$thread_root:localhost")
            restarted_client.room_messages.reset_mock()
            cached_history = await restarted_access.get_thread_history("!test:localhost", "$thread_root:localhost")
        finally:
            await event_cache.close()

        assert cache_state is not None
        assert cache_state.room_invalidation_reason == "sync_lookup_missing"
        assert [message.body for message in refreshed_history] == ["Root", "Refetched reply"]
        assert [message.body for message in cached_history] == ["Root", "Refetched reply"]
        assert cached_history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        restarted_client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_thread_history_raises_when_refresh_fails(self) -> None:
        """Thread-history reads should fail closed instead of silently returning an empty thread."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access._reads.fetch_thread_history_from_client = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await access.get_thread_history("!test:localhost", "$thread:localhost")

    @pytest.mark.asyncio
    async def test_get_thread_history_refresh_runs_under_room_write_barrier(self) -> None:
        """Thread refreshes should occupy the same room-scoped barrier used by mutations."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access.runtime.event_cache.get_thread_cache_state = AsyncMock(return_value=None)
        access.runtime.event_cache.get_thread_events = AsyncMock(return_value=[{"event_id": "$thread:localhost"}])
        refresh_started = asyncio.Event()
        allow_refresh = asyncio.Event()
        queued_update_started = asyncio.Event()

        async def slow_refresh(
            _room_id: str,
            _thread_id: str,
            _allow_durable_cache: bool,
        ) -> ThreadHistoryResult:
            refresh_started.set()
            await allow_refresh.wait()
            return thread_history_result(
                [_message(event_id="$thread:localhost", body="Root")],
                is_full_history=True,
            )

        async def queued_update() -> None:
            queued_update_started.set()

        access._reads.fetch_thread_history_from_client = AsyncMock(side_effect=slow_refresh)

        refresh_task = asyncio.create_task(access.get_thread_history("!test:localhost", "$thread:localhost"))
        await asyncio.wait_for(refresh_started.wait(), timeout=1.0)

        access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: queued_update(),
            name="matrix_cache_follow_up_update",
        )
        await asyncio.sleep(0)
        assert queued_update_started.is_set() is False

        allow_refresh.set()
        await refresh_task
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")

        assert queued_update_started.is_set()

    @pytest.mark.asyncio
    async def test_get_thread_snapshot_refresh_runs_under_room_write_barrier(self) -> None:
        """Snapshot refreshes should occupy the same room-scoped barrier used by mutations."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        refresh_started = asyncio.Event()
        allow_refresh = asyncio.Event()
        queued_update_started = asyncio.Event()

        async def slow_refresh(
            _room_id: str,
            _thread_id: str,
            _allow_durable_cache: bool,
        ) -> ThreadHistoryResult:
            refresh_started.set()
            await allow_refresh.wait()
            return thread_history_result(
                [_message(event_id="$thread:localhost", body="Root")],
                is_full_history=True,
            )

        async def queued_update() -> None:
            queued_update_started.set()

        access._reads.fetch_thread_snapshot_from_client = AsyncMock(side_effect=slow_refresh)

        refresh_task = asyncio.create_task(access.get_thread_snapshot("!test:localhost", "$thread:localhost"))
        await asyncio.wait_for(refresh_started.wait(), timeout=1.0)

        access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: queued_update(),
            name="matrix_cache_follow_up_update",
        )
        await asyncio.sleep(0)
        assert queued_update_started.is_set() is False

        allow_refresh.set()
        await refresh_task
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")

        assert queued_update_started.is_set()

    @pytest.mark.asyncio
    async def test_thread_read_refetches_once_mutation_starts_after_room_barrier(self) -> None:
        """A read already past the room barrier must still refetch once a mutation starts."""
        event_cache = _runtime_event_cache()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
        )
        thread_state: dict[str, ThreadCacheState] = {
            "value": ThreadCacheState(
                validated_at=time.time(),
                invalidated_at=None,
                invalidation_reason=None,
                room_invalidated_at=None,
                room_invalidation_reason=None,
            ),
        }
        raw_events: list[dict[str, object]] = [
            {"event_id": "$thread:localhost"},
            {"event_id": "$reply-old:localhost"},
        ]
        reader_ready = asyncio.Event()
        allow_reader_continue = asyncio.Event()
        raw_append_committed = asyncio.Event()

        async def pause_reader(_room_id: str) -> None:
            reader_ready.set()
            await allow_reader_continue.wait()

        async def mark_thread_stale(_room_id: str, _thread_id: str, *, reason: str) -> None:
            thread_state["value"] = ThreadCacheState(
                validated_at=thread_state["value"].validated_at,
                invalidated_at=time.time(),
                invalidation_reason=reason,
                room_invalidated_at=None,
                room_invalidation_reason=None,
            )

        async def append_event(
            _room_id: str,
            _thread_id: str,
            event: dict[str, object],
        ) -> bool:
            raw_events.append(event)
            raw_append_committed.set()
            return True

        async def fetch_fresh_history(
            _room_id: str,
            _thread_id: str,
            _allow_durable_cache: bool,
        ) -> ThreadHistoryResult:
            thread_state["value"] = ThreadCacheState(
                validated_at=time.time(),
                invalidated_at=None,
                invalidation_reason=None,
                room_invalidated_at=None,
                room_invalidation_reason=None,
            )
            return thread_history_result(
                [
                    _message(event_id="$thread:localhost", body="Root"),
                    _message(event_id="$reply-old:localhost", body="Old reply"),
                    _message(event_id="$reply-new:localhost", body="New reply"),
                ],
                is_full_history=True,
                diagnostics={THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER},
            )

        event_cache.get_thread_cache_state = AsyncMock(side_effect=lambda *_args, **_kwargs: thread_state["value"])
        event_cache.get_thread_events = AsyncMock(side_effect=lambda *_args, **_kwargs: list(raw_events))
        event_cache.get_thread_id_for_event = AsyncMock(return_value="$thread:localhost")
        event_cache.mark_thread_stale = AsyncMock(side_effect=mark_thread_stale)
        event_cache.append_event = AsyncMock(side_effect=append_event)
        access._reads._wait_for_pending_room_cache_updates = AsyncMock(side_effect=pause_reader)
        access._reads.fetch_thread_history_from_client = AsyncMock(side_effect=fetch_fresh_history)
        new_event_source = {
            "event_id": "$reply-new:localhost",
            "sender": "@agent:localhost",
            "origin_server_ts": 3000,
            "type": "m.room.message",
            "content": {
                "body": "New reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread:localhost"},
            },
        }
        new_event_info = EventInfo.from_event(new_event_source)

        read_task = asyncio.create_task(access.get_thread_history("!room:localhost", "$thread:localhost"))
        await asyncio.wait_for(reader_ready.wait(), timeout=1.0)
        write_task = asyncio.create_task(
            access._writes._record_outbound_message_update(
                "!room:localhost",
                "$reply-new:localhost",
                new_event_source,
                new_event_info,
            ),
        )
        await asyncio.wait_for(raw_append_committed.wait(), timeout=1.0)
        allow_reader_continue.set()
        history = await read_task
        await write_task

        assert [message.body for message in history] == ["Root", "Old reply", "New reply"]
        access._reads.fetch_thread_history_from_client.assert_awaited_once_with(
            "!room:localhost",
            "$thread:localhost",
            True,
        )

    @pytest.mark.asyncio
    async def test_latest_thread_event_lookup_refetches_invalidated_thread_tail(
        self,
        tmp_path: Path,
    ) -> None:
        """MSC3440 fallback should use the refetched latest visible thread event, not a stale cached tail."""
        event_cache = _EventCache(tmp_path / "event_cache.db")
        await event_cache.initialize()
        root_event = _text_event(
            event_id="$thread_root:localhost",
            body="Root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        old_reply = _text_event(
            event_id="$reply_old:localhost",
            body="Old tail",
            sender="@agent:localhost",
            server_timestamp=2000,
            thread_id="$thread_root:localhost",
        )
        new_reply = _text_event(
            event_id="$reply_new:localhost",
            body="New tail",
            sender="@agent:localhost",
            server_timestamp=3000,
            thread_id="$thread_root:localhost",
        )
        initial_client = _relations_client(
            root_event=root_event,
            thread_events=[old_reply],
            next_batch="s_initial",
        )
        refreshed_client = _relations_client(
            root_event=root_event,
            thread_events=[old_reply, new_reply],
            next_batch="s_new_tail",
        )

        try:
            access = MatrixConversationCache(
                logger=MagicMock(),
                runtime=_conversation_runtime(client=initial_client, event_cache=event_cache),
            )
            await access.get_thread_history("!test:localhost", "$thread_root:localhost")
            await event_cache.mark_thread_stale(
                "!test:localhost",
                "$thread_root:localhost",
                reason="test_tail_refresh",
            )

            restarted_access = MatrixConversationCache(
                logger=MagicMock(),
                runtime=_conversation_runtime(client=refreshed_client, event_cache=event_cache),
            )
            latest_event_id = await restarted_access.get_latest_thread_event_id_if_needed(
                "!test:localhost",
                "$thread_root:localhost",
            )
        finally:
            await event_cache.close()

        assert latest_event_id == "$reply_new:localhost"
        assert refreshed_client.room_messages.await_count >= 1

    @pytest.mark.asyncio
    async def test_latest_thread_event_lookup_falls_back_to_thread_root_on_refresh_failure(self) -> None:
        """MSC3440 latest-event resolution must fail open when thread refresh fails."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access._reads.fetch_thread_history_from_client = AsyncMock(side_effect=RuntimeError("boom"))

        latest_event_id = await access.get_latest_thread_event_id_if_needed("!test:localhost", "$thread:localhost")

        assert latest_event_id == "$thread:localhost"

    @pytest.mark.asyncio
    async def test_latest_thread_event_lookup_rejects_stale_cached_tail(self) -> None:
        """MSC3440 latest-event resolution must not reuse a stale cached tail after a failed refetch."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access._reads.fetch_thread_history_from_client = AsyncMock(
            return_value=thread_history_result(
                [
                    _message(event_id="$thread:localhost", body="Root"),
                    _message(event_id="$reply:localhost", body="Cached tail"),
                ],
                is_full_history=True,
                diagnostics={THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_STALE_CACHE},
            ),
        )

        latest_event_id = await access.get_latest_thread_event_id_if_needed("!test:localhost", "$thread:localhost")

        assert latest_event_id == "$thread:localhost"

    @pytest.mark.asyncio
    async def test_live_event_cache_update_recovers_after_same_room_failure(self) -> None:
        """A failed same-room cache update should not block the next queued write."""
        first_update_started = asyncio.Event()
        allow_first_failure = asyncio.Event()
        second_update_finished = asyncio.Event()
        owner = object()
        coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(coordinator=coordinator),
        )

        async def failing_update() -> None:
            first_update_started.set()
            await allow_first_failure.wait()
            msg = "update failed"
            raise RuntimeError(msg)

        async def second_update() -> None:
            second_update_finished.set()

        access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: failing_update(),
            name="matrix_cache_first_update",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: second_update(),
            name="matrix_cache_second_update",
        )
        await asyncio.sleep(0)
        assert second_update_finished.is_set() is False

        allow_first_failure.set()
        await wait_for_background_tasks(timeout=1.0, owner=owner)

        assert second_update_finished.is_set()

    @pytest.mark.asyncio
    async def test_shared_event_cache_write_coordinator_serializes_same_room_updates_across_accesses(self) -> None:
        """Same-room cache writes should serialize even when different bots enqueue them."""
        first_update_started = asyncio.Event()
        release_first_update = asyncio.Event()
        second_update_started = asyncio.Event()
        owner = object()
        coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )
        first_access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(coordinator=coordinator),
        )
        second_access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(coordinator=coordinator),
        )

        async def first_update() -> None:
            first_update_started.set()
            await release_first_update.wait()

        async def second_update() -> None:
            second_update_started.set()

        first_access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: first_update(),
            name="matrix_cache_first_update",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        second_access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: second_update(),
            name="matrix_cache_second_update",
        )
        await asyncio.sleep(0)
        assert second_update_started.is_set() is False

        release_first_update.set()
        await wait_for_background_tasks(timeout=1.0, owner=owner)

        assert second_update_started.is_set()

    @pytest.mark.asyncio
    async def test_cancelled_room_cache_update_does_not_start_queued_coro(self) -> None:
        """Cancelling a queued room update before it runs should not invoke its coroutine factory."""
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        queued_update_started = asyncio.Event()
        owner = object()
        coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(coordinator=coordinator),
        )

        async def blocking_update() -> None:
            blocker_started.set()
            await release_blocker.wait()

        async def queued_update() -> None:
            queued_update_started.set()

        access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: blocking_update(),
            name="matrix_cache_blocking_update",
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=1.0)

        queued_task = access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: queued_update(),
            name="matrix_cache_queued_update",
        )
        queued_task.cancel()
        await asyncio.gather(queued_task, return_exceptions=True)

        release_blocker.set()
        await wait_for_background_tasks(timeout=1.0, owner=owner)

        assert queued_update_started.is_set() is False

    @pytest.mark.asyncio
    async def test_cancelled_room_cache_update_keeps_follow_up_update_behind_running_predecessor(self) -> None:
        """Cancelling a queued room update must not break the same-room serialization chain."""
        first_update_started = asyncio.Event()
        release_first_update = asyncio.Event()
        third_update_started = asyncio.Event()
        owner = object()
        coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(coordinator=coordinator),
        )

        async def first_update() -> None:
            first_update_started.set()
            await release_first_update.wait()

        async def cancelled_update() -> None:
            msg = "Cancelled room cache update should not start"
            raise AssertionError(msg)

        async def third_update() -> None:
            third_update_started.set()

        access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: first_update(),
            name="matrix_cache_first_update",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        cancelled_task = access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: cancelled_update(),
            name="matrix_cache_cancelled_update",
        )
        cancelled_task.cancel()
        await asyncio.gather(cancelled_task, return_exceptions=True)

        access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: third_update(),
            name="matrix_cache_third_update",
        )
        await asyncio.sleep(0)
        assert third_update_started.is_set() is False

        release_first_update.set()
        await wait_for_background_tasks(timeout=1.0, owner=owner)

        assert third_update_started.is_set()

    @pytest.mark.asyncio
    async def test_agent_creates_thread_when_mentioned_in_main_room(self, bot: AgentBot) -> None:
        """Test that agents create threads when mentioned in main room messages."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
        room.name = "Test Room"

        # Create a main room message that mentions the agent
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "@mindroom_general Can you help me?",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": ["@mindroom_general:localhost"]},
                },
                "event_id": "$main_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        # The bot should send a response
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost"),
        )

        # Mock thread history fetch (returns empty for new thread)
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"},
                room_id="!test:localhost",
            ),
        )
        bot.event_cache = _runtime_event_cache()
        bot.event_cache.get_thread_events.return_value = None
        bot.event_cache.append_event.return_value = True
        _install_runtime_write_coordinator(bot)

        # Initialize the bot (to set up components it needs)

        # Mock interactive.handle_text_response to return None (not an interactive response)
        # Mock _generate_response to capture the call and send a test response
        bot._generate_response = AsyncMock()
        install_generate_response_mock(bot, bot._generate_response)
        with (
            patch("mindroom.turn_controller.interactive.handle_text_response", AsyncMock(return_value=None)),
        ):
            # Process the message
            await bot._on_message(room, event)

            # Check that _generate_response was called
            bot._generate_response.assert_called_once()

            # Now simulate the response being sent
            await bot._send_response(
                room.room_id,
                event.event_id,
                "I can help you with that!",
                None,
                reply_to_event=event,
            )

        # Check the final response content.
        assert bot.client.room_send.call_count == 1
        content = bot.client.room_send.call_args_list[0].kwargs["content"]

        # The response should create a thread from the original message
        assert "m.relates_to" in content
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$main_msg:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$main_msg:localhost"

    @pytest.mark.asyncio
    async def test_agent_responds_in_existing_thread(self, bot: AgentBot) -> None:
        """Test that agents respond correctly in existing threads."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
        room.name = "Test Room"

        # Create a message in a thread
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "@mindroom_general What about this?",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": ["@mindroom_general:localhost"]},
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$thread_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        # Mock the bot's response
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost"),
        )

        # Mock thread history
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"},
                room_id="!test:localhost",
            ),
        )
        bot.event_cache = _runtime_event_cache()
        bot.event_cache.get_thread_events.return_value = None
        bot.event_cache.append_event.return_value = True
        _install_runtime_write_coordinator(bot)

        # Initialize response tracking

        # Mock interactive.handle_text_response and make AI fast
        with (
            patch("mindroom.turn_controller.interactive.handle_text_response", AsyncMock(return_value=None)),
            patch("mindroom.response_runner.ai_response", AsyncMock(return_value="OK")),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                AsyncMock(return_value="latest_thread_event"),
            ),
            patch.object(
                bot._conversation_cache,
                "get_thread_snapshot",
                AsyncMock(return_value=thread_history_result([], is_full_history=False)),
            ),
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock(return_value=[])),
        ):
            # Process the message
            await bot._on_message(room, event)

        # Verify the bot sent messages (thinking + final)
        assert bot.client.room_send.call_count == 2

        # Check the initial message (first call)
        first_call = bot.client.room_send.call_args_list[0]
        initial_content = first_call.kwargs["content"]
        assert "m.relates_to" in initial_content
        assert initial_content["m.relates_to"]["rel_type"] == "m.thread"
        assert initial_content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert initial_content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$thread_msg:localhost"

    @pytest.mark.asyncio
    async def test_extract_context_edit_uses_thread_from_new_content(self, bot: AgentBot) -> None:
        """Edit events should resolve thread context from m.new_content thread relation."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_msg:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567894,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root"),
            _message(event_id="$thread_msg:localhost", body="Original"),
        ]
        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(return_value=expected_history),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        mock_fetch.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
            allow_durable_cache=False,
        )

    @pytest.mark.asyncio
    async def test_extract_context_edit_resolves_thread_from_original_event(self, bot: AgentBot) -> None:
        """Edits without nested thread metadata should still resolve to the edited message thread."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_msg:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567895,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Thread message",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$thread_msg:localhost",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567893,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root"),
            _message(event_id="$thread_msg:localhost", body="Thread message"),
        ]
        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(return_value=expected_history),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$thread_msg:localhost")
        mock_fetch.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
            allow_durable_cache=False,
        )

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_to_threaded_message_stays_plain_reply(self, bot: AgentBot) -> None:
        """Plain replies should not inherit thread context from earlier threaded messages."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Newest plain reply from non-thread client",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain2:localhost"}},
                },
                "event_id": "$plain3:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567895,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Second plain reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$plain1:localhost"}},
                        },
                        "event_id": "$plain2:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "First plain reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                        },
                        "event_id": "$plain1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567893,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Earlier threaded message",
                            "msgtype": "m.text",
                            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                        },
                        "event_id": "$thread_msg:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567892,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_plain_reply_does_not_require_full_thread_history(
        self,
        bot: AgentBot,
    ) -> None:
        """Dispatch preview should not promote plain replies into deferred thread hydration."""
        _clear_mxc_cache()
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Newest plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain1:localhost"}},
                },
                "event_id": "$incoming:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.download = AsyncMock(
            return_value=MagicMock(
                spec=nio.DownloadResponse,
                body=json.dumps(
                    {
                        "msgtype": "m.text",
                        "body": "Hydrated plain reply from sidecar",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                    },
                ).encode("utf-8"),
            ),
        )
        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Preview plain reply [Message continues in attached file]",
                            "msgtype": "m.file",
                            "info": {"mimetype": "application/json"},
                            "io.mindroom.long_text": {
                                "version": 2,
                                "encoding": "matrix_event_content_json",
                            },
                            "url": "mxc://server/plain1-sidecar",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                        },
                        "event_id": "$plain1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Earlier threaded message",
                            "msgtype": "m.text",
                            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                        },
                        "event_id": "$thread_msg:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        with (
            patch.object(bot._conversation_cache, "get_thread_snapshot", new=AsyncMock()) as mock_snapshot,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(),
            ) as mock_fetch,
        ):
            preview_context = await bot._conversation_resolver.extract_dispatch_context(room, event)

            assert preview_context.is_thread is False
            assert preview_context.thread_id is None
            assert preview_context.thread_history == []
            assert preview_context.requires_full_thread_history is False
            bot.client.download.assert_not_awaited()
            mock_snapshot.assert_not_called()
            mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_command_as_reply_doesnt_cause_thread_error(self, tmp_path: Path) -> None:
        """Plain-reply commands should stay plain replies without thread promotion."""
        # Create a router bot to handle commands
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password=TEST_PASSWORD,
            display_name="Router",
            agent_name="router",
        )

        config = _runtime_bound_config(
            Config(
                agents={"router": AgentConfig(display_name="Router", rooms=["!test:localhost"])},
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
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        wrap_extracted_collaborators(bot)

        # Mock the orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        # Create a mock client
        bot.client = _make_client_mock(user_id="@mindroom_router:localhost")
        bot.event_cache = _runtime_event_cache()
        bot.event_cache_write_coordinator = _install_runtime_write_coordinator(bot)

        # Initialize components that depend on client

        # Mock the agent to return a response
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "I can help you with that!"

        # Make the agent's arun method return the response
        async def mock_arun(*_args: object, **_kwargs: object) -> MagicMock:
            return mock_response

        mock_agent.arun = mock_arun

        with patch("mindroom.bot.create_agent", return_value=mock_agent):
            room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
            room.name = "Test Room"

            # Create a command that's a reply to another message (not in a thread)
            event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "!help",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$some_other_msg:localhost"}},
                    },
                    "event_id": "$cmd_reply:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )

            # Mock the bot's response - it should succeed
            bot.client.room_send = AsyncMock(
                return_value=nio.RoomSendResponse.from_dict(
                    {"event_id": "$response:localhost"},
                    room_id="!test:localhost",
                ),
            )

            with (
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_snapshot",
                    AsyncMock(return_value=thread_history_result([], is_full_history=False)),
                ),
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_history",
                    AsyncMock(return_value=[]),
                ),
            ):
                # Process the command
                await bot._on_message(room, event)

            # The bot should send an error message about needing threads
            bot.client.room_send.assert_called_once()

            # Check the content
            call_args = bot.client.room_send.call_args
            content = call_args.kwargs["content"]

            assert "m.relates_to" in content
            assert "rel_type" not in content["m.relates_to"]
            assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$cmd_reply:localhost"

    @pytest.mark.asyncio
    async def test_command_in_thread_works_correctly(self, tmp_path: Path) -> None:
        """Test that commands in threads work without errors."""
        # Create a router bot to handle commands
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password=TEST_PASSWORD,
            display_name="Router",
            agent_name="router",
        )

        config = _runtime_bound_config(
            Config(
                agents={"router": AgentConfig(display_name="Router", rooms=["!test:localhost"])},
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
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        wrap_extracted_collaborators(bot)
        # Mock the orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        # Create a mock client
        bot.client = _make_client_mock(user_id="@mindroom_router:localhost")
        bot.event_cache = _runtime_event_cache()
        bot.event_cache_write_coordinator = _install_runtime_write_coordinator(bot)

        # Initialize components that depend on client

        # Mock the agent to return a response
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "I can help you with that!"

        # Make the agent's arun method return the response
        async def mock_arun(*_args: object, **_kwargs: object) -> MagicMock:
            return mock_response

        mock_agent.arun = mock_arun

        with patch("mindroom.bot.create_agent", return_value=mock_agent):
            room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
            room.name = "Test Room"

            # Create a command in a thread
            event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "!list_schedules",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$cmd_thread:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )

            # Mock room_get_state for list_schedules command
            bot.client.room_get_state = AsyncMock(
                return_value=nio.RoomGetStateResponse.from_dict(
                    [],  # No scheduled tasks
                    room_id="!test:localhost",
                ),
            )

            # Mock the bot's response
            bot.client.room_send = AsyncMock(
                return_value=nio.RoomSendResponse.from_dict(
                    {"event_id": "$response:localhost"},
                    room_id="!test:localhost",
                ),
            )

            with (
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_snapshot",
                    AsyncMock(return_value=thread_history_result([], is_full_history=False)),
                ),
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_history",
                    AsyncMock(return_value=[]),
                ),
            ):
                # Process the command
                await bot._on_message(room, event)

            # The bot should respond
            bot.client.room_send.assert_called_once()

            # Check the content
            call_args = bot.client.room_send.call_args
            content = call_args.kwargs["content"]

            # The response should be in the same thread
            assert "m.relates_to" in content
            assert content["m.relates_to"]["rel_type"] == "m.thread"
            assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
            assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$cmd_thread:localhost"

    @pytest.mark.asyncio
    async def test_command_reply_to_thread_message_stays_plain_reply(self, tmp_path: Path) -> None:
        """Plain replies to threaded messages should not get upgraded into threads."""
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password=TEST_PASSWORD,
            display_name="Router",
            agent_name="router",
        )

        config = _runtime_bound_config(
            Config(
                agents={"router": AgentConfig(display_name="Router", rooms=["!test:localhost"])},
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
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        wrap_extracted_collaborators(bot)
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        bot.client = _make_client_mock(user_id="@mindroom_router:localhost")
        bot.event_cache = _runtime_event_cache()
        bot.event_cache_write_coordinator = _install_runtime_write_coordinator(bot)

        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "!help",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                },
                "event_id": "$cmd_reply_plain:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict(
                {"event_id": "$response:localhost"},
                room_id="!test:localhost",
            ),
        )

        with (
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_history",
                AsyncMock(return_value=[]),
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_snapshot",
                AsyncMock(return_value=thread_history_result([], is_full_history=False)),
            ),
        ):
            await bot._on_message(room, event)

        bot.client.room_send.assert_called_once()
        content = bot.client.room_send.call_args.kwargs["content"]
        assert "rel_type" not in content["m.relates_to"]
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$cmd_reply_plain:localhost"

    @pytest.mark.asyncio
    async def test_router_routing_reply_to_thread_message_uses_existing_thread_root(self, tmp_path: Path) -> None:
        """Router routing should resolve plain replies back to the real thread root."""
        agent_user = AgentMatrixUser(
            user_id="@mindroom_router:localhost",
            password=TEST_PASSWORD,
            display_name="Router",
            agent_name="router",
        )

        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(display_name="General", rooms=["!test:localhost"]),
                },
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
            enable_streaming=False,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        wrap_extracted_collaborators(bot)
        mock_orchestrator = MagicMock()
        mock_orchestrator.current_config = config
        bot.orchestrator = mock_orchestrator

        bot.client = _make_client_mock(user_id="@mindroom_router:localhost")
        bot.event_cache = _runtime_event_cache()
        bot.event_cache_write_coordinator = _install_runtime_write_coordinator(bot)

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Can someone help with this?",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                },
                "event_id": "$plain_reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Earlier message in thread",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$thread_msg:localhost",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567889,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        with (
            patch("mindroom.turn_controller.suggest_agent_for_message", AsyncMock(return_value="general")),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                AsyncMock(return_value="$latest:localhost"),
            ),
            patch.object(
                bot._conversation_cache,
                "get_thread_snapshot",
                AsyncMock(return_value=thread_history_result([], is_full_history=False)),
            ),
            patch(
                "mindroom.delivery_gateway.send_message_result",
                AsyncMock(
                    return_value=DeliveredMatrixEvent(
                        event_id="$router_response:localhost",
                        content_sent={"body": "router relay"},
                    ),
                ),
            ) as mock_send,
        ):
            await bot._turn_controller._execute_router_relay(
                room,
                event,
                thread_history=[],
                thread_id="$thread_root:localhost",
                requester_user_id="@user:localhost",
            )

        mock_send.assert_awaited_once()
        bot.client.room_get_event.assert_not_called()
        content = mock_send.call_args.args[2]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$plain_reply:localhost"

    @pytest.mark.asyncio
    async def test_message_with_multiple_relations_handled_correctly(self, bot: AgentBot) -> None:
        """Test that messages with complex relations are handled properly."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.client.user_id)
        room.name = "Test Room"

        # Create a message that's both in a thread AND a reply (complex relations)
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "@mindroom_general Complex question?",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": ["@mindroom_general:localhost"]},
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread_root:localhost",
                        "m.in_reply_to": {"event_id": "$previous_msg:localhost"},
                    },
                },
                "event_id": "$complex_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        # Mock the bot's response
        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$response:localhost"}, room_id="!test:localhost"),
        )

        # Mock thread history
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse.from_dict(
                {"chunk": [], "start": "s1", "end": "e1"},
                room_id="!test:localhost",
            ),
        )
        bot.event_cache = _runtime_event_cache()
        bot.event_cache.get_thread_events.return_value = None
        bot.event_cache.append_event.return_value = True
        _install_runtime_write_coordinator(bot)

        # Initialize response tracking

        # Mock interactive.handle_text_response and generate_response
        bot._generate_response = AsyncMock()
        install_generate_response_mock(bot, bot._generate_response)
        with (
            patch("mindroom.turn_controller.interactive.handle_text_response", AsyncMock(return_value=None)),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_snapshot",
                AsyncMock(return_value=thread_history_result([], is_full_history=False)),
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_history",
                AsyncMock(return_value=[]),
            ),
        ):
            # Process the message
            await bot._on_message(room, event)

            # Check that _generate_response was called
            bot._generate_response.assert_called_once()

            # Now simulate the response being sent
            await bot._send_response(
                room.room_id,
                event.event_id,
                "I can help with that complex question!",
                "$thread_root:localhost",
            )

        # Check the final response content.
        assert bot.client.room_send.call_count == 1
        content = bot.client.room_send.call_args_list[0].kwargs["content"]

        # The response should maintain the thread context
        assert "m.relates_to" in content
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$complex_msg:localhost"
