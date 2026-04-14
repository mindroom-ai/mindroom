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
from contextlib import asynccontextmanager
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
from mindroom.hooks import EVENT_AGENT_STARTED
from mindroom.matrix._event_cache import _EventCache
from mindroom.matrix._event_cache_write_coordinator import _EventCacheWriteCoordinator
from mindroom.matrix.client import (
    DeliveredMatrixEvent,
    PermanentMatrixStartupError,
    ResolvedVisibleMessage,
    ThreadHistoryResult,
)
from mindroom.matrix.conversation_cache import MatrixConversationCache, ThreadRepairRequiredError
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import _clear_mxc_cache
from mindroom.matrix.reply_chain import (
    ReplyChainCaches,
    _merge_thread_and_chain_history,
    _ReplyChainNode,
    _ReplyChainRoot,
)
from mindroom.matrix.thread_cache import ResolvedThreadCache, resolved_thread_cache_entry
from mindroom.matrix.thread_history_result import (
    THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC,
    THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
    thread_history_result,
)
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


def _runtime_event_cache() -> AsyncMock:
    """Return a cache-shaped async mock for runtime-state tests."""
    return make_event_cache_mock()


def _runtime_write_coordinator() -> _EventCacheWriteCoordinator:
    """Return one real coordinator for runtime-state tests."""
    return _EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=object(),
    )


def _prime_resolved_thread_history(
    access: MatrixConversationCache,
    *,
    room_id: str,
    thread_id: str,
    history: list[ResolvedVisibleMessage],
    source_event_ids: frozenset[str],
    thread_version: int = 0,
) -> None:
    """Seed one resolved-thread cache entry for targeted cache-coherence tests."""
    access._resolved_thread_cache.store(
        room_id,
        thread_id,
        resolved_thread_cache_entry(
            history=history,
            source_event_ids=source_event_ids,
            thread_version=thread_version,
        ),
    )


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


