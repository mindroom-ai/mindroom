"""Test threading behavior to reproduce and fix the threading error.

This test verifies that:
1. Agents always respond in threads (never in main room)
2. Commands that are replies don't cause threading errors
3. The bot handles various message relation scenarios correctly
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio

from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.matrix.client import PermanentMatrixStartupError, ResolvedVisibleMessage, ThreadHistoryResult
from mindroom.matrix.conversation_access import MatrixConversationAccess
from mindroom.matrix.event_cache import EventCache
from mindroom.matrix.event_cache_write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import _clear_mxc_cache
from mindroom.matrix.reply_chain import ReplyChainCaches, _merge_thread_and_chain_history
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_generate_response_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


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


def _state_writer(bot: AgentBot) -> object:
    """Return the writer instance actually captured by the resolver."""
    return unwrap_extracted_collaborator(bot._conversation_state_writer)


def _make_client_mock(*, user_id: str = "@mindroom_general:localhost") -> AsyncMock:
    """Return one AsyncClient-shaped mock with sync-token support for bot tests."""
    client = make_matrix_client_mock(user_id=user_id)
    client.homeserver = "http://localhost:8008"
    return client


def _conversation_runtime(
    *,
    client: nio.AsyncClient | None = None,
    event_cache: EventCache | None = None,
    coordinator: EventCacheWriteCoordinator | None = None,
) -> BotRuntimeState:
    """Build one minimal live runtime state for conversation-access tests."""
    return BotRuntimeState(
        client=client,
        config=MagicMock(spec=Config),
        enable_streaming=True,
        orchestrator=None,
        event_cache=event_cache,
        event_cache_write_coordinator=coordinator,
    )


def _install_runtime_write_coordinator(bot: AgentBot) -> EventCacheWriteCoordinator:
    """Attach one explicit runtime write coordinator to a bot test double."""
    coordinator = EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=bot._runtime_view,
    )
    bot.event_cache_write_coordinator = coordinator
    return coordinator


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
            assert bot.event_cache is not None
            assert bot.client is start_client
            assert bot.event_cache_write_coordinator is not None

            await bot.stop(reason="test")

        assert bot.event_cache is None
        assert bot.event_cache_write_coordinator is None
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

        shared_cache = EventCache(bot.config.cache.resolve_db_path(bot.runtime_paths))
        await shared_cache.initialize()
        bot.event_cache = shared_cache
        other_bot.event_cache = shared_cache

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
            assert bot.event_cache is None
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
        bot.event_cache = MagicMock(spec=EventCache)

        with pytest.raises(
            RuntimeError,
            match="Runtime support services must be injected all together or not at all",
        ):
            await bot._initialize_runtime_support_services()

    @pytest.mark.asyncio
    async def test_try_start_partial_runtime_support_injection_fails_before_login(self, bot: AgentBot) -> None:
        """Mixed runtime support injection should stop startup before any login side effects."""
        bot.client = None
        bot.event_cache = MagicMock(spec=EventCache)

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
    async def test_standalone_runtime_support_survives_event_cache_init_failure(self, bot: AgentBot) -> None:
        """Standalone startup should degrade cleanly to no runtime support when SQLite cache init fails."""
        with patch("mindroom.runtime_support.EventCache.initialize", AsyncMock(side_effect=RuntimeError("boom"))):
            await bot._initialize_runtime_support_services()
            await bot._initialize_runtime_support_services()

        assert bot.event_cache is None
        assert bot.event_cache_write_coordinator is None

        await bot._close_runtime_support_services()

        assert bot.event_cache is None
        assert bot.event_cache_write_coordinator is None

    @pytest.mark.asyncio
    async def test_sync_response_caches_timeline_events_for_point_lookups(self, bot: AgentBot) -> None:
        """Sync-response handling should persist timeline events into SQLite-backed lookups."""
        await bot._initialize_runtime_support_services()
        assert bot.event_cache is not None

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
    async def test_cache_sync_timeline_schedules_background_write(self, bot: AgentBot) -> None:
        """Sync timeline caching should return before a slow cache write finishes."""
        store_started = asyncio.Event()
        allow_store_finish = asyncio.Event()

        async def slow_store_events_batch(_events: object) -> None:
            store_started.set()
            await allow_store_finish.wait()

        event_cache = MagicMock(spec=EventCache)
        event_cache.store_events_batch = AsyncMock(side_effect=slow_store_events_batch)
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

        bot._conversation_access.cache_sync_timeline(sync_response)
        await asyncio.wait_for(store_started.wait(), timeout=1.0)

        allow_store_finish.set()
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        event_cache.store_events_batch.assert_awaited_once()
        event_cache.redact_event.assert_not_awaited()

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

        event_cache = MagicMock(spec=EventCache)
        event_cache.store_events_batch = AsyncMock(side_effect=slow_store_events_batch)
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

        bot._conversation_access.cache_sync_timeline(first_sync_response)
        await asyncio.wait_for(store_started.wait(), timeout=1.0)

        bot._conversation_access.cache_sync_timeline(second_sync_response)
        await asyncio.sleep(0)
        event_cache.redact_event.assert_not_awaited()

        allow_store_finish.set()
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        assert call_order == ["store-start", "store-finish", "redact"]

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

        event_cache = MagicMock(spec=EventCache)
        event_cache.store_events_batch = AsyncMock(side_effect=store_events_batch)
        event_cache.redact_event = AsyncMock()
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        bot._conversation_access.cache_sync_timeline(sync_response_for("!room-a:localhost", "$room_a_msg:localhost"))
        await asyncio.wait_for(room_a_started.wait(), timeout=1.0)

        bot._conversation_access.cache_sync_timeline(sync_response_for("!room-b:localhost", "$room_b_msg:localhost"))
        await asyncio.wait_for(room_b_finished.wait(), timeout=1.0)

        release_room_a.set()
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        assert event_cache.store_events_batch.await_count == 2

    @pytest.mark.asyncio
    async def test_live_redaction_callback_removes_persisted_lookup_event(self, bot: AgentBot) -> None:
        """Live redaction callbacks should remove point-lookup cache entries."""
        await bot._initialize_runtime_support_services()
        assert bot.event_cache is not None

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
        await bot._initialize_runtime_support_services()
        assert bot.event_cache is not None

        try:
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

            bot._conversation_access.cache_sync_timeline(sync_response)
            await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)
            cached_event = await bot.event_cache.get_event("!test:localhost", "$thread_msg:localhost")
        finally:
            await bot._close_runtime_support_services()

        assert cached_event is None

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
        event_cache = MagicMock(spec=EventCache)
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

        await bot._conversation_access.append_live_event(
            "!test:localhost",
            edit_event,
            event_info=EventInfo.from_event(edit_event.source),
        )

        event_cache.get_thread_id_for_event.assert_awaited_once_with("!test:localhost", "$thread_msg:localhost")
        event_cache.append_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_redaction_cache_lookup_failure_still_attempts_cache_delete(self, bot: AgentBot) -> None:
        """Redaction callbacks should continue even when the thread lookup cannot read SQLite."""
        event_cache = MagicMock(spec=EventCache)
        event_cache.get_thread_id_for_event = AsyncMock(side_effect=RuntimeError("database is locked"))
        event_cache.redact_event = AsyncMock(return_value=False)
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

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

        await bot._conversation_access.apply_redaction("!test:localhost", redaction_event)

        event_cache.get_thread_id_for_event.assert_awaited_once_with("!test:localhost", "$thread_msg:localhost")
        event_cache.redact_event.assert_awaited_once_with("!test:localhost", "$thread_msg:localhost")

    @pytest.mark.asyncio
    async def test_live_event_cache_update_recovers_after_same_room_failure(self) -> None:
        """A failed same-room cache update should not block the next queued write."""
        first_update_started = asyncio.Event()
        allow_first_failure = asyncio.Event()
        second_update_finished = asyncio.Event()
        owner = object()
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )
        access = MatrixConversationAccess(
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

        access._queue_room_cache_update(
            "!test:localhost",
            lambda: failing_update(),
            name="matrix_cache_first_update",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        access._queue_room_cache_update(
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
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )
        first_access = MatrixConversationAccess(
            logger=MagicMock(),
            runtime=_conversation_runtime(coordinator=coordinator),
        )
        second_access = MatrixConversationAccess(
            logger=MagicMock(),
            runtime=_conversation_runtime(coordinator=coordinator),
        )

        async def first_update() -> None:
            first_update_started.set()
            await release_first_update.wait()

        async def second_update() -> None:
            second_update_started.set()

        first_access._queue_room_cache_update(
            "!test:localhost",
            lambda: first_update(),
            name="matrix_cache_first_update",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        second_access._queue_room_cache_update(
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
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )
        access = MatrixConversationAccess(
            logger=MagicMock(),
            runtime=_conversation_runtime(coordinator=coordinator),
        )

        async def blocking_update() -> None:
            blocker_started.set()
            await release_blocker.wait()

        async def queued_update() -> None:
            queued_update_started.set()

        access._queue_room_cache_update(
            "!test:localhost",
            lambda: blocking_update(),
            name="matrix_cache_blocking_update",
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=1.0)

        queued_task = access._queue_room_cache_update(
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
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )
        access = MatrixConversationAccess(
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

        access._queue_room_cache_update(
            "!test:localhost",
            lambda: first_update(),
            name="matrix_cache_first_update",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        cancelled_task = access._queue_room_cache_update(
            "!test:localhost",
            lambda: cancelled_update(),
            name="matrix_cache_cancelled_update",
        )
        cancelled_task.cancel()
        await asyncio.gather(cancelled_task, return_exceptions=True)

        access._queue_room_cache_update(
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
    async def test_get_thread_history_skips_incremental_refresh_when_sync_is_fresh(self) -> None:
        """Fresh sync activity should disable the incremental Matrix room scan on cache hits."""
        runtime = _conversation_runtime(
            client=_make_client_mock(),
            event_cache=MagicMock(spec=EventCache),
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationAccess(
            logger=MagicMock(),
            runtime=runtime,
        )
        cached_history = ThreadHistoryResult([], is_full_history=True)

        with patch(
            "mindroom.matrix.conversation_access.fetch_thread_history",
            new=AsyncMock(return_value=cached_history),
        ) as mock_fetch_thread_history:
            history = await access.get_thread_history("!room:localhost", "$thread")

        mock_fetch_thread_history.assert_awaited_once_with(
            runtime.client,
            "!room:localhost",
            "$thread",
            event_cache=runtime.event_cache,
            refresh_cache=False,
        )
        assert isinstance(history, ThreadHistoryResult)
        assert history.thread_version == 0

    @pytest.mark.asyncio
    async def test_get_thread_history_reuses_resolved_cache_across_turns(self) -> None:
        """A second turn on the same thread should hit the resolved cache instead of rebuilding."""
        runtime = _conversation_runtime(client=_make_client_mock())
        access = MatrixConversationAccess(
            logger=MagicMock(),
            runtime=runtime,
        )
        initial_history = ThreadHistoryResult(
            [_message(event_id="$thread", body="Root"), _message(event_id="$reply", body="Reply")],
            is_full_history=True,
        )

        with patch(
            "mindroom.matrix.conversation_access.fetch_thread_history",
            new=AsyncMock(return_value=initial_history),
        ) as first_fetch:
            async with access.turn_scope():
                first_history = await access.get_thread_history("!room:localhost", "$thread")

        with patch(
            "mindroom.matrix.conversation_access.fetch_thread_history",
            new=AsyncMock(side_effect=AssertionError("should use resolved cache")),
        ) as second_fetch:
            async with access.turn_scope():
                second_history = await access.get_thread_history("!room:localhost", "$thread")

        first_fetch.assert_awaited_once()
        second_fetch.assert_not_awaited()
        assert [message.event_id for message in first_history] == ["$thread", "$reply"]
        assert [message.event_id for message in second_history] == ["$thread", "$reply"]
        assert second_history.thread_version == 0

    @pytest.mark.asyncio
    async def test_get_thread_history_incrementally_refreshes_resolved_cache_from_sync(self, tmp_path: Path) -> None:
        """A cached thread with one new sync-delivered reply should append incrementally."""
        cache = EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=_make_client_mock(),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationAccess(
            logger=MagicMock(),
            runtime=runtime,
        )

        root_event = {
            "event_id": "$thread",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {"body": "Root", "msgtype": "m.text"},
        }
        first_reply = {
            "event_id": "$reply-1",
            "sender": "@agent:localhost",
            "origin_server_ts": 2000,
            "type": "m.room.message",
            "content": {
                "body": "Reply 1",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
            },
        }
        await cache.store_thread_events("!room:localhost", "$thread", [root_event, first_reply])

        try:
            async with access.turn_scope():
                initial_history = await access.get_thread_history("!room:localhost", "$thread")

            second_reply_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "Reply 2",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
                    },
                    "event_id": "$reply-2",
                    "sender": "@agent:localhost",
                    "origin_server_ts": 3000,
                    "room_id": "!room:localhost",
                    "type": "m.room.message",
                },
            )
            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {
                "!room:localhost": MagicMock(timeline=MagicMock(events=[second_reply_event])),
            }

            access.cache_sync_timeline(sync_response)
            await wait_for_background_tasks(timeout=1.0, owner=runtime)

            with patch(
                "mindroom.matrix.conversation_access.fetch_thread_history",
                new=AsyncMock(side_effect=AssertionError("should use incremental resolved cache refresh")),
            ) as mock_fetch_thread_history:
                async with access.turn_scope():
                    refreshed_history = await access.get_thread_history("!room:localhost", "$thread")

            mock_fetch_thread_history.assert_not_awaited()
        finally:
            await cache.close()

        assert [message.event_id for message in initial_history] == ["$thread", "$reply-1"]
        assert [message.event_id for message in refreshed_history] == ["$thread", "$reply-1", "$reply-2"]
        assert refreshed_history.thread_version == 1

    @pytest.mark.asyncio
    async def test_get_thread_history_waits_for_pending_sync_write_before_refresh(self, tmp_path: Path) -> None:
        """A read issued immediately after sync should wait for the queued SQLite write."""
        cache = EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=_make_client_mock(),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationAccess(
            logger=MagicMock(),
            runtime=runtime,
        )

        root_event = {
            "event_id": "$thread",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {"body": "Root", "msgtype": "m.text"},
        }
        first_reply = {
            "event_id": "$reply-1",
            "sender": "@agent:localhost",
            "origin_server_ts": 2000,
            "type": "m.room.message",
            "content": {
                "body": "Reply 1",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
            },
        }
        await cache.store_thread_events("!room:localhost", "$thread", [root_event, first_reply])

        try:
            async with access.turn_scope():
                await access.get_thread_history("!room:localhost", "$thread")

            second_reply_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "Reply 2",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
                    },
                    "event_id": "$reply-2",
                    "sender": "@agent:localhost",
                    "origin_server_ts": 3000,
                    "room_id": "!room:localhost",
                    "type": "m.room.message",
                },
            )
            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {
                "!room:localhost": MagicMock(timeline=MagicMock(events=[second_reply_event])),
            }

            write_started = asyncio.Event()
            release_write = asyncio.Event()
            original_persist = access._persist_room_sync_timeline_updates

            async def blocked_persist(
                event_cache: EventCache,
                room_id: str,
                cached_events: list[tuple[str, str, dict[str, object]]],
                redacted_event_ids: list[str],
                threaded_events: list[dict[str, object]],
            ) -> None:
                write_started.set()
                await release_write.wait()
                await original_persist(event_cache, room_id, cached_events, redacted_event_ids, threaded_events)

            with (
                patch.object(access, "_persist_room_sync_timeline_updates", new=blocked_persist),
                patch(
                    "mindroom.matrix.conversation_access.fetch_thread_history",
                    new=AsyncMock(side_effect=AssertionError("should use incremental resolved cache refresh")),
                ) as mock_fetch_thread_history,
            ):
                access.cache_sync_timeline(sync_response)
                await asyncio.wait_for(write_started.wait(), timeout=1.0)

                async def read_history() -> ThreadHistoryResult:
                    async with access.turn_scope():
                        return await access.get_thread_history("!room:localhost", "$thread")

                history_task = asyncio.create_task(read_history())
                await asyncio.sleep(0)
                assert history_task.done() is False

                release_write.set()
                refreshed_history = await asyncio.wait_for(history_task, timeout=1.0)

            mock_fetch_thread_history.assert_not_awaited()
        finally:
            await cache.close()

        assert [message.event_id for message in refreshed_history] == ["$thread", "$reply-1", "$reply-2"]
        assert refreshed_history.thread_version == 1

    @pytest.mark.asyncio
    async def test_get_thread_history_invalidates_resolved_cache_on_sync_edit(self, tmp_path: Path) -> None:
        """Edit deltas should invalidate the resolved cache so the full resolver re-runs."""
        cache = EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=_make_client_mock(),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationAccess(
            logger=MagicMock(),
            runtime=runtime,
        )

        root_event = {
            "event_id": "$thread",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {"body": "Root", "msgtype": "m.text"},
        }
        first_reply = {
            "event_id": "$reply-1",
            "sender": "@agent:localhost",
            "origin_server_ts": 2000,
            "type": "m.room.message",
            "content": {
                "body": "Reply 1",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
            },
        }
        await cache.store_thread_events("!room:localhost", "$thread", [root_event, first_reply])

        try:
            async with access.turn_scope():
                await access.get_thread_history("!room:localhost", "$thread")

            edit_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "* Reply 1 edited",
                        "msgtype": "m.text",
                        "m.new_content": {
                            "body": "Reply 1 edited",
                            "msgtype": "m.text",
                            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
                        },
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply-1"},
                    },
                    "event_id": "$edit-1",
                    "sender": "@agent:localhost",
                    "origin_server_ts": 3000,
                    "room_id": "!room:localhost",
                    "type": "m.room.message",
                },
            )
            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {
                "!room:localhost": MagicMock(timeline=MagicMock(events=[edit_event])),
            }
            access.cache_sync_timeline(sync_response)
            await wait_for_background_tasks(timeout=1.0, owner=runtime)

            refreshed_history = ThreadHistoryResult(
                [
                    _message(event_id="$thread", body="Root"),
                    _message(event_id="$reply-1", body="Reply 1 edited", sender="@agent:localhost"),
                ],
                is_full_history=True,
            )
            with patch(
                "mindroom.matrix.conversation_access.fetch_thread_history",
                new=AsyncMock(return_value=refreshed_history),
            ) as mock_fetch_thread_history:
                async with access.turn_scope():
                    history = await access.get_thread_history("!room:localhost", "$thread")

            mock_fetch_thread_history.assert_awaited_once_with(
                runtime.client,
                "!room:localhost",
                "$thread",
                event_cache=runtime.event_cache,
                refresh_cache=False,
            )
        finally:
            await cache.close()

        assert [message.body for message in history] == ["Root", "Reply 1 edited"]

    @pytest.mark.asyncio
    async def test_get_thread_history_invalidates_resolved_cache_on_sync_redaction(self, tmp_path: Path) -> None:
        """Sync-delivered redactions should invalidate the resolved cache before serving history."""
        cache = EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=_make_client_mock(),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationAccess(
            logger=MagicMock(),
            runtime=runtime,
        )

        root_event = {
            "event_id": "$thread",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {"body": "Root", "msgtype": "m.text"},
        }
        first_reply = {
            "event_id": "$reply-1",
            "sender": "@agent:localhost",
            "origin_server_ts": 2000,
            "type": "m.room.message",
            "content": {
                "body": "Reply 1",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
            },
        }
        await cache.store_thread_events("!room:localhost", "$thread", [root_event, first_reply])

        try:
            async with access.turn_scope():
                await access.get_thread_history("!room:localhost", "$thread")

            redaction_event = nio.RedactionEvent.from_dict(
                {
                    "content": {"reason": "remove"},
                    "event_id": "$redaction-1",
                    "redacts": "$reply-1",
                    "sender": "@user:localhost",
                    "origin_server_ts": 3000,
                    "room_id": "!room:localhost",
                    "type": "m.room.redaction",
                },
            )
            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {
                "!room:localhost": MagicMock(timeline=MagicMock(events=[redaction_event])),
            }

            write_started = asyncio.Event()
            release_write = asyncio.Event()
            original_persist = access._persist_room_sync_timeline_updates

            async def blocked_persist(
                event_cache: EventCache,
                room_id: str,
                cached_events: list[tuple[str, str, dict[str, object]]],
                redacted_event_ids: list[str],
                threaded_events: list[dict[str, object]],
            ) -> None:
                write_started.set()
                await release_write.wait()
                await original_persist(event_cache, room_id, cached_events, redacted_event_ids, threaded_events)

            refreshed_history = ThreadHistoryResult(
                [_message(event_id="$thread", body="Root")],
                is_full_history=True,
            )
            with (
                patch.object(access, "_persist_room_sync_timeline_updates", new=blocked_persist),
                patch(
                    "mindroom.matrix.conversation_access.fetch_thread_history",
                    new=AsyncMock(return_value=refreshed_history),
                ) as mock_fetch_thread_history,
            ):
                access.cache_sync_timeline(sync_response)
                await asyncio.wait_for(write_started.wait(), timeout=1.0)

                async def read_history() -> ThreadHistoryResult:
                    async with access.turn_scope():
                        return await access.get_thread_history("!room:localhost", "$thread")

                history_task = asyncio.create_task(read_history())
                await asyncio.sleep(0)
                assert history_task.done() is False

                release_write.set()
                history = await asyncio.wait_for(history_task, timeout=1.0)

            mock_fetch_thread_history.assert_awaited_once_with(
                runtime.client,
                "!room:localhost",
                "$thread",
                event_cache=runtime.event_cache,
                refresh_cache=False,
            )
        finally:
            await cache.close()

        assert [message.event_id for message in history] == ["$thread"]
        assert history.thread_version == 1

    @pytest.mark.asyncio
    async def test_get_thread_history_invalidates_resolved_cache_on_redaction(self, tmp_path: Path) -> None:
        """Redactions should drop the resolved cache entry instead of serving stale replies."""
        cache = EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=_make_client_mock(),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationAccess(
            logger=MagicMock(),
            runtime=runtime,
        )

        root_event = {
            "event_id": "$thread",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {"body": "Root", "msgtype": "m.text"},
        }
        first_reply = {
            "event_id": "$reply-1",
            "sender": "@agent:localhost",
            "origin_server_ts": 2000,
            "type": "m.room.message",
            "content": {
                "body": "Reply 1",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
            },
        }
        await cache.store_thread_events("!room:localhost", "$thread", [root_event, first_reply])

        try:
            async with access.turn_scope():
                await access.get_thread_history("!room:localhost", "$thread")

            redaction_event = nio.RedactionEvent.from_dict(
                {
                    "content": {"reason": "remove"},
                    "event_id": "$redaction-1",
                    "redacts": "$reply-1",
                    "sender": "@user:localhost",
                    "origin_server_ts": 3000,
                    "room_id": "!room:localhost",
                    "type": "m.room.redaction",
                },
            )
            await access.apply_redaction("!room:localhost", redaction_event)
            await wait_for_background_tasks(timeout=1.0, owner=runtime)

            refreshed_history = ThreadHistoryResult(
                [_message(event_id="$thread", body="Root")],
                is_full_history=True,
            )
            with patch(
                "mindroom.matrix.conversation_access.fetch_thread_history",
                new=AsyncMock(return_value=refreshed_history),
            ) as mock_fetch_thread_history:
                async with access.turn_scope():
                    history = await access.get_thread_history("!room:localhost", "$thread")

            mock_fetch_thread_history.assert_awaited_once_with(
                runtime.client,
                "!room:localhost",
                "$thread",
                event_cache=runtime.event_cache,
                refresh_cache=False,
            )
        finally:
            await cache.close()

        assert [message.event_id for message in history] == ["$thread"]

    @pytest.mark.asyncio
    async def test_reply_chain_event_cache_write_through_supports_later_sqlite_lookup(self, bot: AgentBot) -> None:
        """Reply-chain resolution should populate the event cache and later reuse it without network I/O."""
        await bot._initialize_runtime_support_services()
        assert bot.event_cache is not None

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Newest plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$msg2:localhost"}},
                },
                "event_id": "$incoming:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567893,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Second message",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$msg1:localhost"}},
                        },
                        "event_id": "$msg2:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567892,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {"body": "First message", "msgtype": "m.text"},
                        "event_id": "$msg1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567891,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        try:
            with patch.object(bot._conversation_access, "get_thread_history", AsyncMock()) as mock_fetch:
                first_context = await bot._conversation_resolver.extract_message_context(room, event)
            await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

            assert first_context.is_thread is True
            assert first_context.thread_id == "$msg1:localhost"
            assert [msg.event_id for msg in first_context.thread_history] == [
                "$msg1:localhost",
                "$msg2:localhost",
            ]
            assert await bot.event_cache.get_event("!test:localhost", "$msg2:localhost") is not None
            assert await bot.event_cache.get_event("!test:localhost", "$msg1:localhost") is not None
            mock_fetch.assert_not_called()

            bot._conversation_resolver.reply_chain = ReplyChainCaches()
            bot.client.room_get_event = AsyncMock(side_effect=AssertionError("should use persisted cache"))

            with patch.object(bot._conversation_access, "get_thread_history", AsyncMock()) as mock_fetch_again:
                second_context = await bot._conversation_resolver.extract_message_context(room, event)

            assert second_context.is_thread is True
            assert second_context.thread_id == "$msg1:localhost"
            assert [msg.event_id for msg in second_context.thread_history] == [
                "$msg1:localhost",
                "$msg2:localhost",
            ]
            mock_fetch_again.assert_not_called()
            bot.client.room_get_event.assert_not_awaited()
        finally:
            await bot._close_runtime_support_services()

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
            await bot._send_response(room.room_id, event.event_id, "I can help you with that!", None)

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

        # Initialize response tracking

        # Mock interactive.handle_text_response and make AI fast
        with (
            patch("mindroom.turn_controller.interactive.handle_text_response", AsyncMock(return_value=None)),
            patch("mindroom.response_runner.ai_response", AsyncMock(return_value="OK")),
            patch(
                "mindroom.delivery_gateway.get_latest_thread_event_id_if_needed",
                AsyncMock(return_value="latest_thread_event"),
            ),
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
    async def test_extract_context_maps_plain_reply_to_existing_thread(self, bot: AgentBot) -> None:
        """Plain replies to thread messages should resolve to the original thread root."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Follow-up from a non-thread client",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                },
                "event_id": "$reply_plain:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567891,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Agent answer in thread",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$thread_root:localhost",
                        },
                    },
                    "event_id": "$thread_msg:localhost",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root"),
            _message(event_id="$thread_msg:localhost", body="Agent answer in thread"),
        ]
        with patch.object(
            bot._conversation_access,
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
        )

    @pytest.mark.asyncio
    async def test_extract_context_maps_plain_reply_to_thread_root_with_existing_replies(self, bot: AgentBot) -> None:
        """Plain replies to a thread root should load full thread history, not just the root event."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Follow-up from a non-thread client",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_root:localhost"}},
                },
                "event_id": "$reply_plain_root:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567892,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Original root message",
                        "msgtype": "m.text",
                    },
                    "event_id": "$thread_root:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567889,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Original root message"),
            _message(event_id="$thread_msg:localhost", body="Agent answer in thread"),
        ]
        with patch.object(
            bot._conversation_access,
            "get_thread_snapshot",
            AsyncMock(return_value=expected_history),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        mock_fetch.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
        )

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
            bot._conversation_access,
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
            bot._conversation_access,
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
        )

    @pytest.mark.asyncio
    async def test_extract_context_edit_keeps_reply_chain_context_without_threads(self, bot: AgentBot) -> None:
        """Reply-chain edits without thread metadata should keep linear context."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated third message",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated third message",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$msg3:localhost"},
                },
                "event_id": "$edit_msg3:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Third message",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$msg2:localhost"}},
                        },
                        "event_id": "$msg3:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567893,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Second message",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$msg1:localhost"}},
                        },
                        "event_id": "$msg2:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567892,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "First message",
                            "msgtype": "m.text",
                        },
                        "event_id": "$msg1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567891,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        with patch.object(bot._conversation_access, "get_thread_history", AsyncMock()) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$msg1:localhost"
        assert [msg.event_id for msg in context.thread_history] == [
            "$msg1:localhost",
            "$msg2:localhost",
            "$msg3:localhost",
        ]
        assert bot.client.room_get_event.await_count == 3
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_context_builds_reply_chain_history_without_threads(self, bot: AgentBot) -> None:
        """Reply-only chains should still keep linear conversation context."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Third message in a reply-only chain",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$msg2:localhost"}},
                },
                "event_id": "$msg3:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567893,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Second message",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$msg1:localhost"}},
                        },
                        "event_id": "$msg2:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567892,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "First message",
                            "msgtype": "m.text",
                        },
                        "event_id": "$msg1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567891,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        with patch.object(bot._conversation_access, "get_thread_history", AsyncMock()) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$msg1:localhost"
        assert [msg.event_id for msg in context.thread_history] == ["$msg1:localhost", "$msg2:localhost"]
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_context_long_reply_chain_keeps_true_root(self, bot: AgentBot) -> None:
        """Long reply chains should keep a stable root instead of drifting."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        chain_length = 55  # Intentionally exceeds the old fixed depth cap of 50.
        newest_parent_id = f"$msg{chain_length}:localhost"
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": f"{chain_length + 1}th message in reply-only chain",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": newest_parent_id}},
                },
                "event_id": f"$msg{chain_length + 1}:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890 + chain_length + 1,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        responses: list[nio.RoomGetEventResponse] = []
        for i in range(chain_length, 0, -1):
            content = {"body": f"Message {i}", "msgtype": "m.text"}
            if i > 1:
                content["m.relates_to"] = {"m.in_reply_to": {"event_id": f"$msg{i - 1}:localhost"}}

            responses.append(
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": content,
                        "event_id": f"$msg{i}:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567880 + i,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            )

        bot.client.room_get_event = AsyncMock(side_effect=responses)

        with patch.object(bot._conversation_access, "get_thread_history", AsyncMock()) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)
            # Re-resolving should use cached reply-chain nodes and roots.
            context_cached = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context_cached.is_thread is True
        assert context.thread_id == "$msg1:localhost"
        assert context_cached.thread_id == "$msg1:localhost"
        assert len(context.thread_history) == chain_length
        assert context.thread_history[0].event_id == "$msg1:localhost"
        assert context.thread_history[-1].event_id == newest_parent_id
        assert bot.client.room_get_event.await_count == chain_length
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_context_reply_chain_cycle_stops_cleanly(self, bot: AgentBot) -> None:
        """Cycle traversal should terminate without looping forever."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Cycle edge case",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$msg3:localhost"}},
                },
                "event_id": "$incoming:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Message 3",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$msg2:localhost"}},
                        },
                        "event_id": "$msg3:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Message 2",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$msg3:localhost"}},
                        },
                        "event_id": "$msg2:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        with patch.object(bot._conversation_access, "get_thread_history", AsyncMock()) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$msg2:localhost"
        assert [msg.event_id for msg in context.thread_history] == ["$msg2:localhost", "$msg3:localhost"]
        assert bot.client.room_get_event.await_count == 2
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_context_reply_chain_traversal_limit_warns_and_falls_back(self, bot: AgentBot) -> None:
        """Traversal limit should warn and return the oldest resolved event as fallback root."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Deep non-cyclic reply chain",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$msg6:localhost"}},
                },
                "event_id": "$incoming:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        responses: list[nio.RoomGetEventResponse] = []
        for i in range(6, 0, -1):
            content = {"body": f"Message {i}", "msgtype": "m.text"}
            if i > 1:
                content["m.relates_to"] = {"m.in_reply_to": {"event_id": f"$msg{i - 1}:localhost"}}
            responses.append(
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": content,
                        "event_id": f"$msg{i}:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567880 + i,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            )

        bot.client.room_get_event = AsyncMock(side_effect=responses)

        bot._conversation_resolver.reply_chain.traversal_limit = 3
        with (
            patch.object(bot.logger, "warning") as mock_warning,
            patch.object(bot._conversation_access, "get_thread_history", AsyncMock()) as mock_fetch,
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$msg4:localhost"
        assert [msg.event_id for msg in context.thread_history] == [
            "$msg4:localhost",
            "$msg5:localhost",
            "$msg6:localhost",
        ]
        assert bot.client.room_get_event.await_count == 3
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs["traversal_limit"] == 3
        assert mock_warning.call_args.kwargs["traversed_events"] == 3
        assert mock_warning.call_args.kwargs["last_event_id"] == "$msg4:localhost"
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_context_deeper_traversal_overrides_stale_limit_cache(self, bot: AgentBot) -> None:
        """A successful deeper traversal must override a stale root cached from a prior limit hit."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        def _make_chain_responses(length: int) -> list[nio.RoomGetEventResponse]:
            responses: list[nio.RoomGetEventResponse] = []
            for i in range(length, 0, -1):
                content: dict[str, Any] = {"body": f"Message {i}", "msgtype": "m.text"}
                if i > 1:
                    content["m.relates_to"] = {"m.in_reply_to": {"event_id": f"$msg{i - 1}:localhost"}}
                responses.append(
                    nio.RoomGetEventResponse.from_dict(
                        {
                            "content": content,
                            "event_id": f"$msg{i}:localhost",
                            "sender": "@user:localhost",
                            "origin_server_ts": 1234567880 + i,
                            "room_id": "!test:localhost",
                            "type": "m.room.message",
                        },
                    ),
                )
            return responses

        # --- First resolve: limit=3 starting from $msg6, caches stale root $msg4 ---
        event1 = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "First incoming",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$msg6:localhost"}},
                },
                "event_id": "$incoming1:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567900,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(side_effect=_make_chain_responses(6))
        bot._conversation_resolver.reply_chain.traversal_limit = 3
        with patch.object(bot._conversation_access, "get_thread_history", AsyncMock()) as mock_fetch:
            ctx1 = await bot._conversation_resolver.extract_message_context(room, event1)

        assert ctx1.thread_id == "$msg4:localhost"  # stale root from limit hit
        mock_fetch.assert_not_called()

        # --- Second resolve: default limit, new event replies to $msg6 (overlapping cached events) ---
        bot._conversation_resolver.reply_chain.traversal_limit = 500
        event2 = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Second incoming",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$msg6:localhost"}},
                },
                "event_id": "$incoming2:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567901,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        # Node cache already has $msg6..$msg4; supply $msg3..$msg1 for the deeper walk
        bot.client.room_get_event = AsyncMock(side_effect=_make_chain_responses(3))
        with patch.object(bot._conversation_access, "get_thread_history", AsyncMock()) as mock_fetch:
            ctx2 = await bot._conversation_resolver.extract_message_context(room, event2)

        assert ctx2.is_thread is True
        assert ctx2.thread_id == "$msg1:localhost"  # true root, not stale $msg4
        assert ctx2.thread_history[0].event_id == "$msg1:localhost"
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_context_reply_chain_caches_stay_bounded(self, bot: AgentBot) -> None:
        """Reply-chain caches should stay bounded by LRU eviction."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        chain_length = 12
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Check bounded caches",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": f"$msg{chain_length}:localhost"}},
                },
                "event_id": "$incoming:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567990,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        responses: list[nio.RoomGetEventResponse] = []
        for i in range(chain_length, 0, -1):
            content = {"body": f"Message {i}", "msgtype": "m.text"}
            if i > 1:
                content["m.relates_to"] = {"m.in_reply_to": {"event_id": f"$msg{i - 1}:localhost"}}
            responses.append(
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": content,
                        "event_id": f"$msg{i}:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567900 + i,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            )

        bot.client.room_get_event = AsyncMock(side_effect=responses)

        bot._conversation_resolver.reply_chain.nodes.maxsize = 5
        bot._conversation_resolver.reply_chain.roots.maxsize = 5
        with patch.object(bot._conversation_access, "get_thread_history", AsyncMock()) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$msg1:localhost"
        assert len(bot._conversation_resolver.reply_chain.nodes) <= 5
        assert len(bot._conversation_resolver.reply_chain.roots) <= 5
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_context_preserves_plain_replies_before_thread_link(self, bot: AgentBot) -> None:
        """Reply-chain messages should be preserved when chain eventually points to a thread."""
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

        thread_history = [
            _message(event_id="$thread_root:localhost", body="Thread root"),
            _message(event_id="$thread_msg:localhost", body="Earlier threaded message"),
        ]
        with patch.object(
            bot._conversation_access,
            "get_thread_history",
            AsyncMock(return_value=thread_history),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert [msg.event_id for msg in context.thread_history] == [
            "$thread_root:localhost",
            "$thread_msg:localhost",
            "$plain1:localhost",
            "$plain2:localhost",
        ]
        mock_fetch.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
        )

    @pytest.mark.asyncio
    async def test_extract_context_hydrates_sidecar_plain_reply_chain_messages(self, bot: AgentBot) -> None:
        """Plain reply-chain context should hydrate sidecar-backed reply bodies before caching them."""
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

        thread_history = [
            _message(event_id="$thread_root:localhost", body="Thread root"),
            _message(event_id="$thread_msg:localhost", body="Earlier threaded message"),
        ]
        with patch.object(
            bot._conversation_access,
            "get_thread_history",
            AsyncMock(return_value=thread_history),
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert [msg.event_id for msg in context.thread_history] == [
            "$thread_root:localhost",
            "$thread_msg:localhost",
            "$plain1:localhost",
        ]
        assert context.thread_history[-1].body == "Hydrated plain reply from sidecar"
        assert context.thread_history[-1].content["body"] == "Hydrated plain reply from sidecar"
        bot.client.download.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_defers_sidecar_hydration_and_reuses_reply_chain_nodes(
        self,
        bot: AgentBot,
    ) -> None:
        """Dispatch preview should defer sidecar hydration and reuse cached reply-chain nodes later."""
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

        preview_snapshot = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Thread root"),
                _message(event_id="$thread_msg:localhost", body="Earlier threaded message"),
            ],
            is_full_history=False,
        )
        full_thread_history = [
            _message(event_id="$thread_root:localhost", body="Thread root"),
            _message(event_id="$thread_msg:localhost", body="Earlier threaded message"),
        ]
        mock_snapshot = AsyncMock(return_value=preview_snapshot)

        with (
            patch.object(bot._conversation_access, "get_thread_snapshot", new=mock_snapshot),
            patch.object(
                bot._conversation_access,
                "get_thread_history",
                AsyncMock(return_value=full_thread_history),
            ) as mock_fetch,
        ):
            context = await bot._conversation_resolver.extract_dispatch_context(room, event)

            assert context.thread_id == "$thread_root:localhost"
            assert [msg.event_id for msg in context.thread_history] == [
                "$thread_root:localhost",
                "$thread_msg:localhost",
                "$plain1:localhost",
            ]
            assert context.thread_history[-1].body == "Preview plain reply [Message continues in attached file]"
            assert context.requires_full_thread_history is True
            bot.client.download.assert_not_awaited()
            assert bot.client.room_get_event.await_count == 2

            await bot._conversation_resolver.hydrate_dispatch_context(room, event, context)

        assert context.thread_id == "$thread_root:localhost"
        assert [msg.event_id for msg in context.thread_history] == [
            "$thread_root:localhost",
            "$thread_msg:localhost",
            "$plain1:localhost",
        ]
        assert context.thread_history[-1].body == "Hydrated plain reply from sidecar"
        assert context.thread_history[-1].content["body"] == "Hydrated plain reply from sidecar"
        bot.client.download.assert_awaited_once()
        assert bot.client.room_get_event.await_count == 2
        mock_snapshot.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        mock_fetch.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
        )

    @pytest.mark.asyncio
    async def test_extract_context_preserves_plain_replies_across_thread_reentries(self, bot: AgentBot) -> None:
        """Plain replies should remain in context even when chain re-enters threaded events."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Newest plain reply from non-thread client",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$p2:localhost"}},
                },
                "event_id": "$incoming:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
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
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$t2:localhost"}},
                        },
                        "event_id": "$p2:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Thread reply after plain interleave",
                            "msgtype": "m.text",
                            "m.relates_to": {
                                "rel_type": "m.thread",
                                "event_id": "$root:localhost",
                                "m.in_reply_to": {"event_id": "$p1:localhost"},
                            },
                        },
                        "event_id": "$t2:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "First plain interleaved reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$t1:localhost"}},
                        },
                        "event_id": "$p1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567893,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "First threaded reply",
                            "msgtype": "m.text",
                            "m.relates_to": {
                                "rel_type": "m.thread",
                                "event_id": "$root:localhost",
                                "m.in_reply_to": {"event_id": "$root:localhost"},
                            },
                        },
                        "event_id": "$t1:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567892,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {"body": "Thread root", "msgtype": "m.text"},
                        "event_id": "$root:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567891,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        thread_history = [
            _message(event_id="$root:localhost", body="Thread root"),
            _message(event_id="$t1:localhost", body="First threaded reply"),
            _message(event_id="$t2:localhost", body="Thread reply after plain interleave"),
        ]
        with patch.object(
            bot._conversation_access,
            "get_thread_history",
            AsyncMock(return_value=thread_history),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$root:localhost"
        assert [msg.event_id for msg in context.thread_history] == [
            "$root:localhost",
            "$t1:localhost",
            "$p1:localhost",
            "$t2:localhost",
            "$p2:localhost",
        ]
        mock_fetch.assert_awaited_once_with(
            room.room_id,
            "$root:localhost",
        )

    def test_merge_thread_and_chain_history_preserves_chronological_order(self) -> None:
        """Merged context should preserve chronological order for interleaved plain replies."""
        thread_history = [
            _message(event_id="$root:localhost", body="Thread root"),
            _message(event_id="$t1:localhost", body="First threaded reply"),
            _message(event_id="$t2:localhost", body="Thread reply after plain interleave"),
        ]
        chain_history = [
            _message(event_id="$root:localhost", body="Thread root"),
            _message(event_id="$t1:localhost", body="First threaded reply"),
            _message(event_id="$p1:localhost", body="First plain interleaved reply"),
            _message(event_id="$t2:localhost", body="Thread reply after plain interleave"),
            _message(event_id="$p2:localhost", body="Second plain reply"),
        ]

        merged = _merge_thread_and_chain_history(thread_history, chain_history)

        assert [msg.event_id for msg in merged] == [
            "$root:localhost",
            "$t1:localhost",
            "$p1:localhost",
            "$t2:localhost",
            "$p2:localhost",
        ]

    @pytest.mark.asyncio
    async def test_command_as_reply_doesnt_cause_thread_error(self, tmp_path: Path) -> None:
        """Test that commands sent as replies don't cause threading errors."""
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

            # Process the command
            await bot._on_message(room, event)

            # The bot should send an error message about needing threads
            bot.client.room_send.assert_called_once()

            # Check the content
            call_args = bot.client.room_send.call_args
            content = call_args.kwargs["content"]

            # The error response should create a thread from the message the command is replying to
            # Since the command is a reply to $some_other_msg:localhost, that becomes the thread root
            assert "m.relates_to" in content
            assert content["m.relates_to"]["rel_type"] == "m.thread"
            # Thread root should be the message the command was replying to
            assert content["m.relates_to"]["event_id"] == "$some_other_msg:localhost"
            # Should reply to the command message
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
    async def test_command_reply_to_thread_message_uses_existing_thread_root(self, tmp_path: Path) -> None:
        """Plain replies to a threaded message should keep command replies in that thread."""
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

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Agent thread message",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$thread_msg:localhost",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        bot.client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict(
                {"event_id": "$response:localhost"},
                room_id="!test:localhost",
            ),
        )

        with patch.object(bot._conversation_access, "get_thread_history", AsyncMock(return_value=[])):
            await bot._on_message(room, event)

        bot.client.room_send.assert_called_once()
        content = bot.client.room_send.call_args.kwargs["content"]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread_root:localhost"
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
                "mindroom.delivery_gateway.get_latest_thread_event_id_if_needed",
                AsyncMock(return_value="$latest:localhost"),
            ),
            patch(
                "mindroom.delivery_gateway.send_message",
                AsyncMock(return_value="$router_response:localhost"),
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

        # Initialize response tracking

        # Mock interactive.handle_text_response and generate_response
        bot._generate_response = AsyncMock()
        install_generate_response_mock(bot, bot._generate_response)
        with patch("mindroom.turn_controller.interactive.handle_text_response", AsyncMock(return_value=None)):
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
