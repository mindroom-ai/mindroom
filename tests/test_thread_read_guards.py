"""Thread read guards and stale-cache rejection: live mutation barriers, guarded refills, and refetch behavior."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_cache_state import ThreadAppendOutcome
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread
from tests.threading_helpers import (
    EmptyProjection,
    ThreadingBehaviorTestBase,
    _conversation_runtime,
    _make_client_mock,
    _runtime_event_cache,
    _runtime_write_coordinator,
    _text_event,
    _wait_for_room_cache_idle,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot


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
        event_cache.mark_room_threads_gap.assert_awaited_once_with(
            "!test:localhost",
            reason="live_thread_lookup_unavailable",
        )
        event_cache.apply_thread_mutation_append.assert_not_awaited()

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
            store=EmptyProjection(),
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
            store=EmptyProjection(),
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
            store=EmptyProjection(),
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
            store=EmptyProjection(),
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
