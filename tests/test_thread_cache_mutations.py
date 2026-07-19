"""Thread mutation cache policy and conversation-cache thread reads."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import nio
import pytest
from nio.api import RelationshipType

import mindroom.matrix.cache as matrix_cache
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.matrix import thread_bookkeeping
from mindroom.matrix.cache import thread_writes
from mindroom.matrix.cache.event_cache import EventCacheBackendUnavailableError
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_reads import ThreadReadMode
from mindroom.matrix.cache.thread_writes import (
    _apply_thread_message_mutation,
    _apply_thread_redaction_mutation,
    _collect_sync_timeline_cache_updates,
)
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.mxc_plaintext_cache import cache_mxc_plaintext, get_cached_mxc_plaintext
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_CACHE,
    THREAD_HISTORY_SOURCE_DEGRADED,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
)
from tests.conftest import (
    runtime_paths_for,
)
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread
from tests.threading_helpers import (
    _conversation_runtime,
    _conversation_runtime_config,
    _make_client_mock,
    _make_room_get_event_response,
    _message,
    _message_mutation_event_info,
    _reopen_event_cache,
    _runtime_event_cache,
    _runtime_write_coordinator,
    _thread_mutation_cache_ops,
    _wait_for_room_cache_idle,
    thread_history_result,
)

if TYPE_CHECKING:
    from pathlib import Path


def _thread_reply_lookup_response() -> nio.RoomGetEventResponse:
    """Return typed metadata for one cache-indexed threaded message."""
    return nio.RoomGetEventResponse.from_dict(
        {
            "content": {
                "body": "thread reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "event_id": "$thread-root:localhost",
                    "rel_type": "m.thread",
                },
            },
            "event_id": "$thread-reply:localhost",
            "sender": "@bridge:localhost",
            "origin_server_ts": 1000,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )


def test_matrix_cache_package_does_not_export_thread_policy_wrappers() -> None:
    """Thread policy wrappers should not remain on the public cache package surface."""
    assert "ThreadReadPolicy" not in matrix_cache.__all__
    assert "ThreadWritePolicy" not in matrix_cache.__all__
    assert not hasattr(matrix_cache, "ThreadReadPolicy")
    assert not hasattr(matrix_cache, "ThreadWritePolicy")
    assert not hasattr(matrix_cache, "_ThreadReadPolicy")
    assert not hasattr(matrix_cache, "_ThreadMutationCacheOps")
    assert not hasattr(matrix_cache, "_ThreadOutboundWritePolicy")
    assert not hasattr(matrix_cache, "_ThreadLiveWritePolicy")
    assert not hasattr(matrix_cache, "_ThreadSyncWritePolicy")


def test_thread_writes_uses_shared_mutation_write_context_alias() -> None:
    """Thread writes should reuse the shared mutation-write context alias."""
    assert thread_writes.MutationWriteContext is thread_bookkeeping.MutationWriteContext


def test_thread_writes_does_not_keep_message_impact_wrapper() -> None:
    """Message-impact resolution should call the resolver directly instead of wrapping it."""
    assert not hasattr(thread_writes, "_resolve_thread_message_mutation_impact")


class TestThreadMutationHelpers:
    """Direct mutation-helper coverage for outbound/live/sync message and redaction paths."""

    @pytest.mark.asyncio
    async def test_room_purge_evicts_process_plaintext_when_durable_backend_fails(self) -> None:
        """A leave must drop this principal's RAM plaintext even during a cache outage."""
        cache_ops, logger, event_cache = _thread_mutation_cache_ops()
        room_id = "!left:localhost"
        cache_key = (
            "@alice:localhost",
            room_id,
            "$event",
            "mxc://server/plaintext",
        )
        event_cache.principal_id = "@alice:localhost"
        event_cache.purge_room = AsyncMock(side_effect=EventCacheBackendUnavailableError("offline"))
        cache_mxc_plaintext(
            "mxc://server/plaintext",
            "secret",
            time.time(),
            cache_key=cache_key,
        )

        await cache_ops.purge_room(room_id)

        assert get_cached_mxc_plaintext(cache_key) is None
        logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_departure_fences_reads_before_ordered_purge_can_start(self, tmp_path: Path) -> None:
        """A queued predecessor must not extend plaintext visibility after a confirmed leave."""
        root = SqliteEventCache(tmp_path / "event_cache.db")
        await root.initialize()
        cache = root.for_principal("@alice:localhost")
        coordinator = _runtime_write_coordinator()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=cache, coordinator=coordinator),
        )
        room_id = "!left:localhost"
        event_id = "$event"
        mxc_url = "mxc://server/plaintext"
        event = {
            "event_id": event_id,
            "sender": "@alice:localhost",
            "origin_server_ts": 1,
            "type": "m.room.message",
            "content": {
                "body": "preview",
                "msgtype": "m.file",
                "url": mxc_url,
                "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
            },
        }
        cache_key = (cache.principal_id, room_id, event_id, mxc_url)
        predecessor_started = asyncio.Event()
        release_predecessor = asyncio.Event()

        async def block_predecessor() -> None:
            predecessor_started.set()
            await release_predecessor.wait()

        try:
            await cache.store_event(event_id, room_id, event)
            assert await cache.store_mxc_text(room_id, event_id, mxc_url, "durable plaintext")
            cache_mxc_plaintext(mxc_url, "process plaintext", time.time(), cache_key=cache_key)
            predecessor = coordinator.queue_room_update(
                room_id,
                block_predecessor,
                name="matrix_cache_test_departure_predecessor",
            )
            await predecessor_started.wait()

            purge = asyncio.create_task(access.purge_room(room_id))
            await asyncio.sleep(0)

            assert not purge.done()
            assert await cache.get_event(room_id, event_id) is None
            assert await cache.get_mxc_text(room_id, event_id, mxc_url) is None
            assert get_cached_mxc_plaintext(cache_key) is None

            release_predecessor.set()
            await predecessor
            await purge
        finally:
            release_predecessor.set()
            await root.close()

    @pytest.mark.asyncio
    async def test_departure_batch_fences_every_room_before_first_purge_waits(self, tmp_path: Path) -> None:
        """All confirmed leaves must become unreadable before any ordered cleanup wait."""
        root = SqliteEventCache(tmp_path / "event_cache.db")
        await root.initialize()
        cache = root.for_principal("@alice:localhost")
        coordinator = _runtime_write_coordinator()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=cache, coordinator=coordinator),
        )
        first_room_id = "!first-left:localhost"
        second_room_id = "!second-left:localhost"
        predecessor_started = asyncio.Event()
        release_predecessor = asyncio.Event()

        async def block_first_room() -> None:
            predecessor_started.set()
            await release_predecessor.wait()

        try:
            for index, room_id in enumerate((first_room_id, second_room_id), start=1):
                await cache.store_event(
                    f"$event-{index}",
                    room_id,
                    {
                        "event_id": f"$event-{index}",
                        "sender": "@alice:localhost",
                        "origin_server_ts": index,
                        "type": "m.room.message",
                        "content": {"body": "secret", "msgtype": "m.text"},
                    },
                )
            predecessor = coordinator.queue_room_update(
                first_room_id,
                block_first_room,
                name="matrix_cache_test_batch_departure_predecessor",
            )
            await predecessor_started.wait()

            purge = asyncio.create_task(access.purge_rooms((first_room_id, second_room_id)))
            await asyncio.sleep(0)

            assert not purge.done()
            assert await cache.get_event(first_room_id, "$event-1") is None
            assert await cache.get_event(second_room_id, "$event-2") is None

            release_predecessor.set()
            await predecessor
            await purge
        finally:
            release_predecessor.set()
            await root.close()

    @pytest.mark.asyncio
    async def test_queued_rejoin_cannot_clear_newer_departure_fence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale queued join must not reopen reads after a newer authoritative leave."""
        root = SqliteEventCache(tmp_path / "event_cache.db")
        await root.initialize()
        cache = root.for_principal("@alice:localhost")
        coordinator = _runtime_write_coordinator()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=cache, coordinator=coordinator),
        )
        room_id = "!membership-race:localhost"
        event_id = "$event"
        event = {
            "event_id": event_id,
            "sender": "@alice:localhost",
            "origin_server_ts": 1,
            "type": "m.room.message",
            "content": {"body": "secret", "msgtype": "m.text"},
        }
        predecessor_started = asyncio.Event()
        release_predecessor = asyncio.Event()
        purge_started = asyncio.Event()
        release_purge = asyncio.Event()
        original_purge = cache.purge_room

        async def block_predecessor() -> None:
            predecessor_started.set()
            await release_predecessor.wait()

        async def delay_newer_purge(delayed_room_id: str) -> None:
            purge_started.set()
            await release_purge.wait()
            await original_purge(delayed_room_id)

        try:
            await cache.store_event(event_id, room_id, event)
            cache.mark_room_departed(room_id)
            predecessor = coordinator.queue_room_update(
                room_id,
                block_predecessor,
                name="matrix_cache_test_rejoin_predecessor",
            )
            await predecessor_started.wait()
            stale_rejoin = asyncio.create_task(access.mark_room_joined(room_id))
            await asyncio.sleep(0)

            monkeypatch.setattr(cache, "purge_room", delay_newer_purge)
            newer_leave = asyncio.create_task(access.purge_room(room_id))
            await asyncio.sleep(0)
            release_predecessor.set()
            await predecessor
            await stale_rejoin
            await purge_started.wait()

            assert await cache.get_event(room_id, event_id) is None

            release_purge.set()
            await newer_leave
        finally:
            release_predecessor.set()
            release_purge.set()
            await root.close()

    @pytest.mark.asyncio
    async def test_unbound_cache_runtime_falls_back_without_attribute_errors(self) -> None:
        """Early cache-only operations must remain harmless before runtime support is bound."""
        cache_ops, _logger, _event_cache = _thread_mutation_cache_ops()
        cache_ops.runtime.event_cache = None
        cache_ops.runtime.event_cache_write_coordinator = None
        completed: list[str] = []

        async def record(name: str) -> None:
            completed.append(name)

        await cache_ops.queue_room_cache_update(
            "!room:localhost",
            lambda: record("room"),
            name="unbound_room_cache_update",
        )
        await cache_ops.queue_thread_cache_update(
            "!room:localhost",
            "$thread:localhost",
            lambda: record("thread"),
            name="unbound_thread_cache_update",
        )
        await cache_ops.purge_room("!room:localhost")
        cache_ops.purge_process_plaintext("!room:localhost")

        assert completed == ["room", "thread"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("context", "invalidate_on_append_failure"),
        [
            ("outbound", False),
            ("live", True),
            ("sync", True),
        ],
    )
    async def test_thread_message_mutation_room_level_skips_invalidation(
        self,
        context: str,
        invalidate_on_append_failure: bool,
    ) -> None:
        """Room-level message mutations should only log and leave thread state untouched."""
        cache_ops, logger, event_cache = _thread_mutation_cache_ops()

        result = await _apply_thread_message_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            event_info=_message_mutation_event_info(),
            impact=MutationThreadImpact.room_level(),
            event_source=None,
            event_id="$event:localhost",
            context=context,
            room_level_skip_message=f"skip-{context}",
            invalidate_on_append_failure=invalidate_on_append_failure,
        )

        assert result is False
        logger.debug.assert_called_once_with(
            f"skip-{context}",
            room_id="!room:localhost",
            event_id="$event:localhost",
            original_event_id="$target:localhost",
        )
        event_cache.append_event.assert_not_awaited()
        event_cache.mark_room_threads_stale.assert_not_awaited()
        event_cache.mark_thread_stale.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("context", "invalidate_on_append_failure"),
        [
            ("outbound", False),
            ("live", True),
            ("sync", True),
        ],
    )
    async def test_thread_message_mutation_unknown_invalidates_room_once(
        self,
        context: str,
        invalidate_on_append_failure: bool,
    ) -> None:
        """Unknown message mutations should fail closed with one room-thread invalidation."""
        cache_ops, _logger, event_cache = _thread_mutation_cache_ops()

        result = await _apply_thread_message_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            event_info=_message_mutation_event_info(),
            impact=MutationThreadImpact.unknown(),
            event_source=None,
            event_id="$event:localhost",
            context=context,
            room_level_skip_message=f"skip-{context}",
            invalidate_on_append_failure=invalidate_on_append_failure,
        )

        assert result is True
        event_cache.append_event.assert_not_awaited()
        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            "!room:localhost",
            reason=f"{context}_thread_lookup_unavailable",
        )
        event_cache.mark_thread_stale.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("context", "invalidate_on_append_failure"),
        [
            ("outbound", False),
            ("live", True),
            ("sync", True),
        ],
    )
    async def test_thread_message_mutation_threaded_success_uses_context_reasons(
        self,
        context: str,
        invalidate_on_append_failure: bool,
    ) -> None:
        """Threaded message mutations should stale-mark once, append, and avoid room invalidation."""
        cache_ops, _logger, event_cache = _thread_mutation_cache_ops()
        event_source = {"event_id": "$event:localhost"}

        result = await _apply_thread_message_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            event_info=_message_mutation_event_info(),
            impact=MutationThreadImpact.threaded("$thread:localhost"),
            event_source=event_source,
            event_id="$event:localhost",
            context=context,
            room_level_skip_message=f"skip-{context}",
            invalidate_on_append_failure=invalidate_on_append_failure,
        )

        assert result is False
        event_cache.append_event.assert_awaited_once_with(
            "!room:localhost",
            "$thread:localhost",
            event_source,
        )
        event_cache.mark_room_threads_stale.assert_not_awaited()
        event_cache.mark_thread_stale.assert_awaited_once_with(
            "!room:localhost",
            "$thread:localhost",
            reason=f"{context}_thread_mutation",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("context", "invalidate_on_append_failure", "expected_reasons"),
        [
            ("outbound", False, ["outbound_thread_mutation"]),
            ("live", True, ["live_thread_mutation", "live_append_failed"]),
            ("sync", True, ["sync_thread_mutation", "sync_append_failed"]),
        ],
    )
    async def test_thread_message_mutation_threaded_append_failure_uses_path_policy(
        self,
        context: str,
        invalidate_on_append_failure: bool,
        expected_reasons: list[str],
    ) -> None:
        """Append failures should only add the extra stale mark on the live and sync paths."""
        cache_ops, _logger, event_cache = _thread_mutation_cache_ops()
        event_cache.append_event = AsyncMock(return_value=False)

        result = await _apply_thread_message_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            event_info=_message_mutation_event_info(),
            impact=MutationThreadImpact.threaded("$thread:localhost"),
            event_source={"event_id": "$event:localhost"},
            event_id="$event:localhost",
            context=context,
            room_level_skip_message=f"skip-{context}",
            invalidate_on_append_failure=invalidate_on_append_failure,
        )

        assert result is False
        assert event_cache.mark_thread_stale.await_args_list == [
            call("!room:localhost", "$thread:localhost", reason=reason) for reason in expected_reasons
        ]
        event_cache.mark_room_threads_stale.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("context", "redact_room_level_event"),
        [
            ("outbound", False),
            ("live", True),
            ("sync", True),
        ],
    )
    async def test_thread_redaction_mutation_room_level_skips_thread_invalidations(
        self,
        context: str,
        redact_room_level_event: bool,
    ) -> None:
        """Room-level redactions should never stale-mark thread state."""
        cache_ops, logger, event_cache = _thread_mutation_cache_ops()

        result = await _apply_thread_redaction_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            redacted_event_id="$target:localhost",
            impact=MutationThreadImpact.room_level(),
            context=context,
            redact_room_level_event=redact_room_level_event,
        )

        assert result is False
        event_cache.mark_room_threads_stale.assert_not_awaited()
        event_cache.mark_thread_stale.assert_not_awaited()
        if redact_room_level_event:
            event_cache.redact_event.assert_awaited_once_with("!room:localhost", "$target:localhost")
            logger.debug.assert_not_called()
        else:
            event_cache.redact_event.assert_not_awaited()
            logger.debug.assert_called_once_with(
                "Skipping outbound thread cache bookkeeping for non-threaded redaction",
                room_id="!room:localhost",
                redacted_event_id="$target:localhost",
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", ["outbound", "live", "sync"])
    async def test_thread_redaction_mutation_unknown_invalidates_room_once(self, context: str) -> None:
        """Unknown redactions should fail closed with one room-thread invalidation."""
        cache_ops, _logger, event_cache = _thread_mutation_cache_ops()

        result = await _apply_thread_redaction_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            redacted_event_id="$target:localhost",
            impact=MutationThreadImpact.unknown(),
            context=context,
        )

        assert result is True
        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            "!room:localhost",
            reason=f"{context}_redaction_lookup_unavailable",
        )
        event_cache.mark_thread_stale.assert_not_awaited()
        event_cache.redact_event.assert_awaited_once_with("!room:localhost", "$target:localhost")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", ["outbound", "live", "sync"])
    async def test_thread_redaction_mutation_threaded_success_uses_context_reason(self, context: str) -> None:
        """Threaded redactions should stale-mark the owning thread once on success."""
        cache_ops, _logger, event_cache = _thread_mutation_cache_ops()

        result = await _apply_thread_redaction_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            redacted_event_id="$target:localhost",
            impact=MutationThreadImpact.threaded("$thread:localhost"),
            context=context,
        )

        assert result is False
        event_cache.mark_room_threads_stale.assert_not_awaited()
        event_cache.mark_thread_stale.assert_awaited_once_with(
            "!room:localhost",
            "$thread:localhost",
            reason=f"{context}_redaction",
        )
        event_cache.redact_event.assert_awaited_once_with("!room:localhost", "$target:localhost")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", ["outbound", "live", "sync"])
    async def test_thread_redaction_mutation_threaded_failure_uses_failure_reason(self, context: str) -> None:
        """Threaded redaction failures should stale-mark once with the failure reason."""
        cache_ops, _logger, event_cache = _thread_mutation_cache_ops()
        event_cache.redact_event = AsyncMock(return_value=False)

        result = await _apply_thread_redaction_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            redacted_event_id="$target:localhost",
            impact=MutationThreadImpact.threaded("$thread:localhost"),
            context=context,
        )

        assert result is False
        event_cache.mark_room_threads_stale.assert_not_awaited()
        event_cache.mark_thread_stale.assert_awaited_once_with(
            "!room:localhost",
            "$thread:localhost",
            reason=f"{context}_redaction_failed",
        )
        event_cache.redact_event.assert_awaited_once_with("!room:localhost", "$target:localhost")


class TestMatrixConversationCacheThreadReads:
    """Targeted read-path tests for invalidate-and-refetch behavior."""

    def test_conversation_cache_does_not_keep_write_policy_wrapper(self) -> None:
        """Conversation cache should own write collaborators directly, not through a write-policy façade."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )

        assert not hasattr(access, "_writes")
        assert not hasattr(access, "_run_fail_open_outbound_write")

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("cache write failed"),
            asyncio.CancelledError(),
        ],
    )
    def test_notify_outbound_message_swallows_internal_write_failure(self, error: BaseException) -> None:
        """The public outbound bookkeeping boundary must fail open for ordinary failures and cancellation."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access._outbound._require_client = Mock(side_effect=error)

        access.notify_outbound_message(
            "!room:localhost",
            "$event:localhost",
            {"body": "hello", "msgtype": "m.text"},
        )

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("cache write failed"),
            asyncio.CancelledError(),
        ],
    )
    def test_notify_outbound_redaction_swallows_internal_write_failure(self, error: BaseException) -> None:
        """The public outbound redaction bookkeeping boundary must fail open too."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        access._outbound._schedule_fail_open_room_update = Mock(side_effect=error)

        access.notify_outbound_redaction(
            "!room:localhost",
            "$event:localhost",
        )

    @pytest.mark.asyncio
    async def test_notify_outbound_message_plain_edit_lookup_miss_invalidates_room_threads(self) -> None:
        """Plain room-mode edits should fail closed when mutation lookup cannot prove room-level state."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        client = _make_client_mock()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=client, event_cache=event_cache),
        )

        access.notify_outbound_message(
            "!room:localhost",
            "$edit:localhost",
            {
                "body": "* updated",
                "msgtype": "m.text",
                "m.new_content": {"body": "updated", "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$room-message:localhost"},
            },
        )
        await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)

        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            "!room:localhost",
            reason="outbound_thread_lookup_unavailable",
        )
        event_cache.mark_thread_stale.assert_not_awaited()
        event_cache.append_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notify_outbound_event_threaded_edit_uses_claimed_thread_barrier(self) -> None:
        """Outbound threaded edits should use the claimed thread barrier instead of the room barrier."""
        coordinator = _runtime_write_coordinator()
        event_cache = _runtime_event_cache()
        client = _make_client_mock(user_id="@agent:localhost")
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(
                client=client,
                event_cache=event_cache,
                coordinator=coordinator,
            ),
        )
        sibling_thread_update_started = asyncio.Event()
        release_sibling_thread_update = asyncio.Event()
        thread_invalidation_started = asyncio.Event()

        async def blocking_sibling_thread_update() -> None:
            sibling_thread_update_started.set()
            await release_sibling_thread_update.wait()

        async def mark_thread_stale(room_id: str, thread_id: str, *, reason: str) -> None:
            assert room_id == "!room:localhost"
            assert thread_id == "$claimed-thread:localhost"
            assert reason == "outbound_thread_mutation"
            thread_invalidation_started.set()

        event_cache.mark_thread_stale = AsyncMock(side_effect=mark_thread_stale)
        sibling_thread_task = coordinator.queue_thread_update(
            "!room:localhost",
            "$sibling-thread:localhost",
            blocking_sibling_thread_update,
            name="matrix_cache_blocking_sibling_thread_update",
        )
        await asyncio.wait_for(sibling_thread_update_started.wait(), timeout=1.0)

        access.notify_outbound_event(
            "!room:localhost",
            {
                "type": "m.room.message",
                "room_id": "!room:localhost",
                "event_id": "$edit:localhost",
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$claimed-thread:localhost"},
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread-message:localhost"},
                },
            },
        )
        try:
            await asyncio.wait_for(thread_invalidation_started.wait(), timeout=1.0)
            await asyncio.wait_for(
                coordinator.wait_for_thread_idle("!room:localhost", "$claimed-thread:localhost"),
                timeout=1.0,
            )
            assert sibling_thread_task.done() is False

            event_cache.mark_thread_stale.assert_awaited_once_with(
                "!room:localhost",
                "$claimed-thread:localhost",
                reason="outbound_thread_mutation",
            )
            event_cache.append_event.assert_awaited_once()
            append_args = event_cache.append_event.await_args.args
            assert append_args[0] == "!room:localhost"
            assert append_args[1] == "$claimed-thread:localhost"
            assert append_args[2]["event_id"] == "$edit:localhost"
        finally:
            release_sibling_thread_update.set()
            await asyncio.wait_for(
                asyncio.gather(sibling_thread_task, return_exceptions=True),
                timeout=1.0,
            )
            await _wait_for_room_cache_idle(coordinator)

    # Resolver disagreement cases now stay covered by the room-barrier fallback for lookup-dependent outbound mutations.

    @pytest.mark.asyncio
    async def test_notify_outbound_redaction_lookup_miss_without_cached_target_does_not_invalidate_room_threads(
        self,
    ) -> None:
        """Unknown redactions should not poison room caches when nothing was actually removed."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
        )

        access.notify_outbound_redaction("!room:localhost", "$room-message:localhost")
        await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)

        event_cache.mark_room_threads_stale.assert_not_awaited()
        event_cache.mark_thread_stale.assert_not_awaited()
        event_cache.redact_event.assert_awaited_once_with("!room:localhost", "$room-message:localhost")

    @pytest.mark.asyncio
    async def test_notify_outbound_reaction_persists_lookup_without_thread_invalidation(self) -> None:
        """Outbound reactions should be cached for later redaction lookups without staling thread history."""
        event_cache = _runtime_event_cache()
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = "@agent:localhost"
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=client, event_cache=event_cache),
        )

        access.notify_outbound_event(
            "!room:localhost",
            {
                "type": "m.reaction",
                "room_id": "!room:localhost",
                "event_id": "$reaction:localhost",
                "sender": "@agent:localhost",
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": "$thread-reply:localhost",
                        "key": "🛑",
                    },
                },
            },
        )
        await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)

        event_cache.store_events_batch.assert_awaited_once()
        stored_batch = event_cache.store_events_batch.await_args.args[0]
        assert len(stored_batch) == 1
        stored_event_id, stored_room_id, stored_event_source = stored_batch[0]
        assert stored_event_id == "$reaction:localhost"
        assert stored_room_id == "!room:localhost"
        assert stored_event_source["type"] == "m.reaction"
        assert stored_event_source["room_id"] == "!room:localhost"
        assert stored_event_source["event_id"] == "$reaction:localhost"
        assert stored_event_source["sender"] == "@agent:localhost"
        assert stored_event_source["content"]["m.relates_to"]["event_id"] == "$thread-reply:localhost"
        assert stored_event_source["content"]["m.relates_to"]["key"] == "🛑"
        assert isinstance(stored_event_source.get("origin_server_ts"), int)
        event_cache.mark_thread_stale.assert_not_awaited()
        event_cache.mark_room_threads_stale.assert_not_awaited()
        event_cache.append_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notify_outbound_reaction_normalizes_event_for_real_cache(
        self,
        tmp_path: Path,
    ) -> None:
        """Synthetic outbound reactions should be normalized before durable cache persistence."""
        event_cache = SqliteEventCache(tmp_path / "event_cache.db")
        await event_cache.initialize()
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = "@agent:localhost"
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=client, event_cache=event_cache),
        )

        try:
            access.notify_outbound_event(
                "!room:localhost",
                {
                    "type": "m.reaction",
                    "room_id": "!room:localhost",
                    "event_id": "$reaction:localhost",
                    "sender": "@agent:localhost",
                    "content": {
                        "m.relates_to": {
                            "rel_type": "m.annotation",
                            "event_id": "$thread-reply:localhost",
                            "key": "🛑",
                        },
                    },
                },
            )
            await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)

            cached_event = await event_cache.get_event("!room:localhost", "$reaction:localhost")
        finally:
            await event_cache.close()

        assert cached_event is not None
        assert cached_event["event_id"] == "$reaction:localhost"
        assert cached_event["content"]["m.relates_to"]["key"] == "🛑"
        assert isinstance(cached_event.get("origin_server_ts"), int)

    @pytest.mark.asyncio
    async def test_notify_outbound_message_plain_reply_to_threaded_target_updates_thread_cache(self) -> None:
        """Plain replies to known threaded targets should still do outbound thread bookkeeping."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(
            side_effect=lambda room_id, event_id: (
                "$thread-root:localhost"
                if (room_id, event_id) == ("!room:localhost", "$thread-reply:localhost")
                else None
            ),
        )
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = "@agent:localhost"
        client.room_get_event = AsyncMock(return_value=_thread_reply_lookup_response())
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=client, event_cache=event_cache),
        )

        access.notify_outbound_message(
            "!room:localhost",
            "$plain-reply:localhost",
            {
                "body": "bridged reply",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
            },
        )
        await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)

        event_cache.mark_thread_stale.assert_awaited_once_with(
            "!room:localhost",
            "$thread-root:localhost",
            reason="outbound_thread_mutation",
        )
        event_cache.append_event.assert_awaited()

    @pytest.mark.asyncio
    async def test_notify_outbound_message_reference_to_threaded_target_updates_thread_cache(self) -> None:
        """References to known threaded targets should still do outbound thread bookkeeping."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(
            side_effect=lambda room_id, event_id: (
                "$thread-root:localhost"
                if (room_id, event_id) == ("!room:localhost", "$thread-reply:localhost")
                else None
            ),
        )
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = "@agent:localhost"
        client.room_get_event = AsyncMock(return_value=_thread_reply_lookup_response())
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=client, event_cache=event_cache),
        )

        access.notify_outbound_message(
            "!room:localhost",
            "$reference:localhost",
            {
                "body": "reference",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.reference", "event_id": "$thread-reply:localhost"},
            },
        )
        await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)

        event_cache.mark_thread_stale.assert_awaited_once_with(
            "!room:localhost",
            "$thread-root:localhost",
            reason="outbound_thread_mutation",
        )
        event_cache.append_event.assert_awaited()

    @pytest.mark.asyncio
    async def test_notify_outbound_redaction_transitive_target_updates_thread_cache(self) -> None:
        """Transitive-threaded redactions should still stale-mark the owning thread."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(
            side_effect=lambda room_id, event_id: (
                "$thread-root:localhost"
                if (room_id, event_id) == ("!room:localhost", "$thread-reply:localhost")
                else None
            ),
        )
        event_cache.redact_event = AsyncMock(return_value=True)
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = "@agent:localhost"

        def room_get_event_response(event_id: str) -> nio.RoomGetEventResponse:
            if event_id == "$plain-two:localhost":
                event = nio.RoomMessageText.from_dict(
                    {
                        "content": {
                            "body": "plain two",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-one:localhost"}},
                        },
                        "event_id": event_id,
                        "sender": "@bridge:localhost",
                        "origin_server_ts": 3000,
                        "room_id": "!room:localhost",
                        "type": "m.room.message",
                    },
                )
                return _make_room_get_event_response(event)
            if event_id == "$plain-one:localhost":
                event = nio.RoomMessageText.from_dict(
                    {
                        "content": {
                            "body": "plain one",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
                        },
                        "event_id": event_id,
                        "sender": "@bridge:localhost",
                        "origin_server_ts": 2000,
                        "room_id": "!room:localhost",
                        "type": "m.room.message",
                    },
                )
                return _make_room_get_event_response(event)
            if event_id == "$thread-reply:localhost":
                return _thread_reply_lookup_response()
            message = f"unexpected lookup for {event_id}"
            raise AssertionError(message)

        async def room_get_event(_room_id: str, event_id: str) -> nio.RoomGetEventResponse:
            return room_get_event_response(event_id)

        client.room_get_event = AsyncMock(side_effect=room_get_event)
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=client, event_cache=event_cache),
        )

        access.notify_outbound_redaction("!room:localhost", "$plain-two:localhost")
        await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)

        event_cache.mark_thread_stale.assert_awaited_once_with(
            "!room:localhost",
            "$thread-root:localhost",
            reason="outbound_redaction",
        )
        event_cache.redact_event.assert_awaited_once_with("!room:localhost", "$plain-two:localhost")

    @pytest.mark.asyncio
    async def test_notify_outbound_redaction_of_reaction_does_not_invalidate_thread_cache(self) -> None:
        """Reaction redactions should not stale-mark thread message history."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(return_value="$thread-root:localhost")
        event_cache.redact_event = AsyncMock(return_value=True)
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = "@agent:localhost"
        client.room_get_event = AsyncMock(
            return_value=_make_room_get_event_response(
                nio.ReactionEvent.from_dict(
                    {
                        "content": {
                            "m.relates_to": {
                                "rel_type": "m.annotation",
                                "event_id": "$thread-reply:localhost",
                                "key": "👍",
                            },
                        },
                        "event_id": "$reaction:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567890,
                        "room_id": "!room:localhost",
                        "type": "m.reaction",
                    },
                ),
            ),
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=client, event_cache=event_cache),
        )

        access.notify_outbound_redaction("!room:localhost", "$reaction:localhost")
        await _wait_for_room_cache_idle(access.runtime.event_cache_write_coordinator)

        event_cache.mark_thread_stale.assert_not_awaited()
        event_cache.mark_room_threads_stale.assert_not_awaited()
        event_cache.redact_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_turn_cache_hit_with_later_persist_request_still_persists_lookup_fill(self) -> None:
        """A later ordinary lookup in the same turn should still persist an earlier non-persist fill."""
        event_cache = _runtime_event_cache()
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

        async with access.turn_scope():
            await access.get_event("!test:localhost", "$event:localhost", persist_lookup_fill=False)
            await access.get_event("!test:localhost", "$event:localhost")

        await _wait_for_room_cache_idle(coordinator)

        client.room_get_event.assert_awaited_once_with("!test:localhost", "$event:localhost")
        event_cache.store_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_turn_scope_memoizes_strict_thread_history_reads(self) -> None:
        """Strict dispatch thread reads should be memoized for the lifetime of one inbound turn."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=_make_client_mock(), event_cache=_runtime_event_cache()),
        )
        expected_history = thread_history_result(
            [
                _message(event_id="$thread_root", body="Root"),
                _message(event_id="$reply", body="Reply"),
            ],
            is_full_history=True,
            diagnostics={THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_CACHE},
        )

        with patch.object(
            access._reads,
            "read_thread",
            new=AsyncMock(return_value=thread_history_result(expected_history, is_full_history=True)),
        ) as mock_read_thread:
            async with access.turn_scope():
                first_history = await access.get_dispatch_thread_history("!test:localhost", "$thread_root")
                second_history = await access.get_dispatch_thread_history("!test:localhost", "$thread_root")

        assert [message.event_id for message in first_history] == ["$thread_root", "$reply"]
        assert [message.event_id for message in second_history] == ["$thread_root", "$reply"]
        assert first_history is not second_history
        mock_read_thread.assert_awaited_once_with(
            "!test:localhost",
            "$thread_root",
            mode=ThreadReadMode.DISPATCH_FULL,
            caller_label="unknown",
        )

    @pytest.mark.asyncio
    async def test_departure_epoch_invalidates_event_and_thread_turn_memos(self) -> None:
        """An active turn must not replay event or thread content memoized before a leave."""
        room_id = "!test:localhost"
        event_id = "$event:localhost"
        thread_id = "$thread_root"
        event_cache = _runtime_event_cache()
        departure_epoch = 0

        def mark_room_departed(_room_id: str) -> int:
            nonlocal departure_epoch
            departure_epoch += 1
            return departure_epoch

        event_cache.mark_room_departed.side_effect = mark_room_departed
        event_cache.room_departure_epoch.side_effect = lambda _room_id: departure_epoch
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=_make_client_mock(), event_cache=event_cache),
        )
        visible_event = _make_room_get_event_response(event_id)
        departed_event = MagicMock(spec=nio.RoomGetEventError)
        visible_thread = thread_history_result(
            [_message(event_id=thread_id, body="Root")],
            is_full_history=True,
            diagnostics={THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_CACHE},
        )
        departed_thread = thread_history_result(
            [],
            is_full_history=False,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
                THREAD_HISTORY_ERROR_DIAGNOSTIC: "departed_room",
            },
        )

        with (
            patch(
                "mindroom.matrix.conversation_cache._cached_room_get_event",
                new=AsyncMock(side_effect=[(visible_event, None), (departed_event, None)]),
            ) as mock_get_event,
            patch.object(
                access._reads,
                "read_thread",
                new=AsyncMock(side_effect=[visible_thread, departed_thread]),
            ) as mock_read_thread,
        ):
            async with access.turn_scope():
                assert await access.get_event(room_id, event_id) is visible_event
                assert await access.get_dispatch_thread_history(room_id, thread_id)

                await access.purge_room(room_id)

                assert await access.get_event(room_id, event_id) is departed_event
                assert not await access.get_dispatch_thread_history(room_id, thread_id)

        assert mock_get_event.await_count == 2
        assert mock_read_thread.await_count == 2

    @pytest.mark.asyncio
    async def test_turn_scope_does_not_memoize_degraded_full_thread_history_reads(self) -> None:
        """A degraded full-history read should not block a later retry in the same turn."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=_make_client_mock(), event_cache=_runtime_event_cache()),
        )
        degraded_history = thread_history_result(
            [],
            is_full_history=False,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
                THREAD_HISTORY_ERROR_DIAGNOSTIC: "cache_coordinator_timeout",
            },
        )
        full_history = thread_history_result(
            [_message(event_id="$thread_root", body="Root")],
            is_full_history=True,
            diagnostics={THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_CACHE},
        )

        with patch.object(
            access._reads,
            "read_thread",
            new=AsyncMock(side_effect=[degraded_history, full_history]),
        ) as mock_read_thread:
            async with access.turn_scope():
                first_history = await access.get_dispatch_thread_history("!test:localhost", "$thread_root")
                second_history = await access.get_dispatch_thread_history("!test:localhost", "$thread_root")

        assert first_history.is_full_history is False
        assert second_history.is_full_history is True
        assert [message.event_id for message in second_history] == ["$thread_root"]
        assert mock_read_thread.await_count == 2

    @pytest.mark.asyncio
    async def test_turn_scope_does_not_memoize_degraded_snapshot_thread_reads(self) -> None:
        """A degraded dispatch snapshot should not block a later retry in the same turn."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(client=_make_client_mock(), event_cache=_runtime_event_cache()),
        )
        degraded_snapshot = thread_history_result(
            [],
            is_full_history=False,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
                THREAD_HISTORY_ERROR_DIAGNOSTIC: "cache_coordinator_timeout",
            },
        )
        recovered_snapshot = thread_history_result(
            [_message(event_id="$thread_root", body="Root")],
            is_full_history=False,
            diagnostics={THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_CACHE},
        )

        with patch.object(
            access._reads,
            "read_thread",
            new=AsyncMock(side_effect=[degraded_snapshot, recovered_snapshot]),
        ) as mock_read_thread:
            async with access.turn_scope():
                first_history = await access.get_dispatch_thread_snapshot("!test:localhost", "$thread_root")
                second_history = await access.get_dispatch_thread_snapshot("!test:localhost", "$thread_root")

        assert first_history.diagnostics[THREAD_HISTORY_DEGRADED_DIAGNOSTIC] is True
        assert second_history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        assert [message.event_id for message in second_history] == ["$thread_root"]
        assert mock_read_thread.await_count == 2

    def test_collect_sync_timeline_cache_updates_treats_reference_as_thread_candidate(self) -> None:
        """Sync bookkeeping should classify references alongside other thread-affecting relations."""
        room_threaded_events: dict[str, list[dict[str, object]]] = {}
        room_plain_events: dict[str, list[dict[str, object]]] = {}
        room_redactions: dict[str, list[str]] = {}
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "reference",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.reference", "event_id": "$target:localhost"},
                },
                "event_id": "$reference:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        _collect_sync_timeline_cache_updates(
            "!test:localhost",
            event,
            room_threaded_events=room_threaded_events,
            room_plain_events=room_plain_events,
            room_redactions=room_redactions,
        )

        assert [cached["event_id"] for cached in room_threaded_events["!test:localhost"]] == ["$reference:localhost"]
        assert room_plain_events == {}
        assert room_redactions == {}

    @pytest.mark.asyncio
    async def test_get_latest_thread_event_id_fails_open_without_write_coordinator(self) -> None:
        """Thread reads should fail open when runtime support omitted the write coordinator."""
        config = _conversation_runtime_config()
        runtime = BotRuntimeState(
            client=AsyncMock(spec=nio.AsyncClient),
            config=config,
            runtime_paths=runtime_paths_for(config),
            enable_streaming=True,
            orchestrator=None,
            event_cache=_runtime_event_cache(),
            event_cache_write_coordinator=None,
        )
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=runtime,
        )
        access._reads.fetch_thread_history_from_client = AsyncMock(
            return_value=thread_history_result([], is_full_history=True),
        )

        latest_event_id = await access.get_latest_thread_event_id_if_needed(
            "!room:localhost",
            "$thread-root:localhost",
        )

        assert latest_event_id == "$thread-root:localhost"
        access._reads.fetch_thread_history_from_client.assert_awaited_once()
        fetch_args = access._reads.fetch_thread_history_from_client.await_args
        assert fetch_args.args == ("!room:localhost", "$thread-root:localhost")
        assert fetch_args.kwargs["caller_label"] == "latest_thread_event_lookup"
        assert fetch_args.kwargs["coordinator_queue_wait_ms"] >= 0.0

    @pytest.mark.asyncio
    async def test_invalidate_known_thread_fails_closed_when_stale_marker_write_fails(self) -> None:
        """Thread invalidation must delete cached rows when the stale marker cannot be persisted."""
        event_cache = _runtime_event_cache()
        event_cache.mark_thread_stale = AsyncMock(side_effect=RuntimeError("sqlite write failed"))
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
        )

        await access._write_cache_ops.invalidate_known_thread(
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

        await access._write_cache_ops.invalidate_room_threads(
            "!room:localhost",
            reason="test_failure",
        )

        event_cache.invalidate_room_threads.assert_awaited_once_with("!room:localhost")

    @pytest.mark.asyncio
    async def test_invalidate_known_thread_keeps_cache_enabled_when_backend_is_temporarily_unavailable(self) -> None:
        """Transient backend loss should not permanently disable a cache that tracks pending markers."""
        event_cache = _runtime_event_cache()
        backend_error = EventCacheBackendUnavailableError("postgres unavailable")
        event_cache.mark_thread_stale = AsyncMock(side_effect=backend_error)
        event_cache.invalidate_thread = AsyncMock(side_effect=backend_error)
        event_cache.disable = Mock()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
        )

        await access._write_cache_ops.invalidate_known_thread(
            "!room:localhost",
            "$thread:localhost",
            reason="test_failure",
        )

        event_cache.disable.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalidate_room_threads_keeps_cache_enabled_when_backend_is_temporarily_unavailable(self) -> None:
        """Transient backend loss should not turn a reconnectable Postgres cache into a permanent miss."""
        event_cache = _runtime_event_cache()
        backend_error = EventCacheBackendUnavailableError("postgres unavailable")
        event_cache.mark_room_threads_stale = AsyncMock(side_effect=backend_error)
        event_cache.invalidate_room_threads = AsyncMock(side_effect=backend_error)
        event_cache.disable = Mock()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
        )

        await access._write_cache_ops.invalidate_room_threads(
            "!room:localhost",
            reason="test_failure",
        )

        event_cache.disable.assert_not_called()

    @pytest.mark.asyncio
    async def test_lookup_miss_invalidation_survives_restart_and_refetches_next_read(self, tmp_path: Path) -> None:
        """Lookup-miss mutations should leave a durable marker that the next runtime observes."""
        event_cache = SqliteEventCache(tmp_path / "event_cache.db")
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
            await _replace_thread(
                event_cache,
                "!test:localhost",
                "$thread:localhost",
                [root_event, stale_reply_event],
                validated_at=time.time(),
            )
            first_access.notify_outbound_message(
                "!test:localhost",
                "$edit:localhost",
                {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "updated", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$missing:localhost"},
                },
            )
            await _wait_for_room_cache_idle(first_access.runtime.event_cache_write_coordinator)

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
