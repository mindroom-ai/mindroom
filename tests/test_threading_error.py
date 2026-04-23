"""Test threading behavior to reproduce and fix the threading error.

This test verifies that:
1. Agents always respond in threads (never in main room)
2. Commands that are replies don't cause threading errors
3. The bot handles various message relation scenarios correctly
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import nio
import pytest
import pytest_asyncio
from nio.api import RelationshipType

import mindroom.matrix.cache as matrix_cache
import mindroom.timing as timing_module
from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.hooks import EVENT_AGENT_STARTED
from mindroom.matrix import thread_bookkeeping
from mindroom.matrix.cache import ThreadHistoryResult, thread_writes
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
from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
from mindroom.matrix.cache.thread_writes import (
    _apply_thread_message_mutation,
    _apply_thread_redaction_mutation,
    _collect_sync_timeline_cache_updates,
)
from mindroom.matrix.cache.write_coordinator import _EventCacheWriteCoordinator
from mindroom.matrix.client import (
    DeliveredMatrixEvent,
    PermanentMatrixStartupError,
    ResolvedVisibleMessage,
)
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import _clear_mxc_cache
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    ThreadRootProof,
    resolve_event_thread_id,
    resolve_related_event_thread_id,
    resolve_related_event_thread_id_best_effort,
    room_scan_thread_membership_access,
    snapshot_thread_membership_access,
)
from mindroom.matrix.thread_projection import resolve_thread_ids_for_event_infos
from mindroom.matrix.users import AgentMatrixUser
from mindroom.runtime_support import (
    OwnedRuntimeSupport,
    StartupThreadPrewarmRegistry,
    close_owned_runtime_support,
    sync_owned_runtime_support,
)
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
    from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
    from typing import Any


def _runtime_bound_config(config: Config, runtime_root: Path) -> Config:
    """Return a runtime-bound config for threading tests."""
    return bind_runtime_paths(config, test_runtime_paths(runtime_root))


def test_plain_reply_event_info_has_no_thread_routing_root() -> None:
    """Plain replies should not populate any synthetic routing root."""
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
    assert event_info.relates_to_event_id is None


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


def _thread_mutation_cache_ops() -> tuple[ThreadMutationCacheOps, MagicMock, MagicMock]:
    """Return concrete thread cache ops backed by one async-mock event cache."""
    logger = MagicMock()
    event_cache = MagicMock()
    event_cache.append_event = AsyncMock(return_value=True)
    event_cache.disable = Mock()
    event_cache.invalidate_room_threads = AsyncMock()
    event_cache.invalidate_thread = AsyncMock()
    event_cache.mark_room_threads_stale = AsyncMock()
    event_cache.mark_thread_stale = AsyncMock()
    event_cache.redact_event = AsyncMock(return_value=True)
    event_cache.revalidate_thread_after_incremental_update = AsyncMock()
    runtime = MagicMock()
    runtime.event_cache = event_cache
    runtime.event_cache_write_coordinator = _runtime_write_coordinator()
    runtime.runtime_started_at = 1234567890.0
    return ThreadMutationCacheOps(logger_getter=lambda: logger, runtime=runtime), logger, event_cache


def _message_mutation_event_info(*, original_event_id: str = "$target:localhost") -> EventInfo:
    """Return one thread-affecting event info for direct mutation-helper tests."""
    return EventInfo.from_event(
        {
            "type": "m.room.message",
            "content": {
                "body": "* updated",
                "msgtype": "m.text",
                "m.new_content": {"body": "updated", "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
            },
        },
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
    config = _conversation_runtime_config()
    return BotRuntimeState(
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        enable_streaming=True,
        orchestrator=None,
        event_cache=event_cache or _runtime_event_cache(),
        event_cache_write_coordinator=coordinator or _runtime_write_coordinator(),
    )


def _conversation_runtime_config() -> Config:
    """Return one runtime-bound config for conversation-cache tests."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp(prefix="mindroom-threading-runtime-")))
    return bind_runtime_paths(
        Config(agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])}),
        runtime_paths,
    )


def _install_runtime_write_coordinator(bot: AgentBot) -> _EventCacheWriteCoordinator:
    """Attach one explicit runtime write coordinator to a bot test double."""
    coordinator = _EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=bot._runtime_view,
    )
    bot.event_cache_write_coordinator = coordinator
    return coordinator


async def _bind_owned_runtime_support(
    bot: AgentBot,
    *,
    db_path: Path | None = None,
) -> OwnedRuntimeSupport:
    """Build one real injected runtime-support bundle for a bot test."""
    support = await sync_owned_runtime_support(
        None,
        db_path=bot.config.cache.resolve_db_path(bot.runtime_paths) if db_path is None else db_path,
        logger=bot.logger,
        background_task_owner=bot._runtime_view,
        init_failure_reason_prefix="test_runtime_init_failed",
        log_db_path_change=False,
    )
    bot.event_cache = support.event_cache
    bot.event_cache_write_coordinator = support.event_cache_write_coordinator
    bot.startup_thread_prewarm_registry = support.startup_thread_prewarm_registry
    bot._runtime_view.mark_runtime_started()
    return support


