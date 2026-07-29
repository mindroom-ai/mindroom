"""Thread read guards and stale-cache rejection: live mutation barriers, guarded refills, and refetch behavior."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

import mindroom.matrix.cache as matrix_cache
import mindroom.matrix.cache.sqlite_event_cache_threads as sqlite_event_cache_threads_module
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_cache_state import ThreadAppendOutcome, ThreadCacheGap
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_CACHE,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
)
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
    _assert_racing_unknown_live_mutation_leaves_thread_gap_marked,
    _bind_owned_runtime_support,
    _close_bound_runtime_support,
    _conversation_runtime,
    _make_client_mock,
    _message,
    _relations_client,
    _reopen_event_cache,
    _runtime_event_cache,
    _runtime_write_coordinator,
    _text_event,
    _wait_for_room_cache_idle,
    thread_history_result,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.matrix.cache import ThreadHistoryResult


class TestThreadingBehavior(ThreadingBehaviorTestBase):
    """Threading behavior tests moved verbatim from tests/test_threading_error.py."""

    @pytest.mark.asyncio
    async def test_live_edit_cache_lookup_failure_does_not_raise(self, bot: AgentBot) -> None:
        """Live edit caching should degrade cleanly when SQLite lookup fails."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(side_effect=RuntimeError("database is locked"))
        event_cache.apply_thread_mutation_append = AsyncMock()
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
        event_cache.apply_thread_mutation_append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_plain_edit_lookup_miss_invalidates_room_threads(self, bot: AgentBot) -> None:
        """Live room-mode edits should fail closed when lookup certainty is unavailable."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        event_cache.get_event = AsyncMock(
            return_value={
                "event_id": "$room_msg:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567889,
                "type": "m.room.message",
                "content": {"body": "Room message", "msgtype": "m.text"},
            },
        )
        event_cache.apply_thread_mutation_append = AsyncMock()
        bot.event_cache = event_cache
        bot.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$room_msg:localhost"},
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
        await _wait_for_room_cache_idle(bot.event_cache_write_coordinator)

        event_cache.get_thread_id_for_event.assert_awaited_once_with("!test:localhost", "$room_msg:localhost")
        event_cache.mark_room_threads_gap.assert_awaited_once_with(
            "!test:localhost",
            reason="live_thread_lookup_unavailable",
        )
        event_cache.apply_thread_mutation_append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_plain_edit_missing_original_invalidates_room_threads(self, bot: AgentBot) -> None:
        """Live plain edits without enough local proof should invalidate room thread snapshots."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        event_cache.get_event = AsyncMock(return_value=None)
        event_cache.apply_thread_mutation_append = AsyncMock()
        bot.event_cache = event_cache
        bot.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$missing-room-msg:localhost"},
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
        await _wait_for_room_cache_idle(bot.event_cache_write_coordinator)

        event_cache.get_thread_id_for_event.assert_awaited_once_with("!test:localhost", "$missing-room-msg:localhost")
        event_cache.get_event.assert_awaited_once_with("!test:localhost", "$missing-room-msg:localhost")
        event_cache.mark_room_threads_gap.assert_awaited_once_with(
            "!test:localhost",
            reason="live_thread_lookup_unavailable",
        )
        event_cache.apply_thread_mutation_append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_message_resolution_does_not_block_same_room_read(self) -> None:
        """A same-room read must not wait on live mutation resolution before the write is queued."""
        coordinator = _runtime_write_coordinator()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=_make_client_mock(),
                event_cache=_runtime_event_cache(),
                coordinator=coordinator,
            ),
        )
        resolve_started = asyncio.Event()
        allow_resolve = asyncio.Event()
        read_finished = asyncio.Event()

        async def slow_resolve(*_args: object, **_kwargs: object) -> MutationThreadImpact:
            resolve_started.set()
            await allow_resolve.wait()
            return MutationThreadImpact.threaded("$mutated-thread:localhost")

        async def quick_history(
            _room_id: str,
            _thread_id: str,
            **_kwargs: object,
        ) -> ThreadHistoryResult:
            read_finished.set()
            return thread_history_result(
                [_message(event_id="$other-thread:localhost", body="Root")],
                is_full_history=True,
            )

        access._live._resolver.resolve_thread_impact_for_mutation = AsyncMock(side_effect=slow_resolve)
        access._reads.fetch_thread_history_from_client = AsyncMock(side_effect=quick_history)

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread-msg:localhost"},
                },
                "event_id": "$edit-event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        live_task = asyncio.create_task(
            access.append_live_event(
                "!test:localhost",
                edit_event,
                event_info=EventInfo.from_event(edit_event.source),
            ),
        )
        await asyncio.wait_for(resolve_started.wait(), timeout=1.0)

        read_task = asyncio.create_task(access.get_thread_history("!test:localhost", "$other-thread:localhost"))
        await asyncio.wait_for(read_finished.wait(), timeout=1.0)
        await asyncio.wait_for(asyncio.shield(read_task), timeout=0.1)

        allow_resolve.set()
        await live_task
        await _wait_for_room_cache_idle(coordinator)

    @pytest.mark.asyncio
    async def test_live_append_during_snapshotless_refill_converges_without_second_scan(
        self,
        tmp_path: Path,
    ) -> None:
        """A refill owns the thread lane so a racing live append extends its installed snapshot."""
        room_id = "!test:localhost"
        thread_id = "$thread:localhost"
        event_cache = SqliteEventCache(tmp_path / "event_cache.db")
        await event_cache.initialize()
        coordinator = _runtime_write_coordinator()
        root_event = _text_event(
            event_id=thread_id,
            body="Root",
            sender="@user:localhost",
            server_timestamp=1000,
            room_id=room_id,
        )
        live_event = _text_event(
            event_id="$live:localhost",
            body="Live",
            sender="@agent:localhost",
            server_timestamp=2000,
            room_id=room_id,
            thread_id=thread_id,
        )
        # The homeserver already has both, which is exactly why the delta need not be retained.
        client = _relations_client(
            root_event=root_event,
            thread_events=[live_event],
            next_batch="s_initial",
        )
        room_messages_response = client.room_messages.return_value
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()

        async def blocking_room_messages(*_args: object, **_kwargs: object) -> nio.RoomMessagesResponse:
            fetch_started.set()
            await release_fetch.wait()
            return room_messages_response

        client.room_messages = AsyncMock(side_effect=blocking_room_messages)
        different_thread_update_started = asyncio.Event()

        async def different_thread_update() -> None:
            different_thread_update_started.set()

        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=client,
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        read_task = asyncio.create_task(access.get_dispatch_thread_history(room_id, thread_id))
        mutation_task: asyncio.Task[object] | None = None

        try:
            await asyncio.wait_for(fetch_started.wait(), timeout=1.0)
            access._write_cache_ops.queue_thread_cache_update(
                room_id,
                "$other-thread:localhost",
                different_thread_update,
                name="matrix_cache_test_different_thread_update",
            )
            await asyncio.wait_for(different_thread_update_started.wait(), timeout=1.0)
            mutation_task = access._write_cache_ops.queue_thread_cache_update(
                room_id,
                thread_id,
                lambda: event_cache.apply_thread_mutation_append(
                    room_id,
                    thread_id,
                    live_event.source,
                    append_failed_reason="live_append_failed",
                ),
                name="matrix_cache_test_live_append",
            )
            await asyncio.sleep(0)
            assert mutation_task.done() is False

            release_fetch.set()
            history = await asyncio.wait_for(read_task, timeout=1.0)
            mutation_outcome = await asyncio.wait_for(mutation_task, timeout=1.0)
            cached_history = await access.get_dispatch_thread_history(room_id, thread_id)
            cached_rows = await event_cache.get_thread_events(room_id, thread_id)
            gap_after_read = await event_cache.get_thread_cache_gap(room_id, thread_id)
        finally:
            release_fetch.set()
            await asyncio.gather(
                read_task,
                *(task for task in [mutation_task] if task is not None),
                return_exceptions=True,
            )
            await coordinator.close()
            await event_cache.close()

        expected_event_ids = [thread_id, "$live:localhost"]
        assert [message.event_id for message in history] == expected_event_ids
        assert mutation_outcome is ThreadAppendOutcome.APPENDED
        assert [message.event_id for message in cached_history] == expected_event_ids
        assert cached_history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        assert cached_rows is not None
        assert [event["event_id"] for event in cached_rows] == expected_event_ids
        assert matrix_cache.thread_cache_rejection_reason(gap_after_read) is None
        assert client.room_messages.await_count == 1

    @pytest.mark.asyncio
    async def test_live_redaction_resolution_does_not_block_same_room_read(self) -> None:
        """A same-room read must not wait on live redaction resolution before the queued cache write starts."""
        coordinator = _runtime_write_coordinator()
        event_cache = _runtime_event_cache()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=_make_client_mock(),
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        resolve_started = asyncio.Event()
        allow_resolve = asyncio.Event()
        read_finished = asyncio.Event()

        async def slow_resolve(*_args: object, **_kwargs: object) -> MutationThreadImpact:
            resolve_started.set()
            await allow_resolve.wait()
            return MutationThreadImpact.room_level()

        async def quick_history(
            _room_id: str,
            _thread_id: str,
            **_kwargs: object,
        ) -> ThreadHistoryResult:
            read_finished.set()
            return thread_history_result(
                [_message(event_id="$other-thread:localhost", body="Root")],
                is_full_history=True,
            )

        access._live._resolver.resolve_redaction_thread_impact = AsyncMock(side_effect=slow_resolve)
        access._reads.fetch_thread_history_from_client = AsyncMock(side_effect=quick_history)
        event_cache.redact_event = AsyncMock(return_value=True)

        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$room-message:localhost"
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$room-message:localhost",
            "room_id": "!test:localhost",
            "sender": "@user:localhost",
            "type": "m.room.redaction",
        }

        live_task = asyncio.create_task(access.apply_redaction("!test:localhost", redaction_event))
        await asyncio.wait_for(resolve_started.wait(), timeout=1.0)

        read_task = asyncio.create_task(access.get_thread_history("!test:localhost", "$other-thread:localhost"))
        await asyncio.wait_for(read_finished.wait(), timeout=1.0)
        await asyncio.wait_for(asyncio.shield(read_task), timeout=0.1)

        allow_resolve.set()
        await live_task
        await _wait_for_room_cache_idle(coordinator)

        event_cache.redact_event.assert_awaited_once_with("!test:localhost", "$room-message:localhost")

    @pytest.mark.asyncio
    async def test_live_room_level_redaction_waits_for_same_room_write_barrier(self) -> None:
        """Live room-level redactions should still run under the room write barrier."""
        coordinator = _runtime_write_coordinator()
        event_cache = _runtime_event_cache()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=_make_client_mock(),
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        prior_write_started = asyncio.Event()
        allow_prior_write_finish = asyncio.Event()

        async def slow_prior_room_update() -> None:
            prior_write_started.set()
            await allow_prior_write_finish.wait()

        access._live._resolver.resolve_redaction_thread_impact = AsyncMock(
            return_value=MutationThreadImpact.room_level(),
        )
        event_cache.redact_event = AsyncMock(return_value=True)

        coordinator.queue_room_update(
            "!test:localhost",
            slow_prior_room_update,
            name="matrix_cache_prior_update",
            coordination_scope=event_cache.principal_id,
        )
        await asyncio.wait_for(prior_write_started.wait(), timeout=1.0)

        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$room-message:localhost"
        redaction_event.source = {
            "content": {},
            "event_id": "$redaction:localhost",
            "origin_server_ts": 1234567891,
            "redacts": "$room-message:localhost",
            "room_id": "!test:localhost",
            "sender": "@user:localhost",
            "type": "m.room.redaction",
        }

        live_task = asyncio.create_task(access.apply_redaction("!test:localhost", redaction_event))
        await asyncio.sleep(0)
        event_cache.redact_event.assert_not_awaited()

        allow_prior_write_finish.set()
        await live_task
        await _wait_for_room_cache_idle(coordinator)

        event_cache.redact_event.assert_awaited_once_with("!test:localhost", "$room-message:localhost")

    @pytest.mark.asyncio
    async def test_live_threaded_redaction_bypasses_sibling_thread_barrier(self) -> None:
        """Live threaded redactions should start without waiting for sibling-thread writes."""
        room_id = "!test:localhost"
        thread_a_id = "$thread-a:localhost"
        thread_b_id = "$thread-b:localhost"
        coordinator = _runtime_write_coordinator()
        event_cache = _runtime_event_cache()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=_make_client_mock(),
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        sibling_update_started = asyncio.Event()
        redaction_started = asyncio.Event()
        redaction_started_at: float | None = None
        sibling_hold_released_at: float | None = None

        async def blocking_sibling_thread_update() -> None:
            nonlocal sibling_hold_released_at
            sibling_update_started.set()
            await asyncio.sleep(0.2)
            sibling_hold_released_at = time.perf_counter()

        async def redact_event(room_id_arg: str, redacted_event_id: str) -> bool:
            nonlocal redaction_started_at
            assert room_id_arg == room_id
            assert redacted_event_id == "$thread-message:localhost"
            redaction_started_at = time.perf_counter()
            redaction_started.set()
            return True

        access._live._resolver.resolve_redaction_thread_impact = AsyncMock(
            return_value=MutationThreadImpact.threaded(thread_a_id),
        )
        event_cache.redact_event = AsyncMock(side_effect=redact_event)
        sibling_task = coordinator.queue_thread_update(
            room_id,
            thread_b_id,
            blocking_sibling_thread_update,
            name="matrix_cache_blocking_sibling_thread_update",
            coordination_scope=event_cache.principal_id,
        )
        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$thread-message:localhost"

        try:
            await asyncio.wait_for(sibling_update_started.wait(), timeout=1.0)

            live_task = asyncio.create_task(access.apply_redaction(room_id, redaction_event))
            await asyncio.wait_for(redaction_started.wait(), timeout=0.1)
            await asyncio.wait_for(live_task, timeout=0.1)

            assert sibling_task.done() is False
            assert redaction_started_at is not None
            assert sibling_hold_released_at is None

            await asyncio.wait_for(sibling_task, timeout=1.0)

            assert sibling_hold_released_at is not None
            assert redaction_started_at < sibling_hold_released_at
            event_cache.redact_event.assert_awaited_once_with(room_id, "$thread-message:localhost")
            event_cache.mark_thread_gap.assert_awaited_once_with(
                room_id,
                thread_a_id,
                reason="live_redaction",
            )
        finally:
            await asyncio.wait_for(
                asyncio.gather(sibling_task, return_exceptions=True),
                timeout=1.0,
            )
            await _wait_for_room_cache_idle(coordinator)

    @pytest.mark.asyncio
    async def test_live_threaded_redaction_waits_for_same_thread_predecessor(self) -> None:
        """Live threaded redactions must stay behind earlier same-thread writes."""
        room_id = "!test:localhost"
        thread_a_id = "$thread-a:localhost"
        coordinator = _runtime_write_coordinator()
        event_cache = _runtime_event_cache()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=_make_client_mock(),
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        predecessor_started = asyncio.Event()
        release_predecessor = asyncio.Event()
        redaction_started = asyncio.Event()
        live_task: asyncio.Task[None] | None = None

        async def blocking_same_thread_update() -> None:
            predecessor_started.set()
            await release_predecessor.wait()

        async def redact_event(_room_id: str, _redacted_event_id: str) -> bool:
            redaction_started.set()
            return True

        access._live._resolver.resolve_redaction_thread_impact = AsyncMock(
            return_value=MutationThreadImpact.threaded(thread_a_id),
        )
        event_cache.redact_event = AsyncMock(side_effect=redact_event)
        predecessor_task = coordinator.queue_thread_update(
            room_id,
            thread_a_id,
            blocking_same_thread_update,
            name="matrix_cache_blocking_same_thread_update",
            coordination_scope=event_cache.principal_id,
        )
        redaction_event = MagicMock(spec=nio.RedactionEvent)
        redaction_event.event_id = "$redaction:localhost"
        redaction_event.redacts = "$thread-message:localhost"

        try:
            await asyncio.wait_for(predecessor_started.wait(), timeout=1.0)

            live_task = asyncio.create_task(access.apply_redaction(room_id, redaction_event))

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(redaction_started.wait(), timeout=0.1)
            assert live_task.done() is False

            release_predecessor.set()
            await asyncio.wait_for(redaction_started.wait(), timeout=1.0)
            await asyncio.wait_for(live_task, timeout=1.0)

            event_cache.redact_event.assert_awaited_once_with(room_id, "$thread-message:localhost")
            event_cache.mark_thread_gap.assert_awaited_once_with(
                room_id,
                thread_a_id,
                reason="live_redaction",
            )
        finally:
            release_predecessor.set()
            await asyncio.wait_for(
                asyncio.gather(
                    predecessor_task,
                    *(task for task in [live_task] if task is not None),
                    return_exceptions=True,
                ),
                timeout=1.0,
            )
            await _wait_for_room_cache_idle(coordinator)

    # UNKNOWN-impact live mutation optimization is deferred to ISSUE-189.

    @pytest.mark.asyncio
    @pytest.mark.parametrize("timing_enabled_for_test", [False, True], ids=["timing_disabled", "timing_enabled"])
    async def test_live_threaded_event_uses_per_thread_barrier_with_and_without_timing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        timing_enabled_for_test: bool,
    ) -> None:
        """Live threaded appends should bypass sibling-thread barriers in both timing modes."""
        if timing_enabled_for_test:
            monkeypatch.setenv("MINDROOM_TIMING", "1")
        else:
            monkeypatch.delenv("MINDROOM_TIMING", raising=False)

        room_id = "!test:localhost"
        thread_a_id = "$thread-a:localhost"
        thread_b_id = "$thread-b:localhost"
        coordinator = _runtime_write_coordinator()
        event_cache = _runtime_event_cache()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        sibling_update_started = asyncio.Event()
        release_sibling_update = asyncio.Event()
        append_started = asyncio.Event()
        append_task: asyncio.Task[None] | None = None

        async def blocking_sibling_thread_update() -> None:
            sibling_update_started.set()
            await release_sibling_update.wait()

        async def apply_thread_mutation_append(
            marked_room_id: str,
            marked_thread_id: str,
            _event_source: dict[str, object],
            *,
            append_failed_reason: str,
        ) -> ThreadAppendOutcome:
            assert marked_room_id == room_id
            assert marked_thread_id == thread_a_id
            assert append_failed_reason == "live_append_failed"
            append_started.set()
            return ThreadAppendOutcome.APPENDED

        access._live._resolver.resolve_thread_impact_for_mutation = AsyncMock(
            return_value=MutationThreadImpact.threaded(thread_a_id),
        )
        event_cache.apply_thread_mutation_append = AsyncMock(side_effect=apply_thread_mutation_append)
        sibling_task = coordinator.queue_thread_update(
            room_id,
            thread_b_id,
            blocking_sibling_thread_update,
            name="matrix_cache_blocking_other_thread_update",
            coordination_scope=event_cache.principal_id,
        )
        try:
            await asyncio.wait_for(sibling_update_started.wait(), timeout=1.0)

            event = _text_event(
                event_id="$reply:localhost",
                body="hello",
                sender="@user:localhost",
                server_timestamp=1234,
                room_id=room_id,
                thread_id=thread_a_id,
            )
            append_task = asyncio.create_task(
                access.append_live_event(
                    room_id,
                    event,
                    event_info=EventInfo.from_event(event.source),
                ),
            )

            await asyncio.wait_for(append_started.wait(), timeout=1.0)
            await asyncio.wait_for(append_task, timeout=1.0)

            assert release_sibling_update.is_set() is False
            assert sibling_task.done() is False
        finally:
            release_sibling_update.set()
            pending_tasks = [sibling_task]
            if append_task is not None:
                pending_tasks.append(append_task)
            await asyncio.wait_for(
                asyncio.gather(
                    *pending_tasks,
                    return_exceptions=True,
                ),
                timeout=1.0,
            )
            await coordinator.close()

        event_cache.apply_thread_mutation_append.assert_awaited_once_with(
            room_id,
            thread_a_id,
            event.source,
            append_failed_reason="live_append_failed",
        )
        event_cache.mark_thread_gap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_edit_marks_cached_thread_stale_and_next_read_refetches(
        self,
        tmp_path: Path,
    ) -> None:
        """A synced thread edit should force the next read to refetch from Matrix, even after a restart."""
        event_cache = SqliteEventCache(tmp_path / "event_cache.db")
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
                "!test:localhost": MagicMock(timeline=MagicMock(events=[reply_edit], limited=False)),
            }
            access.cache_sync_timeline(sync_response)
            await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)
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
    async def test_guarded_thread_replace_skips_stale_prewarm_write_after_newer_live_update(
        self,
        tmp_path: Path,
    ) -> None:
        """A guarded prewarm write must not overwrite a newer thread snapshot written after the fetch began."""
        event_cache = SqliteEventCache(tmp_path / "event_cache.db")
        await event_cache.initialize()
        room_id = "!test:localhost"
        thread_id = "$thread_root:localhost"
        old_root_event = _text_event(
            event_id=thread_id,
            body="Old root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        old_reply_event = _text_event(
            event_id="$reply_old:localhost",
            body="Old reply",
            sender="@agent:localhost",
            server_timestamp=2000,
            thread_id=thread_id,
        )
        new_root_event = _text_event(
            event_id=thread_id,
            body="New root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        new_reply_event = _text_event(
            event_id="$reply_new:localhost",
            body="New reply",
            sender="@agent:localhost",
            server_timestamp=3000,
            thread_id=thread_id,
        )

        try:
            prewarm_fetch_started_at = time.time()
            await _replace_thread(
                event_cache,
                room_id,
                thread_id,
                [new_root_event.source, new_reply_event.source],
                fetch_started_at=prewarm_fetch_started_at + 1,
            )

            replaced = await event_cache.replace_thread(
                room_id,
                thread_id,
                [old_root_event.source, old_reply_event.source],
                expected_membership_epoch=await event_cache.room_membership_epoch(room_id),
                fetch_started_at=prewarm_fetch_started_at,
            )
            cached_history = await event_cache.get_thread_events(room_id, thread_id)
        finally:
            await event_cache.close()

        # The older prewarm reports success -- a strictly fresher snapshot is installed -- but its
        # events must not replace the newer ones.
        assert replaced
        assert cached_history is not None
        assert [event["event_id"] for event in cached_history] == [thread_id, "$reply_new:localhost"]

    @pytest.mark.asyncio
    async def test_startup_prewarm_does_not_starve_live_dispatch(self, bot: AgentBot) -> None:
        """A blocked bulk scan should not occupy the live write coordinator."""
        support = await _bind_owned_runtime_support(bot)
        room_id = "!test:localhost"
        thread_id = "$thread_root:localhost"
        root_event = _text_event(
            event_id=thread_id,
            body="Root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        reply_event = _text_event(
            event_id="$reply:localhost",
            body="Reply",
            sender="@agent:localhost",
            server_timestamp=2000,
            thread_id=thread_id,
        )
        prewarm_started = asyncio.Event()
        release_prewarm = asyncio.Event()
        live_scan_started = asyncio.Event()
        room_scan_count = 0

        async def room_messages(*_args: object, **_kwargs: object) -> nio.RoomMessagesResponse:
            nonlocal room_scan_count
            room_scan_count += 1
            if room_scan_count == 1:
                prewarm_started.set()
                await release_prewarm.wait()
            else:
                live_scan_started.set()
            return nio.RoomMessagesResponse(
                room_id=room_id,
                chunk=[reply_event, root_event],
                start="",
                end=None,
            )

        prewarm_task: asyncio.Task[object] | None = None
        try:
            bot.client.room_messages = AsyncMock(side_effect=room_messages)
            prewarm_task = asyncio.create_task(
                bot._conversation_cache._bulk_refresh_startup_threads(room_id, [thread_id]),
            )
            await asyncio.wait_for(prewarm_started.wait(), timeout=1.0)
            dispatch_task = asyncio.create_task(
                bot._conversation_cache.get_dispatch_thread_history(room_id, thread_id),
            )
            await asyncio.wait_for(live_scan_started.wait(), timeout=1.0)
            history = await asyncio.wait_for(dispatch_task, timeout=1.0)
            assert prewarm_task.done() is False
            release_prewarm.set()
            await prewarm_task
        finally:
            release_prewarm.set()
            if prewarm_task is not None:
                await asyncio.gather(prewarm_task, return_exceptions=True)
            await _close_bound_runtime_support(bot, support)

        assert [message.event_id for message in history] == [thread_id, "$reply:localhost"]
        assert room_scan_count == 2

    @pytest.mark.asyncio
    async def test_prewarm_result_remains_reusable_after_restart(
        self,
        bot: AgentBot,
    ) -> None:
        """A prewarm fetch should stay usable unless an explicit stale marker exists."""
        support = await _bind_owned_runtime_support(bot)
        room_id = "!test:localhost"
        thread_id = "$thread_root:localhost"
        old_root_event = _text_event(
            event_id=thread_id,
            body="Old root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        old_reply_event = _text_event(
            event_id="$reply_old:localhost",
            body="Old reply",
            sender="@agent:localhost",
            server_timestamp=2000,
            thread_id=thread_id,
        )
        fresh_root_event = _text_event(
            event_id=thread_id,
            body="Fresh root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        fresh_reply_event = _text_event(
            event_id="$reply_fresh:localhost",
            body="Fresh reply",
            sender="@agent:localhost",
            server_timestamp=3000,
            thread_id=thread_id,
        )
        prewarm_fetch_started = asyncio.Event()
        allow_prewarm_fetch_finish = asyncio.Event()
        room_scan_count = 0

        async def room_messages(*_args: object, **_kwargs: object) -> nio.RoomMessagesResponse:
            nonlocal room_scan_count
            room_scan_count += 1
            if room_scan_count == 1:
                prewarm_fetch_started.set()
                await allow_prewarm_fetch_finish.wait()
                return nio.RoomMessagesResponse(
                    room_id=room_id,
                    chunk=[old_reply_event, old_root_event],
                    start="",
                    end=None,
                )
            return nio.RoomMessagesResponse(
                room_id=room_id,
                chunk=[fresh_reply_event, fresh_root_event],
                start="",
                end=None,
            )

        try:
            bot.client.room_messages = AsyncMock(side_effect=room_messages)
            prewarm_task = asyncio.create_task(
                bot._conversation_cache._bulk_refresh_startup_threads(
                    room_id,
                    [thread_id],
                ),
            )
            await asyncio.wait_for(prewarm_fetch_started.wait(), timeout=1.0)

            allow_prewarm_fetch_finish.set()
            prewarm_stats = await asyncio.wait_for(prewarm_task, timeout=1.0)

            history = await bot._conversation_cache.get_dispatch_thread_history(room_id, thread_id)
        finally:
            allow_prewarm_fetch_finish.set()
            await _close_bound_runtime_support(bot, support)

        assert prewarm_stats.usable_threads == 1
        assert [message.body for message in history] == ["Old root", "Old reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        assert THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC not in history.diagnostics
        bot.client.room_messages.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_refill_result_remains_reusable_after_restart(
        self,
        bot: AgentBot,
    ) -> None:
        """A normal dispatch refill should stay usable unless an explicit stale marker exists."""
        support = await _bind_owned_runtime_support(bot)
        room_id = "!test:localhost"
        thread_id = "$thread_root:localhost"
        old_root_event = _text_event(
            event_id=thread_id,
            body="Old root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        old_reply_event = _text_event(
            event_id="$reply_old:localhost",
            body="Old reply",
            sender="@agent:localhost",
            server_timestamp=2000,
            thread_id=thread_id,
        )
        fresh_root_event = _text_event(
            event_id=thread_id,
            body="Fresh root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        fresh_reply_event = _text_event(
            event_id="$reply_fresh:localhost",
            body="Fresh reply",
            sender="@agent:localhost",
            server_timestamp=3000,
            thread_id=thread_id,
        )
        dispatch_fetch_started = asyncio.Event()
        allow_dispatch_fetch_finish = asyncio.Event()
        room_scan_count = 0

        async def room_messages(*_args: object, **_kwargs: object) -> nio.RoomMessagesResponse:
            nonlocal room_scan_count
            room_scan_count += 1
            if room_scan_count == 1:
                dispatch_fetch_started.set()
                await allow_dispatch_fetch_finish.wait()
                return nio.RoomMessagesResponse(
                    room_id=room_id,
                    chunk=[old_reply_event, old_root_event],
                    start="",
                    end=None,
                )
            return nio.RoomMessagesResponse(
                room_id=room_id,
                chunk=[fresh_reply_event, fresh_root_event],
                start="",
                end=None,
            )

        try:
            bot.client.room_messages = AsyncMock(side_effect=room_messages)
            dispatch_task = asyncio.create_task(
                bot._conversation_cache.get_dispatch_thread_history(room_id, thread_id),
            )
            await asyncio.wait_for(dispatch_fetch_started.wait(), timeout=1.0)

            allow_dispatch_fetch_finish.set()
            dispatch_history = await asyncio.wait_for(dispatch_task, timeout=1.0)

            history = await bot._conversation_cache.get_dispatch_thread_history(room_id, thread_id)
        finally:
            allow_dispatch_fetch_finish.set()
            await _close_bound_runtime_support(bot, support)

        assert [message.body for message in dispatch_history] == ["Old root", "Old reply"]
        assert [message.body for message in history] == ["Old root", "Old reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        assert THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC not in history.diagnostics
        bot.client.room_messages.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_guarded_refill_commit_remains_reusable_after_restart(
        self,
        bot: AgentBot,
    ) -> None:
        """A guarded replacement should stay usable unless an explicit stale marker exists."""
        support = await _bind_owned_runtime_support(bot)
        room_id = "!test:localhost"
        thread_id = "$thread_root:localhost"
        old_root_event = _text_event(
            event_id=thread_id,
            body="Old root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        old_reply_event = _text_event(
            event_id="$reply_old:localhost",
            body="Old reply",
            sender="@agent:localhost",
            server_timestamp=2000,
            thread_id=thread_id,
        )
        fresh_root_event = _text_event(
            event_id=thread_id,
            body="Fresh root",
            sender="@user:localhost",
            server_timestamp=1000,
        )
        fresh_reply_event = _text_event(
            event_id="$reply_fresh:localhost",
            body="Fresh reply",
            sender="@agent:localhost",
            server_timestamp=3000,
            thread_id=thread_id,
        )
        write_entered = asyncio.Event()
        allow_write_commit = asyncio.Event()
        original_replace = sqlite_event_cache_threads_module.replace_thread_locked

        async def blocked_replace(*args: object, **kwargs: object) -> None:
            write_entered.set()
            await allow_write_commit.wait()
            return await original_replace(*args, **kwargs)

        try:
            fetch_started_at = time.time()
            with patch(
                "mindroom.matrix.cache.sqlite_event_cache_threads.replace_thread_locked",
                new=blocked_replace,
            ):
                write_task = asyncio.create_task(
                    bot.event_cache.replace_thread(
                        room_id,
                        thread_id,
                        [old_root_event.source, old_reply_event.source],
                        expected_membership_epoch=await bot.event_cache.room_membership_epoch(room_id),
                        fetch_started_at=fetch_started_at,
                    ),
                )
                await asyncio.wait_for(write_entered.wait(), timeout=1.0)

                allow_write_commit.set()
                replaced = await asyncio.wait_for(write_task, timeout=1.0)

            page = MagicMock(spec=nio.RoomMessagesResponse)
            page.chunk = [fresh_reply_event, fresh_root_event]
            page.end = None
            bot.client.room_messages = AsyncMock(return_value=page)

            history = await bot._conversation_cache.get_dispatch_thread_history(room_id, thread_id)
        finally:
            allow_write_commit.set()
            await _close_bound_runtime_support(bot, support)

        assert replaced
        assert [message.body for message in history] == ["Old root", "Old reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        assert THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC not in history.diagnostics
        bot.client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lookup_miss_sync_plain_edit_invalidates_room_cache_state(
        self,
        tmp_path: Path,
    ) -> None:
        """Plain sync edits with missing originals should invalidate cached room thread state."""
        event_cache = SqliteEventCache(tmp_path / "event_cache.db")
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
                "!test:localhost": MagicMock(timeline=MagicMock(events=[ambiguous_edit], limited=False)),
            }
            access.cache_sync_timeline(sync_response)
            await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)
            cache_state = await event_cache.get_thread_cache_gap("!test:localhost", "$thread_root:localhost")
        finally:
            await event_cache.close()

        assert cache_state is not None
        assert cache_state.gap_reason == "sync_thread_lookup_unavailable"
        assert cache_state.gap_marked_at is not None

    @pytest.mark.asyncio
    async def test_get_thread_history_raises_when_refresh_fails(self) -> None:
        """Thread-history reads should fail closed instead of silently returning an empty thread."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=_make_client_mock()),
        )
        access._reads.fetch_thread_history_from_client = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await access.get_thread_history("!test:localhost", "$thread:localhost")

    @pytest.mark.asyncio
    async def test_thread_read_refetches_once_mutation_starts_after_room_barrier(self) -> None:
        """A read already past the room barrier must still refetch once a mutation starts."""
        event_cache = _runtime_event_cache()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
        )
        # No gap marker: the cached snapshot reads as usable.
        thread_state: dict[str, ThreadCacheGap | None] = {"value": None}
        raw_events: list[dict[str, object]] = [
            {"event_id": "$thread:localhost"},
            {"event_id": "$reply-old:localhost"},
        ]
        reader_ready = asyncio.Event()
        allow_reader_continue = asyncio.Event()
        raw_append_committed = asyncio.Event()

        async def pause_reader(_room_id: str, _thread_id: str) -> None:
            reader_ready.set()
            await allow_reader_continue.wait()

        async def mark_thread_gap(_room_id: str, _thread_id: str, *, reason: str) -> None:
            thread_state["value"] = ThreadCacheGap(gap_marked_at=time.time(), gap_reason=reason)

        async def apply_thread_mutation_append(
            _room_id: str,
            _thread_id: str,
            event: dict[str, object],
            *,
            append_failed_reason: str,  # noqa: ARG001  # keyword must match the runtime call
        ) -> ThreadAppendOutcome:
            raw_events.append(event)
            raw_append_committed.set()
            return ThreadAppendOutcome.APPENDED

        async def fetch_fresh_history(
            _room_id: str,
            _thread_id: str,
            **_kwargs: object,
        ) -> ThreadHistoryResult:
            thread_state["value"] = None
            return thread_history_result(
                [
                    _message(event_id="$thread:localhost", body="Root"),
                    _message(event_id="$reply-old:localhost", body="Old reply"),
                    _message(event_id="$reply-new:localhost", body="New reply"),
                ],
                is_full_history=True,
                diagnostics={THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_HOMESERVER},
            )

        event_cache.get_thread_cache_gap = AsyncMock(side_effect=lambda *_args, **_kwargs: thread_state["value"])
        event_cache.get_thread_events = AsyncMock(side_effect=lambda *_args, **_kwargs: list(raw_events))
        event_cache.get_thread_id_for_event = AsyncMock(return_value="$thread:localhost")
        event_cache.mark_thread_gap = AsyncMock(side_effect=mark_thread_gap)
        event_cache.apply_thread_mutation_append = AsyncMock(side_effect=apply_thread_mutation_append)
        access._reads._wait_for_pending_thread_cache_updates = AsyncMock(side_effect=pause_reader)
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
            access._outbound._apply_outbound_event_notification(
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
        access._reads.fetch_thread_history_from_client.assert_awaited_once()
        assert access._reads.fetch_thread_history_from_client.await_args.args == (
            "!room:localhost",
            "$thread:localhost",
        )
        assert access._reads.fetch_thread_history_from_client.await_args.kwargs["caller_label"] == "unknown"
        assert access._reads.fetch_thread_history_from_client.await_args.kwargs["coordinator_queue_wait_ms"] > 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("timing_enabled_for_test", [False, True], ids=["timing_disabled", "timing_enabled"])
    async def test_live_unknown_mutation_does_not_let_blocked_read_return_stale_cache(  # noqa: PLR0915
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        timing_enabled_for_test: bool,
    ) -> None:
        """A blocked read must refetch instead of serving stale cache after an unknown live mutation."""
        if timing_enabled_for_test:
            monkeypatch.setenv("MINDROOM_TIMING", "1")
        else:
            monkeypatch.delenv("MINDROOM_TIMING", raising=False)

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
        new_reply = _text_event(
            event_id="$reply-new:localhost",
            body="New reply",
            sender="@agent:localhost",
            server_timestamp=3000,
            room_id=room_id,
            thread_id=thread_id,
        )
        coordinator = _runtime_write_coordinator()
        client = _relations_client(
            root_event=root_event,
            thread_events=[old_reply, new_reply],
            next_batch="s_initial",
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=client,
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        await _replace_thread(
            event_cache,
            room_id,
            thread_id,
            [root_event.source, old_reply.source],
        )

        real_get_thread_cache_gap = event_cache.get_thread_cache_gap
        real_mark_room_threads_gap = event_cache.mark_room_threads_gap
        reader_ready = asyncio.Event()
        release_reader = asyncio.Event()
        room_invalidation_finished = asyncio.Event()
        live_task: asyncio.Task[None] | None = None
        history: ThreadHistoryResult | None = None

        async def blocking_get_thread_cache_gap(room_id_arg: str, thread_id_arg: str) -> ThreadCacheGap | None:
            assert room_id_arg == room_id
            assert thread_id_arg == thread_id
            reader_ready.set()
            await release_reader.wait()
            return await real_get_thread_cache_gap(room_id_arg, thread_id_arg)

        async def mark_room_threads_gap(room_id_arg: str, *, reason: str) -> None:
            assert room_id_arg == room_id
            assert reason == "live_thread_lookup_unavailable"
            await real_mark_room_threads_gap(room_id_arg, reason=reason)
            room_invalidation_finished.set()

        async def resolve_unknown_impact(*_args: object, **_kwargs: object) -> MutationThreadImpact:
            return MutationThreadImpact.unknown()

        event_cache.get_thread_cache_gap = AsyncMock(side_effect=blocking_get_thread_cache_gap)
        event_cache.mark_room_threads_gap = AsyncMock(side_effect=mark_room_threads_gap)
        access._live._resolver.resolve_thread_impact_for_mutation = AsyncMock(side_effect=resolve_unknown_impact)
        unknown_event = _text_event(
            event_id="$unknown-edit:localhost",
            body="* Updated",
            sender="@agent:localhost",
            server_timestamp=4000,
            room_id=room_id,
            replacement_of="$missing:localhost",
            new_body="Updated",
        )
        read_task = asyncio.create_task(access.get_thread_history(room_id, thread_id))

        try:
            await asyncio.wait_for(reader_ready.wait(), timeout=1.0)

            live_task = asyncio.create_task(
                access.append_live_event(
                    room_id,
                    unknown_event,
                    event_info=EventInfo.from_event(unknown_event.source),
                ),
            )
            await asyncio.wait_for(room_invalidation_finished.wait(), timeout=1.0)

            release_reader.set()
            history = await asyncio.wait_for(read_task, timeout=1.0)
            await asyncio.wait_for(live_task, timeout=1.0)
            await _wait_for_room_cache_idle(coordinator)
        finally:
            release_reader.set()
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

        assert history is not None
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_HOMESERVER
        assert [message.body for message in history] == ["Root", "Old reply", "New reply"]
        client.room_messages.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_thread_history_racing_unknown_live_mutation_leaves_gap_marked(
        self,
        tmp_path: Path,
    ) -> None:
        """A gap raised mid-fetch survives, so the next thread-history read refetches."""
        await _assert_racing_unknown_live_mutation_leaves_thread_gap_marked(
            tmp_path,
            read_thread=MatrixConversationCache.get_thread_history,
            force_refetch_reason="test_force_thread_history_refetch",
            expected_full_history=True,
        )

    @pytest.mark.asyncio
    async def test_dispatch_thread_history_racing_unknown_live_mutation_leaves_gap_marked(
        self,
        tmp_path: Path,
    ) -> None:
        """A gap raised mid-fetch survives, so the next dispatch-history read refetches."""
        await _assert_racing_unknown_live_mutation_leaves_thread_gap_marked(
            tmp_path,
            read_thread=MatrixConversationCache.get_dispatch_thread_history,
            force_refetch_reason="test_force_dispatch_history_refetch",
            expected_full_history=True,
        )

    @pytest.mark.asyncio
    async def test_dispatch_thread_snapshot_racing_unknown_live_mutation_leaves_gap_marked(
        self,
        tmp_path: Path,
    ) -> None:
        """A gap raised mid-fetch survives, so the next dispatch-snapshot read refetches."""
        await _assert_racing_unknown_live_mutation_leaves_thread_gap_marked(
            tmp_path,
            read_thread=MatrixConversationCache.get_dispatch_thread_snapshot,
            force_refetch_reason="test_force_dispatch_refetch",
            expected_full_history=False,
        )

    @pytest.mark.asyncio
    async def test_latest_thread_event_lookup_refetches_invalidated_thread_tail(
        self,
        tmp_path: Path,
    ) -> None:
        """MSC3440 fallback should use the refetched latest visible thread event, not a stale cached tail."""
        event_cache = SqliteEventCache(tmp_path / "event_cache.db")
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
            await event_cache.mark_thread_gap(
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
    async def test_dispatch_thread_history_does_not_fall_back_to_stale_cache(self) -> None:
        """Strict dispatch history reads must fail rather than returning stale durable history."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access._reads.fetch_dispatch_thread_history_from_client = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await access.get_dispatch_thread_history("!test:localhost", "$thread:localhost")

    @pytest.mark.asyncio
    async def test_dispatch_thread_snapshot_does_not_fall_back_to_stale_cache(self) -> None:
        """Strict dispatch snapshot reads must fail rather than returning stale durable history."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access._reads.fetch_dispatch_thread_snapshot_from_client = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await access.get_dispatch_thread_snapshot("!test:localhost", "$thread:localhost")
