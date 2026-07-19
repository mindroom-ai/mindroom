"""Event cache write coordination: outbound update queueing, streaming-edit coalescing, timing logs, and same-room serialization."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest

import mindroom.timing as timing_module
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.constants import STREAM_STATUS_COMPLETED, STREAM_STATUS_KEY
from mindroom.matrix.cache import ThreadHistoryResult, thread_writes
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
    _conversation_runtime,
    _make_client_mock,
    _message,
    _outbound_plain_edit_content,
    _outbound_streaming_edit_content,
    _runtime_event_cache,
    _runtime_write_coordinator,
    _thread_mutation_cache_ops,
    _wait_for_room_cache_idle,
    thread_history_result,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from mindroom.bot import AgentBot


class TestThreadingBehavior(ThreadingBehaviorTestBase):
    """Threading behavior tests moved verbatim from tests/test_threading_error.py."""

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
    async def test_get_event_persists_lookup_inline_inside_room_write_barrier(self) -> None:
        """A lookup fill inside the active barrier must not queue a write behind itself."""
        event_cache = _runtime_event_cache()
        event_cache.get_event = AsyncMock(return_value=None)
        event_cache.store_event = AsyncMock()
        coordinator = _runtime_write_coordinator()
        client = _make_client_mock()
        response = nio.RoomGetEventResponse.from_dict(
            {
                "content": {"body": "hello", "msgtype": "m.text"},
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )
        client.room_get_event = AsyncMock(return_value=response)
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=client,
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )

        async def lookup_inside_barrier() -> object:
            return await access.get_event("!test:localhost", "$event:localhost")

        try:
            update_task = coordinator.queue_room_update(
                "!test:localhost",
                lookup_inside_barrier,
                name="lookup_inside_room_barrier",
            )
            result = await asyncio.wait_for(update_task, timeout=1.0)
        finally:
            await coordinator.close()

        assert isinstance(result, nio.RoomGetEventResponse)
        assert result.event.event_id == "$event:localhost"
        event_cache.store_event.assert_awaited_once_with(
            "$event:localhost",
            "!test:localhost",
            response.event.source,
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
        bot._conversation_cache.notify_outbound_redaction = Mock()

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
        bot._conversation_cache.notify_outbound_redaction.assert_called_once_with(
            "!test:localhost",
            "$target:localhost",
        )

    @pytest.mark.asyncio
    async def test_queue_room_cache_update_forwards_false_emit_timing(self) -> None:
        """Room cache facade must not fall through to the coordinator timing default."""
        cache_ops, _logger, _event_cache = _thread_mutation_cache_ops()
        observed_emit_timing: list[bool] = []

        class _RecordingCoordinator:
            def queue_room_update(
                self,
                room_id: str,
                update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
                *,
                name: str,
                log_exceptions: bool = True,
                emit_timing: bool = True,
                coalesce_key: tuple[str, str] | None = None,
                coalesce_log_context: dict[str, object] | None = None,
            ) -> asyncio.Task[object]:
                del room_id, name, log_exceptions, coalesce_key, coalesce_log_context
                observed_emit_timing.append(emit_timing)
                return asyncio.create_task(update_coro_factory())

        async def update() -> None:
            return None

        cache_ops.runtime.event_cache_write_coordinator = _RecordingCoordinator()
        task = cache_ops.queue_room_cache_update("!room:localhost", update, name="matrix_cache_test_update")
        await task

        assert observed_emit_timing == [False]

    @pytest.mark.asyncio
    async def test_queue_thread_cache_update_forwards_default_coordinator_options(self) -> None:
        """Thread cache facade should always forward the expanded coordinator options."""
        cache_ops, _logger, _event_cache = _thread_mutation_cache_ops()
        observed_options: list[tuple[object, object, object]] = []

        class _RecordingCoordinator:
            def queue_thread_update(
                self,
                room_id: str,
                thread_id: str,
                update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
                *,
                name: str,
                log_exceptions: bool = True,
                emit_timing: object = "missing",
                coalesce_key: object = "missing",
                coalesce_log_context: object = "missing",
            ) -> asyncio.Task[object]:
                del room_id, thread_id, name, log_exceptions
                observed_options.append((emit_timing, coalesce_key, coalesce_log_context))
                return asyncio.create_task(update_coro_factory())

        async def update() -> None:
            return None

        cache_ops.runtime.event_cache_write_coordinator = _RecordingCoordinator()
        task = cache_ops.queue_thread_cache_update(
            "!room:localhost",
            "$thread:localhost",
            update,
            name="matrix_cache_test_update",
        )
        await task

        assert observed_options == [(False, None, None)]

    @pytest.mark.asyncio
    async def test_outbound_nonterminal_streaming_edits_coalesce_pending_cache_updates(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pending outbound stream edits for the same event should collapse to the latest edit."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        event_cache = _runtime_event_cache()
        coordinator_logger = MagicMock()
        coordinator = EventCacheWriteCoordinator(
            logger=coordinator_logger,
            background_task_owner=object(),
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=_make_client_mock(),
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def blocker() -> None:
            blocker_started.set()
            await release_blocker.wait()

        try:
            blocker_task = coordinator.queue_thread_update(
                "!test:localhost",
                "$thread:localhost",
                blocker,
                name="matrix_cache_blocker",
            )
            await asyncio.wait_for(blocker_started.wait(), timeout=1.0)

            for index in range(3):
                access.notify_outbound_message(
                    "!test:localhost",
                    f"$edit-{index}:localhost",
                    _outbound_streaming_edit_content(body=f"stream update {index}"),
                )

            release_blocker.set()
            await blocker_task
            await asyncio.wait_for(
                coordinator.wait_for_thread_idle("!test:localhost", "$thread:localhost"),
                timeout=1.0,
            )
        finally:
            release_blocker.set()
            await coordinator.close()

        event_cache.append_event.assert_awaited_once()
        _room_id, _thread_id, appended_event = event_cache.append_event.await_args.args
        assert appended_event["event_id"] == "$edit-2:localhost"
        assert appended_event["content"]["m.new_content"]["body"] == "stream update 2"
        assert any(
            call.kwargs.get("coalesced_update_count") == 2
            and call.kwargs.get("original_event_id") == "$stream-original:localhost"
            for call in coordinator_logger.info.call_args_list
        )
        assert any(
            call.args == ("Event cache update timing",)
            and call.kwargs["barrier_kind"] == "thread"
            and call.kwargs["operation"] == "matrix_cache_notify_outbound_event"
            and call.kwargs["coalesced_update_count"] == 2
            and call.kwargs["predecessor_wait_ms"] >= 0.0
            and call.kwargs["update_run_ms"] >= 0.0
            for call in timing_logger.debug.call_args_list
        )

    @pytest.mark.asyncio
    async def test_outbound_final_streaming_edit_is_preserved_after_coalesced_intermediates(self) -> None:
        """A final outbound stream edit should stay queued behind the latest intermediate edit."""
        event_cache = _runtime_event_cache()
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=_make_client_mock(),
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def blocker() -> None:
            blocker_started.set()
            await release_blocker.wait()

        try:
            blocker_task = coordinator.queue_thread_update(
                "!test:localhost",
                "$thread:localhost",
                blocker,
                name="matrix_cache_blocker",
            )
            await asyncio.wait_for(blocker_started.wait(), timeout=1.0)

            access.notify_outbound_message(
                "!test:localhost",
                "$edit-1:localhost",
                _outbound_streaming_edit_content(body="older intermediate"),
            )
            access.notify_outbound_message(
                "!test:localhost",
                "$edit-2:localhost",
                _outbound_streaming_edit_content(body="latest intermediate"),
            )
            access.notify_outbound_message(
                "!test:localhost",
                "$edit-final:localhost",
                _outbound_streaming_edit_content(
                    body="final answer",
                    stream_status=STREAM_STATUS_COMPLETED,
                ),
            )

            release_blocker.set()
            await blocker_task
            await asyncio.wait_for(
                coordinator.wait_for_thread_idle("!test:localhost", "$thread:localhost"),
                timeout=1.0,
            )
        finally:
            release_blocker.set()
            await coordinator.close()

        appended_event_ids = [call.args[2]["event_id"] for call in event_cache.append_event.await_args_list]
        assert appended_event_ids == ["$edit-2:localhost", "$edit-final:localhost"]
        final_content = event_cache.append_event.await_args_list[-1].args[2]["content"]
        assert final_content["m.new_content"][STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED

    @pytest.mark.asyncio
    async def test_outbound_plain_edits_are_not_coalesced(self) -> None:
        """Normal outbound edits should retain the existing one-cache-update-per-edit behavior."""
        event_cache = _runtime_event_cache()
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=_make_client_mock(),
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def blocker() -> None:
            blocker_started.set()
            await release_blocker.wait()

        try:
            blocker_task = coordinator.queue_thread_update(
                "!test:localhost",
                "$thread:localhost",
                blocker,
                name="matrix_cache_blocker",
            )
            await asyncio.wait_for(blocker_started.wait(), timeout=1.0)

            access.notify_outbound_message(
                "!test:localhost",
                "$plain-edit-1:localhost",
                _outbound_plain_edit_content(body="plain edit 1"),
            )
            access.notify_outbound_message(
                "!test:localhost",
                "$plain-edit-2:localhost",
                _outbound_plain_edit_content(body="plain edit 2"),
            )

            release_blocker.set()
            await blocker_task
            await asyncio.wait_for(
                coordinator.wait_for_thread_idle("!test:localhost", "$thread:localhost"),
                timeout=1.0,
            )
        finally:
            release_blocker.set()
            await coordinator.close()

        appended_event_ids = [call.args[2]["event_id"] for call in event_cache.append_event.await_args_list]
        assert appended_event_ids == ["$plain-edit-1:localhost", "$plain-edit-2:localhost"]

    @pytest.mark.asyncio
    async def test_queue_room_update_logs_timing_breakdown_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Room-scoped cache updates should log predecessor wait versus update time."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )
        first_started = asyncio.Event()
        allow_first_finish = asyncio.Event()

        async def first_update() -> str:
            first_started.set()
            await allow_first_finish.wait()
            return "first"

        async def second_update() -> str:
            return "second"

        try:
            first_task = coordinator.queue_room_update(
                "!room:localhost",
                first_update,
                name="matrix_cache_first_update",
            )
            await asyncio.wait_for(first_started.wait(), timeout=1.0)
            second_task = coordinator.queue_room_update(
                "!room:localhost",
                second_update,
                name="matrix_cache_second_update",
            )
            await asyncio.sleep(0)
            allow_first_finish.set()
            assert await first_task == "first"
            assert await second_task == "second"
        finally:
            await coordinator.close()

        timing_calls = [
            call for call in timing_logger.debug.call_args_list if call.args == ("Event cache update timing",)
        ]
        assert any(
            call.kwargs["barrier_kind"] == "room"
            and call.kwargs["operation"] == "matrix_cache_second_update"
            and call.kwargs["queued_behind_predecessor"] is True
            and call.kwargs["predecessor_count"] >= 1
            and call.kwargs["predecessor_wait_ms"] >= 0.0
            and call.kwargs["update_run_ms"] >= 0.0
            and call.kwargs["total_ms"] >= call.kwargs["update_run_ms"]
            for call in timing_calls
        )

    @pytest.mark.asyncio
    async def test_queue_room_update_logs_wait_from_raw_interval_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Predecessor wait should come from the raw pre-update interval, not rounded subtraction."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        perf_counter_values = iter([0.0, 0.00002, 0.00016, 0.00016])
        monkeypatch.setattr(
            "mindroom.matrix.cache.write_coordinator.time.perf_counter",
            lambda: next(perf_counter_values),
        )
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )

        async def update() -> str:
            return "ok"

        try:
            task = coordinator.queue_room_update(
                "!room:localhost",
                update,
                name="matrix_cache_single_update",
            )
            assert await task == "ok"
        finally:
            await coordinator.close()

        timing_call = next(
            call
            for call in timing_logger.debug.call_args_list
            if call.args == ("Event cache update timing",) and call.kwargs["operation"] == "matrix_cache_single_update"
        )
        assert timing_call.kwargs["predecessor_wait_ms"] == 0.0
        assert timing_call.kwargs["update_run_ms"] == 0.1
        assert timing_call.kwargs["total_ms"] == 0.2

    @pytest.mark.asyncio
    async def test_queue_room_update_logs_full_predecessor_chain_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Room-scoped cache updates should report the full queued predecessor chain length."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )
        first_started = asyncio.Event()
        allow_first_finish = asyncio.Event()
        second_started = asyncio.Event()
        allow_second_finish = asyncio.Event()

        async def first_update() -> str:
            first_started.set()
            await allow_first_finish.wait()
            return "first"

        async def second_update() -> str:
            second_started.set()
            await allow_second_finish.wait()
            return "second"

        async def third_update() -> str:
            return "third"

        try:
            first_task = coordinator.queue_room_update(
                "!room:localhost",
                first_update,
                name="matrix_cache_first_update",
            )
            await asyncio.wait_for(first_started.wait(), timeout=1.0)
            second_task = coordinator.queue_room_update(
                "!room:localhost",
                second_update,
                name="matrix_cache_second_update",
            )
            third_task = coordinator.queue_room_update(
                "!room:localhost",
                third_update,
                name="matrix_cache_third_update",
            )
            await asyncio.sleep(0)
            allow_first_finish.set()
            await asyncio.wait_for(second_started.wait(), timeout=1.0)
            allow_second_finish.set()
            assert await first_task == "first"
            assert await second_task == "second"
            assert await third_task == "third"
        finally:
            await coordinator.close()

        timing_call = next(
            call
            for call in timing_logger.debug.call_args_list
            if call.args == ("Event cache update timing",) and call.kwargs["operation"] == "matrix_cache_third_update"
        )
        assert timing_call.kwargs["predecessor_count"] == 2
        assert timing_call.kwargs["queued_behind_predecessor"] is True

    @pytest.mark.asyncio
    async def test_queue_room_update_skips_timing_overhead_when_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Disabled timing should not touch the perf_counter instrumentation path."""
        monkeypatch.delenv("MINDROOM_TIMING", raising=False)
        monkeypatch.setattr(
            "mindroom.matrix.cache.write_coordinator.time.perf_counter",
            Mock(side_effect=AssertionError("perf_counter should stay unused when timing is disabled")),
        )
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )

        async def update() -> str:
            return "ok"

        try:
            task = coordinator.queue_room_update(
                "!room:localhost",
                update,
                name="matrix_cache_single_update",
            )
            assert await task == "ok"
        finally:
            await coordinator.close()

    @pytest.mark.asyncio
    async def test_append_live_event_logs_phase_breakdown_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Live ingress appends should expose resolver, queue, and cache-write timings."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        event_cache = _runtime_event_cache()
        event_cache.append_event = AsyncMock(return_value=True)
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
        )
        access._live._resolver.resolve_thread_impact_for_mutation = AsyncMock(
            return_value=MutationThreadImpact.threaded("$thread:localhost"),
        )
        event = nio.RoomMessageText.from_dict(
            {
                "type": "m.room.message",
                "room_id": "!room:localhost",
                "event_id": "$reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234,
                "content": {
                    "body": "hello",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread:localhost"},
                },
            },
        )

        await access.append_live_event(
            "!room:localhost",
            event,
            event_info=EventInfo.from_event(event.source),
        )

        append_calls = [
            call for call in timing_logger.debug.call_args_list if call.args == ("Live event cache append timing",)
        ]
        assert any(
            call.kwargs["thread_id"] == "$thread:localhost"
            and call.kwargs["event_id"] == "$reply:localhost"
            and call.kwargs["impact_state"] == "threaded"
            and call.kwargs["impact_resolution_ms"] >= 0.0
            and call.kwargs["queue_and_update_ms"] >= 0.0
            and call.kwargs["invalidate_ms"] >= 0.0
            and call.kwargs["append_ms"] >= 0.0
            and call.kwargs["outcome"] == "ok"
            for call in append_calls
        )

    @pytest.mark.asyncio
    async def test_append_live_event_logs_append_failure_outcome_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Live ingress timing should classify append misses as append failures."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        event_cache = _runtime_event_cache()
        event_cache.append_event = AsyncMock(return_value=False)
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
        )
        access._live._resolver.resolve_thread_impact_for_mutation = AsyncMock(
            return_value=MutationThreadImpact.threaded("$thread:localhost"),
        )
        event = nio.RoomMessageText.from_dict(
            {
                "type": "m.room.message",
                "room_id": "!room:localhost",
                "event_id": "$reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234,
                "content": {
                    "body": "hello",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread:localhost"},
                },
            },
        )

        await access.append_live_event(
            "!room:localhost",
            event,
            event_info=EventInfo.from_event(event.source),
        )

        timing_call = next(
            call for call in timing_logger.debug.call_args_list if call.args == ("Live event cache append timing",)
        )
        assert timing_call.kwargs["thread_id"] == "$thread:localhost"
        assert timing_call.kwargs["appended"] is False
        assert timing_call.kwargs["outcome"] == "append_failed"

    @pytest.mark.asyncio
    async def test_append_live_event_skips_timing_overhead_when_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Disabled timing should not touch the live-append perf_counter instrumentation path."""
        monkeypatch.delenv("MINDROOM_TIMING", raising=False)
        monkeypatch.setattr(
            "mindroom.matrix.cache.thread_writes.time.perf_counter",
            Mock(side_effect=AssertionError("perf_counter should stay unused when timing is disabled")),
        )
        cache_ops, _logger, event_cache = _thread_mutation_cache_ops()

        class _InlineCoordinator:
            def queue_room_update(
                self,
                room_id: str,
                update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
                *,
                name: str,
                log_exceptions: bool = True,
                emit_timing: bool = False,
                coalesce_key: tuple[str, str] | None = None,
                coalesce_log_context: dict[str, object] | None = None,
            ) -> asyncio.Task[object]:
                del room_id, name, log_exceptions, emit_timing, coalesce_key, coalesce_log_context
                return asyncio.create_task(update_coro_factory())

            def queue_thread_update(
                self,
                room_id: str,
                thread_id: str,
                update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
                *,
                name: str,
                log_exceptions: bool = True,
                emit_timing: bool = False,
                coalesce_key: tuple[str, str] | None = None,
                coalesce_log_context: dict[str, object] | None = None,
            ) -> asyncio.Task[object]:
                del thread_id
                return self.queue_room_update(
                    room_id,
                    update_coro_factory,
                    name=name,
                    log_exceptions=log_exceptions,
                    emit_timing=emit_timing,
                    coalesce_key=coalesce_key,
                    coalesce_log_context=coalesce_log_context,
                )

        cache_ops.runtime.event_cache_write_coordinator = _InlineCoordinator()
        resolver = MagicMock()
        resolver.resolve_thread_impact_for_mutation = AsyncMock(
            return_value=MutationThreadImpact.threaded("$thread:localhost"),
        )
        policy = thread_writes.ThreadLiveWritePolicy(
            resolver=resolver,
            cache_ops=cache_ops,
        )
        event = nio.RoomMessageText.from_dict(
            {
                "type": "m.room.message",
                "room_id": "!room:localhost",
                "event_id": "$reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234,
                "content": {
                    "body": "hello",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread:localhost"},
                },
            },
        )

        await policy.append_live_event(
            "!room:localhost",
            event,
            event_info=EventInfo.from_event(event.source),
        )

        event_cache.mark_thread_stale.assert_awaited_once_with(
            "!room:localhost",
            "$thread:localhost",
            reason="live_thread_mutation",
        )
        event_cache.append_event.assert_awaited_once()

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
        coordinator = EventCacheWriteCoordinator(
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
    async def test_shared_event_cache_write_coordinator_allows_other_thread_updates_while_one_thread_runs(
        self,
    ) -> None:
        """Same-room thread updates should not serialize across unrelated threads."""
        first_update_started = asyncio.Event()
        release_first_update = asyncio.Event()
        second_update_started = asyncio.Event()
        owner = object()
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )

        async def first_update() -> None:
            first_update_started.set()
            await release_first_update.wait()

        async def second_update() -> None:
            second_update_started.set()

        coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-a:localhost",
            lambda: first_update(),
            name="matrix_cache_first_thread_update",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            lambda: second_update(),
            name="matrix_cache_second_thread_update",
        )
        await asyncio.sleep(0)
        assert second_update_started.is_set()

        release_first_update.set()
        await wait_for_background_tasks(timeout=1.0, owner=owner)

    @pytest.mark.asyncio
    async def test_shared_event_cache_write_coordinator_keeps_pending_room_barrier_across_blocked_threads(  # noqa: PLR0915
        self,
    ) -> None:
        """A queued room update should keep later unrelated threads blocked until the room segment clears."""
        first_thread_started = asyncio.Event()
        release_first_thread = asyncio.Event()
        room_update_started = asyncio.Event()
        release_room_update = asyncio.Event()
        second_thread_started = asyncio.Event()
        release_second_thread = asyncio.Event()
        sibling_thread_started = asyncio.Event()
        release_sibling_thread = asyncio.Event()
        owner = object()
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )

        async def first_thread_update() -> None:
            first_thread_started.set()
            await release_first_thread.wait()

        async def room_update() -> None:
            room_update_started.set()
            await release_room_update.wait()

        async def second_thread_update() -> None:
            second_thread_started.set()
            await release_second_thread.wait()

        async def sibling_thread_update() -> None:
            sibling_thread_started.set()
            await release_sibling_thread.wait()

        first_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-a:localhost",
            first_thread_update,
            name="matrix_cache_first_thread_update",
        )
        await asyncio.wait_for(first_thread_started.wait(), timeout=1.0)

        room_task = coordinator.queue_room_update(
            "!test:localhost",
            room_update,
            name="matrix_cache_room_update",
        )
        second_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-a:localhost",
            second_thread_update,
            name="matrix_cache_second_thread_update",
        )
        sibling_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            sibling_thread_update,
            name="matrix_cache_sibling_thread_update",
        )
        try:
            await asyncio.sleep(0.05)
            assert room_update_started.is_set() is False
            assert second_thread_started.is_set() is False
            assert sibling_thread_started.is_set() is False

            release_first_thread.set()
            await asyncio.wait_for(first_thread_task, timeout=1.0)
            await asyncio.wait_for(room_update_started.wait(), timeout=1.0)

            await asyncio.sleep(0.05)
            assert second_thread_started.is_set() is False
            assert sibling_thread_started.is_set() is False

            release_room_update.set()
            await asyncio.wait_for(room_task, timeout=1.0)
            await asyncio.wait_for(second_thread_started.wait(), timeout=1.0)
            await asyncio.wait_for(sibling_thread_started.wait(), timeout=1.0)

            release_second_thread.set()
            release_sibling_thread.set()
            await asyncio.wait_for(
                asyncio.gather(
                    second_thread_task,
                    sibling_thread_task,
                ),
                timeout=1.0,
            )
        finally:
            release_first_thread.set()
            release_room_update.set()
            release_second_thread.set()
            release_sibling_thread.set()
            await asyncio.wait_for(
                asyncio.gather(
                    first_thread_task,
                    room_task,
                    second_thread_task,
                    sibling_thread_task,
                    return_exceptions=True,
                ),
                timeout=1.0,
            )

    @pytest.mark.asyncio
    async def test_get_thread_history_does_not_wait_for_other_thread_update(self) -> None:
        """Thread reads should not stall behind unrelated thread updates in the same room."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        other_thread_update_started = asyncio.Event()
        release_other_thread_update = asyncio.Event()
        fetch_started = asyncio.Event()

        async def blocking_other_thread_update() -> None:
            other_thread_update_started.set()
            await release_other_thread_update.wait()

        async def fetch_history(
            _room_id: str,
            _thread_id: str,
            *,
            caller_label: str,
            coordinator_queue_wait_ms: float,
        ) -> ThreadHistoryResult:
            assert caller_label == "unknown"
            assert coordinator_queue_wait_ms >= 0.0
            fetch_started.set()
            return thread_history_result(
                [_message(event_id="$thread-a:localhost", body="Root")],
                is_full_history=True,
            )

        access._reads.fetch_thread_history_from_client = AsyncMock(side_effect=fetch_history)
        access.runtime.event_cache_write_coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            lambda: blocking_other_thread_update(),
            name="matrix_cache_blocking_other_thread_update",
        )
        await asyncio.wait_for(other_thread_update_started.wait(), timeout=1.0)

        history = await asyncio.wait_for(
            access.get_thread_history("!test:localhost", "$thread-a:localhost"),
            timeout=1.0,
        )

        assert fetch_started.is_set()
        assert [message.body for message in history] == ["Root"]
        release_other_thread_update.set()
        await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)

    @pytest.mark.asyncio
    async def test_point_read_barrier_does_not_wait_for_later_room_update(self) -> None:
        """A point read must wait for predecessors without being extended by later writes."""
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        release_second = asyncio.Event()
        coordinator = _runtime_write_coordinator()

        async def first_update() -> None:
            first_started.set()
            await release_first.wait()

        async def second_update() -> None:
            second_started.set()
            await release_second.wait()

        first_task = coordinator.queue_room_update(
            "!test:localhost",
            first_update,
            name="first_room_update",
        )
        await first_started.wait()
        read_barrier = asyncio.create_task(
            coordinator.wait_for_prior_room_updates("!test:localhost"),
        )
        await asyncio.sleep(0)
        second_task = coordinator.queue_room_update(
            "!test:localhost",
            second_update,
            name="later_room_update",
        )

        try:
            release_first.set()
            await second_started.wait()
            await asyncio.wait_for(read_barrier, timeout=1.0)
            assert second_task.done() is False
        finally:
            release_first.set()
            release_second.set()
            await asyncio.gather(first_task, second_task, return_exceptions=True)
            await coordinator.close()

    @pytest.mark.asyncio
    async def test_point_read_barrier_waits_for_queued_predecessor(self) -> None:
        """The point-read fence must include predecessors queued but not yet started."""
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        queued_predecessor_started = asyncio.Event()
        release_queued_predecessor = asyncio.Event()
        later_started = asyncio.Event()
        release_later = asyncio.Event()
        coordinator = _runtime_write_coordinator()

        async def first_update() -> None:
            first_started.set()
            await release_first.wait()

        async def queued_predecessor() -> None:
            queued_predecessor_started.set()
            await release_queued_predecessor.wait()

        async def later_update() -> None:
            later_started.set()
            await release_later.wait()

        first_task = coordinator.queue_room_update(
            "!test:localhost",
            first_update,
            name="first_room_update",
        )
        await first_started.wait()
        queued_predecessor_task = coordinator.queue_room_update(
            "!test:localhost",
            queued_predecessor,
            name="queued_predecessor",
        )
        read_barrier = asyncio.create_task(
            coordinator.wait_for_prior_room_updates("!test:localhost"),
        )
        await asyncio.sleep(0)
        later_task = coordinator.queue_room_update(
            "!test:localhost",
            later_update,
            name="later_room_update",
        )

        try:
            release_first.set()
            await queued_predecessor_started.wait()
            await asyncio.sleep(0)
            assert read_barrier.done() is False

            release_queued_predecessor.set()
            await later_started.wait()
            await asyncio.wait_for(read_barrier, timeout=1.0)
            assert later_task.done() is False
        finally:
            release_first.set()
            release_queued_predecessor.set()
            release_later.set()
            await asyncio.gather(
                first_task,
                queued_predecessor_task,
                later_task,
                return_exceptions=True,
            )
            await coordinator.close()

    @pytest.mark.asyncio
    async def test_wait_for_thread_idle_ignores_cancelled_room_fence_for_unrelated_thread(self) -> None:
        """Thread reads should ignore cancelled room fences that only preserve write ordering."""
        first_thread_started = asyncio.Event()
        release_first_thread = asyncio.Event()
        second_thread_started = asyncio.Event()
        release_second_thread = asyncio.Event()
        coordinator = _runtime_write_coordinator()

        async def first_thread_update() -> None:
            first_thread_started.set()
            await release_first_thread.wait()

        async def second_thread_update() -> None:
            second_thread_started.set()
            await release_second_thread.wait()

        async def cancelled_room_update() -> None:
            msg = "Cancelled room cache update should not start"
            raise AssertionError(msg)

        first_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-a:localhost",
            first_thread_update,
            name="matrix_cache_first_thread_update",
        )
        second_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            second_thread_update,
            name="matrix_cache_second_thread_update",
        )
        await asyncio.wait_for(first_thread_started.wait(), timeout=1.0)
        await asyncio.wait_for(second_thread_started.wait(), timeout=1.0)

        cancelled_room_task = coordinator.queue_room_update(
            "!test:localhost",
            cancelled_room_update,
            name="matrix_cache_cancelled_room_update",
        )
        try:
            cancelled_room_task.cancel()
            await asyncio.gather(cancelled_room_task, return_exceptions=True)

            await asyncio.wait_for(
                coordinator.wait_for_thread_idle(
                    "!test:localhost",
                    "$thread-c:localhost",
                    ignore_cancelled_room_fences=True,
                ),
                timeout=0.1,
            )
            assert first_thread_task.done() is False
            assert second_thread_task.done() is False
        finally:
            release_first_thread.set()
            release_second_thread.set()
            if not cancelled_room_task.done():
                cancelled_room_task.cancel()
            await asyncio.wait_for(
                asyncio.gather(
                    first_thread_task,
                    second_thread_task,
                    cancelled_room_task,
                    return_exceptions=True,
                ),
                timeout=1.0,
            )

    @pytest.mark.asyncio
    async def test_run_thread_update_preserves_same_thread_order_across_ignored_cancelled_room_fence(  # noqa: PLR0915
        self,
    ) -> None:
        """Ignoring a cancelled room fence must not let a later same-thread update jump the queue."""
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        first_target_started = asyncio.Event()
        release_first_target = asyncio.Event()
        second_target_started = asyncio.Event()
        release_second_target = asyncio.Event()
        coordinator = _runtime_write_coordinator()
        run_order: list[str] = []

        async def blocking_other_thread() -> None:
            blocker_started.set()
            await release_blocker.wait()

        async def first_target_update() -> str:
            run_order.append("first")
            first_target_started.set()
            await release_first_target.wait()
            return "first"

        async def second_target_update() -> str:
            run_order.append("second")
            second_target_started.set()
            await release_second_target.wait()
            return "second"

        async def cancelled_room_update() -> None:
            msg = "Cancelled room cache update should not start"
            raise AssertionError(msg)

        blocker_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$other-thread:localhost",
            blocking_other_thread,
            name="matrix_cache_blocking_other_thread_update",
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=1.0)

        cancelled_room_task = coordinator.queue_room_update(
            "!test:localhost",
            cancelled_room_update,
            name="matrix_cache_cancelled_room_update",
        )
        first_target_task = asyncio.create_task(
            coordinator.run_thread_update(
                "!test:localhost",
                "$target-thread:localhost",
                first_target_update,
                name="matrix_cache_first_target_thread_update",
            ),
        )
        second_target_task = asyncio.create_task(
            coordinator.run_thread_update(
                "!test:localhost",
                "$target-thread:localhost",
                second_target_update,
                name="matrix_cache_second_target_thread_update",
                ignore_cancelled_room_fences=True,
            ),
        )

        try:
            cancelled_room_task.cancel()
            await asyncio.gather(cancelled_room_task, return_exceptions=True)

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(second_target_started.wait(), timeout=0.1)
            assert first_target_started.is_set() is False

            release_blocker.set()
            await asyncio.wait_for(first_target_started.wait(), timeout=1.0)
            assert run_order == ["first"]

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(second_target_started.wait(), timeout=0.1)

            release_first_target.set()
            await asyncio.wait_for(second_target_started.wait(), timeout=1.0)
            release_second_target.set()
            assert await asyncio.wait_for(first_target_task, timeout=1.0) == "first"
            assert await asyncio.wait_for(second_target_task, timeout=1.0) == "second"
            assert run_order == ["first", "second"]
        finally:
            release_blocker.set()
            release_first_target.set()
            release_second_target.set()
            if not cancelled_room_task.done():
                cancelled_room_task.cancel()
            await asyncio.wait_for(
                asyncio.gather(
                    blocker_task,
                    first_target_task,
                    second_target_task,
                    cancelled_room_task,
                    return_exceptions=True,
                ),
                timeout=1.0,
            )
            await _wait_for_room_cache_idle(coordinator)

    @pytest.mark.asyncio
    async def test_get_thread_history_ignores_cancelled_room_fence_for_unrelated_thread(self) -> None:
        """Public thread-history reads should bypass cancelled room fences without waiting for other threads."""
        first_thread_started = asyncio.Event()
        release_first_thread = asyncio.Event()
        second_thread_started = asyncio.Event()
        release_second_thread = asyncio.Event()
        coordinator = _runtime_write_coordinator()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(coordinator=coordinator),
        )

        async def first_thread_update() -> None:
            first_thread_started.set()
            await release_first_thread.wait()

        async def second_thread_update() -> None:
            second_thread_started.set()
            await release_second_thread.wait()

        async def cancelled_room_update() -> None:
            msg = "Cancelled room cache update should not start"
            raise AssertionError(msg)

        access._reads.fetch_thread_history_from_client = AsyncMock(
            return_value=thread_history_result(
                [_message(event_id="$thread-c:localhost", body="Root")],
                is_full_history=True,
            ),
        )
        first_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-a:localhost",
            first_thread_update,
            name="matrix_cache_first_thread_update",
        )
        second_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            second_thread_update,
            name="matrix_cache_second_thread_update",
        )
        await asyncio.wait_for(first_thread_started.wait(), timeout=1.0)
        await asyncio.wait_for(second_thread_started.wait(), timeout=1.0)

        cancelled_room_task = coordinator.queue_room_update(
            "!test:localhost",
            cancelled_room_update,
            name="matrix_cache_cancelled_room_update",
        )
        try:
            cancelled_room_task.cancel()
            await asyncio.gather(cancelled_room_task, return_exceptions=True)

            history = await asyncio.wait_for(
                access.get_thread_history("!test:localhost", "$thread-c:localhost"),
                timeout=0.1,
            )

            assert [message.body for message in history] == ["Root"]
            access._reads.fetch_thread_history_from_client.assert_awaited_once()
            fetch_args = access._reads.fetch_thread_history_from_client.await_args
            assert fetch_args is not None
            assert fetch_args.args == ("!test:localhost", "$thread-c:localhost")
            assert fetch_args.kwargs["caller_label"] == "unknown"
            assert fetch_args.kwargs["coordinator_queue_wait_ms"] >= 0.0
            assert first_thread_task.done() is False
            assert second_thread_task.done() is False
        finally:
            release_first_thread.set()
            release_second_thread.set()
            if not cancelled_room_task.done():
                cancelled_room_task.cancel()
            await asyncio.wait_for(
                asyncio.gather(
                    first_thread_task,
                    second_thread_task,
                    cancelled_room_task,
                    return_exceptions=True,
                ),
                timeout=1.0,
            )

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
        coordinator = EventCacheWriteCoordinator(
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
    async def test_cancelled_room_cache_update_keeps_follow_up_thread_update_behind_all_predecessors(  # noqa: PLR0915
        self,
    ) -> None:
        """Cancelling a room update must not let a later thread update skip unfinished room predecessors."""
        first_thread_started = asyncio.Event()
        release_first_thread = asyncio.Event()
        second_thread_started = asyncio.Event()
        release_second_thread = asyncio.Event()
        follow_up_thread_started = asyncio.Event()
        release_follow_up_thread = asyncio.Event()
        owner = object()
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )

        async def first_thread_update() -> None:
            first_thread_started.set()
            await release_first_thread.wait()

        async def second_thread_update() -> None:
            second_thread_started.set()
            await release_second_thread.wait()

        async def cancelled_room_update() -> None:
            msg = "Cancelled room cache update should not start"
            raise AssertionError(msg)

        async def follow_up_thread_update() -> None:
            follow_up_thread_started.set()
            await release_follow_up_thread.wait()

        first_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-a:localhost",
            first_thread_update,
            name="matrix_cache_first_thread_update",
        )
        second_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            second_thread_update,
            name="matrix_cache_second_thread_update",
        )
        await asyncio.wait_for(first_thread_started.wait(), timeout=1.0)
        await asyncio.wait_for(second_thread_started.wait(), timeout=1.0)

        cancelled_room_task = coordinator.queue_room_update(
            "!test:localhost",
            cancelled_room_update,
            name="matrix_cache_cancelled_room_update",
        )
        follow_up_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-c:localhost",
            follow_up_thread_update,
            name="matrix_cache_follow_up_thread_update",
        )
        try:
            cancelled_room_task.cancel()
            await asyncio.gather(cancelled_room_task, return_exceptions=True)

            await asyncio.sleep(0.05)
            assert follow_up_thread_started.is_set() is False
            assert follow_up_thread_task.done() is False

            release_first_thread.set()
            await asyncio.wait_for(first_thread_task, timeout=1.0)

            await asyncio.sleep(0.05)
            assert follow_up_thread_started.is_set() is False
            assert follow_up_thread_task.done() is False

            release_second_thread.set()
            await asyncio.wait_for(second_thread_task, timeout=1.0)
            await asyncio.wait_for(follow_up_thread_started.wait(), timeout=1.0)
            assert follow_up_thread_task.done() is False

            release_follow_up_thread.set()
            await asyncio.wait_for(follow_up_thread_task, timeout=1.0)
        finally:
            release_first_thread.set()
            release_second_thread.set()
            release_follow_up_thread.set()
            if not cancelled_room_task.done():
                cancelled_room_task.cancel()
            pending_tasks = [first_thread_task, second_thread_task, cancelled_room_task]
            if follow_up_thread_task is not None:
                pending_tasks.append(follow_up_thread_task)
            await asyncio.wait_for(
                asyncio.gather(
                    *pending_tasks,
                    return_exceptions=True,
                ),
                timeout=1.0,
            )

    @pytest.mark.asyncio
    async def test_cancelled_room_cache_update_still_blocks_later_thread_updates_queued_after_cancel(  # noqa: PLR0915
        self,
    ) -> None:
        """Cancelling a queued room update must not let later thread work overtake the earlier room segment."""
        first_thread_started = asyncio.Event()
        release_first_thread = asyncio.Event()
        second_thread_started = asyncio.Event()
        release_second_thread = asyncio.Event()
        follow_up_thread_started = asyncio.Event()
        release_follow_up_thread = asyncio.Event()
        coordinator = _runtime_write_coordinator()

        async def first_thread_update() -> None:
            first_thread_started.set()
            await release_first_thread.wait()

        async def second_thread_update() -> None:
            second_thread_started.set()
            await release_second_thread.wait()

        async def cancelled_room_update() -> None:
            msg = "Cancelled room cache update should not start"
            raise AssertionError(msg)

        async def follow_up_thread_update() -> None:
            follow_up_thread_started.set()
            await release_follow_up_thread.wait()

        first_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-a:localhost",
            first_thread_update,
            name="matrix_cache_first_thread_update",
        )
        second_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            second_thread_update,
            name="matrix_cache_second_thread_update",
        )
        await asyncio.wait_for(first_thread_started.wait(), timeout=1.0)
        await asyncio.wait_for(second_thread_started.wait(), timeout=1.0)

        cancelled_room_task = coordinator.queue_room_update(
            "!test:localhost",
            cancelled_room_update,
            name="matrix_cache_cancelled_room_update",
        )
        follow_up_thread_task: asyncio.Task[object] | None = None
        try:
            cancelled_room_task.cancel()
            await asyncio.gather(cancelled_room_task, return_exceptions=True)

            follow_up_thread_task = coordinator.queue_thread_update(
                "!test:localhost",
                "$thread-c:localhost",
                follow_up_thread_update,
                name="matrix_cache_follow_up_thread_update",
            )

            await asyncio.sleep(0.05)
            assert follow_up_thread_started.is_set() is False
            assert follow_up_thread_task.done() is False

            release_first_thread.set()
            await asyncio.wait_for(first_thread_task, timeout=1.0)

            await asyncio.sleep(0.05)
            assert follow_up_thread_started.is_set() is False
            assert follow_up_thread_task.done() is False

            release_second_thread.set()
            await asyncio.wait_for(second_thread_task, timeout=1.0)
            await asyncio.wait_for(follow_up_thread_started.wait(), timeout=1.0)

            release_follow_up_thread.set()
            await asyncio.wait_for(follow_up_thread_task, timeout=1.0)
        finally:
            release_first_thread.set()
            release_second_thread.set()
            release_follow_up_thread.set()
            if not cancelled_room_task.done():
                cancelled_room_task.cancel()
            await asyncio.wait_for(
                asyncio.gather(
                    first_thread_task,
                    second_thread_task,
                    cancelled_room_task,
                    follow_up_thread_task,
                    return_exceptions=True,
                ),
                timeout=1.0,
            )

    @pytest.mark.asyncio
    async def test_run_room_update_does_not_log_handled_exception_as_background_failure(self) -> None:
        """Awaited room updates should not be logged as unhandled background task failures."""
        owner = object()
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )

        async def failing_update() -> None:
            msg = "boom"
            raise RuntimeError(msg)

        with patch("mindroom.background_tasks.logger.exception") as background_logger_exception:
            with pytest.raises(RuntimeError, match="boom"):
                await coordinator.queue_room_update(
                    "!test:localhost",
                    lambda: failing_update(),
                    name="matrix_cache_test_failure",
                    log_exceptions=False,
                )
            await asyncio.sleep(0)

        background_logger_exception.assert_not_called()
