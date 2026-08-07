"""Event cache write coordination: update queueing, timing logs, and same-room serialization."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock

import nio
import pytest

import mindroom.timing as timing_module
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.matrix.cache import thread_writes
from mindroom.matrix.cache.thread_cache_state import ThreadAppendOutcome
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact
from tests.threading_helpers import (
    EmptyProjection,
    ThreadingBehaviorTestBase,
    _conversation_runtime,
    _make_client_mock,
    _runtime_event_cache,
    _runtime_write_coordinator,
    _thread_mutation_cache_ops,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any


class TestThreadingBehavior(ThreadingBehaviorTestBase):
    """Threading behavior tests moved verbatim from tests/test_threading_error.py."""

    @pytest.mark.asyncio
    async def test_same_turn_point_read_is_resolved_once(self) -> None:
        """A point read repeated inside one turn must resolve once, not once per caller."""
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
        client = _make_client_mock()
        client.room_get_event = AsyncMock(return_value=response)
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=client,
                event_cache=_runtime_event_cache(),
                coordinator=_runtime_write_coordinator(),
            ),
            store=EmptyProjection(),
        )

        async with access.turn_scope():
            first = await access.get_event("!test:localhost", "$event:localhost")
            second = await access.get_event("!test:localhost", "$event:localhost")

        assert first is second
        client.room_get_event.assert_awaited_once()

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
                emit_timing: bool = True,
                coordination_scope: str | None = None,
            ) -> asyncio.Task[object]:
                del room_id, name, coordination_scope
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
        cache_ops, _logger, event_cache = _thread_mutation_cache_ops()
        observed_options: list[tuple[object, object]] = []

        class _RecordingCoordinator:
            def queue_thread_update(
                self,
                room_id: str,
                thread_id: str,
                update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
                *,
                name: str,
                emit_timing: object = "missing",
                coordination_scope: object = "missing",
            ) -> asyncio.Task[object]:
                del room_id, thread_id, name
                observed_options.append((emit_timing, coordination_scope))
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

        assert observed_options == [(False, event_cache.principal_id)]

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
                coordination_scope="test-principal",
            )
            await asyncio.wait_for(first_started.wait(), timeout=1.0)
            second_task = coordinator.queue_room_update(
                "!room:localhost",
                second_update,
                name="matrix_cache_second_update",
                coordination_scope="test-principal",
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
                coordination_scope="test-principal",
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
                coordination_scope="test-principal",
            )
            await asyncio.wait_for(first_started.wait(), timeout=1.0)
            second_task = coordinator.queue_room_update(
                "!room:localhost",
                second_update,
                name="matrix_cache_second_update",
                coordination_scope="test-principal",
            )
            third_task = coordinator.queue_room_update(
                "!room:localhost",
                third_update,
                name="matrix_cache_third_update",
                coordination_scope="test-principal",
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
                coordination_scope="test-principal",
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
        event_cache.apply_thread_mutation_append = AsyncMock(return_value=ThreadAppendOutcome.APPENDED)
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
            store=EmptyProjection(),
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
        event_cache.apply_thread_mutation_append = AsyncMock(return_value=ThreadAppendOutcome.SNAPSHOT_MISSING)
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
            store=EmptyProjection(),
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
                emit_timing: bool = False,
                coordination_scope: str | None = None,
            ) -> asyncio.Task[object]:
                del room_id, name, emit_timing, coordination_scope
                return asyncio.create_task(update_coro_factory())

            def queue_thread_update(
                self,
                room_id: str,
                thread_id: str,
                update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
                *,
                name: str,
                emit_timing: bool = False,
                coordination_scope: str | None = None,
            ) -> asyncio.Task[object]:
                del thread_id
                return self.queue_room_update(
                    room_id,
                    update_coro_factory,
                    name=name,
                    emit_timing=emit_timing,
                    coordination_scope=coordination_scope,
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

        event_cache.apply_thread_mutation_append.assert_awaited_once()
        assert event_cache.apply_thread_mutation_append.await_args.kwargs["append_failed_reason"] == (
            "live_append_failed"
        )
        event_cache.mark_thread_gap.assert_not_awaited()

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
            store=EmptyProjection(),
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
            coordination_scope="test-principal",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: second_update(),
            name="matrix_cache_second_update",
            coordination_scope="test-principal",
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
            store=EmptyProjection(),
        )
        second_access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(coordinator=coordinator),
            store=EmptyProjection(),
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
            coordination_scope="test-principal",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        second_access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: second_update(),
            name="matrix_cache_second_update",
            coordination_scope="test-principal",
        )
        await asyncio.sleep(0)
        assert second_update_started.is_set() is False

        release_first_update.set()
        await wait_for_background_tasks(timeout=1.0, owner=owner)

        assert second_update_started.is_set()

    @pytest.mark.parametrize("principal_count", [10, 100])
    @pytest.mark.asyncio
    async def test_shared_event_cache_write_coordinator_parallelizes_distinct_principals(
        self,
        principal_count: int,
    ) -> None:
        """Principal-isolated caches should not share a same-room serialization lane."""
        started = [asyncio.Event() for _ in range(principal_count)]
        release_updates = asyncio.Event()
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )

        async def update(index: int) -> None:
            started[index].set()
            await release_updates.wait()

        tasks = [
            coordinator.queue_room_update(
                "!test:localhost",
                lambda index=index: update(index),
                name=f"matrix_cache_principal_update_{index}",
                coordination_scope=f"@agent-{index}:localhost",
            )
            for index in range(principal_count)
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in started)),
                timeout=1.0,
            )
        finally:
            release_updates.set()
            await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_coordination_keys_do_not_alias_on_component_delimiters(self) -> None:
        """Structured scope and room keys should remain distinct for arbitrary strings."""
        started = [asyncio.Event(), asyncio.Event()]
        release_updates = asyncio.Event()
        coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )

        async def update(index: int) -> None:
            started[index].set()
            await release_updates.wait()

        tasks = [
            coordinator.queue_room_update(
                "room",
                lambda: update(0),
                name="matrix_cache_delimited_scope",
                coordination_scope="scope\x1fother",
            ),
            coordinator.queue_room_update(
                "other\x1froom",
                lambda: update(1),
                name="matrix_cache_delimited_room",
                coordination_scope="scope",
            ),
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in started)),
                timeout=1.0,
            )
        finally:
            release_updates.set()
            await asyncio.gather(*tasks)

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
            coordination_scope="test-principal",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            lambda: second_update(),
            name="matrix_cache_second_thread_update",
            coordination_scope="test-principal",
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
            coordination_scope="test-principal",
        )
        await asyncio.wait_for(first_thread_started.wait(), timeout=1.0)

        room_task = coordinator.queue_room_update(
            "!test:localhost",
            room_update,
            name="matrix_cache_room_update",
            coordination_scope="test-principal",
        )
        second_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-a:localhost",
            second_thread_update,
            name="matrix_cache_second_thread_update",
            coordination_scope="test-principal",
        )
        sibling_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            sibling_thread_update,
            name="matrix_cache_sibling_thread_update",
            coordination_scope="test-principal",
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
            coordination_scope="test-principal",
        )
        second_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            second_thread_update,
            name="matrix_cache_second_thread_update",
            coordination_scope="test-principal",
        )
        await asyncio.wait_for(first_thread_started.wait(), timeout=1.0)
        await asyncio.wait_for(second_thread_started.wait(), timeout=1.0)

        cancelled_room_task = coordinator.queue_room_update(
            "!test:localhost",
            cancelled_room_update,
            name="matrix_cache_cancelled_room_update",
            coordination_scope="test-principal",
        )
        try:
            cancelled_room_task.cancel()
            await asyncio.gather(cancelled_room_task, return_exceptions=True)

            await asyncio.wait_for(
                coordinator.wait_for_thread_idle(
                    "!test:localhost",
                    "$thread-c:localhost",
                    coordination_scope="test-principal",
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
            store=EmptyProjection(),
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
            coordination_scope="test-principal",
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=1.0)

        queued_task = access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: queued_update(),
            name="matrix_cache_queued_update",
            coordination_scope="test-principal",
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
            store=EmptyProjection(),
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
            coordination_scope="test-principal",
        )
        await asyncio.wait_for(first_update_started.wait(), timeout=1.0)

        cancelled_task = access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: cancelled_update(),
            name="matrix_cache_cancelled_update",
            coordination_scope="test-principal",
        )
        cancelled_task.cancel()
        await asyncio.gather(cancelled_task, return_exceptions=True)

        access.runtime.event_cache_write_coordinator.queue_room_update(
            "!test:localhost",
            lambda: third_update(),
            name="matrix_cache_third_update",
            coordination_scope="test-principal",
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
            coordination_scope="test-principal",
        )
        second_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            second_thread_update,
            name="matrix_cache_second_thread_update",
            coordination_scope="test-principal",
        )
        await asyncio.wait_for(first_thread_started.wait(), timeout=1.0)
        await asyncio.wait_for(second_thread_started.wait(), timeout=1.0)

        cancelled_room_task = coordinator.queue_room_update(
            "!test:localhost",
            cancelled_room_update,
            name="matrix_cache_cancelled_room_update",
            coordination_scope="test-principal",
        )
        follow_up_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-c:localhost",
            follow_up_thread_update,
            name="matrix_cache_follow_up_thread_update",
            coordination_scope="test-principal",
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
            coordination_scope="test-principal",
        )
        second_thread_task = coordinator.queue_thread_update(
            "!test:localhost",
            "$thread-b:localhost",
            second_thread_update,
            name="matrix_cache_second_thread_update",
            coordination_scope="test-principal",
        )
        await asyncio.wait_for(first_thread_started.wait(), timeout=1.0)
        await asyncio.wait_for(second_thread_started.wait(), timeout=1.0)

        cancelled_room_task = coordinator.queue_room_update(
            "!test:localhost",
            cancelled_room_update,
            name="matrix_cache_cancelled_room_update",
            coordination_scope="test-principal",
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
                coordination_scope="test-principal",
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