async def _close_bound_runtime_support(bot: AgentBot, support: OwnedRuntimeSupport) -> None:
    """Close one test-owned runtime-support bundle."""
    await close_owned_runtime_support(support, logger=bot.logger)


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
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!room:localhost")

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
            await coordinator.wait_for_room_idle("!room:localhost")

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
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!room:localhost")

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
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!room:localhost")

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
        event_cache = _EventCache(tmp_path / "event_cache.db")
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
            await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!room:localhost")

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
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!room:localhost")

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
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!room:localhost")

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
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!room:localhost")

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
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!room:localhost")

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

        await coordinator.wait_for_room_idle("!test:localhost")

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
            new=AsyncMock(return_value=expected_history),
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
            full_history=True,
            dispatch_safe=True,
        )

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
        access._reads.fetch_thread_history_from_client.assert_awaited_once_with(
            "!room:localhost",
            "$thread-root:localhost",
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
            await first_access.runtime.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")

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
        bot.startup_thread_prewarm_registry = StartupThreadPrewarmRegistry()

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

                await bot.stop(reason="test")

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

        shared_cache = _EventCache(bot.config.cache.resolve_db_path(bot.runtime_paths))
        shared_coordinator = _EventCacheWriteCoordinator(
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
                await bot.stop(reason="test")

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
                "!test:localhost": MagicMock(timeline=MagicMock(events=[message_event])),
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
            await bot.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")

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
    async def test_sync_timeline_redaction_does_not_resurrect_point_lookup_cache(self, bot: AgentBot) -> None:
        """A sync batch that contains both a message and its redaction must leave no cached lookup entry."""
        support = await _bind_owned_runtime_support(bot)
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
        event_cache.append_event = AsyncMock()
        bot.event_cache = event_cache
        bot.event_cache_write_coordinator = _EventCacheWriteCoordinator(
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
        await bot.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")

        event_cache.get_thread_id_for_event.assert_awaited_once_with("!test:localhost", "$room_msg:localhost")
        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            "!test:localhost",
            reason="live_thread_lookup_unavailable",
        )
        event_cache.append_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_plain_edit_missing_original_invalidates_room_threads(self, bot: AgentBot) -> None:
        """Live plain edits without enough local proof should invalidate room thread snapshots."""
        event_cache = _runtime_event_cache()
        event_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        event_cache.get_event = AsyncMock(return_value=None)
        event_cache.append_event = AsyncMock()
        bot.event_cache = event_cache
        bot.event_cache_write_coordinator = _EventCacheWriteCoordinator(
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
        await bot.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")

        event_cache.get_thread_id_for_event.assert_awaited_once_with("!test:localhost", "$missing-room-msg:localhost")
        event_cache.get_event.assert_awaited_once_with("!test:localhost", "$missing-room-msg:localhost")
        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            "!test:localhost",
            reason="live_thread_lookup_unavailable",
        )
        event_cache.append_event.assert_not_awaited()

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

        async def quick_snapshot(_room_id: str, _thread_id: str) -> ThreadHistoryResult:
            read_finished.set()
            return thread_history_result(
                [_message(event_id="$other-thread:localhost", body="Root")],
                is_full_history=True,
            )

        access._live._resolver.resolve_thread_impact_for_mutation = AsyncMock(side_effect=slow_resolve)
        access._reads.fetch_thread_snapshot_from_client = AsyncMock(side_effect=quick_snapshot)

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

        read_task = asyncio.create_task(access.get_thread_snapshot("!test:localhost", "$other-thread:localhost"))
        await asyncio.wait_for(read_finished.wait(), timeout=1.0)
        await asyncio.wait_for(asyncio.shield(read_task), timeout=0.1)

        allow_resolve.set()
        await live_task
        await coordinator.wait_for_room_idle("!test:localhost")

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

        async def quick_snapshot(_room_id: str, _thread_id: str) -> ThreadHistoryResult:
            read_finished.set()
            return thread_history_result(
                [_message(event_id="$other-thread:localhost", body="Root")],
                is_full_history=True,
            )

        access._live._resolver.resolve_redaction_thread_impact = AsyncMock(side_effect=slow_resolve)
        access._reads.fetch_thread_snapshot_from_client = AsyncMock(side_effect=quick_snapshot)
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

        read_task = asyncio.create_task(access.get_thread_snapshot("!test:localhost", "$other-thread:localhost"))
        await asyncio.wait_for(read_finished.wait(), timeout=1.0)
        await asyncio.wait_for(asyncio.shield(read_task), timeout=0.1)

        allow_resolve.set()
        await live_task
        await coordinator.wait_for_room_idle("!test:localhost")

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
        await coordinator.wait_for_room_idle("!test:localhost")

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
            event_cache.mark_thread_stale.assert_awaited_once_with(
                room_id,
                thread_a_id,
                reason="live_redaction",
            )
        finally:
            await asyncio.wait_for(
                asyncio.gather(sibling_task, return_exceptions=True),
                timeout=1.0,
            )
            await coordinator.wait_for_room_idle(room_id)

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
            event_cache.mark_thread_stale.assert_awaited_once_with(
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
            await coordinator.wait_for_room_idle(room_id)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("timing_enabled_for_test", [False, True], ids=["timing_disabled", "timing_enabled"])
    async def test_live_unknown_event_publishes_room_stale_marker_before_room_barrier(
        self,
        monkeypatch: pytest.MonkeyPatch,
        timing_enabled_for_test: bool,
    ) -> None:
        """Live unknown thread mutations should publish the room stale marker before queued bookkeeping."""
        if timing_enabled_for_test:
            monkeypatch.setenv("MINDROOM_TIMING", "1")
        else:
            monkeypatch.delenv("MINDROOM_TIMING", raising=False)

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
        room_invalidation_started = asyncio.Event()

        async def slow_prior_room_update() -> None:
            prior_write_started.set()
            await allow_prior_write_finish.wait()

        async def mark_room_threads_stale(room_id: str, *, reason: str) -> None:
            assert room_id == "!test:localhost"
            assert reason == "live_thread_lookup_unavailable"
            room_invalidation_started.set()

        access._live._resolver.resolve_thread_impact_for_mutation = AsyncMock(
            return_value=MutationThreadImpact.unknown(),
        )
        event_cache.mark_room_threads_stale = AsyncMock(side_effect=mark_room_threads_stale)

        coordinator.queue_room_update(
            "!test:localhost",
            slow_prior_room_update,
            name="matrix_cache_prior_update",
        )
        await asyncio.wait_for(prior_write_started.wait(), timeout=1.0)

        event = nio.RoomMessageText.from_dict(
            {
                "type": "m.room.message",
                "room_id": "!test:localhost",
                "event_id": "$unknown-edit:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234,
                "content": {
                    "body": "updated",
                    "msgtype": "m.text",
                },
            },
        )

        live_task = asyncio.create_task(
            access.append_live_event(
                "!test:localhost",
                event,
                event_info=EventInfo.from_event(event.source),
            ),
        )
        await asyncio.wait_for(room_invalidation_started.wait(), timeout=1.0)
        assert live_task.done() is False

        allow_prior_write_finish.set()
        await live_task
        await coordinator.wait_for_room_idle("!test:localhost")

        event_cache.mark_room_threads_stale.assert_awaited_once_with(
            "!test:localhost",
            reason="live_thread_lookup_unavailable",
        )
        event_cache.append_event.assert_not_awaited()

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
        append_started = asyncio.Event()
        append_task: asyncio.Task[None] | None = None

        async def blocking_sibling_thread_update() -> None:
            sibling_update_started.set()
            await asyncio.sleep(0.2)

        async def mark_thread_stale(
            marked_room_id: str,
            marked_thread_id: str,
            *,
            reason: str,
        ) -> None:
            assert marked_room_id == room_id
            assert marked_thread_id == thread_a_id
            assert reason == "live_thread_mutation"
            append_started.set()

        access._live._resolver.resolve_thread_impact_for_mutation = AsyncMock(
            return_value=MutationThreadImpact.threaded(thread_a_id),
        )
        event_cache.mark_thread_stale = AsyncMock(side_effect=mark_thread_stale)
        event_cache.append_event = AsyncMock(return_value=True)
        sibling_task = coordinator.queue_thread_update(
            room_id,
            thread_b_id,
            blocking_sibling_thread_update,
            name="matrix_cache_blocking_other_thread_update",
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
            started = time.perf_counter()
            append_task = asyncio.create_task(
                access.append_live_event(
                    room_id,
                    event,
                    event_info=EventInfo.from_event(event.source),
                ),
            )

            await asyncio.wait_for(append_started.wait(), timeout=0.1)
            await asyncio.wait_for(append_task, timeout=0.1)

            assert (time.perf_counter() - started) * 1000.0 < 100.0
            assert sibling_task.done() is False
        finally:
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

        event_cache.mark_thread_stale.assert_awaited_once_with(
            room_id,
            thread_a_id,
            reason="live_thread_mutation",
        )
        event_cache.append_event.assert_awaited_once_with(
            room_id,
            thread_a_id,
            event.source,
        )

    @pytest.mark.asyncio
    async def test_live_plain_reply_to_threaded_event_persists_event_thread_membership(
        self,
        bot: AgentBot,
    ) -> None:
        """Plain replies to threaded events should keep a durable event-to-thread mapping."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_id = "$plain_reply:localhost"

        real_event_cache = _EventCache(bot.storage_path / "plain-reply-thread-membership.db")
        await real_event_cache.initialize()
        bot.event_cache = real_event_cache
        bot.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )
        try:
            await real_event_cache.store_event(
                thread_reply_id,
                room_id,
                {
                    "content": {
                        "body": "Thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": thread_reply_id,
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567894,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

            plain_reply_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "bridged plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                    "event_id": plain_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

            await bot._conversation_cache.append_live_event(
                room_id,
                plain_reply_event,
                event_info=EventInfo.from_event(plain_reply_event.source),
            )
            await bot.event_cache_write_coordinator.wait_for_room_idle(room_id)

            assert await real_event_cache.get_thread_id_for_event(room_id, plain_reply_id) == thread_root_id
        finally:
            await real_event_cache.close()

    @pytest.mark.asyncio
    async def test_live_plain_reply_chain_persists_thread_membership_transitively(
        self,
        bot: AgentBot,
    ) -> None:
        """A plain-reply chain should persist thread membership transitively once it reaches a thread."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_id = "$plain_reply:localhost"
        second_plain_reply_id = "$second_plain_reply:localhost"

        real_event_cache = _EventCache(bot.storage_path / "plain-reply-second-hop-membership.db")
        await real_event_cache.initialize()
        bot.event_cache = real_event_cache
        bot.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )
        try:
            await real_event_cache.store_event(
                thread_reply_id,
                room_id,
                {
                    "content": {
                        "body": "Thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": thread_reply_id,
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567894,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )
            await real_event_cache.store_event(
                plain_reply_id,
                room_id,
                {
                    "content": {
                        "body": "first bridge reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                    "event_id": plain_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

            second_plain_reply_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "second bridge reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": plain_reply_id}},
                    },
                    "event_id": second_plain_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567896,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

            await bot._conversation_cache.append_live_event(
                room_id,
                second_plain_reply_event,
                event_info=EventInfo.from_event(second_plain_reply_event.source),
            )
            await bot.event_cache_write_coordinator.wait_for_room_idle(room_id)

            assert await real_event_cache.get_thread_id_for_event(room_id, second_plain_reply_id) == thread_root_id
        finally:
            await real_event_cache.close()

    @pytest.mark.asyncio
    async def test_media_ingress_primes_transitive_ancestors_before_persisting_membership(
        self,
        bot: AgentBot,
    ) -> None:
        """Cold-start media ingress should persist the same transitive thread membership used at runtime."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_id = "$plain_reply:localhost"
        audio_event_id = "$audio_reply:localhost"
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = room_id
        real_event_cache = _EventCache(bot.storage_path / "media-ingress-thread-membership.db")
        await real_event_cache.initialize()
        bot.event_cache = real_event_cache
        bot.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )
        audio_event = nio.RoomMessageAudio.from_dict(
            {
                "content": {
                    "body": "voice-note.ogg",
                    "msgtype": "m.audio",
                    "url": "mxc://localhost/voice-note",
                    "m.relates_to": {"m.in_reply_to": {"event_id": plain_reply_id}},
                },
                "event_id": audio_event_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )
        prechecked_event = MagicMock(event=audio_event, requester_user_id="@user:localhost")
        bot._turn_controller._precheck_dispatch_event = MagicMock(return_value=prechecked_event)
        bot._turn_controller._dispatch_special_media_as_text = AsyncMock(return_value=True)
        bot._turn_controller._enqueue_for_dispatch = AsyncMock()

        def room_get_event_response(event_id: str, content: dict[str, object]) -> nio.RoomGetEventResponse:
            return nio.RoomGetEventResponse.from_dict(
                {
                    "content": content,
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

        async def fetch_related_event(fetch_room_id: str, event_id: str) -> nio.RoomGetEventResponse:
            assert fetch_room_id == room_id
            if event_id == plain_reply_id:
                return room_get_event_response(
                    plain_reply_id,
                    {
                        "body": "bridge reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                )
            if event_id == thread_reply_id:
                return room_get_event_response(
                    thread_reply_id,
                    {
                        "body": "thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                )
            msg = f"unexpected event lookup: {event_id}"
            raise AssertionError(msg)

        bot.client.room_get_event = AsyncMock(side_effect=fetch_related_event)

        try:
            await bot._turn_controller.handle_media_event(room, audio_event)
            await bot.event_cache_write_coordinator.wait_for_room_idle(room_id)

            assert await real_event_cache.get_thread_id_for_event(room_id, audio_event_id) == thread_root_id
        finally:
            await real_event_cache.close()

    @pytest.mark.asyncio
    async def test_transitive_thread_membership_handles_long_reply_chains(
        self,
    ) -> None:
        """The shared transitive resolver should handle reply chains longer than the old 32-hop ceiling."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        threaded_event_id = "$thread_reply:localhost"
        last_event_id = "$plain_reply_33:localhost"
        event_infos: dict[str, EventInfo] = {
            threaded_event_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": threaded_event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
        }
        for index in range(1, 34):
            event_id = f"$plain_reply_{index}:localhost"
            reply_target_id = threaded_event_id if index == 1 else f"$plain_reply_{index - 1}:localhost"
            event_infos[event_id] = EventInfo.from_event(
                {
                    "content": {
                        "body": f"plain reply {index}",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": reply_target_id}},
                    },
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": index + 1,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return event_infos.get(event_id)

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            return ThreadRootProof.not_a_thread_root()

        resolved_thread_id = await resolve_event_thread_id(
            room_id,
            event_infos[last_event_id],
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolved_thread_id == thread_root_id

    @pytest.mark.asyncio
    async def test_resolve_thread_ids_for_event_infos_reaches_fixpoint_across_transitive_chain(
        self,
    ) -> None:
        """Map-backed resolution should derive thread IDs even when children are visited before parents."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_1_id = "$plain_reply_1:localhost"
        plain_reply_2_id = "$plain_reply_2:localhost"
        event_infos = {
            thread_reply_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": thread_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
            plain_reply_1_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "plain reply 1",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                    "event_id": plain_reply_1_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 2,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
            plain_reply_2_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "plain reply 2",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": plain_reply_1_id}},
                    },
                    "event_id": plain_reply_2_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 3,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
        }

        resolved_thread_ids = await resolve_thread_ids_for_event_infos(
            room_id,
            event_infos=event_infos,
            ordered_event_ids=[
                plain_reply_2_id,
                plain_reply_1_id,
                thread_reply_id,
            ],
        )

        assert resolved_thread_ids == {
            thread_reply_id: thread_root_id,
            plain_reply_1_id: thread_root_id,
            plain_reply_2_id: thread_root_id,
        }

    @pytest.mark.asyncio
    async def test_resolve_event_thread_id_follows_reaction_target_transitively(
        self,
    ) -> None:
        """The shared entrypoint should inherit thread membership across reaction targets too."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_id = "$plain_reply:localhost"
        reaction_event = EventInfo.from_event(
            {
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": plain_reply_id,
                        "key": "👍",
                    },
                },
                "event_id": "$reaction:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 4,
                "room_id": room_id,
                "type": "m.reaction",
            },
        )
        event_infos: dict[str, EventInfo] = {
            thread_reply_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": thread_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
            plain_reply_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                    "event_id": plain_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 2,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
        }

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return event_infos.get(event_id)

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            return ThreadRootProof.not_a_thread_root()

        resolved_thread_id = await resolve_event_thread_id(
            room_id,
            reaction_event,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolved_thread_id == thread_root_id

    @pytest.mark.asyncio
    async def test_room_scan_thread_membership_access_treats_root_with_children_as_threaded(
        self,
    ) -> None:
        """Room-scan-backed access should apply one shared root-children rule."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        root_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "root",
                    "msgtype": "m.text",
                },
                "event_id": thread_root_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return root_event_info if event_id == thread_root_id else None

        async def fetch_thread_event_sources(
            lookup_room_id: str,
            requested_thread_root_id: str,
        ) -> tuple[list[dict[str, object]], bool]:
            assert lookup_room_id == room_id
            assert requested_thread_root_id == thread_root_id
            return [
                {"event_id": thread_root_id},
                {"event_id": "$child:localhost"},
            ], True

        resolved_thread_id = await resolve_related_event_thread_id(
            room_id,
            thread_root_id,
            access=room_scan_thread_membership_access(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                fetch_thread_event_sources=fetch_thread_event_sources,
            ),
        )

        assert resolved_thread_id == thread_root_id

    @pytest.mark.asyncio
    async def test_room_scan_thread_membership_access_does_not_treat_root_edit_as_child_proof(
        self,
    ) -> None:
        """A root edit alone should not prove that plain replies to the root belong to a thread."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        plain_reply_id = "$plain_reply:localhost"
        root_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "root",
                    "msgtype": "m.text",
                },
                "event_id": thread_root_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )
        plain_reply_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": thread_root_id}},
                },
                "event_id": plain_reply_id,
                "sender": "@user:localhost",
                "origin_server_ts": 2,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            if event_id == thread_root_id:
                return root_event_info
            if event_id == plain_reply_id:
                return plain_reply_event_info
            return None

        async def fetch_thread_event_sources(
            lookup_room_id: str,
            requested_thread_root_id: str,
        ) -> tuple[list[dict[str, object]], bool]:
            assert lookup_room_id == room_id
            assert requested_thread_root_id == thread_root_id
            return [
                {
                    "event_id": thread_root_id,
                    "type": "m.room.message",
                    "content": {
                        "body": "root",
                        "msgtype": "m.text",
                    },
                },
                {
                    "event_id": "$root_edit:localhost",
                    "type": "m.room.message",
                    "content": {
                        "body": "* root edited",
                        "msgtype": "m.text",
                        "m.new_content": {
                            "body": "root edited",
                            "msgtype": "m.text",
                        },
                        "m.relates_to": {
                            "rel_type": "m.replace",
                            "event_id": thread_root_id,
                        },
                    },
                },
            ], True

        resolved_thread_id = await resolve_event_thread_id(
            room_id,
            plain_reply_event_info,
            event_id=plain_reply_id,
            access=room_scan_thread_membership_access(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                fetch_thread_event_sources=fetch_thread_event_sources,
            ),
        )

        assert resolved_thread_id is None

    @pytest.mark.asyncio
    async def test_snapshot_thread_membership_access_treats_root_with_children_as_threaded(
        self,
    ) -> None:
        """Snapshot-backed access should apply the same root-children contract."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        root_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "root",
                    "msgtype": "m.text",
                },
                "event_id": thread_root_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        @dataclass(frozen=True)
        class SnapshotMessage:
            event_id: str

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return root_event_info if event_id == thread_root_id else None

        async def fetch_thread_snapshot(
            lookup_room_id: str,
            requested_thread_root_id: str,
        ) -> list[SnapshotMessage]:
            assert lookup_room_id == room_id
            assert requested_thread_root_id == thread_root_id
            return [
                SnapshotMessage(event_id=thread_root_id),
                SnapshotMessage(event_id="$child:localhost"),
            ]

        resolved_thread_id = await resolve_related_event_thread_id(
            room_id,
            thread_root_id,
            access=snapshot_thread_membership_access(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                fetch_thread_snapshot=fetch_thread_snapshot,
            ),
        )

        assert resolved_thread_id == thread_root_id

    @pytest.mark.asyncio
    async def test_snapshot_thread_membership_access_propagates_root_proof_failure(
        self,
    ) -> None:
        """Snapshot proof failures should surface instead of silently downgrading membership."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        root_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "root",
                    "msgtype": "m.text",
                },
                "event_id": thread_root_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return root_event_info if event_id == thread_root_id else None

        async def fetch_thread_snapshot(_room_id: str, _thread_root_id: str) -> list[object]:
            msg = "snapshot unavailable"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="snapshot unavailable"):
            await resolve_related_event_thread_id(
                room_id,
                thread_root_id,
                access=snapshot_thread_membership_access(
                    lookup_thread_id=lookup_thread_id,
                    fetch_event_info=fetch_event_info,
                    fetch_thread_snapshot=fetch_thread_snapshot,
                ),
            )

    @pytest.mark.asyncio
    async def test_related_thread_resolution_propagates_event_lookup_failure(
        self,
    ) -> None:
        """Strict resolution should fail closed when related-event lookup is unavailable."""
        room_id = "!test:localhost"
        related_event_id = "$related:localhost"

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
            msg = "lookup unavailable"
            raise RuntimeError(msg)

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            return ThreadRootProof.not_a_thread_root()

        with pytest.raises(RuntimeError, match="lookup unavailable"):
            await resolve_related_event_thread_id(
                room_id,
                related_event_id,
                access=ThreadMembershipAccess(
                    lookup_thread_id=lookup_thread_id,
                    fetch_event_info=fetch_event_info,
                    prove_thread_root=prove_thread_root,
                ),
            )

    @pytest.mark.asyncio
    async def test_best_effort_related_thread_resolution_degrades_when_event_lookup_fails(
        self,
    ) -> None:
        """Best-effort resolution should degrade when related-event lookup is unavailable."""
        room_id = "!test:localhost"
        related_event_id = "$related:localhost"

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
            msg = "lookup unavailable"
            raise RuntimeError(msg)

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            return ThreadRootProof.not_a_thread_root()

        resolved_thread_id = await resolve_related_event_thread_id_best_effort(
            room_id,
            related_event_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolved_thread_id is None

    @pytest.mark.asyncio
    async def test_best_effort_related_thread_resolution_degrades_when_root_proof_fails(
        self,
    ) -> None:
        """Best-effort callers should treat proof failures as unknown instead of raising."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        root_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "root",
                    "msgtype": "m.text",
                },
                "event_id": thread_root_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return root_event_info if event_id == thread_root_id else None

        async def fetch_thread_snapshot(_room_id: str, _thread_root_id: str) -> list[object]:
            msg = "snapshot unavailable"
            raise RuntimeError(msg)

        resolved_thread_id = await resolve_related_event_thread_id_best_effort(
            room_id,
            thread_root_id,
            access=snapshot_thread_membership_access(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                fetch_thread_snapshot=fetch_thread_snapshot,
            ),
        )

        assert resolved_thread_id is None

    @pytest.mark.asyncio
    async def test_live_edit_of_promoted_plain_reply_persists_event_thread_membership(
        self,
        bot: AgentBot,
    ) -> None:
        """Edits of promoted plain replies should keep the same durable thread membership."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_id = "$plain_reply:localhost"
        plain_reply_edit_id = "$plain_reply_edit:localhost"

        real_event_cache = _EventCache(bot.storage_path / "plain-reply-edit-thread-membership.db")
        await real_event_cache.initialize()
        bot.event_cache = real_event_cache
        bot.event_cache_write_coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )
        try:
            await real_event_cache.store_event(
                thread_reply_id,
                room_id,
                {
                    "content": {
                        "body": "Thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": thread_reply_id,
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567894,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )
            plain_reply_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "bridged plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                    "event_id": plain_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )
            await bot._conversation_cache.append_live_event(
                room_id,
                plain_reply_event,
                event_info=EventInfo.from_event(plain_reply_event.source),
            )
            await bot.event_cache_write_coordinator.wait_for_room_idle(room_id)

            edit_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "* updated bridged plain reply",
                        "msgtype": "m.text",
                        "m.new_content": {
                            "body": "updated bridged plain reply",
                            "msgtype": "m.text",
                        },
                        "m.relates_to": {"rel_type": "m.replace", "event_id": plain_reply_id},
                    },
                    "event_id": plain_reply_edit_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567896,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

            await bot._conversation_cache.append_live_event(
                room_id,
                edit_event,
                event_info=EventInfo.from_event(edit_event.source),
            )
            await bot.event_cache_write_coordinator.wait_for_room_idle(room_id)

            assert await real_event_cache.get_thread_id_for_event(room_id, plain_reply_edit_id) == thread_root_id
        finally:
            await real_event_cache.close()

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
    async def test_queue_room_update_logs_timing_breakdown_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Room-scoped cache updates should log predecessor wait versus update time."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        coordinator = _EventCacheWriteCoordinator(
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
            call for call in timing_logger.info.call_args_list if call.args == ("Event cache update timing",)
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
        coordinator = _EventCacheWriteCoordinator(
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
            for call in timing_logger.info.call_args_list
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
        coordinator = _EventCacheWriteCoordinator(
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
            for call in timing_logger.info.call_args_list
            if call.args == ("Event cache update timing",) and call.kwargs["operation"] == "matrix_cache_third_update"
        )
        assert timing_call.kwargs["predecessor_count"] == 2
        assert timing_call.kwargs["queued_behind_predecessor"] is True

    @pytest.mark.asyncio
    async def test_wait_for_room_idle_logs_timing_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Room-idle waits should log how long the caller sat behind queued room work."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )
        update_started = asyncio.Event()
        allow_update_finish = asyncio.Event()

        async def slow_update() -> None:
            update_started.set()
            await allow_update_finish.wait()

        try:
            queued_task = coordinator.queue_room_update(
                "!room:localhost",
                slow_update,
                name="matrix_cache_slow_update",
            )
            await asyncio.wait_for(update_started.wait(), timeout=1.0)
            waiter = asyncio.create_task(coordinator.wait_for_room_idle("!room:localhost"))
            await asyncio.sleep(0)
            allow_update_finish.set()
            await queued_task
            await waiter
        finally:
            await coordinator.close()

        idle_wait_calls = [
            call for call in timing_logger.info.call_args_list if call.args == ("Event cache idle wait timing",)
        ]
        assert any(
            call.kwargs["barrier_kind"] == "room"
            and call.kwargs["pending_task_count"] >= 1
            and call.kwargs["wait_ms"] >= 0.0
            for call in idle_wait_calls
        )

    @pytest.mark.asyncio
    async def test_wait_for_room_idle_logs_full_pending_chain_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Room-idle waits should report the full pending same-room backlog."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )
        first_started = asyncio.Event()
        allow_first_finish = asyncio.Event()
        second_started = asyncio.Event()
        allow_second_finish = asyncio.Event()

        async def first_update() -> None:
            first_started.set()
            await allow_first_finish.wait()

        async def second_update() -> None:
            second_started.set()
            await allow_second_finish.wait()

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
            waiter = asyncio.create_task(coordinator.wait_for_room_idle("!room:localhost"))
            await asyncio.sleep(0)
            allow_first_finish.set()
            await asyncio.wait_for(second_started.wait(), timeout=1.0)
            allow_second_finish.set()
            await first_task
            await second_task
            await waiter
        finally:
            await coordinator.close()

        timing_call = next(
            call for call in timing_logger.info.call_args_list if call.args == ("Event cache idle wait timing",)
        )
        assert timing_call.kwargs["pending_task_count"] == 2

    @pytest.mark.asyncio
    async def test_wait_for_room_idle_counts_tasks_queued_after_wait_starts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Room-idle waits should include same-room tasks queued after the wait begins."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )
        first_started = asyncio.Event()
        allow_first_finish = asyncio.Event()
        second_started = asyncio.Event()
        allow_second_finish = asyncio.Event()

        async def first_update() -> None:
            first_started.set()
            await allow_first_finish.wait()

        async def second_update() -> None:
            second_started.set()
            await allow_second_finish.wait()

        try:
            first_task = coordinator.queue_room_update(
                "!room:localhost",
                first_update,
                name="matrix_cache_first_update",
            )
            await asyncio.wait_for(first_started.wait(), timeout=1.0)
            waiter = asyncio.create_task(coordinator.wait_for_room_idle("!room:localhost"))
            await asyncio.sleep(0)
            second_task = coordinator.queue_room_update(
                "!room:localhost",
                second_update,
                name="matrix_cache_second_update",
            )
            await asyncio.sleep(0)
            allow_first_finish.set()
            await asyncio.wait_for(second_started.wait(), timeout=1.0)
            allow_second_finish.set()
            await first_task
            await second_task
            await waiter
        finally:
            await coordinator.close()

        timing_call = next(
            call for call in timing_logger.info.call_args_list if call.args == ("Event cache idle wait timing",)
        )
        assert timing_call.kwargs["pending_task_count"] == 2

    @pytest.mark.asyncio
    async def test_wait_for_room_idle_counts_full_backlog_when_queue_grows_mid_wait(  # noqa: PLR0915
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Room-idle waits should count both the initial backlog and tasks queued later."""
        monkeypatch.setenv("MINDROOM_TIMING", "1")
        timing_logger = MagicMock()
        monkeypatch.setattr(timing_module, "logger", timing_logger)
        coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=object(),
        )
        first_started = asyncio.Event()
        allow_first_finish = asyncio.Event()
        second_started = asyncio.Event()
        allow_second_finish = asyncio.Event()
        third_started = asyncio.Event()
        allow_third_finish = asyncio.Event()
        fourth_started = asyncio.Event()
        allow_fourth_finish = asyncio.Event()

        async def first_update() -> None:
            first_started.set()
            await allow_first_finish.wait()

        async def second_update() -> None:
            second_started.set()
            await allow_second_finish.wait()

        async def third_update() -> None:
            third_started.set()
            await allow_third_finish.wait()

        async def fourth_update() -> None:
            fourth_started.set()
            await allow_fourth_finish.wait()

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
            waiter = asyncio.create_task(coordinator.wait_for_room_idle("!room:localhost"))
            await asyncio.sleep(0)
            fourth_task = coordinator.queue_room_update(
                "!room:localhost",
                fourth_update,
                name="matrix_cache_fourth_update",
            )
            await asyncio.sleep(0)
            allow_first_finish.set()
            await asyncio.wait_for(second_started.wait(), timeout=1.0)
            allow_second_finish.set()
            await asyncio.wait_for(third_started.wait(), timeout=1.0)
            allow_third_finish.set()
            await asyncio.wait_for(fourth_started.wait(), timeout=1.0)
            allow_fourth_finish.set()
            await first_task
            await second_task
            await third_task
            await fourth_task
            await waiter
        finally:
            await coordinator.close()

        timing_call = next(
            call for call in timing_logger.info.call_args_list if call.args == ("Event cache idle wait timing",)
        )
        assert timing_call.kwargs["pending_task_count"] == 4

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
        coordinator = _EventCacheWriteCoordinator(
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
            call for call in timing_logger.info.call_args_list if call.args == ("Live event cache append timing",)
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
            call for call in timing_logger.info.call_args_list if call.args == ("Live event cache append timing",)
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
            ) -> asyncio.Task[object]:
                del room_id, name, log_exceptions
                return asyncio.create_task(update_coro_factory())

            def queue_thread_update(
                self,
                room_id: str,
                thread_id: str,
                update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
                *,
                name: str,
                log_exceptions: bool = True,
            ) -> asyncio.Task[object]:
                del thread_id
                return self.queue_room_update(
                    room_id,
                    update_coro_factory,
                    name=name,
                    log_exceptions=log_exceptions,
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
    async def test_guarded_thread_replace_skips_stale_prewarm_write_after_newer_live_update(
        self,
        tmp_path: Path,
    ) -> None:
        """A guarded prewarm write must not overwrite a newer thread snapshot written after the fetch began."""
        event_cache = _EventCache(tmp_path / "event_cache.db")
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
            await event_cache.replace_thread(
                room_id,
                thread_id,
                [new_root_event.source, new_reply_event.source],
                validated_at=prewarm_fetch_started_at + 1,
            )

            replaced = await event_cache.replace_thread_if_not_newer(
                room_id,
                thread_id,
                [old_root_event.source, old_reply_event.source],
                fetch_started_at=prewarm_fetch_started_at,
                validated_at=prewarm_fetch_started_at + 2,
            )
            cached_history = await event_cache.get_thread_events(room_id, thread_id)
        finally:
            await event_cache.close()

        assert replaced is False
        assert cached_history is not None
        assert [event["event_id"] for event in cached_history] == [thread_id, "$reply_new:localhost"]

    @pytest.mark.asyncio
    async def test_lookup_miss_sync_plain_edit_invalidates_room_cache_state(
        self,
        tmp_path: Path,
    ) -> None:
        """Plain sync edits with missing originals should invalidate cached room thread state."""
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
                "!test:localhost": MagicMock(timeline=MagicMock(events=[ambiguous_edit])),
            }
            access.cache_sync_timeline(sync_response)
            await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")
            cache_state = await event_cache.get_thread_cache_state("!test:localhost", "$thread_root:localhost")
        finally:
            await event_cache.close()

        assert cache_state is not None
        assert cache_state.room_invalidation_reason == "sync_thread_lookup_unavailable"
        assert cache_state.room_invalidated_at is not None

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
    async def test_get_thread_history_refresh_runs_under_same_thread_write_barrier(self) -> None:
        """Thread refreshes should serialize with same-thread mutations without blocking other threads."""
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

        access.runtime.event_cache_write_coordinator.queue_thread_update(
            "!test:localhost",
            "$thread:localhost",
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
    async def test_get_thread_snapshot_refresh_runs_under_same_thread_write_barrier(self) -> None:
        """Snapshot refreshes should serialize with same-thread mutations without blocking other threads."""
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

        access.runtime.event_cache_write_coordinator.queue_thread_update(
            "!test:localhost",
            "$thread:localhost",
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

        async def pause_reader(_room_id: str, _thread_id: str) -> None:
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
        access._reads.fetch_thread_history_from_client.assert_awaited_once_with(
            "!room:localhost",
            "$thread:localhost",
        )

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
        event_cache = _EventCache(tmp_path / "event_cache.db")
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
        await event_cache.replace_thread(
            room_id,
            thread_id,
            [root_event.source, old_reply.source],
            validated_at=time.time(),
        )

        real_get_thread_cache_state = event_cache.get_thread_cache_state
        real_mark_room_threads_stale = event_cache.mark_room_threads_stale
        real_queue_room_cache_update = access._live._cache_ops.queue_room_cache_update
        reader_ready = asyncio.Event()
        release_reader = asyncio.Event()
        room_update_queued = asyncio.Event()
        live_task: asyncio.Task[None] | None = None
        history: ThreadHistoryResult | None = None

        async def blocking_get_thread_cache_state(room_id_arg: str, thread_id_arg: str) -> ThreadCacheState | None:
            assert room_id_arg == room_id
            assert thread_id_arg == thread_id
            reader_ready.set()
            await release_reader.wait()
            return await real_get_thread_cache_state(room_id_arg, thread_id_arg)

        async def mark_room_threads_stale(room_id_arg: str, *, reason: str) -> None:
            assert room_id_arg == room_id
            assert reason == "live_thread_lookup_unavailable"
            await real_mark_room_threads_stale(room_id_arg, reason=reason)

        async def resolve_unknown_impact(*_args: object, **_kwargs: object) -> MutationThreadImpact:
            return MutationThreadImpact.unknown()

        def queue_room_cache_update(
            room_id_arg: str,
            update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
            *,
            name: str,
        ) -> asyncio.Task[object]:
            assert room_id_arg == room_id
            assert name == "matrix_cache_append_live_event"
            room_update_queued.set()
            return real_queue_room_cache_update(room_id_arg, update_coro_factory, name=name)

        event_cache.get_thread_cache_state = AsyncMock(side_effect=blocking_get_thread_cache_state)
        event_cache.mark_room_threads_stale = AsyncMock(side_effect=mark_room_threads_stale)
        access._live._resolver.resolve_thread_impact_for_mutation = AsyncMock(side_effect=resolve_unknown_impact)
        access._live._cache_ops.queue_room_cache_update = queue_room_cache_update
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
            await asyncio.wait_for(room_update_queued.wait(), timeout=1.0)

            release_reader.set()
            history = await asyncio.wait_for(read_task, timeout=1.0)
            await asyncio.wait_for(live_task, timeout=1.0)
            await coordinator.wait_for_room_idle(room_id)
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
            await coordinator.wait_for_room_idle(room_id)
            await event_cache.close()

        assert history is not None
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_HOMESERVER
        assert [message.body for message in history] == ["Root", "Old reply", "New reply"]
        client.room_messages.assert_awaited_once()

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

    @pytest.mark.asyncio
    async def test_get_thread_messages_routes_through_collapsed_read_primitive(self) -> None:
        """Thread reads should route through one internal primitive with explicit mode flags."""
        access = MatrixConversationCache(
            logger=MagicMock(),
            runtime=_conversation_runtime(),
        )
        expected = thread_history_result(
            [_message(event_id="$thread:localhost", body="Root")],
            is_full_history=True,
        )

        access._reads.read_thread = AsyncMock(return_value=expected)

        result = await access.get_thread_messages(
            "!test:localhost",
            "$thread:localhost",
            full_history=True,
            dispatch_safe=True,
        )

        assert result == expected
        access._reads.read_thread.assert_awaited_once_with(
            "!test:localhost",
            "$thread:localhost",
            full_history=True,
            dispatch_safe=True,
        )

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
    async def test_shared_event_cache_write_coordinator_allows_other_thread_updates_while_one_thread_runs(
        self,
    ) -> None:
        """Same-room thread updates should not serialize across unrelated threads."""
        first_update_started = asyncio.Event()
        release_first_update = asyncio.Event()
        second_update_started = asyncio.Event()
        owner = object()
        coordinator = _EventCacheWriteCoordinator(
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
        coordinator = _EventCacheWriteCoordinator(
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
        ) -> ThreadHistoryResult:
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
        await access.runtime.event_cache_write_coordinator.wait_for_room_idle("!test:localhost")

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
            await coordinator.wait_for_room_idle("!test:localhost")

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
            access._reads.fetch_thread_history_from_client.assert_awaited_once_with(
                "!test:localhost",
                "$thread-c:localhost",
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
        coordinator = _EventCacheWriteCoordinator(
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
        coordinator = _EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=owner,
        )

        async def failing_update() -> None:
            msg = "boom"
            raise RuntimeError(msg)

        with patch("mindroom.background_tasks.logger.exception") as background_logger_exception:
            with pytest.raises(RuntimeError, match="boom"):
                await coordinator.run_room_update(
                    "!test:localhost",
                    lambda: failing_update(),
                    name="matrix_cache_test_failure",
                )
            await asyncio.sleep(0)

        background_logger_exception.assert_not_called()

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
                "get_dispatch_thread_snapshot",
                AsyncMock(return_value=thread_history_result([], is_full_history=False)),
            ),
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", AsyncMock(return_value=[])),
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
    async def test_extract_context_edit_of_plain_root_message_stays_room_level(self, bot: AgentBot) -> None:
        """Edits of plain room-root messages should not be promoted into thread context."""
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
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$room_message:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Room message",
                        "msgtype": "m.text",
                    },
                    "event_id": "$room_message:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(
                return_value=thread_history_result(
                    [
                        _message(event_id="$room_message:localhost", body="Room message"),
                    ],
                    is_full_history=True,
                ),
            ),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$room_message:localhost")
        mock_fetch.assert_awaited_once_with(room.room_id, "$room_message:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_to_thread_reply_inherits_existing_thread(
        self,
        bot: AgentBot,
    ) -> None:
        """Plain replies to explicit thread messages should stay in that thread."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "follow-up from bridge",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                },
                "event_id": "$plain_reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root"),
            _message(event_id="$thread_msg:localhost", body="Thread message"),
        ]
        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value="$thread_root:localhost"),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(return_value=expected_history),
            ) as mock_fetch,
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        mock_lookup.assert_awaited_once_with(room.room_id, "$thread_msg:localhost")
        mock_fetch.assert_awaited_once_with(room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_to_thread_root_inherits_existing_thread(
        self,
        bot: AgentBot,
    ) -> None:
        """Plain replies to the explicit thread root should stay in that thread."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "follow-up from bridge",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_root:localhost"}},
                },
                "event_id": "$plain_reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Root message",
                        "msgtype": "m.text",
                    },
                    "event_id": "$thread_root:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            ),
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(return_value=None)

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root message"),
            _message(event_id="$thread_reply:localhost", body="Thread reply"),
        ]
        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(return_value=expected_history),
            ) as mock_fetch,
            patch.object(
                bot._conversation_cache,
                "get_thread_snapshot",
                AsyncMock(return_value=expected_history),
            ) as mock_snapshot,
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        bot.event_cache.get_thread_id_for_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        mock_snapshot.assert_not_called()
        assert mock_fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_chain_stays_threaded_transitively(
        self,
        bot: AgentBot,
    ) -> None:
        """A plain reply chain should stay threaded when it eventually reaches a threaded ancestor."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "second bridge reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain_reply_1:localhost"}},
                },
                "event_id": "$plain_reply_2:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "first bridge reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                        },
                        "event_id": "$plain_reply_1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567896,
                        "room_id": room.room_id,
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "thread reply",
                            "msgtype": "m.text",
                            "m.relates_to": {
                                "rel_type": "m.thread",
                                "event_id": "$thread_root:localhost",
                            },
                        },
                        "event_id": "$thread_msg:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": room.room_id,
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value=None),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(
                    return_value=[
                        _message(event_id="$thread_root:localhost", body="Root message"),
                        _message(event_id="$thread_msg:localhost", body="Thread reply"),
                        _message(event_id="$plain_reply_1:localhost", body="first bridge reply"),
                    ],
                ),
            ) as mock_fetch,
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert [message.event_id for message in context.thread_history] == [
            "$thread_root:localhost",
            "$thread_msg:localhost",
            "$plain_reply_1:localhost",
        ]
        assert mock_lookup.await_args_list == [
            call(room.room_id, "$plain_reply_1:localhost"),
            call(room.room_id, "$thread_msg:localhost"),
        ]
        assert bot.client.room_get_event.await_args_list == [
            call(room.room_id, "$plain_reply_1:localhost"),
            call(room.room_id, "$thread_msg:localhost"),
        ]
        mock_fetch.assert_awaited_once_with(room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_to_promoted_plain_reply_stays_threaded(
        self,
        bot: AgentBot,
    ) -> None:
        """A plain reply should inherit thread membership transitively through a promoted plain reply."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "second bridge reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain_reply_1:localhost"}},
                },
                "event_id": "$plain_reply_2:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "first bridge reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                    },
                    "event_id": "$plain_reply_1:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567896,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            ),
        )

        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value="$thread_root:localhost"),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(return_value=[_message(event_id="$thread_root:localhost", body="root")]),
            ) as mock_fetch,
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == [_message(event_id="$thread_root:localhost", body="root")]
        mock_lookup.assert_awaited_once_with(room.room_id, "$plain_reply_1:localhost")
        mock_fetch.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        bot.client.room_get_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_thread_root_uses_cached_root_mapping(self, bot: AgentBot) -> None:
        """Edits of a thread root should stay threaded once any child reply taught the cache that thread."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        real_event_cache = _EventCache(bot.storage_path / "root-edit-thread-cache.db")
        await real_event_cache.initialize()
        bot.event_cache = real_event_cache

        reply_event_source = {
            "content": {
                "body": "Reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
            },
            "event_id": "$reply:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567896,
            "room_id": "!test:localhost",
            "type": "m.room.message",
        }
        try:
            await bot.event_cache.store_events_batch(
                [("$reply:localhost", room.room_id, reply_event_source)],
            )

            event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "* updated root",
                        "msgtype": "m.text",
                        "m.new_content": {
                            "body": "updated root",
                            "msgtype": "m.text",
                        },
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$edit_event:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567897,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            )

            bot.client.room_get_event = AsyncMock(
                return_value=nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Root message",
                            "msgtype": "m.text",
                        },
                        "event_id": "$thread_root:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": room.room_id,
                        "type": "m.room.message",
                    },
                ),
            )

            expected_history = [
                _message(event_id="$thread_root:localhost", body="Root message"),
                _message(event_id="$reply:localhost", body="Reply"),
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
            bot.client.room_get_event.assert_not_awaited()
            mock_fetch.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        finally:
            await real_event_cache.close()

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_thread_root_refetches_when_thread_lookup_cache_is_cold(
        self,
        bot: AgentBot,
    ) -> None:
        """Edits of thread roots should stay threaded when authoritative history proves child replies exist."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated root",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated root",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Root message",
                        "msgtype": "m.text",
                    },
                    "event_id": "$thread_root:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            ),
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(return_value=None)

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root message"),
            _message(event_id="$reply:localhost", body="Reply"),
        ]
        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_snapshot",
                AsyncMock(),
            ) as mock_snapshot,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(return_value=expected_history),
            ) as mock_history,
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        bot.event_cache.get_thread_id_for_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        mock_snapshot.assert_not_called()
        assert mock_history.await_count == 2

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_promoted_plain_reply_refetches_thread_when_lookup_cache_is_cold(
        self,
        bot: AgentBot,
    ) -> None:
        """Edits of promoted plain replies should stay threaded without a warmed event-thread mapping."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* edited bridged reply",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "edited bridged reply",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$plain-reply:localhost"},
                },
                "event_id": "$edit-event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Bridged plain reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
                        },
                        "event_id": "$plain-reply:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": room.room_id,
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Thread reply",
                            "msgtype": "m.text",
                            "m.relates_to": {
                                "rel_type": "m.thread",
                                "event_id": "$thread-root:localhost",
                            },
                        },
                        "event_id": "$thread-reply:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": room.room_id,
                        "type": "m.room.message",
                    },
                ),
            ],
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(return_value=None)

        expected_history = [
            _message(event_id="$thread-root:localhost", body="Root"),
            _message(event_id="$thread-reply:localhost", body="Thread reply"),
            _message(event_id="$plain-reply:localhost", body="Bridged plain reply"),
        ]
        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(return_value=expected_history),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread-root:localhost"
        assert context.thread_history == expected_history
        assert bot.client.room_get_event.await_args_list[0].args == (room.room_id, "$plain-reply:localhost")
        assert bot.client.room_get_event.await_args_list[1].args == (room.room_id, "$thread-reply:localhost")
        mock_fetch.assert_awaited_once_with(room.room_id, "$thread-root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_plain_root_message_stays_room_level_when_snapshot_has_only_root(
        self,
        bot: AgentBot,
    ) -> None:
        """Root-edit fallback should require child events before treating a message as threaded."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated room message",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated room message",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$room_root:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Room root",
                        "msgtype": "m.text",
                    },
                    "event_id": "$room_root:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            ),
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(return_value=None)

        with patch.object(
            bot._conversation_cache,
            "get_thread_snapshot",
            AsyncMock(
                return_value=ThreadHistoryResult(
                    [_message(event_id="$room_root:localhost", body="Room root")],
                    is_full_history=False,
                ),
            ),
        ) as mock_snapshot:
            context = await bot._conversation_resolver.extract_message_context(room, event, full_history=False)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$room_root:localhost")
        bot.event_cache.get_thread_id_for_event.assert_awaited_once_with(room.room_id, "$room_root:localhost")
        mock_snapshot.assert_awaited_once_with(room.room_id, "$room_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_plain_root_message_degrades_when_thread_lookup_fails(
        self,
        bot: AgentBot,
    ) -> None:
        """Advisory thread-id lookup failures should not break plain edit context resolution."""
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
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$room_message:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Room message",
                        "msgtype": "m.text",
                    },
                    "event_id": "$room_message:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567896,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(side_effect=RuntimeError("sqlite boom"))

        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(
                return_value=thread_history_result(
                    [
                        _message(
                            event_id="$room_message:localhost",
                            body="Room message",
                        ),
                    ],
                    is_full_history=True,
                ),
            ),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$room_message:localhost")
        bot.event_cache.get_thread_id_for_event.assert_awaited_once_with(room.room_id, "$room_message:localhost")
        mock_fetch.assert_awaited_once_with(room.room_id, "$room_message:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_to_threaded_message_stays_threaded_transitively(
        self,
        bot: AgentBot,
    ) -> None:
        """Plain replies should inherit thread context transitively from earlier threaded messages."""
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

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Thread root"),
            _message(event_id="$thread_msg:localhost", body="Earlier threaded message"),
            _message(event_id="$plain1:localhost", body="First plain reply"),
            _message(event_id="$plain2:localhost", body="Second plain reply"),
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
        mock_fetch.assert_awaited_once_with(room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_explicit_thread_id_returns_none_for_cyclic_edit_chain(self, bot: AgentBot) -> None:
        """Cyclic edit chains should fail closed instead of raising from the shared resolver."""
        bot._conversation_resolver.deps.conversation_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        bot._conversation_resolver.deps.conversation_cache.get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "* a",
                            "msgtype": "m.text",
                            "m.new_content": {"body": "a", "msgtype": "m.text"},
                            "m.relates_to": {"rel_type": "m.replace", "event_id": "$edit-b:localhost"},
                        },
                        "event_id": "$edit-a:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "* b",
                            "msgtype": "m.text",
                            "m.new_content": {"body": "b", "msgtype": "m.text"},
                            "m.relates_to": {"rel_type": "m.replace", "event_id": "$edit-a:localhost"},
                        },
                        "event_id": "$edit-b:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 2,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )
        event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "* incoming",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "incoming", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$edit-a:localhost"},
                },
                "event_id": "$incoming-edit:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 3,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        thread_id = await bot._conversation_resolver._explicit_thread_id_for_event(
            "!test:localhost",
            "$incoming-edit:localhost",
            event_info,
            full_history=False,
            dispatch_safe=False,
        )

        assert thread_id is None

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_plain_reply_inherits_thread_and_marks_full_history_required(
        self,
        bot: AgentBot,
    ) -> None:
        """Dispatch preview should inherit an existing explicit thread across plain replies."""
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
        bot.client.room_get_event = AsyncMock()

        preview_snapshot = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Root"),
                _message(event_id="$thread_msg:localhost", body="Earlier threaded message"),
                _message(event_id="$plain1:localhost", body="Preview plain reply [Message continues in attached file]"),
            ],
            is_full_history=False,
        )
        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value="$thread_root:localhost"),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_snapshot",
                new=AsyncMock(return_value=preview_snapshot),
            ) as mock_snapshot,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(),
            ) as mock_fetch,
        ):
            preview_context = await bot._conversation_resolver.extract_dispatch_context(room, event)

            assert preview_context.is_thread is True
            assert preview_context.thread_id == "$thread_root:localhost"
            assert [message.event_id for message in preview_context.thread_history] == [
                "$thread_root:localhost",
                "$thread_msg:localhost",
                "$plain1:localhost",
            ]
            assert preview_context.requires_full_thread_history is True
            bot.client.download.assert_not_awaited()
            bot.client.room_get_event.assert_not_awaited()
            mock_lookup.assert_awaited_once_with(room.room_id, "$plain1:localhost")
            mock_snapshot.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
            mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_routes_preview_reads_through_single_cache_entrypoint(
        self,
        bot: AgentBot,
    ) -> None:
        """Dispatch preview resolution should select read mode through one cache helper."""
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.name = "Test Room"
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain1:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        preview_snapshot = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Root"),
                _message(event_id="$plain1:localhost", body="Preview"),
            ],
            is_full_history=False,
        )

        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value="$thread_root:localhost"),
            ),
            patch.object(
                bot._conversation_cache,
                "get_thread_messages",
                AsyncMock(return_value=preview_snapshot),
                create=True,
            ) as mock_read,
        ):
            context = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.requires_full_thread_history is True
        mock_read.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
            full_history=False,
            dispatch_safe=True,
        )

    @pytest.mark.asyncio
    async def test_full_history_thread_resolution_uses_full_history_to_prove_root(
        self,
        bot: AgentBot,
    ) -> None:
        """Full-history resolution should use full history, not partial snapshots, to prove a root thread exists."""
        room_id = "!test:localhost"
        incoming_event_id = "$incoming:localhost"
        event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "Newest plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_root:localhost"}},
                },
                "event_id": incoming_event_id,
                "sender": "@user:localhost",
                "origin_server_ts": 3,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )
        thread_history = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Root"),
                _message(event_id="$thread_reply:localhost", body="Thread reply"),
            ],
            is_full_history=True,
        )

        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value=None),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_event",
                AsyncMock(
                    return_value=nio.RoomGetEventResponse.from_dict(
                        {
                            "content": {
                                "body": "Root",
                                "msgtype": "m.text",
                            },
                            "event_id": "$thread_root:localhost",
                            "sender": "@user:localhost",
                            "origin_server_ts": 1,
                            "room_id": room_id,
                            "type": "m.room.message",
                        },
                    ),
                ),
            ) as mock_get_event,
            patch.object(
                bot._conversation_cache,
                "get_thread_snapshot",
                AsyncMock(side_effect=AssertionError("snapshot should not be used for full-history proof")),
            ) as mock_snapshot,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(return_value=thread_history),
            ) as mock_history,
        ):
            (
                is_thread,
                thread_id,
                history,
                requires_full_thread_history,
            ) = await bot._conversation_resolver._resolve_thread_context(
                room_id,
                incoming_event_id,
                event_info,
                full_history=True,
                dispatch_safe=False,
            )

        assert is_thread is True
        assert thread_id == "$thread_root:localhost"
        assert [message.event_id for message in history] == [
            "$thread_root:localhost",
            "$thread_reply:localhost",
        ]
        assert requires_full_thread_history is False
        mock_lookup.assert_awaited_once_with(room_id, "$thread_root:localhost")
        mock_get_event.assert_awaited_once_with(room_id, "$thread_root:localhost")
        assert mock_history.await_count == 2
        mock_snapshot.assert_not_called()

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
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_snapshot",
                    AsyncMock(return_value=thread_history_result([], is_full_history=False)),
                ),
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history",
                    AsyncMock(return_value=[]),
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
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_snapshot",
                    AsyncMock(return_value=thread_history_result([], is_full_history=False)),
                ),
                patch(
                    "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history",
                    AsyncMock(return_value=[]),
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
    async def test_command_reply_to_thread_message_stays_in_thread_transitively(
        self,
        tmp_path: Path,
    ) -> None:
        """Plain command replies to threaded messages should stay in the inherited thread."""
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
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history",
                AsyncMock(return_value=[]),
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_snapshot",
                AsyncMock(return_value=thread_history_result([], is_full_history=False)),
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_thread_id_for_event",
                AsyncMock(return_value="$thread_root:localhost"),
            ),
        ):
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
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_snapshot",
                AsyncMock(return_value=thread_history_result([], is_full_history=False)),
            ),
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history",
                AsyncMock(return_value=[]),
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