class TestResolvedThreadCache:
    """Unit tests for the process-local resolved thread cache."""

    def test_lookup_evicts_expired_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Expired entries should be removed on lookup instead of being served stale."""
        cache = ResolvedThreadCache(ttl_seconds=300.0)
        cache.store(
            "!room:localhost",
            "$thread",
            resolved_thread_cache_entry(
                history=[_message(event_id="$thread", body="Root")],
                source_event_ids=frozenset({"$thread"}),
                thread_version=1,
            ),
        )
        cache._entries[("!room:localhost", "$thread")].cached_at_monotonic = 10.0

        monkeypatch.setattr("mindroom.matrix.thread_cache.time.monotonic", lambda: 311.0)

        lookup = cache.lookup("!room:localhost", "$thread")

        assert lookup.entry is None
        assert lookup.expired is True

    def test_store_enforces_lru_bound(self) -> None:
        """Adding beyond the bound should evict the least recently used thread entry."""
        cache = ResolvedThreadCache(max_entries=1)
        cache.store(
            "!room:localhost",
            "$thread-a",
            resolved_thread_cache_entry(
                history=[_message(event_id="$thread-a", body="A")],
                source_event_ids=frozenset({"$thread-a"}),
                thread_version=1,
            ),
        )
        cache.store(
            "!room:localhost",
            "$thread-b",
            resolved_thread_cache_entry(
                history=[_message(event_id="$thread-b", body="B")],
                source_event_ids=frozenset({"$thread-b"}),
                thread_version=1,
            ),
        )

        assert cache.lookup("!room:localhost", "$thread-a").entry is None
        assert cache.lookup("!room:localhost", "$thread-b").entry is not None

    @pytest.mark.asyncio
    async def test_entry_lock_serializes_same_thread_updates(self) -> None:
        """Each thread key should have its own async fill lock."""
        cache = ResolvedThreadCache()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def first_lock_holder() -> None:
            async with cache.entry_lock("!room:localhost", "$thread"):
                first_entered.set()
                await release_first.wait()

        async def second_lock_holder() -> None:
            await first_entered.wait()
            async with cache.entry_lock("!room:localhost", "$thread"):
                second_entered.set()

        first_task = asyncio.create_task(first_lock_holder())
        second_task = asyncio.create_task(second_lock_holder())
        await asyncio.wait_for(first_entered.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert second_entered.is_set() is False

        release_first.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=1.0)
        assert second_entered.is_set()

    @pytest.mark.asyncio
    async def test_invalidate_prunes_clean_thread_lock_state(self) -> None:
        """Dropping the last clean entry should also prune the ephemeral per-thread lock."""
        cache = ResolvedThreadCache()
        thread_key = ("!room:localhost", "$thread")
        cache.store(
            *thread_key,
            resolved_thread_cache_entry(
                history=[_message(event_id="$thread", body="Root")],
                source_event_ids=frozenset({"$thread"}),
                thread_version=1,
            ),
        )

        async with cache.entry_lock(*thread_key):
            pass

        assert thread_key in cache._locks

        cache.invalidate(*thread_key)

        assert thread_key not in cache._locks
        assert cache.lookup(*thread_key).entry is None

    def test_thread_versions_prune_after_eviction_but_future_bumps_still_increase_globally(self) -> None:
        """Evicted clean threads can forget their generation without reusing generation numbers."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access._resolved_thread_cache = ResolvedThreadCache(max_entries=1, generation_retention_seconds=0)
        thread_a_key = ("!room:localhost", "$thread-a")
        thread_b_key = ("!room:localhost", "$thread-b")

        access._resolved_thread_cache.bump_version(*thread_a_key)
        access._resolved_thread_cache.store(
            *thread_a_key,
            resolved_thread_cache_entry(
                history=[_message(event_id="$thread-a", body="A")],
                source_event_ids=frozenset({"$thread-a"}),
                thread_version=1,
            ),
        )
        access._resolved_thread_cache.bump_version(*thread_b_key)
        access._resolved_thread_cache.store(
            *thread_b_key,
            resolved_thread_cache_entry(
                history=[_message(event_id="$thread-b", body="B")],
                source_event_ids=frozenset({"$thread-b"}),
                thread_version=2,
            ),
        )

        assert access._resolved_thread_cache.lookup(*thread_a_key).entry is None
        assert access.thread_version(*thread_a_key) == 0
        assert access.thread_version(*thread_b_key) == 2

        assert access._bump_thread_version(*thread_a_key) == 3

    @pytest.mark.asyncio
    async def test_lru_eviction_prunes_clean_thread_lock_state(self) -> None:
        """Resolved-entry eviction should prune the clean ephemeral lock for the evicted thread."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access._resolved_thread_cache = ResolvedThreadCache(max_entries=1, generation_retention_seconds=0)
        thread_a_key = ("!room:localhost", "$thread-a")
        thread_b_key = ("!room:localhost", "$thread-b")
        access.runtime.event_cache.get_thread_events.side_effect = [
            [{"event_id": "$thread-a"}],
            [{"event_id": "$thread-b"}],
        ]
        access._resolved_thread_cache.bump_version(*thread_a_key)
        access._resolved_thread_cache.bump_version(*thread_b_key)

        async with access._resolved_thread_cache.entry_lock(*thread_a_key):
            await access._reads._store_resolved_thread_cache_entry(
                *thread_a_key,
                history=[_message(event_id="$thread-a", body="A")],
                thread_version=1,
            )

        assert thread_a_key in access._resolved_thread_cache._locks

        await access._reads._store_resolved_thread_cache_entry(
            *thread_b_key,
            history=[_message(event_id="$thread-b", body="B")],
            thread_version=2,
        )

        assert access._resolved_thread_cache.lookup(*thread_a_key).entry is None
        assert access._resolved_thread_cache.lookup(*thread_b_key).entry is not None
        assert access.thread_version(*thread_a_key) == 0
        assert access.thread_version(*thread_b_key) == 2
        assert thread_a_key not in access._resolved_thread_cache._locks

    @pytest.mark.asyncio
    async def test_prunes_clean_thread_lock_state_once_entry_is_gone(self) -> None:
        """Clean thread locks should disappear once no entry still needs them."""
        cache = ResolvedThreadCache(max_entries=1)
        thread_key = ("!room:localhost", "$thread-a")

        async with cache.entry_lock(*thread_key):
            pass

        assert thread_key not in cache._locks

        cache.store(
            *thread_key,
            resolved_thread_cache_entry(
                history=[_message(event_id="$thread-a", body="A")],
                source_event_ids=frozenset({"$thread-a"}),
                thread_version=1,
            ),
        )

        assert thread_key not in cache._locks

        cache.invalidate(*thread_key)

        assert thread_key not in cache._locks


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
        _prime_resolved_thread_history(
            bot._conversation_cache,
            room_id="!test:localhost",
            thread_id="$thread",
            history=[_message(event_id="$thread", body="Root")],
            source_event_ids=frozenset({"$thread"}),
            thread_version=7,
        )
        bot._conversation_cache._bump_thread_version("!test:localhost", "$thread")

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
            assert bot._conversation_cache._resolved_thread_cache.lookup("!test:localhost", "$thread").entry is None
            assert bot._conversation_cache.thread_version("!test:localhost", "$thread") == 0

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
    async def test_standalone_runtime_support_raises_when_event_cache_init_fails(self, bot: AgentBot) -> None:
        """Standalone startup should fail fast when SQLite cache init fails."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None

        with (
            patch("mindroom.runtime_support._EventCache.initialize", AsyncMock(side_effect=RuntimeError("boom"))),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await bot._initialize_runtime_support_services()

        assert bot._standalone_runtime_support is None
        assert bot._runtime_view.event_cache is None
        assert bot._runtime_view.event_cache_write_coordinator is None

    @pytest.mark.asyncio
    async def test_start_closes_logged_in_client_when_runtime_support_init_fails(self, bot: AgentBot) -> None:
        """Startup should close and clear a logged-in client if runtime support init fails."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        start_client = _make_client_mock(user_id="@mindroom_general:localhost")
        start_client.close = AsyncMock()

        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=start_client)),
            patch.object(
                bot,
                "_initialize_runtime_support_services",
                AsyncMock(side_effect=RuntimeError("cache init failed")),
            ),
            pytest.raises(RuntimeError, match="cache init failed"),
        ):
            await bot.start()

        start_client.close.assert_awaited_once()
        assert bot.client is None
        assert bot._standalone_runtime_support is None
        assert bot._runtime_view.event_cache is None
        assert bot._runtime_view.event_cache_write_coordinator is None

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
        bot._conversation_cache._resolved_thread_cache.store(
            "!test:localhost",
            "$thread",
            resolved_thread_cache_entry(
                history=[_message(event_id="$thread", body="Root")],
                source_event_ids=frozenset({"$thread"}),
                thread_version=0,
            ),
        )
        await bot._close_runtime_support_services()

        assert bot._standalone_runtime_support is None
        assert bot._runtime_view.event_cache is None
        assert bot._runtime_view.event_cache_write_coordinator is None
        assert bot._conversation_cache._resolved_thread_cache.lookup("!test:localhost", "$thread").entry is None

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
    async def test_sync_error_does_not_advance_cache_freshness_clock(self, bot: AgentBot) -> None:
        """Sync errors should keep the watchdog alive without suppressing cache repair reads."""
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock(join={})
        sync_error = MagicMock(spec=nio.SyncError)
        bot._first_sync_done = True

        with patch("mindroom.bot.time.monotonic", side_effect=[100.0, 200.0]):
            await bot._on_sync_response(sync_response)
            await bot._on_sync_error(sync_error)

        assert bot._last_sync_monotonic == 200.0
        assert bot._runtime_view.last_sync_activity_monotonic == 100.0

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
        event_cache.append_thread_event = AsyncMock(return_value=False)
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
        event_cache.append_thread_event.assert_awaited_once()
        event_cache.redact_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_appends_thread_events_to_cached_threads(self, bot: AgentBot) -> None:
        """Sync timeline writes should append direct thread events through the thread-cache helper."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.append_thread_event = AsyncMock(return_value=False)
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

        event_cache.append_thread_event.assert_awaited_once()
        append_args = event_cache.append_thread_event.await_args.args
        assert append_args[0] == "!test:localhost"
        assert append_args[1] == "$thread_root:localhost"
        assert append_args[2]["event_id"] == "$thread_msg:localhost"

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_appends_threaded_edits_to_cached_threads(self, bot: AgentBot) -> None:
        """Sync timeline writes should append threaded edits using the thread root from m.new_content."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.append_thread_event = AsyncMock(return_value=False)
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

        event_cache.append_thread_event.assert_awaited_once()
        append_args = event_cache.append_thread_event.await_args.args
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
            await bot.event_cache.store_events(
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
        event_cache.append_thread_event = AsyncMock(return_value=False)
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

        event_cache.append_thread_event.assert_not_awaited()

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
        event_cache.append_thread_event = AsyncMock(return_value=False)
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
        event_cache.append_thread_event = AsyncMock(side_effect=RuntimeError("append failed"))
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

        event_cache.append_thread_event.assert_awaited_once()
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
        event_cache.append_thread_event = AsyncMock(return_value=False)
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
            await bot.event_cache.store_events(
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
        event_cache.append_thread_event = AsyncMock(return_value=False)
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
        event_cache.append_thread_event.assert_not_awaited()
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
    async def test_live_edit_invalidates_cached_reply_chain_for_edited_event(self, bot: AgentBot) -> None:
        """A live edit should evict stale reply-chain nodes for the edited event and descendants."""
        reply_chain = bot._conversation_resolver.reply_chain
        reply_chain.nodes.put(
            "!test:localhost",
            "$original:localhost",
            _ReplyChainNode(
                message=_message(event_id="$original:localhost", body="original"),
                parent_event_id=None,
                thread_root_id=None,
                has_relations=False,
            ),
        )
        reply_chain.nodes.put(
            "!test:localhost",
            "$reply:localhost",
            _ReplyChainNode(
                message=_message(event_id="$reply:localhost", body="reply"),
                parent_event_id="$original:localhost",
                thread_root_id=None,
                has_relations=True,
            ),
        )
        reply_chain.roots.put(
            "!test:localhost",
            "$reply:localhost",
            _ReplyChainRoot(root_event_id="$original:localhost", points_to_thread=False),
        )

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original:localhost"},
                },
                "event_id": "$edit:localhost",
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

        assert reply_chain.nodes.get("!test:localhost", "$original:localhost") is None
        assert reply_chain.nodes.get("!test:localhost", "$reply:localhost") is None
        assert reply_chain.roots.get("!test:localhost", "$reply:localhost") is None

    @pytest.mark.asyncio
    async def test_outbound_edit_invalidates_cached_reply_chain_for_edited_event(self, bot: AgentBot) -> None:
        """A locally sent edit should evict stale reply-chain nodes before sync catches up."""
        reply_chain = bot._conversation_resolver.reply_chain
        reply_chain.nodes.put(
            "!test:localhost",
            "$original:localhost",
            _ReplyChainNode(
                message=_message(event_id="$original:localhost", body="original"),
                parent_event_id=None,
                thread_root_id=None,
                has_relations=False,
            ),
        )
        reply_chain.nodes.put(
            "!test:localhost",
            "$reply:localhost",
            _ReplyChainNode(
                message=_message(event_id="$reply:localhost", body="reply"),
                parent_event_id="$original:localhost",
                thread_root_id=None,
                has_relations=True,
            ),
        )
        reply_chain.roots.put(
            "!test:localhost",
            "$reply:localhost",
            _ReplyChainRoot(root_event_id="$original:localhost", points_to_thread=False),
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(return_value="$thread:localhost")
        bot.event_cache.append_event = AsyncMock(return_value=True)

        await bot._conversation_cache.record_outbound_message(
            "!test:localhost",
            "$edit:localhost",
            {
                "body": "* updated",
                "msgtype": "m.text",
                "m.new_content": {"body": "updated", "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original:localhost"},
            },
        )

        assert reply_chain.nodes.get("!test:localhost", "$original:localhost") is None
        assert reply_chain.nodes.get("!test:localhost", "$reply:localhost") is None
        assert reply_chain.roots.get("!test:localhost", "$reply:localhost") is None

    @pytest.mark.asyncio
    async def test_outbound_write_through_ignores_advisory_cache_failure_after_successful_send(
        self,
        bot: AgentBot,
    ) -> None:
        """Successful sends must not raise just because advisory cache finalization failed."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(return_value="$thread:localhost")
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.mark_thread_repair_required = AsyncMock(side_effect=RuntimeError("cache repair write failed"))
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        await bot._conversation_cache.record_outbound_message(
            "!test:localhost",
            "$edit:localhost",
            {
                "body": "* updated",
                "msgtype": "m.text",
                "m.new_content": {"body": "updated", "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread:localhost"},
            },
        )

        event_cache.append_event.assert_awaited_once()
        event_cache.mark_thread_repair_required.assert_awaited_once_with("!test:localhost", "$thread:localhost")

    @pytest.mark.asyncio
    async def test_wait_for_room_idle_ignores_outbound_advisory_failure_after_successful_send(self) -> None:
        """Unrelated room-idle waiters should not inherit advisory failures from successful sends."""
        event_cache = _runtime_event_cache()
        finalize_started = asyncio.Event()
        allow_finalize_failure = asyncio.Event()
        event_cache.get_thread_id_for_event = AsyncMock(return_value="$thread:localhost")
        event_cache.append_event = AsyncMock(return_value=False)

        failure_message = "cache repair write failed"

        async def _fail_mark_thread_repair_required(_room_id: str, _thread_id: str) -> None:
            finalize_started.set()
            await allow_finalize_failure.wait()
            raise RuntimeError(failure_message)

        event_cache.mark_thread_repair_required = AsyncMock(side_effect=_fail_mark_thread_repair_required)
        coordinator = _runtime_write_coordinator()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=_make_client_mock(),
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )

        send_task = asyncio.create_task(
            access.record_outbound_message(
                "!test:localhost",
                "$edit:localhost",
                {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread:localhost"},
                },
            ),
        )
        await asyncio.wait_for(finalize_started.wait(), timeout=1.0)

        waiter_task = asyncio.create_task(coordinator.wait_for_room_idle("!test:localhost"))
        await asyncio.sleep(0)
        assert waiter_task.done() is False

        allow_finalize_failure.set()
        await asyncio.wait_for(send_task, timeout=1.0)
        await asyncio.wait_for(waiter_task, timeout=1.0)

        event_cache.append_event.assert_awaited_once()
        event_cache.mark_thread_repair_required.assert_awaited_once_with("!test:localhost", "$thread:localhost")

    @pytest.mark.asyncio
    async def test_reset_runtime_state_clears_reply_chain_caches(self, bot: AgentBot) -> None:
        """Runtime resets should clear reply-chain caches as well as resolved thread state."""
        reply_chain = bot._conversation_resolver.reply_chain
        reply_chain.nodes.put(
            "!test:localhost",
            "$original:localhost",
            _ReplyChainNode(
                message=_message(event_id="$original:localhost", body="original"),
                parent_event_id=None,
                thread_root_id=None,
                has_relations=False,
            ),
        )
        reply_chain.roots.put(
            "!test:localhost",
            "$reply:localhost",
            _ReplyChainRoot(root_event_id="$original:localhost", points_to_thread=False),
        )

        bot._conversation_cache.reset_runtime_state()

        assert reply_chain.nodes.get("!test:localhost", "$original:localhost") is None
        assert reply_chain.roots.get("!test:localhost", "$reply:localhost") is None

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
    async def test_live_edit_false_write_marks_thread_repair_required(self, bot: AgentBot) -> None:
        """A degraded live append result should still mark the thread repair-required."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(return_value="$thread_msg:localhost")
        event_cache.append_event = AsyncMock(return_value=False)
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

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

        assert await bot._conversation_cache._thread_requires_refresh("!test:localhost", "$thread_msg:localhost")

    @pytest.mark.asyncio
    async def test_live_redaction_cache_lookup_failure_still_attempts_cache_delete(self, bot: AgentBot) -> None:
        """Redaction callbacks should continue even when the thread lookup cannot read SQLite."""
        event_cache = _runtime_event_cache()
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

        await bot._conversation_cache.apply_redaction("!test:localhost", redaction_event)

        event_cache.get_thread_id_for_event.assert_awaited_once_with("!test:localhost", "$thread_msg:localhost")
        event_cache.redact_event.assert_awaited_once_with("!test:localhost", "$thread_msg:localhost")

    @pytest.mark.asyncio
    async def test_live_redaction_invalidates_cached_reply_chain_for_redacted_edit(self, bot: AgentBot) -> None:
        """Redacting an edit should evict stale reply-chain nodes for the edit, original, and descendants."""
        event_cache = _runtime_event_cache()
        event_cache.get_event = AsyncMock(
            return_value={
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original:localhost"},
                },
                "event_id": "$edit:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        event_cache.redact_event = AsyncMock(return_value=False)
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        reply_chain = bot._conversation_resolver.reply_chain
        reply_chain.nodes.put(
            "!test:localhost",
            "$original:localhost",
            _ReplyChainNode(
                message=_message(event_id="$original:localhost", body="original"),
                parent_event_id=None,
                thread_root_id=None,
                has_relations=False,
            ),
        )
        reply_chain.nodes.put(
            "!test:localhost",
            "$edit:localhost",
            _ReplyChainNode(
                message=_message(event_id="$edit:localhost", body="updated"),
                parent_event_id="$original:localhost",
                thread_root_id=None,
                has_relations=True,
            ),
        )
        reply_chain.nodes.put(
            "!test:localhost",
            "$reply:localhost",
            _ReplyChainNode(
                message=_message(event_id="$reply:localhost", body="reply"),
                parent_event_id="$original:localhost",
                thread_root_id=None,
                has_relations=True,
            ),
        )
        reply_chain.roots.put(
            "!test:localhost",
            "$reply:localhost",
            _ReplyChainRoot(root_event_id="$original:localhost", points_to_thread=False),
        )

        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$edit:localhost"
        redaction_event.sender = "@user:localhost"
        redaction_event.server_timestamp = 1234567891
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$edit:localhost",
            "room_id": "!test:localhost",
            "type": "m.room.redaction",
        }

        await bot._conversation_cache.apply_redaction("!test:localhost", redaction_event)

        assert reply_chain.nodes.get("!test:localhost", "$original:localhost") is None
        assert reply_chain.nodes.get("!test:localhost", "$edit:localhost") is None
        assert reply_chain.nodes.get("!test:localhost", "$reply:localhost") is None
        assert reply_chain.roots.get("!test:localhost", "$reply:localhost") is None
        event_cache.redact_event.assert_awaited_once_with("!test:localhost", "$edit:localhost")

    @pytest.mark.asyncio
    async def test_live_redaction_false_write_marks_thread_repair_required(self, bot: AgentBot) -> None:
        """A degraded live redaction result should still mark the thread repair-required."""
        event_cache = _runtime_event_cache()
        event_cache.get_event = AsyncMock(return_value=None)
        event_cache.get_thread_id_for_event = AsyncMock(return_value="$thread_msg:localhost")
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

        await bot._conversation_cache.apply_redaction("!test:localhost", redaction_event)

        assert await bot._conversation_cache._thread_requires_refresh("!test:localhost", "$thread_msg:localhost")

    @pytest.mark.asyncio
    async def test_local_bot_redaction_records_outbound_cache_update(self, bot: AgentBot) -> None:
        """Successful local redactions should write through to the conversation cache immediately."""
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
    async def test_get_thread_history_waits_for_live_append_finalization(self, bot: AgentBot) -> None:
        """Authoritative thread reads should not reuse stale resolved history while live finalization is pending."""
        event_cache = _runtime_event_cache()
        event_cache.append_event = AsyncMock(return_value=True)
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)
        access = bot._conversation_cache
        stale_history = ThreadHistoryResult(
            [_message(event_id="$thread", body="Root")],
            is_full_history=True,
            thread_version=1,
        )
        _prime_resolved_thread_history(
            access,
            room_id="!test:localhost",
            thread_id="$thread",
            history=list(stale_history),
            source_event_ids=frozenset({"$thread"}),
            thread_version=1,
        )
        access._resolved_thread_cache.bump_version("!test:localhost", "$thread")
        finalize_started = asyncio.Event()
        allow_finalize = asyncio.Event()
        original_finalize = access._writes._finalize_thread_cache_mutation
        refreshed_history = ThreadHistoryResult(
            [
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply", body="Reply", sender="@user:localhost"),
            ],
            is_full_history=True,
        )
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
                },
                "event_id": "$reply",
                "sender": "@user:localhost",
                "origin_server_ts": 2,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        async def delayed_finalize(*args: object, **kwargs: object) -> None:
            finalize_started.set()
            await allow_finalize.wait()
            await original_finalize(*args, **kwargs)

        with (
            patch.object(
                access._writes,
                "_finalize_thread_cache_mutation",
                new=AsyncMock(side_effect=delayed_finalize),
            ),
            patch(
                "mindroom.matrix.conversation_cache.fetch_thread_history",
                new=AsyncMock(return_value=refreshed_history),
            ),
        ):
            append_task = asyncio.create_task(
                access.append_live_event(
                    "!test:localhost",
                    event,
                    event_info=EventInfo.from_event(event.source),
                ),
            )
            await finalize_started.wait()

            history_task = asyncio.create_task(access.get_thread_history("!test:localhost", "$thread"))
            await asyncio.sleep(0)

            assert history_task.done() is False

            allow_finalize.set()
            await append_task
            history = await history_task

        assert history.thread_version is not None
        assert history.thread_version > stale_history.thread_version
        assert [message.event_id for message in history] == ["$thread", "$reply"]

    @pytest.mark.asyncio
    async def test_sync_redaction_invalidates_cached_reply_chain_for_redacted_edit(self) -> None:
        """Sync redactions should evict stale reply-chain nodes for a cached edit and its original."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        reply_chain = ReplyChainCaches()
        access.bind_reply_chain_caches(lambda: reply_chain)

        reply_chain.nodes.put(
            "!test:localhost",
            "$original:localhost",
            _ReplyChainNode(
                message=_message(event_id="$original:localhost", body="original"),
                parent_event_id=None,
                thread_root_id=None,
                has_relations=False,
            ),
        )
        reply_chain.nodes.put(
            "!test:localhost",
            "$edit:localhost",
            _ReplyChainNode(
                message=_message(event_id="$edit:localhost", body="updated"),
                parent_event_id="$original:localhost",
                thread_root_id=None,
                has_relations=True,
                event_source={
                    "content": {
                        "body": "* updated",
                        "msgtype": "m.text",
                        "m.new_content": {"body": "updated", "msgtype": "m.text"},
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$original:localhost"},
                    },
                    "event_id": "$edit:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )
        reply_chain.nodes.put(
            "!test:localhost",
            "$reply:localhost",
            _ReplyChainNode(
                message=_message(event_id="$reply:localhost", body="reply"),
                parent_event_id="$original:localhost",
                thread_root_id=None,
                has_relations=True,
            ),
        )
        reply_chain.roots.put(
            "!test:localhost",
            "$reply:localhost",
            _ReplyChainRoot(root_event_id="$original:localhost", points_to_thread=False),
        )

        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$edit:localhost"
        redaction_event.sender = "@user:localhost"
        redaction_event.server_timestamp = 1234567891
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$edit:localhost",
            "room_id": "!test:localhost",
            "type": "m.room.redaction",
        }

        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[redaction_event])),
        }

        access.cache_sync_timeline(sync_response)
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")

        assert reply_chain.nodes.get("!test:localhost", "$original:localhost") is None
        assert reply_chain.nodes.get("!test:localhost", "$edit:localhost") is None
        assert reply_chain.nodes.get("!test:localhost", "$reply:localhost") is None
        assert reply_chain.roots.get("!test:localhost", "$reply:localhost") is None

    @pytest.mark.asyncio
    async def test_sync_redaction_invalidates_cached_reply_chain_for_durable_cached_edit_only(
        self,
        tmp_path: Path,
    ) -> None:
        """Sync redactions should inspect the durable cache when the edit is absent from reply-chain nodes."""
        event_cache = _EventCache(tmp_path / "event_cache.db")
        await event_cache.initialize()
        try:
            access = MatrixConversationCache(
                logger=MagicMock(),
                runtime=_conversation_runtime(event_cache=event_cache),
            )
            reply_chain = ReplyChainCaches()
            access.bind_reply_chain_caches(lambda: reply_chain)

            await event_cache.store_event(
                "$edit:localhost",
                "!test:localhost",
                {
                    "content": {
                        "body": "* updated",
                        "msgtype": "m.text",
                        "m.new_content": {"body": "updated", "msgtype": "m.text"},
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$original:localhost"},
                    },
                    "event_id": "$edit:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )

            reply_chain.nodes.put(
                "!test:localhost",
                "$original:localhost",
                _ReplyChainNode(
                    message=_message(event_id="$original:localhost", body="original"),
                    parent_event_id=None,
                    thread_root_id=None,
                    has_relations=False,
                ),
            )
            reply_chain.nodes.put(
                "!test:localhost",
                "$reply:localhost",
                _ReplyChainNode(
                    message=_message(event_id="$reply:localhost", body="reply"),
                    parent_event_id="$original:localhost",
                    thread_root_id=None,
                    has_relations=True,
                ),
            )
            reply_chain.roots.put(
                "!test:localhost",
                "$reply:localhost",
                _ReplyChainRoot(root_event_id="$original:localhost", points_to_thread=False),
            )

            redaction_event = MagicMock(spec=nio.RedactionEvent)
            redaction_event.event_id = "$redaction:localhost"
            redaction_event.redacts = "$edit:localhost"
            redaction_event.sender = "@user:localhost"
            redaction_event.server_timestamp = 1234567891
            redaction_event.source = {
                "content": {},
                "event_id": "$redaction:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567891,
                "redacts": "$edit:localhost",
                "room_id": "!test:localhost",
                "type": "m.room.redaction",
            }

            sync_response = MagicMock()
            sync_response.__class__ = nio.SyncResponse
            sync_response.rooms = MagicMock()
            sync_response.rooms.join = {
                "!test:localhost": MagicMock(timeline=MagicMock(events=[redaction_event])),
            }

            access.cache_sync_timeline(sync_response)
            await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")

            assert reply_chain.nodes.get("!test:localhost", "$original:localhost") is None
            assert reply_chain.nodes.get("!test:localhost", "$reply:localhost") is None
            assert reply_chain.roots.get("!test:localhost", "$reply:localhost") is None
        finally:
            await event_cache.close()

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
    async def test_get_thread_history_skips_incremental_refresh_when_sync_is_fresh(self) -> None:
        """Fresh sync activity should disable the incremental Matrix room scan on cache hits."""
        runtime = _conversation_runtime(
            client=_make_client_mock(),
            event_cache=_runtime_event_cache(),
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=runtime,
        )
        cached_history = ThreadHistoryResult([], is_full_history=True)

        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
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
        event_cache = _runtime_event_cache()
        event_cache.get_thread_events = AsyncMock(
            return_value=[
                {"event_id": "$thread"},
                {"event_id": "$reply"},
            ],
        )
        runtime = _conversation_runtime(
            client=_make_client_mock(),
            event_cache=event_cache,
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=runtime,
        )
        initial_history = ThreadHistoryResult(
            [_message(event_id="$thread", body="Root"), _message(event_id="$reply", body="Reply")],
            is_full_history=True,
        )

        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
            new=AsyncMock(return_value=initial_history),
        ) as first_fetch:
            async with access.turn_scope():
                first_history = await access.get_thread_history("!room:localhost", "$thread")

        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
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
    async def test_get_thread_history_rechecks_thread_state_after_entry_lock_acquisition(self) -> None:
        """Thread version and repair state should be observed after taking the per-thread entry lock."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_events = AsyncMock(return_value=None)
        runtime = _conversation_runtime(
            client=_make_client_mock(),
            event_cache=event_cache,
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=runtime,
        )
        access._bump_thread_version("!room:localhost", "$thread")
        access._resolved_thread_cache.store(
            "!room:localhost",
            "$thread",
            resolved_thread_cache_entry(
                history=[_message(event_id="$thread", body="Stale root")],
                source_event_ids=frozenset({"$thread"}),
                thread_version=1,
            ),
        )
        original_entry_lock = access._resolved_thread_cache.entry_lock

        @asynccontextmanager
        async def bump_version_before_yield(
            _cache: ResolvedThreadCache,
            room_id: str,
            thread_id: str,
        ) -> AsyncGenerator[None]:
            access._bump_thread_version(room_id, thread_id)
            async with original_entry_lock(room_id, thread_id):
                yield

        refreshed_history = ThreadHistoryResult(
            [
                _message(event_id="$thread", body="Fresh root"),
                _message(event_id="$reply-1", body="Fresh reply", sender="@agent:localhost"),
            ],
            is_full_history=True,
        )
        with (
            patch.object(ResolvedThreadCache, "entry_lock", bump_version_before_yield),
            patch(
                "mindroom.matrix.conversation_cache.fetch_thread_history",
                new=AsyncMock(return_value=refreshed_history),
            ) as mock_fetch_thread_history,
        ):
            async with access.turn_scope():
                history = await access.get_thread_history("!room:localhost", "$thread")

        mock_fetch_thread_history.assert_awaited_once_with(
            runtime.client,
            "!room:localhost",
            "$thread",
            event_cache=runtime.event_cache,
            refresh_cache=True,
        )
        assert [message.body for message in history] == ["Fresh root", "Fresh reply"]
        assert history.thread_version == 2

    @pytest.mark.asyncio
    async def test_get_thread_history_incrementally_refreshes_resolved_cache_from_sync(self, tmp_path: Path) -> None:
        """A cached thread with one new sync-delivered reply should append incrementally."""
        cache = _EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationCache(
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
        await cache.store_events("!room:localhost", "$thread", [root_event, first_reply])

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
                "mindroom.matrix.conversation_cache.fetch_thread_history",
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
        cache = _EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationCache(
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
        await cache.store_events("!room:localhost", "$thread", [root_event, first_reply])

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
            original_persist = access._writes._persist_room_sync_timeline_updates

            async def blocked_persist(
                room_id: str,
                plain_events: Sequence[dict[str, object]],
                room_threaded_events: Sequence[dict[str, object]],
                redacted_event_ids: Sequence[str],
            ) -> None:
                write_started.set()
                await release_write.wait()
                await original_persist(
                    room_id,
                    plain_events,
                    room_threaded_events,
                    redacted_event_ids,
                )

            with (
                patch.object(access._writes, "_persist_room_sync_timeline_updates", new=blocked_persist),
                patch(
                    "mindroom.matrix.conversation_cache.fetch_thread_history",
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
        cache = _EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationCache(
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
        await cache.store_events("!room:localhost", "$thread", [root_event, first_reply])

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
                "mindroom.matrix.conversation_cache.fetch_thread_history",
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
    async def test_get_thread_history_invalidates_resolved_cache_on_sync_edit_via_cached_lookup(
        self,
        tmp_path: Path,
    ) -> None:
        """Edit syncs resolved via original_event_id should still bump the thread version and invalidate history."""
        cache = _EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationCache(
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
        await cache.store_events("!room:localhost", "$thread", [root_event, first_reply])

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
                "mindroom.matrix.conversation_cache.fetch_thread_history",
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

        assert access.thread_version("!room:localhost", "$thread") == 1
        assert [message.body for message in history] == ["Root", "Reply 1 edited"]

    @pytest.mark.asyncio
    async def test_get_thread_history_invalidates_resolved_cache_on_sync_redaction(self, tmp_path: Path) -> None:
        """Sync-delivered redactions should invalidate the resolved cache before serving history."""
        cache = _EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationCache(
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
        await cache.store_events("!room:localhost", "$thread", [root_event, first_reply])

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
            original_persist = access._writes._persist_room_sync_timeline_updates

            async def blocked_persist(
                room_id: str,
                plain_events: Sequence[dict[str, object]],
                room_threaded_events: Sequence[dict[str, object]],
                redacted_event_ids: Sequence[str],
            ) -> None:
                write_started.set()
                await release_write.wait()
                await original_persist(
                    room_id,
                    plain_events,
                    room_threaded_events,
                    redacted_event_ids,
                )

            refreshed_history = ThreadHistoryResult(
                [_message(event_id="$thread", body="Root")],
                is_full_history=True,
            )
            with (
                patch.object(access._writes, "_persist_room_sync_timeline_updates", new=blocked_persist),
                patch(
                    "mindroom.matrix.conversation_cache.fetch_thread_history",
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
    async def test_get_thread_history_forces_homeserver_refresh_after_sync_store_failure(self, tmp_path: Path) -> None:
        """Failed sync persistence should mark the thread dirty and force a homeserver-backed refresh."""
        cache = _EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationCache(
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
        await cache.store_events("!room:localhost", "$thread", [root_event, first_reply])

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

            with patch.object(cache, "store_events_batch", AsyncMock(side_effect=RuntimeError("store failed"))):
                access.cache_sync_timeline(sync_response)
                await wait_for_background_tasks(timeout=1.0, owner=runtime)

            refreshed_history = ThreadHistoryResult(
                [
                    _message(event_id="$thread", body="Root"),
                    _message(event_id="$reply-1", body="Reply 1", sender="@agent:localhost"),
                    _message(event_id="$reply-2", body="Reply 2", sender="@agent:localhost"),
                ],
                is_full_history=True,
                diagnostics={
                    THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                    THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: True,
                    THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: True,
                },
            )

            async def authoritative_refill(
                client: nio.AsyncClient,
                room_id: str,
                thread_id: str,
                *,
                event_cache: _EventCache,
                refresh_cache: bool,
            ) -> ThreadHistoryResult:
                assert client is runtime.client
                assert room_id == "!room:localhost"
                assert thread_id == "$thread"
                assert event_cache is runtime.event_cache
                assert refresh_cache is True
                await event_cache.store_events(
                    room_id,
                    thread_id,
                    [
                        root_event,
                        first_reply,
                        second_reply_event.source,
                    ],
                )
                return refreshed_history

            with patch(
                "mindroom.matrix.conversation_cache.fetch_thread_history",
                new=AsyncMock(side_effect=authoritative_refill),
            ) as mock_fetch_thread_history:
                async with access.turn_scope():
                    history = await access.get_thread_history("!room:localhost", "$thread")

                async with access.turn_scope():
                    cached_history = await access.get_thread_history("!room:localhost", "$thread")

            mock_fetch_thread_history.assert_awaited_once_with(
                runtime.client,
                "!room:localhost",
                "$thread",
                event_cache=runtime.event_cache,
                refresh_cache=True,
            )
        finally:
            await cache.close()

        assert [message.event_id for message in history] == ["$thread", "$reply-1", "$reply-2"]
        assert [message.event_id for message in cached_history] == ["$thread", "$reply-1", "$reply-2"]

    @pytest.mark.asyncio
    async def test_get_thread_history_raises_after_degraded_repair_result(self, tmp_path: Path) -> None:
        """A degraded repair read should fail explicitly instead of serving suspect history."""
        cache = _EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationCache(
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
        await cache.store_events("!room:localhost", "$thread", [root_event, first_reply])

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

            with patch.object(cache, "store_events_batch", AsyncMock(side_effect=RuntimeError("store failed"))):
                access.cache_sync_timeline(sync_response)
                await wait_for_background_tasks(timeout=1.0, owner=runtime)

            degraded_history = ThreadHistoryResult(
                [_message(event_id="$thread", body="Root")],
                is_full_history=True,
                diagnostics={
                    THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                    THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: False,
                    THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: False,
                },
            )
            with (
                patch(
                    "mindroom.matrix.conversation_cache.fetch_thread_history",
                    new=AsyncMock(return_value=degraded_history),
                ),
                pytest.raises(ThreadRepairRequiredError),
            ):
                async with access.turn_scope():
                    await access.get_thread_history("!room:localhost", "$thread")
        finally:
            await cache.close()

        assert await access._thread_requires_refresh("!room:localhost", "$thread")
        assert access._resolved_thread_cache.lookup("!room:localhost", "$thread").entry is None

    @pytest.mark.asyncio
    async def test_get_thread_history_does_not_cache_degraded_non_authoritative_result(self) -> None:
        """Non-authoritative full-history fetches should not become resolved-cache hits."""
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=_runtime_event_cache(),
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        degraded_history = ThreadHistoryResult(
            [_message(event_id="$thread", body="Root")],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: False,
                THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: False,
            },
        )
        authoritative_history = ThreadHistoryResult(
            [
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply", body="Reply", sender="@agent:localhost"),
            ],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: True,
                THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: True,
            },
        )

        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
            new=AsyncMock(side_effect=[degraded_history, authoritative_history]),
        ) as mock_fetch_thread_history:
            async with access.turn_scope():
                first_history = await access.get_thread_history("!room:localhost", "$thread")
            async with access.turn_scope():
                second_history = await access.get_thread_history("!room:localhost", "$thread")

        assert [message.event_id for message in first_history] == ["$thread"]
        assert [message.event_id for message in second_history] == ["$thread", "$reply"]
        assert mock_fetch_thread_history.await_count == 2

    @pytest.mark.asyncio
    async def test_get_thread_history_does_not_cache_result_without_source_event_ids(self) -> None:
        """Resolved-thread reuse should be skipped when durable source-event IDs cannot be recovered."""
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=_runtime_event_cache(),
        )
        runtime.event_cache.get_thread_events = AsyncMock(return_value=None)
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        authoritative_history = ThreadHistoryResult(
            [_message(event_id="$thread", body="Root")],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: True,
                THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: True,
            },
        )

        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
            new=AsyncMock(return_value=authoritative_history),
        ) as mock_fetch_thread_history:
            async with access.turn_scope():
                first_history = await access.get_thread_history("!room:localhost", "$thread")
            async with access.turn_scope():
                second_history = await access.get_thread_history("!room:localhost", "$thread")

        assert [message.event_id for message in first_history] == ["$thread"]
        assert [message.event_id for message in second_history] == ["$thread"]
        assert mock_fetch_thread_history.await_count == 2
        assert access._resolved_thread_cache.lookup("!room:localhost", "$thread").entry is None

    @pytest.mark.asyncio
    async def test_live_edit_lookup_failure_invalidates_matching_resolved_thread_cache(self) -> None:
        """Live lookup failures should force a refetch for threads containing the target event."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event.side_effect = RuntimeError("database is locked")
        event_cache.matching_pending_lookup_repairs = AsyncMock(return_value=frozenset({"$reply-1"}))
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=event_cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        _prime_resolved_thread_history(
            access,
            room_id="!room:localhost",
            thread_id="$thread",
            history=[
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply-1", body="Reply 1", sender="@agent:localhost"),
            ],
            source_event_ids=frozenset({"$thread", "$reply-1"}),
        )

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply-1"},
                },
                "event_id": "$edit:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!room:localhost",
                "type": "m.room.message",
            },
        )

        await access.append_live_event(
            "!room:localhost",
            edit_event,
            event_info=EventInfo.from_event(edit_event.source),
        )

        refreshed_history = ThreadHistoryResult(
            [
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply-1", body="Reply 1 updated", sender="@agent:localhost"),
            ],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: True,
                THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: True,
            },
        )
        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
            new=AsyncMock(return_value=refreshed_history),
        ) as mock_fetch_thread_history:
            async with access.turn_scope():
                history = await access.get_thread_history("!room:localhost", "$thread")

        mock_fetch_thread_history.assert_awaited_once()
        assert [message.body for message in history] == ["Root", "Reply 1 updated"]

    @pytest.mark.asyncio
    async def test_lookup_repair_promotion_bumps_thread_generation_before_repair(self) -> None:
        """Repair-triggering lookup promotions must advance generation before storing repaired history."""
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=_runtime_event_cache(),
        )
        runtime.event_cache.matching_pending_lookup_repairs = AsyncMock(return_value=frozenset({"$reply-1"}))
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        access._bump_thread_version("!room:localhost", "$thread")
        stale_history = thread_history_result(
            [
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply-1", body="Reply 1", sender="@agent:localhost"),
            ],
            is_full_history=True,
            thread_version=1,
        )
        _prime_resolved_thread_history(
            access,
            room_id="!room:localhost",
            thread_id="$thread",
            history=list(stale_history),
            source_event_ids=frozenset({"$thread", "$reply-1"}),
            thread_version=1,
        )
        await access._writes._mark_lookup_repair_pending(
            "!room:localhost",
            "$reply-1",
            reason="sync_redaction_lookup_missing",
        )

        repaired_history = ThreadHistoryResult(
            [_message(event_id="$thread", body="Root")],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: True,
                THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: True,
            },
        )
        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
            new=AsyncMock(return_value=repaired_history),
        ):
            async with access.turn_scope():
                refreshed_history = await access.get_thread_history("!room:localhost", "$thread")

        assert refreshed_history.thread_version is not None
        assert refreshed_history.thread_version > stale_history.thread_version

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_uses_authoritative_full_history(self) -> None:
        """MSC3440 fallback resolution should force a refreshed authoritative full-history read."""
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=_runtime_event_cache(),
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        latest_message = _message(event_id="$reply", body="Reply", sender="@agent:localhost")
        latest_message.apply_edit(
            body="Reply edited",
            timestamp=2,
            latest_event_id="$reply_edit",
            thread_id="$thread",
            content={"body": "Reply edited"},
        )
        full_history = thread_history_result(
            [
                _message(event_id="$thread", body="Root"),
                latest_message,
            ],
            is_full_history=True,
        )

        with (
            patch.object(
                access._reads,
                "fetch_thread_history_from_client",
                new=AsyncMock(return_value=full_history),
            ) as mock_fetch_thread_history,
        ):
            latest_event_id = await access.get_latest_thread_event_id_if_needed("!room:localhost", "$thread")

        assert latest_event_id == "$reply_edit"
        mock_fetch_thread_history.assert_awaited_once_with(
            "!room:localhost",
            "$thread",
            refresh_cache=True,
        )

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_uses_repair_aware_history_for_dirty_threads(self, tmp_path: Path) -> None:
        """Dirty-thread fallback resolution should honor the same repair path as get_thread_history()."""
        cache = _EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)

        root_event = {
            "event_id": "$thread",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {"body": "Root", "msgtype": "m.text"},
        }
        reply_event = {
            "event_id": "$reply",
            "sender": "@agent:localhost",
            "origin_server_ts": 2000,
            "type": "m.room.message",
            "content": {
                "body": "Reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
            },
        }
        await cache.store_events("!room:localhost", "$thread", [root_event, reply_event])
        await cache.mark_thread_repair_required("!room:localhost", "$thread")

        stale_history = thread_history_result(
            [
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply", body="Reply", sender="@agent:localhost"),
            ],
            is_full_history=True,
        )
        latest_message = _message(event_id="$reply", body="Reply", sender="@agent:localhost")
        latest_message.apply_edit(
            body="Reply edited",
            timestamp=2,
            latest_event_id="$reply_edit",
            thread_id="$thread",
            content={"body": "Reply edited"},
        )
        repaired_history = ThreadHistoryResult(
            [
                _message(event_id="$thread", body="Root"),
                latest_message,
            ],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: True,
                THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: True,
            },
        )

        async def fetch_history_side_effect(
            _client: nio.AsyncClient,
            room_id: str,
            thread_id: str,
            *,
            event_cache: _EventCache,
            refresh_cache: bool = True,
        ) -> ThreadHistoryResult:
            assert refresh_cache is True
            cached_thread_events = await event_cache.get_thread_events(room_id, thread_id)
            if cached_thread_events is not None:
                return stale_history
            return repaired_history

        try:
            with patch(
                "mindroom.matrix.conversation_cache.fetch_thread_history",
                new=AsyncMock(side_effect=fetch_history_side_effect),
            ):
                async with access.turn_scope():
                    latest_event_id = await access.get_latest_thread_event_id_if_needed("!room:localhost", "$thread")
        finally:
            await cache.close()

        assert latest_event_id == "$reply_edit"

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_forces_authoritative_refresh_even_when_sync_is_fresh(self) -> None:
        """MSC3440 fallback should bypass the sync-fresh shortcut and request a refresh."""
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=_runtime_event_cache(),
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        refreshed_history = thread_history_result(
            [
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply", body="Reply", sender="@agent:localhost"),
            ],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: True,
                THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: True,
            },
        )

        with (
            patch.object(
                access._reads,
                "fetch_thread_history_from_client",
                new=AsyncMock(return_value=refreshed_history),
            ) as mock_fetch_thread_history,
            patch.object(
                access._reads,
                "_store_resolved_thread_cache_entry",
                new=AsyncMock(return_value=frozenset({"$thread", "$reply"})),
            ),
        ):
            latest_event_id = await access.get_latest_thread_event_id_if_needed("!room:localhost", "$thread")

        assert latest_event_id == "$reply"
        assert mock_fetch_thread_history.await_args.kwargs["refresh_cache"] is True

    @pytest.mark.asyncio
    async def test_outbound_room_edit_without_thread_mapping_does_not_mark_lookup_repair(self) -> None:
        """Ordinary room-mode edits should not persist thread repair rows when no mapping exists."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event.return_value = None
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = "@mindroom_test:localhost"
        runtime = _conversation_runtime(
            client=client,
            event_cache=event_cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)

        await access.record_outbound_message(
            "!room:localhost",
            "$edit:localhost",
            {
                "body": "* updated",
                "msgtype": "m.text",
                "m.new_content": {"body": "updated", "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$room-message"},
            },
        )

        event_cache.mark_pending_lookup_repair.assert_not_awaited()
        event_cache.append_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_outbound_room_edit_without_thread_mapping_marks_matching_thread_for_refresh(self) -> None:
        """Room-mode edits should mark known affected threads dirty when mapping lookup misses."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event.return_value = None
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = "@mindroom_test:localhost"
        runtime = _conversation_runtime(
            client=client,
            event_cache=event_cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        _prime_resolved_thread_history(
            access,
            room_id="!room:localhost",
            thread_id="$thread",
            history=[
                _message(event_id="$thread", body="Root"),
                _message(event_id="$room-message", body="Reply"),
            ],
            source_event_ids=frozenset({"$thread", "$room-message"}),
        )

        await access.record_outbound_message(
            "!room:localhost",
            "$edit:localhost",
            {
                "body": "* updated",
                "msgtype": "m.text",
                "m.new_content": {"body": "updated", "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$room-message"},
            },
        )

        event_cache.mark_thread_repair_required.assert_awaited_once_with("!room:localhost", "$thread")

    @pytest.mark.asyncio
    async def test_sync_room_edit_without_thread_mapping_does_not_mark_lookup_repair(self) -> None:
        """Sync room-mode edits should not persist thread repair rows when no mapping exists."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event.return_value = None
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=event_cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$room-message"},
                },
                "event_id": "$edit:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!room:localhost",
                "type": "m.room.message",
            },
        )
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock(
            join={
                "!room:localhost": MagicMock(timeline=MagicMock(events=[edit_event])),
            },
        )

        access.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=runtime)

        event_cache.mark_pending_lookup_repair.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_room_redaction_without_thread_mapping_marks_matching_thread_for_refresh(self) -> None:
        """Room-mode redactions should mark known affected threads dirty when mapping lookup misses."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event.return_value = None
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=event_cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        _prime_resolved_thread_history(
            access,
            room_id="!room:localhost",
            thread_id="$thread",
            history=[
                _message(event_id="$thread", body="Root"),
                _message(event_id="$room-message", body="Reply"),
            ],
            source_event_ids=frozenset({"$thread", "$room-message"}),
        )

        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$room-message"
        redaction_event.sender = "@user:localhost"
        redaction_event.server_timestamp = 1234567891
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$room-message",
            "room_id": "!room:localhost",
            "type": "m.room.redaction",
        }
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock(
            join={
                "!room:localhost": MagicMock(timeline=MagicMock(events=[redaction_event])),
            },
        )

        access.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=runtime)

        event_cache.mark_thread_repair_required.assert_awaited_once_with("!room:localhost", "$thread")

    @pytest.mark.asyncio
    async def test_sync_room_redaction_without_thread_mapping_does_not_mark_lookup_repair(self) -> None:
        """Redacting a non-thread event should not create durable thread repair obligations."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event.return_value = None
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=event_cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)

        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$room-message"
        redaction_event.sender = "@user:localhost"
        redaction_event.server_timestamp = 1234567891
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$room-message",
            "room_id": "!room:localhost",
            "type": "m.room.redaction",
        }
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock(
            join={
                "!room:localhost": MagicMock(timeline=MagicMock(events=[redaction_event])),
            },
        )

        access.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=runtime)

        event_cache.mark_pending_lookup_repair.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unrelated_lookup_failure_does_not_force_other_threads_to_full_history(self) -> None:
        """Room-scoped lookup failures should only promote matching threads into repair mode."""
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=_runtime_event_cache(),
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        _prime_resolved_thread_history(
            access,
            room_id="!room:localhost",
            thread_id="$thread",
            history=[
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply-1", body="Reply 1", sender="@agent:localhost"),
            ],
            source_event_ids=frozenset({"$thread", "$reply-1"}),
        )
        await access._writes._mark_lookup_repair_pending(
            "!room:localhost",
            "$other-event",
            reason="lookup_failed_elsewhere",
        )

        with (
            patch(
                "mindroom.matrix.conversation_cache.fetch_thread_snapshot",
                new=AsyncMock(
                    return_value=ThreadHistoryResult(
                        [_message(event_id="$thread", body="Root")],
                        is_full_history=False,
                    ),
                ),
            ) as mock_fetch_thread_snapshot,
            patch(
                "mindroom.matrix.conversation_cache.fetch_thread_history",
                new=AsyncMock(side_effect=AssertionError("should not force full thread repair")),
            ),
        ):
            async with access.turn_scope():
                history = await access.get_thread_snapshot("!room:localhost", "$thread")

        mock_fetch_thread_snapshot.assert_awaited_once()
        assert [message.body for message in history] == ["Root"]
        assert await access._thread_requires_refresh("!room:localhost", "$thread") is False

    @pytest.mark.asyncio
    async def test_event_cache_removes_thread_membership_for_redacted_events(self, tmp_path: Path) -> None:
        """Redactions should clear the durable event-to-thread mapping for deleted events."""
        event_cache = _EventCache(tmp_path / "event-cache.db")
        await event_cache.initialize()
        try:
            await event_cache.store_events(
                "!room:localhost",
                "$thread",
                [
                    {
                        "content": {"body": "Root", "msgtype": "m.text"},
                        "event_id": "$thread",
                        "origin_server_ts": 1,
                        "room_id": "!room:localhost",
                        "sender": "@user:localhost",
                        "type": "m.room.message",
                    },
                    {
                        "content": {
                            "body": "Reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
                        },
                        "event_id": "$reply",
                        "origin_server_ts": 2,
                        "room_id": "!room:localhost",
                        "sender": "@user:localhost",
                        "type": "m.room.message",
                    },
                ],
            )

            assert await event_cache.get_thread_id_for_event("!room:localhost", "$reply") == "$thread"

            await event_cache.redact_event("!room:localhost", "$reply")

            assert await event_cache.get_thread_id_for_event("!room:localhost", "$reply") is None
        finally:
            await event_cache.close()

    @pytest.mark.asyncio
    async def test_sync_thread_lookup_failure_invalidates_matching_resolved_thread_cache(self) -> None:
        """Sync edits resolved via cached membership should not leave stale resolved entries behind."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event.side_effect = RuntimeError("database is locked")
        event_cache.matching_pending_lookup_repairs = AsyncMock(return_value=frozenset({"$reply-1"}))
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=event_cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        _prime_resolved_thread_history(
            access,
            room_id="!room:localhost",
            thread_id="$thread",
            history=[
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply-1", body="Reply 1", sender="@agent:localhost"),
            ],
            source_event_ids=frozenset({"$thread", "$reply-1"}),
        )

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply-1"},
                },
                "event_id": "$edit:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!room:localhost",
                "type": "m.room.message",
            },
        )
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock(
            join={
                "!room:localhost": MagicMock(timeline=MagicMock(events=[edit_event])),
            },
        )

        access.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=runtime)

        refreshed_history = ThreadHistoryResult(
            [
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply-1", body="Reply 1 updated", sender="@agent:localhost"),
            ],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: True,
                THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: True,
            },
        )
        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
            new=AsyncMock(return_value=refreshed_history),
        ) as mock_fetch_thread_history:
            async with access.turn_scope():
                history = await access.get_thread_history("!room:localhost", "$thread")

        mock_fetch_thread_history.assert_awaited_once()
        assert [message.body for message in history] == ["Root", "Reply 1 updated"]

    @pytest.mark.asyncio
    async def test_sync_redaction_lookup_failure_invalidates_matching_resolved_thread_cache(self) -> None:
        """Sync redactions must force a refetch when point lookup cannot map the redacted event."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event.side_effect = RuntimeError("database is locked")
        event_cache.matching_pending_lookup_repairs = AsyncMock(return_value=frozenset({"$reply-1"}))
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=event_cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        _prime_resolved_thread_history(
            access,
            room_id="!room:localhost",
            thread_id="$thread",
            history=[
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply-1", body="Reply 1", sender="@agent:localhost"),
            ],
            source_event_ids=frozenset({"$thread", "$reply-1"}),
        )

        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$reply-1"
        redaction_event.sender = "@user:localhost"
        redaction_event.server_timestamp = 1234567891
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$reply-1",
            "room_id": "!room:localhost",
            "type": "m.room.redaction",
        }
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock(
            join={
                "!room:localhost": MagicMock(timeline=MagicMock(events=[redaction_event])),
            },
        )

        access.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=runtime)

        refreshed_history = ThreadHistoryResult(
            [_message(event_id="$thread", body="Root")],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: True,
                THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: True,
            },
        )
        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
            new=AsyncMock(return_value=refreshed_history),
        ) as mock_fetch_thread_history:
            async with access.turn_scope():
                history = await access.get_thread_history("!room:localhost", "$thread")

        mock_fetch_thread_history.assert_awaited_once()
        assert [message.event_id for message in history] == ["$thread"]

    @pytest.mark.asyncio
    async def test_successful_redaction_repair_clears_pending_lookup_state(self) -> None:
        """Successful authoritative repair should clear thread-specific lookup repairs for later snapshots."""
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=_runtime_event_cache(),
        )
        runtime.event_cache.matching_pending_lookup_repairs = AsyncMock(
            side_effect=[frozenset({"$reply-1"}), frozenset()],
        )
        access = MatrixConversationCache(logger=MagicMock(), runtime=runtime)
        _prime_resolved_thread_history(
            access,
            room_id="!room:localhost",
            thread_id="$thread",
            history=[
                _message(event_id="$thread", body="Root"),
                _message(event_id="$reply-1", body="Reply 1", sender="@agent:localhost"),
            ],
            source_event_ids=frozenset({"$thread", "$reply-1"}),
        )
        await access._writes._mark_lookup_repair_pending(
            "!room:localhost",
            "$reply-1",
            reason="sync_redaction_lookup_missing",
        )

        refreshed_history = ThreadHistoryResult(
            [_message(event_id="$thread", body="Root")],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER,
                THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC: True,
                THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC: True,
            },
        )
        with patch(
            "mindroom.matrix.conversation_cache.fetch_thread_history",
            new=AsyncMock(return_value=refreshed_history),
        ):
            async with access.turn_scope():
                history = await access.get_thread_history("!room:localhost", "$thread")

        assert [message.event_id for message in history] == ["$thread"]
        assert await access._thread_requires_refresh("!room:localhost", "$thread") is False

        with (
            patch(
                "mindroom.matrix.conversation_cache.fetch_thread_snapshot",
                new=AsyncMock(
                    return_value=ThreadHistoryResult(
                        [_message(event_id="$thread", body="Root")],
                        is_full_history=False,
                    ),
                ),
            ) as mock_fetch_thread_snapshot,
            patch(
                "mindroom.matrix.conversation_cache.fetch_thread_history",
                new=AsyncMock(side_effect=AssertionError("repair should have been cleared")),
            ),
        ):
            async with access.turn_scope():
                snapshot = await access.get_thread_snapshot("!room:localhost", "$thread")

        mock_fetch_thread_snapshot.assert_awaited_once()
        assert [message.event_id for message in snapshot] == ["$thread"]

    @pytest.mark.asyncio
    async def test_get_thread_history_invalidates_resolved_cache_on_redaction(self, tmp_path: Path) -> None:
        """Redactions should drop the resolved cache entry instead of serving stale replies."""
        cache = _EventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime = _conversation_runtime(
            client=AsyncMock(spec=nio.AsyncClient),
            event_cache=cache,
        )
        runtime.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=runtime,
        )
        runtime.last_sync_activity_monotonic = time.monotonic()
        access = MatrixConversationCache(
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
        await cache.store_events("!room:localhost", "$thread", [root_event, first_reply])

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
                "mindroom.matrix.conversation_cache.fetch_thread_history",
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
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        await bot._initialize_runtime_support_services()
        assert bot.event_cache

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
            with patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_fetch:
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

            with patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_fetch_again:
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
            bot._conversation_cache,
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

        with patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_fetch:
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

        with patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_fetch:
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

        with patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_fetch:
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

        with patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_fetch:
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
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_fetch,
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
        with patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_fetch:
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
        with patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_fetch:
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
        with patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_fetch:
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
            bot._conversation_cache,
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
            bot._conversation_cache,
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
    async def test_extract_dispatch_context_defers_sidecar_hydration_until_full_history_is_requested(
        self,
        bot: AgentBot,
    ) -> None:
        """Dispatch preview should stay lightweight until one explicit full-history read is requested."""
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
            patch.object(bot._conversation_cache, "get_thread_snapshot", new=mock_snapshot),
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(return_value=full_thread_history),
            ) as mock_fetch,
        ):
            preview_context = await bot._conversation_resolver.extract_dispatch_context(room, event)

            assert preview_context.thread_id == "$thread_root:localhost"
            assert [msg.event_id for msg in preview_context.thread_history] == [
                "$thread_root:localhost",
                "$thread_msg:localhost",
                "$plain1:localhost",
            ]
            assert preview_context.thread_history[-1].body == "Preview plain reply [Message continues in attached file]"
            assert preview_context.requires_full_thread_history is True
            bot.client.download.assert_not_awaited()
            assert bot.client.room_get_event.await_count == 2

            full_context = await bot._conversation_resolver.extract_message_context(room, event)

        assert full_context.thread_id == "$thread_root:localhost"
        assert [msg.event_id for msg in full_context.thread_history] == [
            "$thread_root:localhost",
            "$thread_msg:localhost",
            "$plain1:localhost",
        ]
        assert full_context.thread_history[-1].body == "Hydrated plain reply from sidecar"
        assert full_context.thread_history[-1].content["body"] == "Hydrated plain reply from sidecar"
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
            bot._conversation_cache,
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

        with patch.object(bot._conversation_cache, "get_thread_history", AsyncMock(return_value=[])):
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
