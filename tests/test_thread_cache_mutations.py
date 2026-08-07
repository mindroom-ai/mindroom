"""Thread mutation cache policy and conversation-cache thread reads."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import nio
import pytest

import mindroom.matrix.cache as matrix_cache
from mindroom.matrix import thread_bookkeeping
from mindroom.matrix.cache import thread_writes
from mindroom.matrix.cache.event_cache import EventCacheBackendUnavailableError
from mindroom.matrix.cache.thread_cache_state import ThreadAppendOutcome
from mindroom.matrix.cache.thread_writes import (
    _apply_thread_message_mutation,
    _apply_thread_redaction_mutation,
)
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact
from tests.threading_helpers import (
    EmptyProjection,
    _conversation_runtime,
    _message_mutation_event_info,
    _runtime_event_cache,
    _thread_mutation_cache_ops,
)


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
    assert not hasattr(matrix_cache, "_ThreadLiveWritePolicy")
    assert not hasattr(matrix_cache, "_ThreadSyncWritePolicy")


def test_thread_writes_uses_shared_mutation_write_context_alias() -> None:
    """Thread writes should reuse the shared mutation-write context alias."""
    assert thread_writes.MutationWriteContext is thread_bookkeeping.MutationWriteContext


def test_thread_writes_does_not_keep_message_impact_wrapper() -> None:
    """Message-impact resolution should call the resolver directly instead of wrapping it."""
    assert not hasattr(thread_writes, "_resolve_thread_message_mutation_impact")


class TestThreadMutationHelpers:
    """Direct mutation-helper coverage for live and sync message and redaction paths."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", ["live", "sync"])
    async def test_thread_message_mutation_room_level_skips_invalidation(
        self,
        context: str,
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
        )

        assert result is False
        logger.debug.assert_called_once_with(
            f"skip-{context}",
            room_id="!room:localhost",
            event_id="$event:localhost",
            original_event_id="$target:localhost",
        )
        event_cache.apply_thread_mutation_append.assert_not_awaited()
        event_cache.mark_room_threads_gap.assert_not_awaited()
        event_cache.mark_thread_gap.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", ["live", "sync"])
    async def test_thread_message_mutation_unknown_invalidates_room_once(
        self,
        context: str,
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
        )

        assert result is True
        event_cache.apply_thread_mutation_append.assert_not_awaited()
        event_cache.mark_room_threads_gap.assert_awaited_once_with(
            "!room:localhost",
            reason=f"{context}_thread_lookup_unavailable",
        )
        event_cache.mark_thread_gap.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", ["live", "sync"])
    async def test_thread_message_mutation_threaded_success_uses_context_reasons(
        self,
        context: str,
    ) -> None:
        """Threaded message mutations should append atomically and avoid room invalidation."""
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
        )

        assert result is False
        # One durable operation appends and settles trust, so a successful mutation writes no marker.
        event_cache.apply_thread_mutation_append.assert_awaited_once_with(
            "!room:localhost",
            "$thread:localhost",
            event_source,
            append_failed_reason=f"{context}_append_failed",
        )
        event_cache.mark_room_threads_gap.assert_not_awaited()
        event_cache.mark_thread_gap.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("context", "expected_reason"),
        [
            ("outbound", "outbound_append_failed"),
            ("live", "live_append_failed"),
            ("sync", "sync_append_failed"),
        ],
    )
    async def test_thread_message_mutation_threaded_append_failure_uses_path_policy(
        self,
        context: str,
        expected_reason: str,
    ) -> None:
        """An append that cannot land must carry each path's own durable marker reason."""
        cache_ops, _logger, event_cache = _thread_mutation_cache_ops()
        event_cache.apply_thread_mutation_append = AsyncMock(return_value=ThreadAppendOutcome.SNAPSHOT_MISSING)

        result = await _apply_thread_message_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            event_info=_message_mutation_event_info(),
            impact=MutationThreadImpact.threaded("$thread:localhost"),
            event_source={"event_id": "$event:localhost"},
            event_id="$event:localhost",
            context=context,
            room_level_skip_message=f"skip-{context}",
        )

        assert result is False
        # The marker is written inside the same operation, under the reason this path asks for.
        event_cache.apply_thread_mutation_append.assert_awaited_once_with(
            "!room:localhost",
            "$thread:localhost",
            {"event_id": "$event:localhost"},
            append_failed_reason=expected_reason,
        )
        event_cache.mark_room_threads_gap.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", ["live", "sync"])
    async def test_thread_redaction_mutation_room_level_redacts_without_thread_invalidations(
        self,
        context: str,
    ) -> None:
        """Room-level redactions should never gap-mark thread state."""
        cache_ops, logger, event_cache = _thread_mutation_cache_ops()

        result = await _apply_thread_redaction_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            redacted_event_id="$target:localhost",
            impact=MutationThreadImpact.room_level(),
            context=context,
        )

        assert result is False
        event_cache.mark_room_threads_gap.assert_not_awaited()
        event_cache.mark_thread_gap.assert_not_awaited()
        event_cache.redact_event.assert_awaited_once_with("!room:localhost", "$target:localhost")
        logger.debug.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", ["live", "sync"])
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
        event_cache.mark_room_threads_gap.assert_awaited_once_with(
            "!room:localhost",
            reason=f"{context}_redaction_lookup_unavailable",
        )
        event_cache.mark_thread_gap.assert_not_awaited()
        event_cache.redact_event.assert_awaited_once_with("!room:localhost", "$target:localhost")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", ["live", "sync"])
    async def test_thread_redaction_mutation_threaded_success_uses_context_reason(self, context: str) -> None:
        """Threaded redactions should gap-mark the owning thread once on success."""
        cache_ops, _logger, event_cache = _thread_mutation_cache_ops()

        result = await _apply_thread_redaction_mutation(
            cache_ops=cache_ops,
            room_id="!room:localhost",
            redacted_event_id="$target:localhost",
            impact=MutationThreadImpact.threaded("$thread:localhost"),
            context=context,
        )

        assert result is False
        event_cache.mark_room_threads_gap.assert_not_awaited()
        event_cache.mark_thread_gap.assert_awaited_once_with(
            "!room:localhost",
            "$thread:localhost",
            reason=f"{context}_redaction",
        )
        event_cache.redact_event.assert_awaited_once_with("!room:localhost", "$target:localhost")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context", ["live", "sync"])
    async def test_thread_redaction_mutation_threaded_failure_uses_failure_reason(self, context: str) -> None:
        """Threaded redaction failures should gap-mark once with the failure reason."""
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
        event_cache.mark_room_threads_gap.assert_not_awaited()
        event_cache.mark_thread_gap.assert_awaited_once_with(
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
            store=EmptyProjection(),
        )

        assert not hasattr(access, "_writes")

    # Resolver disagreement cases now stay covered by the room-barrier fallback for lookup-dependent outbound mutations.

    @pytest.mark.asyncio
    async def test_invalidate_known_thread_fails_closed_when_gap_marker_write_fails(self) -> None:
        """Thread invalidation must delete cached rows when the gap marker cannot be persisted."""
        event_cache = _runtime_event_cache()
        event_cache.mark_thread_gap = AsyncMock(side_effect=RuntimeError("sqlite write failed"))
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
            store=EmptyProjection(),
        )

        await access._write_cache_ops.invalidate_known_thread(
            "!room:localhost",
            "$thread:localhost",
            reason="test_failure",
        )

        event_cache.invalidate_thread.assert_awaited_once_with("!room:localhost", "$thread:localhost")

    @pytest.mark.asyncio
    async def test_invalidate_room_threads_fails_closed_when_gap_marker_write_fails(self) -> None:
        """Room invalidation must delete cached room rows when the gap marker cannot be persisted."""
        event_cache = _runtime_event_cache()
        event_cache.mark_room_threads_gap = AsyncMock(side_effect=RuntimeError("sqlite write failed"))
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
            store=EmptyProjection(),
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
        event_cache.mark_thread_gap = AsyncMock(side_effect=backend_error)
        event_cache.invalidate_thread = AsyncMock(side_effect=backend_error)
        event_cache.disable = Mock()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
            store=EmptyProjection(),
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
        event_cache.mark_room_threads_gap = AsyncMock(side_effect=backend_error)
        event_cache.invalidate_room_threads = AsyncMock(side_effect=backend_error)
        event_cache.disable = Mock()
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(event_cache=event_cache),
            store=EmptyProjection(),
        )

        await access._write_cache_ops.invalidate_room_threads(
            "!room:localhost",
            reason="test_failure",
        )

        event_cache.disable.assert_not_called()
