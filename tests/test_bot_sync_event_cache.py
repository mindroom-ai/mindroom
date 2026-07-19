"""Bot sync lifecycle and sync-timeline event cache maintenance: checkpoints, cache writes, and background task drains."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import nio
import pytest

from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG
from mindroom.hooks import EVENT_AGENT_STARTED
from mindroom.matrix.cache.event_cache import EventCacheBackendUnavailableError
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.client import PermanentMatrixStartupError
from mindroom.matrix.sync_certification import SyncCacheWriteResult, SyncCheckpoint
from mindroom.matrix.sync_tokens import load_sync_token_record
from mindroom.matrix.users import AgentMatrixUser
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN
from mindroom.runtime_support import (
    StartupThreadPrewarmRegistry,
)
from tests.conftest import (
    TEST_PASSWORD,
)
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
    _bind_owned_runtime_support,
    _close_bound_runtime_support,
    _install_runtime_write_coordinator,
    _load_sync_token_value,
    _make_client_mock,
    _runtime_event_cache,
    _save_certified_sync_token,
    _wait_for_room_cache_idle,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestThreadingBehavior(ThreadingBehaviorTestBase):
    """Threading behavior tests moved verbatim from tests/test_threading_error.py."""

    @pytest.mark.asyncio
    async def test_start_and_stop_manage_persistent_event_cache(self, bot: AgentBot) -> None:
        """Startup and stop should leave injected runtime support owned by its external lifecycle."""
        support = await _bind_owned_runtime_support(bot)
        start_client = _make_client_mock(user_id="@mindroom_general:localhost")
        start_client.add_event_callback = MagicMock()
        start_client.add_response_callback = MagicMock()
        start_client.close = AsyncMock()

        try:
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
                redaction_callback = next(
                    callback.args[0]
                    for callback in start_client.add_event_callback.call_args_list
                    if callback.args[1] is nio.RedactionEvent
                )
                assert redaction_callback == bot._on_redaction

                await bot.stop()

            await support.event_cache.store_event(
                "$post-stop-event",
                "!test:localhost",
                {
                    "event_id": "$post-stop-event",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "type": "m.room.message",
                    "content": {"body": "still open", "msgtype": "m.text"},
                },
            )
            cached_event = await support.event_cache.get_event("!test:localhost", "$post-stop-event")
        finally:
            await _close_bound_runtime_support(bot, support)

        assert bot.event_cache is support.event_cache
        assert bot.event_cache_write_coordinator is support.event_cache_write_coordinator
        assert bot.startup_thread_prewarm_registry is support.startup_thread_prewarm_registry
        assert cached_event is not None
        assert cached_event["event_id"] == "$post-stop-event"
        start_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_requires_injected_runtime_support(self, bot: AgentBot) -> None:
        """Agent startup should fail fast when no injected runtime-support bundle is present."""
        bot.event_cache = None
        bot.event_cache_write_coordinator = None
        bot.startup_thread_prewarm_registry = None

        with (
            patch.object(bot, "ensure_user_account", AsyncMock()) as ensure_user_account,
            patch("mindroom.bot.login_agent_user", AsyncMock()) as login_agent_user,
            pytest.raises(
                PermanentMatrixStartupError,
                match="Runtime support services must be injected before startup",
            ),
        ):
            await bot.start()

        ensure_user_account.assert_not_awaited()
        login_agent_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_injected_shared_event_cache_stays_open_for_other_bots(self, bot: AgentBot, tmp_path: Path) -> None:
        """Stopping one bot must not close shared injected runtime support used by another bot."""
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

        shared_cache = SqliteEventCache(bot.config.cache.resolve_db_path(bot.runtime_paths))
        shared_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )
        shared_registry = StartupThreadPrewarmRegistry()
        await shared_cache.initialize()
        bot.event_cache = shared_cache
        bot.event_cache_write_coordinator = shared_coordinator
        bot.startup_thread_prewarm_registry = shared_registry
        other_bot.event_cache = shared_cache
        other_bot.event_cache_write_coordinator = shared_coordinator
        other_bot.startup_thread_prewarm_registry = shared_registry
        bot.client = _make_client_mock(user_id="@mindroom_general:localhost")
        bot.client.close = AsyncMock()

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
            with (
                patch.object(bot, "_emit_agent_lifecycle_event", AsyncMock()),
                patch.object(bot, "prepare_for_sync_shutdown", AsyncMock()),
                patch("mindroom.bot.wait_for_background_tasks", AsyncMock()),
            ):
                await bot.stop()

            cached_event = await other_bot.event_cache.get_event("!test:localhost", "$shared-event")
        finally:
            await shared_cache.close()

        assert bot.event_cache is shared_cache
        assert other_bot.event_cache is shared_cache
        assert cached_event is not None
        assert cached_event["event_id"] == "$shared-event"
        bot.client.close.assert_awaited_once()

    def test_partial_runtime_support_injection_fails_fast(self, bot: AgentBot) -> None:
        """Startup validation should require the full injected runtime-support bundle."""
        bot.startup_thread_prewarm_registry = None

        with pytest.raises(
            PermanentMatrixStartupError,
            match="Runtime support services must be injected before startup",
        ):
            bot._validate_runtime_support_injection_contract_for_startup()

    @pytest.mark.asyncio
    async def test_try_start_partial_runtime_support_injection_fails_before_login(self, bot: AgentBot) -> None:
        """Partial runtime-support injection should stop startup before any login side effects."""
        bot.client = None
        bot.startup_thread_prewarm_registry = None

        with (
            patch.object(bot, "ensure_user_account", AsyncMock()) as ensure_user_account,
            patch("mindroom.bot.login_agent_user", AsyncMock()) as login_agent_user,
            pytest.raises(
                PermanentMatrixStartupError,
                match="Runtime support services must be injected before startup",
            ),
        ):
            await bot.try_start()

        ensure_user_account.assert_not_awaited()
        login_agent_user.assert_not_awaited()
        assert bot.client is None

    @pytest.mark.asyncio
    async def test_start_resets_running_flag_when_agent_started_hooks_fail(self, bot: AgentBot) -> None:
        """Startup cleanup should clear running state if EVENT_AGENT_STARTED emission fails."""
        support = await _bind_owned_runtime_support(bot)
        start_client = _make_client_mock(user_id="@mindroom_general:localhost")
        start_client.add_event_callback = MagicMock()
        start_client.add_response_callback = MagicMock()
        start_client.close = AsyncMock()
        bot.hook_registry = MagicMock()
        bot.hook_registry.has_hooks.side_effect = lambda event_name: event_name == EVENT_AGENT_STARTED

        try:
            with (
                patch.object(bot, "ensure_user_account", AsyncMock()),
                patch("mindroom.bot.login_agent_user", AsyncMock(return_value=start_client)),
                patch.object(bot, "_set_avatar_if_available", AsyncMock()),
                patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
                patch("mindroom.bot.interactive.init_persistence"),
                patch("mindroom.bot.emit", AsyncMock(side_effect=RuntimeError("hook boom"))),
                pytest.raises(RuntimeError, match="hook boom"),
            ):
                await bot.start()
        finally:
            await _close_bound_runtime_support(bot, support)

        start_client.close.assert_awaited_once()
        assert bot.running is False
        assert bot.client is None

    @pytest.mark.asyncio
    async def test_sync_response_caches_timeline_events_for_point_lookups(self, bot: AgentBot) -> None:
        """Sync-response handling should persist timeline events into SQLite-backed lookups."""
        support = await _bind_owned_runtime_support(bot)
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
                "!test:localhost": MagicMock(timeline=MagicMock(events=[message_event], limited=False)),
            }
            bot._first_sync_done = True

            await bot._on_sync_response(sync_response)
            await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)
            cached_event = await bot.event_cache.get_event("!test:localhost", "$thread_msg:localhost")
        finally:
            await _close_bound_runtime_support(bot, support)

        assert cached_event is not None
        assert cached_event["event_id"] == "$thread_msg:localhost"
        assert cached_event["content"]["body"] == "Thread reply"

    @pytest.mark.asyncio
    async def test_non_first_sync_waits_for_cache_write_before_token_persist(self, bot: AgentBot) -> None:
        """Incremental sync tokens must not save until their cache writes are durable."""
        cache_started = asyncio.Event()
        allow_cache_finish = asyncio.Event()

        async def delayed_cache_result(_response: nio.SyncResponse) -> SyncCacheWriteResult:
            cache_started.set()
            await allow_cache_finish.wait()
            return SyncCacheWriteResult(complete=True)

        bot._first_sync_done = True
        bot.client.next_batch = "s_after_delayed_cache"
        bot._coalescing_gate.drain_all = AsyncMock()
        sync_response = self._sync_response({})

        with patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            AsyncMock(side_effect=delayed_cache_result),
        ):
            response_task = asyncio.create_task(
                self._run_sync_response_without_startup_side_effects(bot, sync_response),
            )
            try:
                await asyncio.wait_for(cache_started.wait(), timeout=1.0)

                assert _load_sync_token_value(bot.storage_path, bot.agent_name) is None
                await bot.prepare_for_sync_shutdown()
                assert _load_sync_token_value(bot.storage_path, bot.agent_name) is None

                allow_cache_finish.set()
                await asyncio.wait_for(response_task, timeout=1.0)
            finally:
                allow_cache_finish.set()
                await asyncio.gather(response_task, return_exceptions=True)

        assert _load_sync_token_value(bot.storage_path, bot.agent_name) == "s_after_delayed_cache"

    @pytest.mark.asyncio
    async def test_restored_first_sync_success_updates_checkpoint(self, bot: AgentBot) -> None:
        """Successful restored-token catch-up should save the new checkpoint token."""
        _save_certified_sync_token(bot, "s_before_complete")
        bot._runtime_view.mark_runtime_started()
        bot._restore_saved_sync_token()
        bot.client.next_batch = "s_after_complete"

        await self._run_sync_response_without_startup_side_effects(bot, self._sync_response({}))

        token_record = load_sync_token_record(bot.storage_path, bot.agent_name)
        assert token_record is not None
        assert token_record.checkpoint.token == "s_after_complete"  # noqa: S105
        assert token_record.checkpoint == SyncCheckpoint("s_after_complete")

    @pytest.mark.asyncio
    async def test_limited_restored_first_sync_clears_token(self, bot: AgentBot) -> None:
        """Limited restored-token catch-up must fail closed and force a cold retry token."""
        _save_certified_sync_token(bot, "s_before_limited")
        bot._runtime_view.mark_runtime_started()
        bot._restore_saved_sync_token()
        bot.client.next_batch = "s_after_limited"
        sync_response = self._sync_response(
            {"!test:localhost": MagicMock(timeline=MagicMock(events=[], limited=True))},
        )

        await self._run_sync_response_without_startup_side_effects(bot, sync_response)

        assert bot.client.next_batch is None
        assert _load_sync_token_value(bot.storage_path, bot.agent_name) is None

    @pytest.mark.asyncio
    async def test_cache_failure_clears_token_then_later_success_saves_checkpoint(
        self,
        bot: AgentBot,
    ) -> None:
        """After cache uncertainty, later successful sync responses can save a checkpoint."""
        _save_certified_sync_token(bot, "s_before_failure")
        bot._runtime_view.mark_runtime_started()
        bot._restore_saved_sync_token()
        bot._first_sync_done = True
        bot.client.next_batch = "s_after_failure"
        failed_result = SyncCacheWriteResult(complete=True, errors=(RuntimeError("cache failed"),))

        with patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            AsyncMock(return_value=failed_result),
        ):
            await self._run_sync_response_without_startup_side_effects(bot, self._sync_response({}))

        assert _load_sync_token_value(bot.storage_path, bot.agent_name) is None

        bot.client.next_batch = "s_after_recovery"
        with patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
        ):
            await self._run_sync_response_without_startup_side_effects(bot, self._sync_response({}))

        token_record = load_sync_token_record(bot.storage_path, bot.agent_name)
        assert token_record is not None
        assert token_record.checkpoint.token == "s_after_recovery"  # noqa: S105
        assert token_record.checkpoint == SyncCheckpoint("s_after_recovery")

    @pytest.mark.asyncio
    async def test_empty_joined_rooms_first_sync_certifies_checkpoint(self, bot: AgentBot) -> None:
        """A non-limited empty sync response can certify that there were no room deltas."""
        _save_certified_sync_token(bot, "s_before_empty")
        bot._runtime_view.mark_runtime_started()
        bot._restore_saved_sync_token()
        bot.client.next_batch = "s_after_empty"

        await self._run_sync_response_without_startup_side_effects(bot, self._sync_response({}))

        token_record = load_sync_token_record(bot.storage_path, bot.agent_name)
        assert token_record is not None
        assert token_record.checkpoint.token == "s_after_empty"  # noqa: S105
        assert token_record.checkpoint == SyncCheckpoint("s_after_empty")

    @pytest.mark.asyncio
    async def test_empty_sync_flushes_pending_cache_writes_before_certifying(self, bot: AgentBot) -> None:
        """Sync-token certification should include pending runtime-only cache writes."""
        pending_room_ids = {"!pending:localhost"}
        event_cache = _runtime_event_cache()
        event_cache.pending_durable_write_room_ids.side_effect = lambda: tuple(sorted(pending_room_ids))

        async def flush_pending_durable_writes(room_id: str) -> None:
            pending_room_ids.discard(room_id)

        event_cache.flush_pending_durable_writes.side_effect = flush_pending_durable_writes
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        result = await bot._conversation_cache.cache_sync_timeline_for_certification(self._sync_response({}))

        assert result.complete is True
        assert result.task_count == 1
        event_cache.flush_pending_durable_writes.assert_awaited_once_with("!pending:localhost")

    @pytest.mark.asyncio
    async def test_empty_sync_does_not_certify_while_pending_cache_writes_remain(self, bot: AgentBot) -> None:
        """A sync token is not cache-certified while runtime-only writes remain in memory."""
        event_cache = _runtime_event_cache()
        event_cache.pending_durable_write_room_ids.return_value = ("!pending:localhost",)
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        result = await bot._conversation_cache.cache_sync_timeline_for_certification(self._sync_response({}))

        assert result.complete is False
        assert result.task_count == 1
        event_cache.flush_pending_durable_writes.assert_awaited_once_with("!pending:localhost")

    @pytest.mark.asyncio
    async def test_sync_write_failure_marks_room_stale_before_certification_retry(self, bot: AgentBot) -> None:
        """A failed sync timeline write must protect cached room threads before a later token can certify."""
        room_id = "!room:localhost"
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock(side_effect=RuntimeError("postgres write failed"))
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)
        message_event = nio.RoomMessageText.from_dict(
            {
                "content": {"body": "Fresh message", "msgtype": "m.text"},
                "event_id": "$message:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        result = await bot._conversation_cache.cache_sync_timeline_for_certification(
            self._sync_response({room_id: MagicMock(timeline=MagicMock(events=[message_event], limited=False))}),
        )

        assert result.complete is False
        assert [type(error) for error in result.errors] == [RuntimeError]
        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            room_id,
            reason="sync_timeline_write_failed",
        )

    @pytest.mark.asyncio
    async def test_sync_write_transient_failure_records_pending_room_stale_marker(self, bot: AgentBot) -> None:
        """Transient sync write loss should block later certification until the room marker is flushed."""
        room_id = "!room:localhost"
        backend_error = EventCacheBackendUnavailableError("postgres unavailable")
        pending_room_ids: set[str] = set()
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock(side_effect=backend_error)
        event_cache.pending_durable_write_room_ids.side_effect = lambda: tuple(sorted(pending_room_ids))

        async def mark_room_threads_stale(room_id_arg: str, *, reason: str) -> None:
            assert reason == "sync_timeline_write_failed"
            pending_room_ids.add(room_id_arg)
            raise backend_error

        event_cache.mark_room_threads_stale = AsyncMock(side_effect=mark_room_threads_stale)
        event_cache.invalidate_room_threads = AsyncMock(side_effect=backend_error)
        event_cache.disable = Mock()
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)
        message_event = nio.RoomMessageText.from_dict(
            {
                "content": {"body": "Fresh message", "msgtype": "m.text"},
                "event_id": "$message:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        result = await bot._conversation_cache.cache_sync_timeline_for_certification(
            self._sync_response({room_id: MagicMock(timeline=MagicMock(events=[message_event], limited=False))}),
        )

        assert result.complete is False
        assert result.errors == (backend_error,)
        assert result.runtime_diagnostics == {"cache_backend": "mock"}
        assert event_cache.pending_durable_write_room_ids() == (room_id,)
        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            room_id,
            reason="sync_timeline_write_failed",
        )
        event_cache.disable.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_error_keeps_watchdog_clock_on_latest_activity(self, bot: AgentBot) -> None:
        """Sync errors should keep the watchdog alive using the latest observed sync activity."""
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock(join={})
        sync_error = MagicMock(spec=nio.SyncError)
        bot._first_sync_done = True

        monotonic_values = iter([100.0, 200.0])

        def monotonic_side_effect() -> float:
            return next(monotonic_values, 200.0)

        with patch("mindroom.bot.time.monotonic", side_effect=monotonic_side_effect):
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
        support = await _bind_owned_runtime_support(bot)
        assert bot.event_cache

        try:
            await _replace_thread(
                bot.event_cache,
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
            await _wait_for_room_cache_idle(bot.event_cache_write_coordinator)

            cached_thread_events = await bot.event_cache.get_thread_events(
                "!test:localhost",
                "$thread_root:localhost",
            )
            cached_thread_id = await bot.event_cache.get_thread_id_for_event(
                "!test:localhost",
                "$thread_edit:localhost",
            )
        finally:
            await _close_bound_runtime_support(bot, support)

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
    async def test_cache_sync_timeline_plain_edit_lookup_miss_invalidates_room_threads(
        self,
        bot: AgentBot,
    ) -> None:
        """Sync room-mode edits should fail closed when lookup certainty is unavailable."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.redact_event = AsyncMock()
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        event_cache.get_event = AsyncMock(
            return_value={
                "event_id": "$room_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
                "content": {"body": "Room message", "msgtype": "m.text"},
            },
        )
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* Updated room message",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "Updated room message",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$room_msg:localhost"},
                },
                "event_id": "$room_edit:localhost",
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

        event_cache.get_thread_id_for_event.assert_awaited_once_with("!test:localhost", "$room_msg:localhost")
        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            "!test:localhost",
            reason="sync_thread_lookup_unavailable",
        )
        event_cache.append_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_plain_edit_missing_original_invalidates_room_threads(
        self,
        bot: AgentBot,
    ) -> None:
        """Sync plain edits without enough local proof should invalidate room thread snapshots once."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.redact_event = AsyncMock()
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        event_cache.get_event = AsyncMock(return_value=None)
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* Updated room message",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "Updated room message",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$missing-room-msg:localhost"},
                },
                "event_id": "$room_edit:localhost",
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

        event_cache.get_thread_id_for_event.assert_awaited_once_with("!test:localhost", "$missing-room-msg:localhost")
        event_cache.get_event.assert_awaited_once_with("!test:localhost", "$missing-room-msg:localhost")
        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            "!test:localhost",
            reason="sync_thread_lookup_unavailable",
        )
        event_cache.append_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_reaction_redaction_lookup_miss_without_cached_target_does_not_invalidate_room_threads(
        self,
        bot: AgentBot,
    ) -> None:
        """Sync redaction lookup misses should not poison the room when the target was already removed."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.redact_event = AsyncMock(return_value=False)
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        event_cache.get_event = AsyncMock(return_value=None)
        bot.event_cache = event_cache
        _install_runtime_write_coordinator(bot)

        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$reaction:localhost"
        redaction_event.sender = "@user:localhost"
        redaction_event.server_timestamp = 1234567891
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$reaction:localhost",
            "room_id": "!test:localhost",
            "type": "m.room.redaction",
        }
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[redaction_event])),
        }

        bot._conversation_cache.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        event_cache.redact_event.assert_awaited_once_with("!test:localhost", "$reaction:localhost")
        event_cache.mark_room_threads_stale.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_unknown_thread_mutations_invalidate_room_threads_once_without_room_scan(
        self,
        bot: AgentBot,
    ) -> None:
        """Sync mutation fallback should invalidate once per room and avoid room-history scans."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.append_event = AsyncMock(return_value=False)
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        event_cache.get_event = AsyncMock(return_value=None)
        bot.event_cache = event_cache
        bot.client = _make_client_mock()
        bot.client.room_messages = AsyncMock(side_effect=AssertionError("should not room-scan during sync mutations"))
        _install_runtime_write_coordinator(bot)

        first_edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* Updated room message",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "Updated room message",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$missing-room-msg-1:localhost"},
                },
                "event_id": "$room_edit_1:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567891,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        second_edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* Updated room message again",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "Updated room message again",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$missing-room-msg-2:localhost"},
                },
                "event_id": "$room_edit_2:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567892,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = {
            "!test:localhost": MagicMock(timeline=MagicMock(events=[first_edit_event, second_edit_event])),
        }

        bot._conversation_cache.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            "!test:localhost",
            reason="sync_thread_lookup_unavailable",
        )
        bot.client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_sync_timeline_unknown_redactions_invalidate_room_threads_once(self, bot: AgentBot) -> None:
        """Sync redaction fallback should stale-mark the room once even when multiple lookups miss in one batch."""
        event_cache = _runtime_event_cache()
        event_cache.store_events_batch = AsyncMock()
        event_cache.redact_event = AsyncMock(return_value=True)
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        event_cache.get_event = AsyncMock(return_value=None)
        bot.event_cache = event_cache
        bot.client = _make_client_mock()
        bot.client.room_get_event = AsyncMock(return_value=MagicMock())
        _install_runtime_write_coordinator(bot)

        first_redaction_event = MagicMock(spec=nio.RedactionEvent)
        first_redaction_event.event_id = "$redaction-1:localhost"
        first_redaction_event.redacts = "$missing-room-msg-1:localhost"
        first_redaction_event.sender = "@user:localhost"
        first_redaction_event.server_timestamp = 1234567891
        first_redaction_event.source = {
            "content": {},
            "event_id": "$redaction-1:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$missing-room-msg-1:localhost",
            "room_id": "!test:localhost",
            "type": "m.room.redaction",
        }
        second_redaction_event = MagicMock(spec=nio.RedactionEvent)
        second_redaction_event.event_id = "$redaction-2:localhost"
        second_redaction_event.redacts = "$missing-room-msg-2:localhost"
        second_redaction_event.sender = "@user:localhost"
        second_redaction_event.server_timestamp = 1234567892
        second_redaction_event.source = {
            "content": {},
            "event_id": "$redaction-2:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567892,
            "redacts": "$missing-room-msg-2:localhost",
            "room_id": "!test:localhost",
            "type": "m.room.redaction",
        }
        sync_response = MagicMock()
        sync_response.__class__ = nio.SyncResponse
        sync_response.rooms = MagicMock()
        sync_response.rooms.join = {
            "!test:localhost": MagicMock(
                timeline=MagicMock(events=[first_redaction_event, second_redaction_event]),
            ),
        }

        bot._conversation_cache.cache_sync_timeline(sync_response)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        assert event_cache.redact_event.await_args_list == [
            call("!test:localhost", "$missing-room-msg-1:localhost"),
            call("!test:localhost", "$missing-room-msg-2:localhost"),
        ]
        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            "!test:localhost",
            reason="sync_redaction_lookup_unavailable",
        )

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
        support = await _bind_owned_runtime_support(bot)
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
            await _close_bound_runtime_support(bot, support)

        assert cached_event is None

    @pytest.mark.asyncio
    async def test_live_redaction_callback_delegates_to_cleanup(self, bot: AgentBot) -> None:
        """The bot should await durable tombstoning and advisory cache cleanup."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_agent:localhost")
        redaction_event = MagicMock(spec=nio.RedactionEvent)

        with patch.object(
            bot._redacted_turn_cleanup,
            "handle",
            AsyncMock(),
        ) as handle:
            await bot._on_redaction(room, redaction_event)

        handle.assert_awaited_once_with(room, redaction_event)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", [RuntimeError("persist failed"), RuntimeError("cache failed")])
    async def test_live_redaction_failure_rewinds_to_last_certified_sync(
        self,
        bot: AgentBot,
        failure: RuntimeError,
    ) -> None:
        """A critical redaction failure must replay the sync delta on the same client."""
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_agent:localhost")
        redaction_event = MagicMock(spec=nio.RedactionEvent)
        _save_certified_sync_token(bot, "s_before_redaction")
        bot._sync_checkpoint = SyncCheckpoint("s_before_redaction")
        bot.client.next_batch = "s_after_redaction"

        with (
            patch.object(
                bot._redacted_turn_cleanup,
                "handle",
                AsyncMock(side_effect=failure),
            ),
            pytest.raises(RuntimeError, match=str(failure)),
        ):
            await bot._on_redaction(room, redaction_event)

        assert bot.client.next_batch == "s_before_redaction"
        assert _load_sync_token_value(bot.storage_path, bot.agent_name) == "s_before_redaction"

    @pytest.mark.asyncio
    async def test_sync_timeline_redaction_does_not_resurrect_point_lookup_cache(self, bot: AgentBot) -> None:
        """A sync batch that contains both a message and its redaction must leave no cached lookup entry."""
        support = await _bind_owned_runtime_support(bot)
        assert bot.event_cache

        try:
            await _replace_thread(
                bot.event_cache,
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
            await _close_bound_runtime_support(bot, support)

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
    async def test_wait_for_background_tasks_timeout_returns_when_task_suppresses_cancel(self) -> None:
        """Timed-out draining should not hang on a task that ignores cancellation."""
        owner = object()
        task_started = asyncio.Event()
        release_task = asyncio.Event()
        cancel_count = 0

        async def stubborn_task() -> None:
            nonlocal cancel_count
            task_started.set()
            while not release_task.is_set():
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancel_count += 1
                    if release_task.is_set():
                        raise

        task = create_background_task(stubborn_task(), name="stubborn_task", owner=owner)
        await asyncio.wait_for(task_started.wait(), timeout=1.0)

        try:
            completed = await asyncio.wait_for(
                wait_for_background_tasks(timeout=0.0, owner=owner),
                timeout=1.0,
            )
            assert completed is False
            assert cancel_count >= 1
        finally:
            release_task.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_wait_for_background_tasks_timeout_preserves_shutdown_intent(self) -> None:
        """Timed-out owner task cancellation should preserve shutdown provenance."""
        owner = object()
        task_started = asyncio.Event()
        cancelled_args: list[tuple[object, ...]] = []

        async def never_finishes() -> None:
            task_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as exc:
                cancelled_args.append(exc.args)
                raise

        create_background_task(never_finishes(), name="sync_restart_cancelled_task", owner=owner)
        await asyncio.wait_for(task_started.wait(), timeout=1.0)

        completed = await wait_for_background_tasks(
            timeout=0.0,
            owner=owner,
            shutdown_intent=SYNC_RESTART_SHUTDOWN,
        )

        assert completed is False
        assert cancelled_args == [(SYNC_RESTART_CANCEL_MSG,)]
