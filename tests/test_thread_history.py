"""Tests for thread history fetching, especially including thread root messages."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import aiohttp
import nio
import pytest
from nio.api import RelationshipType
from nio.responses import RoomThreadsError, RoomThreadsResponse

import mindroom.matrix.client_thread_history as matrix_client_module
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import STREAM_WARMUP_SUFFIX_KEY
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.cache.event_cache import ThreadCacheState
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_history_result import (
    THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC,
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_CACHE,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
)
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.client import ResolvedVisibleMessage, RoomThreadsPageError, get_room_threads_page
from mindroom.matrix.client_delivery import build_threaded_edit_content as _build_threaded_edit_content_impl
from mindroom.matrix.client_thread_history import (
    _event_source_for_cache,
    _fetch_thread_history_via_room_messages_with_events,
    _resolve_scanned_thread_message_sources,
    _resolve_thread_history_from_event_sources_timed,
)
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.thread_projection import ordered_event_ids_from_scanned_event_sources
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def _event_cache() -> AsyncMock:
    return make_event_cache_mock()


async def fetch_thread_history(*args: object, **kwargs: object) -> ThreadHistoryResult:
    """Inject a concrete event cache for test-local calls into the real helper."""
    kwargs.setdefault("event_cache", _event_cache())
    return await matrix_client_module.fetch_thread_history(*args, **kwargs)


async def fetch_thread_snapshot(*args: object, **kwargs: object) -> ThreadHistoryResult:
    """Inject a concrete event cache for test-local snapshot helpers."""
    kwargs.setdefault("event_cache", _event_cache())
    return await matrix_client_module.fetch_thread_snapshot(*args, **kwargs)


def build_threaded_edit_content(*args: object, **kwargs: object) -> dict[str, object]:
    """Call the real threaded edit-content helper directly."""
    return _build_threaded_edit_content_impl(*args, **kwargs)


class TestThreadHistory:
    """Test thread history fetching functionality."""

    @staticmethod
    def _make_text_event(
        *,
        event_id: str,
        sender: str,
        body: str,
        server_timestamp: int,
        source_content: dict,
    ) -> MagicMock:
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = event_id
        event.sender = sender
        event.body = body
        event.server_timestamp = server_timestamp
        normalized_content = dict(source_content)
        normalized_content.setdefault("msgtype", "m.text")
        event.source = {
            "type": "m.room.message",
            "content": normalized_content,
        }
        return event

    @staticmethod
    def _make_notice_event(
        *,
        event_id: str,
        sender: str,
        body: str,
        server_timestamp: int,
        source_content: dict,
    ) -> MagicMock:
        event = MagicMock(spec=nio.RoomMessageNotice)
        event.event_id = event_id
        event.sender = sender
        event.body = body
        event.server_timestamp = server_timestamp
        event.source = {
            "type": "m.room.message",
            "content": source_content,
        }
        return event

    @staticmethod
    def _make_audio_event(
        *,
        event_id: str,
        sender: str,
        body: str,
        server_timestamp: int,
        source_content: dict,
    ) -> MagicMock:
        event = MagicMock(spec=nio.RoomMessageAudio)
        event.event_id = event_id
        event.sender = sender
        event.body = body
        event.server_timestamp = server_timestamp
        normalized_content = dict(source_content)
        normalized_content.setdefault("msgtype", "m.audio")
        normalized_content.setdefault("body", body)
        event.source = {
            "type": "m.room.message",
            "content": normalized_content,
        }
        return event

    @staticmethod
    def _make_room_get_event_response(event: nio.Event) -> MagicMock:
        response = MagicMock(spec=nio.RoomGetEventResponse)
        response.event = event
        return response

    @staticmethod
    def _relation_key(
        event_id: str,
        rel_type: RelationshipType,
        *,
        event_type: str = "m.room.message",
        direction: nio.MessageDirection = nio.MessageDirection.back,
        limit: int | None = None,
    ) -> tuple[str, RelationshipType, str, nio.MessageDirection, int | None]:
        return (event_id, rel_type, event_type, direction, limit)

    @classmethod
    def _make_relations_client(
        cls,
        *,
        root_event: nio.Event,
        relations: dict[
            tuple[str, RelationshipType, str, nio.MessageDirection, int | None],
            Iterable[nio.Event] | Exception,
        ],
    ) -> MagicMock:
        client = MagicMock()
        client.room_get_event = AsyncMock(return_value=cls._make_room_get_event_response(root_event))

        def room_get_event_relations(
            _room_id: str,
            event_id: str,
            rel_type: RelationshipType | None = None,
            event_type: str | None = None,
            *,
            direction: nio.MessageDirection = nio.MessageDirection.back,
            limit: int | None = None,
        ) -> object:
            assert rel_type is not None
            assert event_type is not None
            key = (event_id, rel_type, event_type, direction, limit)
            fallback_key = (event_id, rel_type, event_type, direction, None)
            value = relations.get(key, relations.get(fallback_key, []))

            async def iterator() -> object:
                if isinstance(value, Exception):
                    raise value
                for event in value:
                    yield event

            return iterator()

        client.room_get_event_relations = MagicMock(side_effect=room_get_event_relations)
        room_scan_chunk: list[nio.Event] = [root_event]
        seen_event_ids = {root_event.event_id}
        for value in relations.values():
            if isinstance(value, Exception):
                continue
            for event in value:
                event_id = event.event_id
                if event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)
                room_scan_chunk.insert(-1, event)
        client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(
                room_id="!room:localhost",
                chunk=room_scan_chunk,
                start="",
                end=None,
            ),
        )
        return client

    @pytest.mark.asyncio
    async def test_fetch_thread_history_delegates_to_room_scan_helper(self) -> None:
        """Thread fetches should use the single room-scan helper path."""
        client = AsyncMock()
        event_cache = make_event_cache_mock()
        expected_history = [{"event_id": "$thread_root", "body": "root"}]

        with (
            patch(
                "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
                new=AsyncMock(
                    return_value=MagicMock(
                        history=expected_history,
                        event_sources=[{"event_id": "$thread_root"}],
                        resolution_ms=0.0,
                        sidecar_hydration_ms=0.0,
                    ),
                ),
            ) as mock_fallback,
            patch("mindroom.matrix.client_thread_history._store_thread_history_cache", new=AsyncMock()) as mock_store,
        ):
            history = await fetch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=event_cache,
            )

        assert history == expected_history
        mock_fallback.assert_awaited_once_with(
            client,
            "!room:localhost",
            "$thread_root",
            hydrate_sidecars=True,
            event_cache=event_cache,
            trusted_sender_ids=(),
        )
        mock_store.assert_awaited_once_with(
            event_cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            event_sources=[{"event_id": "$thread_root"}],
            fetch_started_at=ANY,
        )

    @pytest.mark.asyncio
    async def test_fetch_thread_history_uses_room_scan_instead_of_relations_fast_path(self) -> None:
        """Thread history should use the room-scan path so promoted descendants stay in the thread."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        thread_event = self._make_text_event(
            event_id="$thread_reply",
            sender="@agent:localhost",
            body="Reply in thread",
            server_timestamp=2000,
            source_content={
                "body": "Reply in thread",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        plain_reply = self._make_text_event(
            event_id="$plain_reply",
            sender="@bridge:localhost",
            body="Bridged reply",
            server_timestamp=3000,
            source_content={
                "body": "Bridged reply",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_reply"}},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.thread): [
                    thread_event,
                ],
            },
        )
        page = MagicMock(spec=nio.RoomMessagesResponse)
        page.chunk = [plain_reply, thread_event, root_event]
        page.end = None
        client.room_messages = AsyncMock(return_value=page)

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [message.event_id for message in history] == ["$thread_root", "$thread_reply", "$plain_reply"]
        assert history[0].body == "Root message"
        assert history[1].body == "Reply in thread"
        assert history[2].body == "Bridged reply"
        client.room_messages.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_thread_history_uses_bundled_root_edit_without_replacement_lookup(self) -> None:
        """Bundled replacement data should update the root without extra fetches."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Original root",
            server_timestamp=1000,
            source_content={
                "body": "Original root",
            },
        )
        root_event.source["unsigned"] = {
            "m.relations": {
                "m.replace": {
                    "event_id": "$root_edit",
                    "sender": "@user:localhost",
                    "origin_server_ts": 3000,
                    "type": "m.room.message",
                    "content": {
                        "body": "* Updated root",
                        "msgtype": "m.text",
                        "m.new_content": {"body": "Updated root", "msgtype": "m.text"},
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_root"},
                    },
                },
            },
        }
        thread_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Reply in thread",
            server_timestamp=2000,
            source_content={
                "body": "Reply in thread",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.thread): [thread_event],
                self._relation_key("$reply", RelationshipType.replacement): [],
            },
        )

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert history[0].event_id == "$thread_root"
        assert history[0].body == "Updated root"
        client.room_messages.assert_awaited_once()
        client.room_get_event_relations.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_thread_history_uses_nested_bundled_root_edit_without_validation_noise(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nested bundled replacement payloads should not be parsed through the outer wrapper."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Original root",
            server_timestamp=1000,
            source_content={
                "body": "Original root",
            },
        )
        root_event.source["unsigned"] = {
            "m.relations": {
                "m.replace": {
                    "latest_event": {
                        "event_id": "$root_edit",
                        "sender": "@user:localhost",
                        "origin_server_ts": 3000,
                        "type": "m.room.message",
                        "content": {
                            "body": "* Updated root",
                            "msgtype": "m.text",
                            "m.new_content": {"body": "Updated root", "msgtype": "m.text"},
                            "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_root"},
                        },
                    },
                },
            },
        }
        thread_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Reply in thread",
            server_timestamp=2000,
            source_content={
                "body": "Reply in thread",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.thread): [thread_event],
                self._relation_key("$reply", RelationshipType.replacement): [],
            },
        )

        with caplog.at_level(logging.WARNING, logger="nio.events.misc"):
            history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert history[0].event_id == "$thread_root"
        assert history[0].body == "Updated root"
        assert not any("Error validating event" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_fetch_thread_history_applies_reply_edits_and_stream_status_without_cached_latest_edit(self) -> None:
        """Thread history should keep latest edits from the homeserver even with a cold latest-edit cache."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        thread_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Thinking...",
            server_timestamp=2000,
            source_content={
                "body": "Thinking...",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                "io.mindroom.stream_status": "pending",
            },
        )
        newer_edit = self._make_text_event(
            event_id="$edit_b",
            sender="@agent:localhost",
            body="* final",
            server_timestamp=3000,
            source_content={
                "body": "* final",
                "m.new_content": {
                    "body": "Final answer",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                    "io.mindroom.stream_status": "completed",
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.thread): [thread_event],
            },
        )
        page = MagicMock(spec=nio.RoomMessagesResponse)
        page.chunk = [newer_edit, thread_event, root_event]
        page.end = None
        client.room_messages = AsyncMock(return_value=page)
        event_cache = make_event_cache_mock()
        event_cache.get_latest_edit.return_value = None

        history = await fetch_thread_history(
            client,
            "!room:localhost",
            "$thread_root",
            event_cache=event_cache,
        )

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history[1].body == "Final answer"
        assert history[1].content["body"] == "Final answer"
        assert history[1].stream_status == "completed"
        client.room_messages.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_thread_history_applies_trusted_canonical_body_from_internal_edit(self) -> None:
        """Thread history should honor trusted visible-body metadata when applying latest edits."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        thread_event = self._make_text_event(
            event_id="$reply",
            sender="@mindroom_general:localhost",
            body="Thinking...",
            server_timestamp=2000,
            source_content={
                "body": "Thinking...",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                "io.mindroom.stream_status": "pending",
            },
        )
        newer_edit = self._make_text_event(
            event_id="$edit_b",
            sender="@mindroom_general:localhost",
            body="* hello",
            server_timestamp=3000,
            source_content={
                "body": "* hello",
                "m.new_content": {
                    "body": "hello\n\n⏳ Preparing isolated worker...",
                    "io.mindroom.visible_body": "hello",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                    "io.mindroom.stream_status": "completed",
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
            },
        )
        page = MagicMock(spec=nio.RoomMessagesResponse)
        page.chunk = [newer_edit, thread_event, root_event]
        page.end = None
        client = MagicMock()
        client.room_messages = AsyncMock(return_value=page)
        event_cache = make_event_cache_mock()
        event_cache.get_latest_edit.return_value = None

        history = await fetch_thread_history(
            client,
            "!room:localhost",
            "$thread_root",
            event_cache=event_cache,
            trusted_sender_ids={"@mindroom_general:localhost"},
        )

        assert history[1].body == "hello"
        assert history[1].content["body"] == "hello"
        assert history[1].stream_status == "completed"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_includes_notice_reply(self) -> None:
        """Thread history should keep notice messages in thread history."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"msgtype": "m.text", "body": "Root message"},
        )
        notice_event = self._make_notice_event(
            event_id="$notice_reply",
            sender="@mindroom:localhost",
            body="Compacted 12 messages",
            server_timestamp=2000,
            source_content={
                "msgtype": "m.notice",
                "body": "Compacted 12 messages",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.thread): [notice_event],
            },
        )

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [message.event_id for message in history] == ["$thread_root", "$notice_reply"]
        assert "msgtype" not in history[0].to_dict()
        client.room_messages.assert_awaited_once()
        assert history[1].body == "Compacted 12 messages"
        assert history[1].to_dict()["msgtype"] == "m.notice"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_relations_path_includes_notice_root(self) -> None:
        """Relations-first fetch should keep a notice thread root."""
        root_event = self._make_notice_event(
            event_id="$thread_root",
            sender="@mindroom:localhost",
            body="Compacted summary",
            server_timestamp=1000,
            source_content={"msgtype": "m.notice", "body": "Compacted summary"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@user:localhost",
            body="thanks",
            server_timestamp=2000,
            source_content={
                "msgtype": "m.text",
                "body": "thanks",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.thread): [reply_event],
            },
        )

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history[0].to_dict()["msgtype"] == "m.notice"
        assert history[0].body == "Compacted summary"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_skips_cache_store_for_degraded_room_scan_result(self) -> None:
        """A degraded room-scan refill should not be persisted as a healed thread cache entry."""
        client = AsyncMock()
        fallback_history = [
            ResolvedVisibleMessage.synthetic(
                sender="@user:localhost",
                body="fallback",
                event_id="$thread_root",
                content={"body": "fallback"},
            ),
        ]

        with (
            patch(
                "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
                new=AsyncMock(
                    return_value=MagicMock(
                        history=fallback_history,
                        event_sources=[],
                        resolution_ms=0.0,
                        sidecar_hydration_ms=0.0,
                    ),
                ),
            ),
            patch("mindroom.matrix.client_thread_history._store_thread_history_cache", new=AsyncMock()) as mock_store,
        ):
            history = await fetch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=make_event_cache_mock(),
            )

        assert [message.event_id for message in history] == ["$thread_root"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_HOMESERVER
        mock_store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_thread_history_logs_cache_store_skip_for_missing_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Skipped homeserver refills should expose why the advisory cache was not repopulated."""
        client = AsyncMock()
        logger = MagicMock()
        fallback_history = [
            ResolvedVisibleMessage.synthetic(
                sender="@user:localhost",
                body="reply",
                event_id="$reply",
                content={"body": "reply"},
            ),
        ]
        monkeypatch.setattr(matrix_client_module, "logger", logger)

        with (
            patch(
                "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
                new=AsyncMock(
                    return_value=MagicMock(
                        history=fallback_history,
                        event_sources=[{"event_id": "$reply"}],
                        fetch_ms=10.0,
                        room_scan_pages=73,
                        scanned_event_count=7300,
                        resolution_ms=0.0,
                        sidecar_hydration_ms=0.0,
                    ),
                ),
            ),
            patch("mindroom.matrix.client_thread_history._store_thread_history_cache", new=AsyncMock()) as mock_store,
        ):
            await fetch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=make_event_cache_mock(),
            )

        mock_store.assert_not_awaited()
        logger.info.assert_any_call(
            "Thread history cache store skipped",
            room_id="!room:localhost",
            thread_id="$thread_root",
            cache_store_skipped_reason="missing_thread_root",
            has_thread_root=False,
            event_count=1,
            history_event_count=1,
            homeserver_scan_pages=73,
            homeserver_scanned_event_count=7300,
            homeserver_thread_event_count=1,
        )

    @pytest.mark.asyncio
    async def test_fetch_thread_snapshot_miss_uses_authoritative_refresh_path(self) -> None:
        """Snapshot misses should reuse the authoritative refresh path instead of a separate fast path."""
        refreshed_history = ThreadHistoryResult(
            [
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="refreshed",
                    event_id="$thread_root",
                    content={"body": "refreshed"},
                ),
            ],
            is_full_history=False,
        )
        client = AsyncMock()

        with patch(
            "mindroom.matrix.client_thread_history.refresh_thread_history_from_source",
            new=AsyncMock(return_value=refreshed_history),
        ) as mock_refresh:
            snapshot = await fetch_thread_snapshot(client, "!room:localhost", "$thread_root")

        assert isinstance(snapshot, ThreadHistoryResult)
        assert snapshot.is_full_history is False
        assert [message.event_id for message in snapshot] == ["$thread_root"]
        assert snapshot[0].body == "refreshed"
        mock_refresh.assert_awaited_once_with(
            client,
            "!room:localhost",
            "$thread_root",
            ANY,
            hydrate_sidecars=False,
            allow_stale_fallback=True,
            cache_write_guard_started_at=None,
            cache_reject_diagnostics={
                THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: "no_cache_state",
            },
            trusted_sender_ids=(),
            caller_label="unknown",
            coordinator_queue_wait_ms=0.0,
        )

    @pytest.mark.asyncio
    async def test_fetch_thread_snapshot_miss_preserves_authoritative_non_text_children(self) -> None:
        """Snapshot misses should accept authoritative histories with non-text thread children."""
        refreshed_history = ThreadHistoryResult(
            [
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="voice-note.ogg",
                    event_id="$thread_root",
                    timestamp=1000,
                    content={"body": "voice-note.ogg", "msgtype": "m.audio"},
                ),
                ResolvedVisibleMessage.synthetic(
                    sender="@agent:localhost",
                    body="agent-reply.ogg",
                    event_id="$reply_audio",
                    timestamp=2000,
                    content={"body": "agent-reply.ogg", "msgtype": "m.audio"},
                    thread_id="$thread_root",
                ),
            ],
            is_full_history=False,
        )
        client = AsyncMock()

        with patch(
            "mindroom.matrix.client_thread_history.refresh_thread_history_from_source",
            new=AsyncMock(return_value=refreshed_history),
        ) as mock_refresh:
            snapshot = await fetch_thread_snapshot(client, "!room:localhost", "$thread_root")

        assert isinstance(snapshot, ThreadHistoryResult)
        assert snapshot.is_full_history is False
        assert [message.event_id for message in snapshot] == ["$thread_root", "$reply_audio"]
        assert snapshot[1].content["msgtype"] == "m.audio"
        mock_refresh.assert_awaited_once_with(
            client,
            "!room:localhost",
            "$thread_root",
            ANY,
            hydrate_sidecars=False,
            allow_stale_fallback=True,
            cache_write_guard_started_at=None,
            cache_reject_diagnostics={
                THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: "no_cache_state",
            },
            trusted_sender_ids=(),
            caller_label="unknown",
            coordinator_queue_wait_ms=0.0,
        )

    @pytest.mark.asyncio
    async def test_build_threaded_edit_content_uses_latest_thread_event_id_for_fallback(self) -> None:
        """Threaded edits should preserve MSC3440 fallback semantics through the latest visible event."""
        with patch(
            "mindroom.matrix.client_delivery.format_message_with_mentions",
            return_value={"body": "edited"},
        ) as mock_format:
            content = build_threaded_edit_content(
                new_text="edited",
                thread_id="$thread_root",
                config=MagicMock(),
                runtime_paths=MagicMock(),
                sender_domain="localhost",
                latest_thread_event_id="$latest",
            )

        assert content == {"body": "edited"}
        assert mock_format.call_args.kwargs["latest_thread_event_id"] == "$latest"

    def test_build_threaded_edit_content_requires_latest_thread_event_id_for_threads(self) -> None:
        """Threaded edit content should require caller-owned fallback resolution."""
        with pytest.raises(ValueError, match="latest_thread_event_id is required for thread fallback"):
            build_threaded_edit_content(
                new_text="edited",
                thread_id="$thread_root",
                config=MagicMock(),
                runtime_paths=MagicMock(),
                sender_domain="localhost",
            )

    @pytest.mark.asyncio
    async def test_fetch_thread_history_includes_root_message(self) -> None:
        """Test that fetch_thread_history includes the thread root message itself."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="look up Feynman on Wikipedia",
            server_timestamp=1000,
            source_content={"body": "look up Feynman on Wikipedia"},
        )
        router_event = self._make_text_event(
            event_id="$router_msg",
            sender="@mindroom_news:localhost",
            body="@mindroom_research:localhost could you help with this?",
            server_timestamp=2000,
            source_content={
                "body": "@mindroom_research:localhost could you help with this?",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )

        # Mock response
        mock_response = MagicMock(spec=nio.RoomMessagesResponse)
        mock_response.chunk = [router_event, root_event]  # Order doesn't matter, will be sorted
        mock_response.end = None  # No more messages

        client.room_messages.return_value = mock_response

        # Fetch thread history
        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        # Verify both messages are included
        assert len(history) == 2

        # Verify they're in chronological order (root first, then router)
        assert history[0].event_id == "$thread_root"
        assert history[0].body == "look up Feynman on Wikipedia"
        assert history[0].sender == "@user:localhost"

        assert history[1].event_id == "$router_msg"
        assert history[1].body == "@mindroom_research:localhost could you help with this?"
        assert history[1].sender == "@mindroom_news:localhost"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_only_thread_messages(self) -> None:
        """Test that fetch_thread_history only includes messages from the specific thread."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread1",
            sender="@user:localhost",
            body="First thread",
            server_timestamp=1000,
            source_content={"body": "First thread"},
        )
        thread1_msg = self._make_text_event(
            event_id="$msg1",
            sender="@agent:localhost",
            body="Reply in thread 1",
            server_timestamp=2000,
            source_content={
                "body": "Reply in thread 1",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread1",
                },
            },
        )
        other_thread_msg = self._make_text_event(
            event_id="$msg2",
            sender="@agent:localhost",
            body="Reply in different thread",
            server_timestamp=3000,
            source_content={
                "body": "Reply in different thread",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread2",
                },
            },
        )
        room_msg = self._make_text_event(
            event_id="$room_msg",
            sender="@user:localhost",
            body="Regular room message",
            server_timestamp=4000,
            source_content={"body": "Regular room message"},
        )

        # Mock response with all messages
        mock_response = MagicMock(spec=nio.RoomMessagesResponse)
        mock_response.chunk = [other_thread_msg, thread1_msg, room_msg, root_event]
        mock_response.end = None

        client.room_messages.return_value = mock_response

        # Fetch thread history for thread1
        history = await fetch_thread_history(client, "!room:localhost", "$thread1")

        # Should only include thread1 messages
        assert len(history) == 2
        assert history[0].event_id == "$thread1"
        assert history[1].event_id == "$msg1"

        # Should not include other thread or room messages
        event_ids = [msg.event_id for msg in history]
        assert "$msg2" not in event_ids
        assert "$room_msg" not in event_ids

    @pytest.mark.asyncio
    async def test_fetch_thread_history_empty_thread(self) -> None:
        """Test fetch_thread_history with a thread that has no replies yet."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="New thread",
            server_timestamp=1000,
            source_content={"body": "New thread"},
        )

        # Mock response
        mock_response = MagicMock(spec=nio.RoomMessagesResponse)
        mock_response.chunk = [root_event]
        mock_response.end = None

        client.room_messages.return_value = mock_response

        # Fetch thread history
        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        # Should only include the root message
        assert len(history) == 1
        assert history[0].event_id == "$thread_root"
        assert history[0].body == "New thread"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_applies_edits(self) -> None:
        """Thread history should show edited body/content for thread messages."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )
        thread_message = self._make_text_event(
            event_id="$agent_msg",
            sender="@agent:localhost",
            body="Thinking...",
            server_timestamp=2000,
            source_content={
                "body": "Thinking...",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )
        edit_event = self._make_text_event(
            event_id="$edit1",
            sender="@agent:localhost",
            body="* Thinking...",
            server_timestamp=3000,
            source_content={
                "body": "* Thinking...",
                "m.new_content": {
                    "body": "Final answer",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread_root",
                    },
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$agent_msg",
                },
            },
        )

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [edit_event, thread_message, root_event]
        response.end = None
        client.room_messages.return_value = response

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [msg.event_id for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[1].body == "Final answer"
        assert history[1].content["body"] == "Final answer"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_applies_v2_sidecar_edits(self) -> None:
        """Thread history should hydrate canonical edit content from v2 sidecars."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )
        thread_message = self._make_text_event(
            event_id="$agent_msg",
            sender="@agent:localhost",
            body="Thinking...",
            server_timestamp=2000,
            source_content={
                "body": "Thinking...",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )
        edit_event = self._make_text_event(
            event_id="$edit1",
            sender="@agent:localhost",
            body="* Preview edit",
            server_timestamp=3000,
            source_content={
                "body": "* Preview edit",
                "m.new_content": {
                    "msgtype": "m.file",
                    "body": "Preview edit",
                    "info": {"mimetype": "application/json"},
                    "io.mindroom.long_text": {
                        "version": 2,
                        "encoding": "matrix_event_content_json",
                    },
                    "url": "mxc://server/thread-history-edit-sidecar",
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$agent_msg",
                },
            },
        )

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [edit_event, thread_message, root_event]
        response.end = None
        client.room_messages.return_value = response
        client.download = AsyncMock(
            return_value=MagicMock(
                spec=nio.DownloadResponse,
                body=json.dumps(
                    {
                        "msgtype": "m.text",
                        "body": "* Full edit body",
                        "m.new_content": {
                            "msgtype": "m.text",
                            "body": "Full final answer",
                            "m.relates_to": {
                                "rel_type": "m.thread",
                                "event_id": "$thread_root",
                            },
                            "io.mindroom.tool_trace": {
                                "version": 1,
                                "events": [{"tool": "shell"}],
                            },
                        },
                        "m.relates_to": {
                            "rel_type": "m.replace",
                            "event_id": "$agent_msg",
                        },
                    },
                ).encode("utf-8"),
            ),
        )

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [msg.event_id for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[1].body == "Full final answer"
        assert history[1].content["body"] == "Full final answer"
        assert history[1].content["io.mindroom.tool_trace"] == {
            "version": 1,
            "events": [{"tool": "shell"}],
        }

    @pytest.mark.asyncio
    async def test_fetch_thread_history_leaves_legacy_v1_edit_preview_untouched(self) -> None:
        """Unsupported v1 edit sidecars should keep preview body/content coherent."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )
        thread_message = self._make_text_event(
            event_id="$agent_msg",
            sender="@agent:localhost",
            body="Thinking...",
            server_timestamp=2000,
            source_content={
                "body": "Thinking...",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )
        edit_event = self._make_text_event(
            event_id="$edit1",
            sender="@agent:localhost",
            body="* Preview edit",
            server_timestamp=3000,
            source_content={
                "body": "* Preview edit",
                "m.new_content": {
                    "msgtype": "m.file",
                    "body": "Preview edit",
                    "io.mindroom.long_text": {
                        "version": 1,
                        "original_size": 100000,
                    },
                    "url": "mxc://server/legacy-edit-sidecar",
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$agent_msg",
                },
            },
        )

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [edit_event, thread_message, root_event]
        response.end = None
        client.room_messages.return_value = response
        client.download = AsyncMock()

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [msg.event_id for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[1].body == "Preview edit"
        assert history[1].content["body"] == "Preview edit"
        client.download.assert_not_called()

    @pytest.mark.asyncio
    async def test_room_message_scan_includes_notice_messages(self) -> None:
        """Room-message fallback should keep notice replies in thread history."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"msgtype": "m.text", "body": "root"},
        )
        notice_event = self._make_notice_event(
            event_id="$notice_reply",
            sender="@mindroom:localhost",
            body="Compacted 12 messages",
            server_timestamp=2000,
            source_content={
                "msgtype": "m.notice",
                "body": "Compacted 12 messages",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [notice_event, root_event]
        response.end = None
        client.room_messages.return_value = response

        history = (
            await _fetch_thread_history_via_room_messages_with_events(
                client,
                "!room:localhost",
                "$thread_root",
                hydrate_sidecars=True,
                event_cache=_event_cache(),
            )
        ).history
        serialized = [message.to_dict() for message in history]

        assert [msg["event_id"] for msg in serialized] == ["$thread_root", "$notice_reply"]
        assert serialized[1]["msgtype"] == "m.notice"

    @pytest.mark.asyncio
    async def test_notice_edit_event_sets_effective_msgtype_from_new_content(self) -> None:
        """Notice edit events should update the final msgtype from m.new_content."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"msgtype": "m.text", "body": "root"},
        )
        original_message = self._make_text_event(
            event_id="$agent_msg",
            sender="@mindroom:localhost",
            body="Initial text",
            server_timestamp=2000,
            source_content={
                "msgtype": "m.text",
                "body": "Initial text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        notice_edit = self._make_notice_event(
            event_id="$edit1",
            sender="@mindroom:localhost",
            body="* Compacted 12 messages",
            server_timestamp=3000,
            source_content={
                "msgtype": "m.notice",
                "body": "* Compacted 12 messages",
                "m.new_content": {
                    "msgtype": "m.notice",
                    "body": "Compacted 12 messages",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$agent_msg"},
            },
        )

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [notice_edit, original_message, root_event]
        response.end = None
        client.room_messages.return_value = response

        history = (
            await _fetch_thread_history_via_room_messages_with_events(
                client,
                "!room:localhost",
                "$thread_root",
                hydrate_sidecars=True,
                event_cache=_event_cache(),
            )
        ).history
        serialized = [message.to_dict() for message in history]

        assert [msg["event_id"] for msg in serialized] == ["$thread_root", "$agent_msg"]
        assert serialized[1]["body"] == "Compacted 12 messages"
        assert serialized[1]["content"]["msgtype"] == "m.notice"
        assert serialized[1]["msgtype"] == "m.notice"

    @pytest.mark.asyncio
    async def test_room_scan_includes_promoted_plain_reply_to_thread_message(self) -> None:
        """Cold room scans should keep plain replies whose direct target already belongs to the thread."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"msgtype": "m.text", "body": "root"},
        )
        thread_reply = self._make_text_event(
            event_id="$thread_reply",
            sender="@agent:localhost",
            body="explicit reply",
            server_timestamp=2000,
            source_content={
                "msgtype": "m.text",
                "body": "explicit reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        plain_reply = self._make_text_event(
            event_id="$plain_reply",
            sender="@bridge:localhost",
            body="bridged reply",
            server_timestamp=3000,
            source_content={
                "msgtype": "m.text",
                "body": "bridged reply",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_reply"}},
            },
        )

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [plain_reply, thread_reply, root_event]
        response.end = None
        client.room_messages.return_value = response

        history = (
            await _fetch_thread_history_via_room_messages_with_events(
                client,
                "!room:localhost",
                "$thread_root",
                hydrate_sidecars=True,
                event_cache=_event_cache(),
            )
        ).history

        assert [message.event_id for message in history] == [
            "$thread_root",
            "$thread_reply",
            "$plain_reply",
        ]

    @pytest.mark.asyncio
    async def test_room_scan_does_not_promote_plain_reply_to_non_thread_root(self) -> None:
        """Cold room scans must not treat arbitrary room replies as threaded."""
        resolved = await _resolve_scanned_thread_message_sources(
            room_id="!room:localhost",
            thread_id="$room_root",
            scanned_message_sources={
                "$room_root": {
                    "event_id": "$room_root",
                    "origin_server_ts": 1000,
                    "type": "m.room.message",
                    "content": {"msgtype": "m.text", "body": "root"},
                },
                "$plain_reply": {
                    "event_id": "$plain_reply",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.text",
                        "body": "plain reply",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$room_root"}},
                    },
                },
            },
        )

        assert list(resolved) == ["$room_root"]

    @pytest.mark.asyncio
    async def test_room_scan_revisits_inherited_replies_until_fixpoint(self) -> None:
        """Cold room scans should retain descendants even when they sort before their threaded parent."""
        resolved = await _resolve_scanned_thread_message_sources(
            room_id="!room:localhost",
            thread_id="$root",
            scanned_message_sources={
                "$root": {
                    "event_id": "$root",
                    "origin_server_ts": 1000,
                    "type": "m.room.message",
                    "content": {"msgtype": "m.text", "body": "root"},
                },
                "$z-parent": {
                    "event_id": "$z-parent",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.text",
                        "body": "parent",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
                    },
                },
                "$a-child": {
                    "event_id": "$a-child",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.text",
                        "body": "child",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$z-parent"}},
                    },
                },
            },
        )

        assert set(resolved) == {"$root", "$z-parent", "$a-child"}

    @pytest.mark.asyncio
    async def test_room_scan_promotes_transitive_plain_reply_chain(self) -> None:
        """Cold room scans should keep a plain-reply chain inside the same thread transitively."""
        resolved = await _resolve_scanned_thread_message_sources(
            room_id="!room:localhost",
            thread_id="$root",
            scanned_message_sources={
                "$root": {
                    "event_id": "$root",
                    "origin_server_ts": 1000,
                    "type": "m.room.message",
                    "content": {"msgtype": "m.text", "body": "root"},
                },
                "$thread_reply": {
                    "event_id": "$thread_reply",
                    "origin_server_ts": 1500,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.text",
                        "body": "thread reply",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
                    },
                },
                "$plain1": {
                    "event_id": "$plain1",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.text",
                        "body": "plain one",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_reply"}},
                    },
                },
                "$plain2": {
                    "event_id": "$plain2",
                    "origin_server_ts": 2500,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.text",
                        "body": "plain two",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$plain1"}},
                    },
                },
            },
        )

        assert list(resolved) == ["$root", "$thread_reply", "$plain1", "$plain2"]

    def test_ordered_event_ids_from_scanned_event_sources_preserves_input_order_on_timestamp_ties(self) -> None:
        """Scanned-source ordering should preserve first-seen order before falling back to event IDs."""
        ordered_event_ids = ordered_event_ids_from_scanned_event_sources(
            [
                {"event_id": "$zzz_parent", "origin_server_ts": 2000},
                {"event_id": "$aaa_child", "origin_server_ts": 2000},
                {"event_id": "$root", "origin_server_ts": 1000},
            ],
        )

        assert ordered_event_ids == ["$root", "$zzz_parent", "$aaa_child"]

    @pytest.mark.asyncio
    async def test_fetch_thread_history_keeps_same_timestamp_promoted_descendant(self) -> None:
        """Cold history reconstruction should keep promoted descendants even when event-id sort is non-causal."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"msgtype": "m.text", "body": "root"},
        )
        explicit_reply = self._make_text_event(
            event_id="$explicit",
            sender="@agent:localhost",
            body="explicit reply",
            server_timestamp=1500,
            source_content={
                "msgtype": "m.text",
                "body": "explicit reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
            },
        )
        plain_parent = self._make_text_event(
            event_id="$zzz_parent",
            sender="@bridge:localhost",
            body="bridged parent",
            server_timestamp=2000,
            source_content={
                "msgtype": "m.text",
                "body": "bridged parent",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$root"}},
            },
        )
        plain_child = self._make_text_event(
            event_id="$aaa_child",
            sender="@bridge:localhost",
            body="bridged child",
            server_timestamp=2000,
            source_content={
                "msgtype": "m.text",
                "body": "bridged child",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$zzz_parent"}},
            },
        )

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [plain_child, plain_parent, explicit_reply, root_event]
        response.end = None
        client.room_messages.return_value = response

        history = (
            await _fetch_thread_history_via_room_messages_with_events(
                client,
                "!room:localhost",
                "$root",
                hydrate_sidecars=True,
                event_cache=_event_cache(),
            )
        ).history

        event_ids = [message.event_id for message in history]
        assert event_ids == ["$root", "$explicit", "$zzz_parent", "$aaa_child"]

    @pytest.mark.asyncio
    async def test_resolve_thread_history_keeps_same_timestamp_reference_descendant_after_parent(self) -> None:
        """Same-timestamp reference descendants should sort after their related parent."""
        client = AsyncMock()

        history, _sidecar_ms = await _resolve_thread_history_from_event_sources_timed(
            client,
            room_id="!room:localhost",
            thread_id="$root",
            event_sources=[
                {
                    "event_id": "$root",
                    "origin_server_ts": 1000,
                    "type": "m.room.message",
                    "sender": "@user:localhost",
                    "content": {"msgtype": "m.text", "body": "root"},
                },
                {
                    "event_id": "$aaa_child",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "sender": "@bridge:localhost",
                    "content": {
                        "msgtype": "m.text",
                        "body": "reference child",
                        "m.relates_to": {"rel_type": "m.reference", "event_id": "$zzz_parent"},
                    },
                },
                {
                    "event_id": "$zzz_parent",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "sender": "@bridge:localhost",
                    "content": {
                        "msgtype": "m.text",
                        "body": "parent",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
                    },
                },
            ],
            hydrate_sidecars=True,
            event_cache=_event_cache(),
        )

        assert [message.event_id for message in history] == ["$root", "$zzz_parent", "$aaa_child"]

    @pytest.mark.asyncio
    async def test_fetch_thread_history_multiple_edits_keeps_latest(self) -> None:
        """When multiple edits exist, keep the latest one deterministically."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )
        thread_message = self._make_text_event(
            event_id="$agent_msg",
            sender="@agent:localhost",
            body="Thinking...",
            server_timestamp=2000,
            source_content={
                "body": "Thinking...",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )
        older_edit = self._make_text_event(
            event_id="$edit_a",
            sender="@agent:localhost",
            body="* partial",
            server_timestamp=3000,
            source_content={
                "body": "* partial",
                "m.new_content": {
                    "body": "Partial answer",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread_root",
                    },
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$agent_msg",
                },
            },
        )
        newer_edit_same_ts = self._make_text_event(
            event_id="$edit_b",
            sender="@agent:localhost",
            body="* final",
            server_timestamp=3000,
            source_content={
                "body": "* final",
                "m.new_content": {
                    "body": "Final answer",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread_root",
                    },
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$agent_msg",
                },
            },
        )

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [newer_edit_same_ts, older_edit, thread_message, root_event]
        response.end = None
        client.room_messages.return_value = response

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [msg.event_id for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[1].body == "Final answer"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_edit_without_thread_updates_existing_message(self) -> None:
        """Apply edits for known thread messages even without nested thread metadata."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )
        thread_message = self._make_text_event(
            event_id="$agent_msg",
            sender="@agent:localhost",
            body="Original answer",
            server_timestamp=2000,
            source_content={
                "body": "Original answer",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )
        malformed_edit = self._make_text_event(
            event_id="$edit1",
            sender="@agent:localhost",
            body="* replacement",
            server_timestamp=3000,
            source_content={
                "body": "* replacement",
                "m.new_content": {
                    "body": "Updated answer",
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$agent_msg",
                },
            },
        )

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [malformed_edit, thread_message, root_event]
        response.end = None
        client.room_messages.return_value = response

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [msg.event_id for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[1].body == "Updated answer"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_edit_without_thread_does_not_synthesize_missing_original(self) -> None:
        """Do not synthesize unrelated missing messages from edits without thread metadata."""
        client = AsyncMock()
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )

        edit_only_event = self._make_text_event(
            event_id="$edit1",
            sender="@agent:localhost",
            body="* replacement",
            server_timestamp=3000,
            source_content={
                "body": "* replacement",
                "m.new_content": {
                    "body": "Should remain hidden",
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$missing_original",
                },
            },
        )

        history, _sidecar_hydration_ms = await _resolve_thread_history_from_event_sources_timed(
            client,
            room_id="!room:localhost",
            thread_id="$thread_root",
            event_sources=[_event_source_for_cache(root_event), _event_source_for_cache(edit_only_event)],
            event_cache=_event_cache(),
        )

        assert [message.event_id for message in history] == ["$thread_root"]

    @pytest.mark.asyncio
    async def test_fetch_thread_history_skips_unrelated_missing_edit_before_body_extraction(self) -> None:
        """Avoid edit-body extraction for missing originals unrelated to this thread."""
        client = AsyncMock()
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )

        unrelated_edit = self._make_text_event(
            event_id="$edit1",
            sender="@agent:localhost",
            body="* replacement",
            server_timestamp=3000,
            source_content={
                "body": "* replacement",
                "m.new_content": {
                    "body": "Should not be extracted",
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$missing_original",
                },
            },
        )

        with patch(
            "mindroom.matrix.client_visible_messages.extract_edit_body",
            new_callable=AsyncMock,
        ) as mock_extract_edit_body:
            history, _sidecar_hydration_ms = await _resolve_thread_history_from_event_sources_timed(
                client,
                room_id="!room:localhost",
                thread_id="$thread_root",
                event_sources=[_event_source_for_cache(root_event), _event_source_for_cache(unrelated_edit)],
                event_cache=_event_cache(),
            )

        assert [message.event_id for message in history] == ["$thread_root"]
        mock_extract_edit_body.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_thread_history_edit_only_event_still_visible(self) -> None:
        """Synthesize a history entry when only edit events are returned."""
        client = AsyncMock()
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )

        edit_only_event = self._make_text_event(
            event_id="$edit1",
            sender="@agent:localhost",
            body="* final",
            server_timestamp=3000,
            source_content={
                "body": "* final",
                "m.new_content": {
                    "body": "Final answer",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread_root",
                    },
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$missing_original",
                },
            },
        )

        history, _sidecar_hydration_ms = await _resolve_thread_history_from_event_sources_timed(
            client,
            room_id="!room:localhost",
            thread_id="$thread_root",
            event_sources=[_event_source_for_cache(root_event), _event_source_for_cache(edit_only_event)],
            event_cache=_event_cache(),
        )

        assert [message.event_id for message in history] == ["$thread_root", "$missing_original"]
        assert history[1].body == "Final answer"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_does_not_stop_after_edit_only_page(self) -> None:
        """Continue pagination even when a page contains only relevant edits."""
        client = AsyncMock()

        edit_page_event = self._make_text_event(
            event_id="$edit1",
            sender="@agent:localhost",
            body="* final",
            server_timestamp=3000,
            source_content={
                "body": "* final",
                "m.new_content": {
                    "body": "Final answer",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread_root",
                    },
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$agent_msg",
                },
            },
        )
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )
        thread_message = self._make_text_event(
            event_id="$agent_msg",
            sender="@agent:localhost",
            body="Thinking...",
            server_timestamp=2000,
            source_content={
                "body": "Thinking...",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )

        first_page = MagicMock(spec=nio.RoomMessagesResponse)
        first_page.chunk = [edit_page_event]
        first_page.end = "page_2"

        second_page = MagicMock(spec=nio.RoomMessagesResponse)
        second_page.chunk = [thread_message, root_event]
        second_page.end = None

        client.room_messages.side_effect = [first_page, second_page]

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert client.room_messages.call_count == 2
        assert [msg.event_id for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[1].body == "Final answer"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_stops_when_root_is_found(self) -> None:
        """Stop pagination once the thread root has been seen."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )
        thread_message = self._make_text_event(
            event_id="$agent_msg",
            sender="@agent:localhost",
            body="reply",
            server_timestamp=2000,
            source_content={
                "body": "reply",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )

        first_page = MagicMock(spec=nio.RoomMessagesResponse)
        first_page.chunk = [thread_message, root_event]
        first_page.end = "older_page"

        client.room_messages.return_value = first_page

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert client.room_messages.call_count == 1
        assert [msg.event_id for msg in history] == ["$thread_root", "$agent_msg"]

    @pytest.mark.asyncio
    async def test_fetch_thread_history_room_scan_includes_encrypted_timeline_events(self) -> None:
        """Request encrypted timeline events so nio can decrypt threaded history."""
        client = AsyncMock()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root"},
        )
        thread_message = self._make_text_event(
            event_id="$agent_msg",
            sender="@agent:localhost",
            body="reply",
            server_timestamp=2000,
            source_content={
                "body": "reply",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )

        async def room_messages(*_args: object, **kwargs: object) -> MagicMock:
            message_filter = kwargs.get("message_filter")
            requested_types = set(message_filter.get("types", ())) if isinstance(message_filter, dict) else set()
            response = MagicMock(spec=nio.RoomMessagesResponse)
            response.chunk = [thread_message, root_event] if "m.room.encrypted" in requested_types else []
            response.end = None
            return response

        client.room_messages = AsyncMock(side_effect=room_messages)

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [msg.event_id for msg in history] == ["$thread_root", "$agent_msg"]
        message_filter = client.room_messages.await_args.kwargs["message_filter"]
        assert set(message_filter["types"]) == {"m.room.message", "m.room.encrypted"}

    @pytest.mark.asyncio
    async def test_fetch_thread_history_stops_when_non_text_root_is_found(self) -> None:
        """Stop pagination once a non-text thread root has been seen."""
        client = AsyncMock()

        root_event = TestThreadHistory._make_audio_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="voice-note.ogg",
            server_timestamp=1000,
            source_content={"url": "mxc://localhost/voice-note"},
        )
        thread_message = self._make_text_event(
            event_id="$agent_msg",
            sender="@agent:localhost",
            body="reply",
            server_timestamp=2000,
            source_content={
                "body": "reply",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )

        first_page = MagicMock(spec=nio.RoomMessagesResponse)
        first_page.chunk = [thread_message, root_event]
        first_page.end = "older_page"

        client.room_messages.return_value = first_page

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert client.room_messages.call_count == 1
        assert [msg.event_id for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[0].to_dict()["msgtype"] == "m.audio"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_room_scan_raises_on_api_error_response(self) -> None:
        """Room-scan fallback must fail when the Matrix API returns a non-success response."""
        client = AsyncMock()
        client.room_messages = AsyncMock(return_value=object())

        with pytest.raises(RuntimeError, match="room scan failed"):
            await _fetch_thread_history_via_room_messages_with_events(
                client,
                "!room:localhost",
                "$thread_root",
                hydrate_sidecars=True,
                event_cache=_event_cache(),
            )

    @pytest.mark.asyncio
    async def test_fetch_thread_history_room_scan_raises_when_root_is_missing(self) -> None:
        """Room-scan fallback must fail when pagination never finds the thread root."""
        client = AsyncMock()
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="reply",
            server_timestamp=2000,
            source_content={
                "body": "reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [reply_event]
        response.end = None
        client.room_messages = AsyncMock(return_value=response)

        with pytest.raises(RuntimeError, match="not found during room scan"):
            await _fetch_thread_history_via_room_messages_with_events(
                client,
                "!room:localhost",
                "$thread_root",
                hydrate_sidecars=True,
                event_cache=_event_cache(),
            )


@pytest.mark.asyncio
async def test_get_room_threads_page_uses_single_threads_request() -> None:
    """get_room_threads_page should request exactly one /threads page and preserve next_batch."""
    client = AsyncMock()
    auth_value = "secret"
    page_marker = "page_1"
    next_page = "page_2"
    client.access_token = auth_value
    thread_root = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$thread_root",
            "sender": "@alice:localhost",
            "origin_server_ts": 1234,
            "content": {"msgtype": "m.text", "body": "Thread root"},
        },
    )
    response = RoomThreadsResponse("!room:localhost", [thread_root], next_page)
    client._send = AsyncMock(return_value=response)

    with patch(
        "mindroom.matrix.client_thread_history.nio.Api.room_get_threads",
        return_value=("GET", "/_matrix/client/v1/rooms/%21room%3Alocalhost/threads"),
    ) as mock_api:
        thread_roots, next_token = await get_room_threads_page(
            client,
            "!room:localhost",
            limit=20,
            page_token=page_marker,
        )

    mock_api.assert_called_once_with(
        auth_value,
        "!room:localhost",
        paginate_from=page_marker,
        limit=20,
    )
    client._send.assert_awaited_once_with(
        RoomThreadsResponse,
        "GET",
        "/_matrix/client/v1/rooms/%21room%3Alocalhost/threads",
        response_data=("!room:localhost",),
    )
    assert [event.event_id for event in thread_roots] == ["$thread_root"]
    assert next_token == next_page


@pytest.mark.asyncio
async def test_get_room_threads_page_requires_access_token() -> None:
    """get_room_threads_page should fail early when the client has no access token."""
    client = AsyncMock()
    client.access_token = None

    with pytest.raises(RoomThreadsPageError) as exc_info:
        await get_room_threads_page(
            client,
            "!room:localhost",
            limit=20,
        )

    assert exc_info.value.response == "Matrix client access token is required for room thread pagination."
    client._send.assert_not_called()


@pytest.mark.asyncio
async def test_get_room_threads_page_raises_for_matrix_error() -> None:
    """get_room_threads_page should preserve Matrix error details for invalid tokens."""
    client = AsyncMock()
    auth_value = "secret"
    stale_page = "stale"
    client.access_token = auth_value
    client._send = AsyncMock(
        return_value=RoomThreadsError(
            "Unknown or invalid from token",
            "M_INVALID_PARAM",
        ),
    )

    with pytest.raises(RoomThreadsPageError) as exc_info:
        await get_room_threads_page(
            client,
            "!room:localhost",
            limit=20,
            page_token=stale_page,
        )

    assert exc_info.value.response == "RoomThreadsError: M_INVALID_PARAM Unknown or invalid from token"
    assert exc_info.value.errcode == "M_INVALID_PARAM"
    assert exc_info.value.retry_after_ms is None


@pytest.mark.asyncio
async def test_get_room_threads_page_preserves_rate_limit_details() -> None:
    """get_room_threads_page should preserve retry metadata from nio errors."""
    client = AsyncMock()
    auth_value = "secret"
    page_marker = "page_1"
    client.access_token = auth_value
    client._send = AsyncMock(
        return_value=RoomThreadsError(
            "Too many requests",
            "M_LIMIT_EXCEEDED",
            retry_after_ms=1500,
        ),
    )

    with pytest.raises(RoomThreadsPageError) as exc_info:
        await get_room_threads_page(
            client,
            "!room:localhost",
            limit=20,
            page_token=page_marker,
        )

    assert exc_info.value.response == "RoomThreadsError: M_LIMIT_EXCEEDED Too many requests - retry after 1500ms"
    assert exc_info.value.errcode == "M_LIMIT_EXCEEDED"
    assert exc_info.value.retry_after_ms == 1500


@pytest.mark.asyncio
async def test_get_room_threads_page_wraps_transport_timeout() -> None:
    """get_room_threads_page should convert transport exceptions into structured errors."""
    client = AsyncMock()
    auth_value = "secret"
    page_marker = "page_1"
    client.access_token = auth_value
    client._send = AsyncMock(side_effect=TimeoutError("request timed out"))

    with (
        patch(
            "mindroom.matrix.client_thread_history.nio.Api.room_get_threads",
            return_value=("GET", "/_matrix/client/v1/rooms/%21room%3Alocalhost/threads"),
        ),
        pytest.raises(RoomThreadsPageError) as exc_info,
    ):
        await get_room_threads_page(
            client,
            "!room:localhost",
            limit=20,
            page_token=page_marker,
        )

    assert exc_info.value.response == "TimeoutError: request timed out"
    assert exc_info.value.errcode is None
    assert exc_info.value.retry_after_ms is None


@pytest.mark.asyncio
async def test_get_room_threads_page_wraps_aiohttp_client_errors() -> None:
    """get_room_threads_page should convert aiohttp transport errors into structured errors."""
    client = AsyncMock()
    auth_value = "secret"
    page_marker = "page_1"
    client.access_token = auth_value
    client._send = AsyncMock(side_effect=aiohttp.ClientPayloadError("payload error"))

    with (
        patch(
            "mindroom.matrix.client_thread_history.nio.Api.room_get_threads",
            return_value=("GET", "/_matrix/client/v1/rooms/%21room%3Alocalhost/threads"),
        ),
        pytest.raises(RoomThreadsPageError) as exc_info,
    ):
        await get_room_threads_page(
            client,
            "!room:localhost",
            limit=20,
            page_token=page_marker,
        )

    assert exc_info.value.response == "ClientPayloadError: payload error"
    assert exc_info.value.errcode is None
    assert exc_info.value.retry_after_ms is None


class TestThreadHistoryCache:
    """Focused tests for the persistent thread-history cache."""

    _make_audio_event = staticmethod(TestThreadHistory._make_audio_event)
    _make_text_event = staticmethod(TestThreadHistory._make_text_event)
    _relation_key = staticmethod(TestThreadHistory._relation_key)

    @classmethod
    def _make_relations_client(cls, **kwargs: object) -> MagicMock:
        return TestThreadHistory._make_relations_client(**kwargs)

    @staticmethod
    def _cache_source(event: nio.Event) -> dict[str, object]:
        source = dict(event.source)
        content = dict(source.get("content", {}))
        content.setdefault("msgtype", "m.text")
        source["content"] = content
        source.setdefault("event_id", event.event_id)
        source.setdefault("sender", event.sender)
        source.setdefault("origin_server_ts", event.server_timestamp)
        return source

    @staticmethod
    async def _seed_thread_cache(
        cache: SqliteEventCache,
        *,
        room_id: str,
        thread_id: str,
        events: list[dict[str, object]],
    ) -> None:
        await cache.replace_thread(room_id, thread_id, events)

    @staticmethod
    def _conversation_cache_for_runtime(
        *,
        tmp_path: Path,
        client: nio.AsyncClient,
        event_cache: SqliteEventCache,
        runtime_started_at: float,
    ) -> tuple[MatrixConversationCache, EventCacheWriteCoordinator]:
        runtime_paths = test_runtime_paths(tmp_path)
        config = bind_runtime_paths(
            Config(agents={"general": AgentConfig(display_name="General Agent")}),
            runtime_paths,
        )
        coordinator = EventCacheWriteCoordinator(logger=MagicMock(), background_task_owner=object())
        runtime = BotRuntimeState(
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            enable_streaming=True,
            orchestrator=None,
            event_cache=event_cache,
            event_cache_write_coordinator=coordinator,
        )
        runtime.runtime_started_at = runtime_started_at
        return MatrixConversationCache(logger=MagicMock(), runtime=runtime), coordinator

    @staticmethod
    def _make_redaction_event(
        *,
        event_id: str,
        redacts: str,
        sender: str = "@user:localhost",
        server_timestamp: int = 0,
    ) -> MagicMock:
        event = MagicMock(spec=nio.RedactionEvent)
        event.event_id = event_id
        event.redacts = redacts
        event.sender = sender
        event.server_timestamp = server_timestamp
        event.source = {
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": server_timestamp,
            "type": "m.room.redaction",
            "redacts": redacts,
            "content": {},
        }
        return event

    @pytest.mark.asyncio
    async def test_fetch_thread_history_uses_durable_raw_snapshot_cache_when_fresh(self, tmp_path: Path) -> None:
        """Direct history fetches should reuse fresh durable thread snapshots."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Cached reply",
            server_timestamp=2000,
            source_content={
                "body": "Cached reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        await self._seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[self._cache_source(root_event), self._cache_source(reply_event)],
        )

        client = MagicMock()
        client.user_id = "@mindroom_general:localhost"
        client.room_get_event = AsyncMock(side_effect=AssertionError("should not use relations root lookup"))
        client.room_get_event_relations = MagicMock(side_effect=AssertionError("should not use relations fast path"))
        page = MagicMock(spec=nio.RoomMessagesResponse)
        page.chunk = [reply_event, root_event]
        page.end = None
        client.room_messages = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))
        try:
            history = await fetch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
        finally:
            await cache.close()

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_thread_snapshot_uses_durable_raw_snapshot_cache_when_fresh(self, tmp_path: Path) -> None:
        """Snapshot reads should reuse fresh durable thread snapshots without sidecar hydration."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = TestThreadHistory._make_audio_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="voice-note.ogg",
            server_timestamp=1000,
            source_content={"url": "mxc://localhost/voice-note"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Cached reply",
            server_timestamp=2000,
            source_content={
                "body": "Cached reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        await self._seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[self._cache_source(root_event), self._cache_source(reply_event)],
        )

        client = MagicMock()
        client.room_get_event = AsyncMock(side_effect=AssertionError("should not use relations root lookup"))
        client.room_get_event_relations = MagicMock(side_effect=AssertionError("should not use relations fast path"))
        page = MagicMock(spec=nio.RoomMessagesResponse)
        page.chunk = [reply_event, root_event]
        page.end = None
        client.room_messages = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))

        try:
            snapshot = await fetch_thread_snapshot(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
        finally:
            await cache.close()

        assert isinstance(snapshot, ThreadHistoryResult)
        assert snapshot.is_full_history is False
        assert [message.event_id for message in snapshot] == ["$thread_root", "$reply"]
        assert snapshot[0].body == "voice-note.ogg"
        assert snapshot[0].to_dict()["msgtype"] == "m.audio"
        assert snapshot.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_thread_snapshot_strips_trusted_warmup_suffix_without_sidecar_hydration(
        self,
        tmp_path: Path,
    ) -> None:
        """Snapshot reads should still expose canonical body for trusted streaming previews."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@mindroom_general:localhost",
            body="hello\n\n⏳ Preparing isolated worker...",
            server_timestamp=1000,
            source_content={
                "body": "hello\n\n⏳ Preparing isolated worker...",
                "msgtype": "m.file",
                "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
                STREAM_WARMUP_SUFFIX_KEY: "⏳ Preparing isolated worker...",
            },
        )
        await self._seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[self._cache_source(root_event)],
        )

        client = MagicMock()
        client.user_id = "@mindroom_general:localhost"
        client.room_get_event = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))
        client.room_get_event_relations = MagicMock(side_effect=AssertionError("should not refetch fresh cache"))
        client.room_messages = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))

        try:
            config = bind_runtime_paths(
                Config(agents={"general": AgentConfig(display_name="General Agent")}),
                test_runtime_paths(tmp_path),
            )
            snapshot = await fetch_thread_snapshot(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
                trusted_sender_ids={
                    matrix_id.full_id for matrix_id in config.get_ids(runtime_paths_for(config)).values()
                },
            )
        finally:
            await cache.close()

        assert snapshot[0].body == "hello"

    @pytest.mark.asyncio
    async def test_fetch_dispatch_thread_history_uses_fresh_durable_cache(self, tmp_path: Path) -> None:
        """Strict dispatch history should reuse fresh durable cache instead of refetching."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Cached reply",
            server_timestamp=2000,
            source_content={
                "body": "Cached reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        await self._seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[self._cache_source(root_event), self._cache_source(reply_event)],
        )

        client = MagicMock()
        client.user_id = "@mindroom_general:localhost"
        client.room_get_event = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))
        client.room_get_event_relations = MagicMock(side_effect=AssertionError("should not refetch fresh cache"))
        client.room_messages = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))

        try:
            history = await matrix_client_module.fetch_dispatch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
        finally:
            await cache.close()

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE

    @pytest.mark.asyncio
    async def test_fetch_dispatch_thread_history_refetches_cache_missing_thread_root(
        self,
        tmp_path: Path,
    ) -> None:
        """A cached thread snapshot without its root is incomplete and must not drive prompts."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@user:localhost",
            body="Follow-up in thread",
            server_timestamp=2000,
            source_content={
                "body": "Follow-up in thread",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        await self._seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[self._cache_source(reply_event)],
        )

        client = MagicMock()
        page = MagicMock(spec=nio.RoomMessagesResponse)
        page.chunk = [reply_event, root_event]
        page.end = None
        client.room_messages = AsyncMock(return_value=page)

        try:
            history = await matrix_client_module.fetch_dispatch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
        finally:
            await cache.close()

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_HOMESERVER
        assert history.diagnostics[THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC] == "cache_missing_thread_root"
        client.room_messages.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_dispatch_thread_snapshot_uses_fresh_durable_cache(self, tmp_path: Path) -> None:
        """Strict dispatch snapshots should reuse fresh durable cache instead of refetching."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_audio_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="voice-note.ogg",
            server_timestamp=1000,
            source_content={"url": "mxc://localhost/voice-note"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Cached reply",
            server_timestamp=2000,
            source_content={
                "body": "Cached reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        await self._seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[self._cache_source(root_event), self._cache_source(reply_event)],
        )

        client = MagicMock()
        client.user_id = "@mindroom_general:localhost"
        client.room_get_event = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))
        client.room_get_event_relations = MagicMock(side_effect=AssertionError("should not refetch fresh cache"))
        client.room_messages = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))

        try:
            snapshot = await matrix_client_module.fetch_dispatch_thread_snapshot(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
        finally:
            await cache.close()

        assert [message.event_id for message in snapshot] == ["$thread_root", "$reply"]
        assert snapshot[0].to_dict()["msgtype"] == "m.audio"
        assert snapshot.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE

    @pytest.mark.asyncio
    async def test_fetch_thread_history_cache_miss_populates_cache(self, tmp_path: Path) -> None:
        """Cache misses should fall through to the homeserver and persist the result."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Reply in thread",
            server_timestamp=2000,
            source_content={
                "body": "Reply in thread",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = MagicMock()
        page = MagicMock(spec=nio.RoomMessagesResponse)
        page.chunk = [reply_event, root_event]
        page.end = None
        client.room_messages = AsyncMock(return_value=page)
        try:
            history = await fetch_thread_history(client, "!room:localhost", "$thread_root", event_cache=cache)
            cached_events = await cache.get_thread_events("!room:localhost", "$thread_root")
        finally:
            await cache.close()

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_HOMESERVER
        assert history.diagnostics["homeserver_scan_pages"] == 1
        assert history.diagnostics["homeserver_scanned_event_count"] == 2
        assert history.diagnostics["homeserver_thread_event_count"] == 2
        assert history.diagnostics["homeserver_fetch_ms"] >= 0.0
        assert cached_events is not None
        assert [event["event_id"] for event in cached_events] == ["$thread_root", "$reply"]

    @pytest.mark.asyncio
    async def test_fetch_dispatch_thread_history_includes_cache_reject_reason(self, tmp_path: Path) -> None:
        """Strict dispatch refetches should explain why durable cache reuse was rejected."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Reply in thread",
            server_timestamp=2000,
            source_content={
                "body": "Reply in thread",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        await self._seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[self._cache_source(root_event), self._cache_source(reply_event)],
        )
        await cache.mark_thread_stale("!room:localhost", "$thread_root", reason="sync_thread_mutation")

        client = MagicMock()
        page = MagicMock(spec=nio.RoomMessagesResponse)
        page.chunk = [reply_event, root_event]
        page.end = None
        client.room_messages = AsyncMock(return_value=page)

        try:
            history = await matrix_client_module.fetch_dispatch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
        finally:
            await cache.close()

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_HOMESERVER
        assert history.diagnostics[THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC] == (
            "thread_invalidated_after_validation"
        )
        assert history.diagnostics["cache_invalidation_reason"] == "sync_thread_mutation"
        assert history.diagnostics["homeserver_scan_pages"] == 1

    @pytest.mark.asyncio
    async def test_reuses_pre_runtime_thread_cache_after_restart(self, tmp_path: Path) -> None:
        """Restarted runtimes may reuse pre-runtime snapshots unless a stale marker exists."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime_started_at = time.time()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Cached reply",
            server_timestamp=2000,
            source_content={
                "body": "Cached reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        await cache.replace_thread(
            "!room:localhost",
            "$thread_root",
            [self._cache_source(root_event), self._cache_source(reply_event)],
            validated_at=runtime_started_at - 10.0,
        )

        client = MagicMock()
        client.room_messages = AsyncMock(side_effect=AssertionError("should reuse cache after sync catch-up"))
        conversation_cache, coordinator = self._conversation_cache_for_runtime(
            tmp_path=tmp_path,
            client=client,
            event_cache=cache,
            runtime_started_at=runtime_started_at,
        )

        try:
            history = await conversation_cache.get_dispatch_thread_history(
                "!room:localhost",
                "$thread_root",
            )
        finally:
            await coordinator.close()
            await cache.close()

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        assert THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC not in history.diagnostics
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reuses_older_pre_runtime_thread_cache_after_restart(
        self,
        tmp_path: Path,
    ) -> None:
        """Thread cache reads should not reject old snapshots by process age."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime_started_at = time.time()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Cached reply",
            server_timestamp=2000,
            source_content={
                "body": "Cached reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        await cache.replace_thread(
            "!room:localhost",
            "$thread_root",
            [self._cache_source(root_event), self._cache_source(reply_event)],
            validated_at=runtime_started_at - 900.0,
        )

        client = MagicMock()
        client.room_messages = AsyncMock(side_effect=AssertionError("should reuse old trusted cache"))
        conversation_cache, coordinator = self._conversation_cache_for_runtime(
            tmp_path=tmp_path,
            client=client,
            event_cache=cache,
            runtime_started_at=runtime_started_at,
        )

        try:
            history = await conversation_cache.get_dispatch_thread_history(
                "!room:localhost",
                "$thread_root",
            )
        finally:
            await coordinator.close()
            await cache.close()

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        assert THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC not in history.diagnostics
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_thread_cache_reuse_ignores_runtime_age_without_invalidation(
        self,
        tmp_path: Path,
    ) -> None:
        """Thread cache trust should depend on explicit stale markers, not process age."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        runtime_started_at = time.time()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Reply in thread",
            server_timestamp=2000,
            source_content={
                "body": "Reply in thread",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        await cache.replace_thread(
            "!room:localhost",
            "$thread_root",
            [self._cache_source(root_event), self._cache_source(reply_event)],
            validated_at=runtime_started_at - 10.0,
        )

        client = MagicMock()
        client.room_messages = AsyncMock(side_effect=AssertionError("should reuse cache by default"))
        conversation_cache, coordinator = self._conversation_cache_for_runtime(
            tmp_path=tmp_path,
            client=client,
            event_cache=cache,
            runtime_started_at=runtime_started_at,
        )

        try:
            history = await conversation_cache.get_dispatch_thread_history(
                "!room:localhost",
                "$thread_root",
            )
        finally:
            await coordinator.close()
            await cache.close()

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        assert THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC not in history.diagnostics
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_thread_history_cache_miss_persists_reference_descendant_in_causal_order(
        self,
        tmp_path: Path,
    ) -> None:
        """Fresh room-scan snapshots should store same-timestamp reference descendants after their parent."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_text_event(
            event_id="$root",
            sender="@user:localhost",
            body="root",
            server_timestamp=1000,
            source_content={"body": "root", "msgtype": "m.text"},
        )
        explicit_reply = self._make_text_event(
            event_id="$explicit",
            sender="@agent:localhost",
            body="explicit reply",
            server_timestamp=1500,
            source_content={
                "body": "explicit reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
            },
        )
        reference_parent = self._make_text_event(
            event_id="$zzz_parent",
            sender="@bridge:localhost",
            body="reference parent",
            server_timestamp=2000,
            source_content={
                "body": "reference parent",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
            },
        )
        reference_child = self._make_text_event(
            event_id="$aaa_child",
            sender="@bridge:localhost",
            body="reference child",
            server_timestamp=2000,
            source_content={
                "body": "reference child",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.reference", "event_id": "$zzz_parent"},
            },
        )

        client = MagicMock()
        page = MagicMock(spec=nio.RoomMessagesResponse)
        page.chunk = [reference_child, reference_parent, explicit_reply, root_event]
        page.end = None
        client.room_messages = AsyncMock(return_value=page)

        try:
            history = await fetch_thread_history(
                client,
                "!room:localhost",
                "$root",
                event_cache=cache,
            )
            cached_events = await cache.get_thread_events("!room:localhost", "$root")
        finally:
            await cache.close()

        assert [message.event_id for message in history] == ["$root", "$explicit", "$zzz_parent", "$aaa_child"]
        assert cached_events is not None
        assert [event["event_id"] for event in cached_events] == ["$root", "$explicit", "$zzz_parent", "$aaa_child"]

    @pytest.mark.asyncio
    async def test_fetch_thread_history_refetches_after_durable_room_invalidation(self, tmp_path: Path) -> None:
        """A durable room-level stale marker should force the next read to refetch from Matrix."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        stale_reply = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Stale reply",
            server_timestamp=2000,
            source_content={
                "body": "Stale reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        fresh_reply = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Fresh reply",
            server_timestamp=3000,
            source_content={
                "body": "Fresh reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = MagicMock()
        first_page = MagicMock(spec=nio.RoomMessagesResponse)
        first_page.chunk = [fresh_reply, root_event]
        first_page.end = None
        second_page = MagicMock(spec=nio.RoomMessagesResponse)
        second_page.chunk = [fresh_reply, root_event]
        second_page.end = None
        client.room_messages = AsyncMock(side_effect=[first_page, second_page])

        try:
            await cache.replace_thread(
                "!room:localhost",
                "$thread_root",
                [self._cache_source(root_event), self._cache_source(stale_reply)],
                validated_at=time.time(),
            )
            await cache.mark_room_threads_stale("!room:localhost", reason="sync_lookup_missing")

            history = await fetch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
            second_history = await fetch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
        finally:
            await cache.close()

        assert [message.body for message in history] == ["Root message", "Fresh reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_HOMESERVER
        assert [message.body for message in second_history] == ["Root message", "Fresh reply"]
        assert second_history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_CACHE
        assert client.room_messages.await_count == 1

    @pytest.mark.asyncio
    async def test_fetch_thread_history_returns_stale_cached_history_with_diagnostic_on_refetch_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """Failed refetches should degrade once, then recover to a fresh refetch when the network returns."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        stale_reply = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Cached reply",
            server_timestamp=2000,
            source_content={
                "body": "Cached reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        fresh_reply = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Fresh reply",
            server_timestamp=3000,
            source_content={
                "body": "Fresh reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = MagicMock()
        client.room_get_event = AsyncMock(side_effect=RuntimeError("root fetch failed"))
        client.room_get_event_relations = MagicMock()
        client.room_messages = AsyncMock(side_effect=RuntimeError("scan failed"))
        recovered_client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.thread): [fresh_reply],
                self._relation_key("$thread_root", RelationshipType.replacement): [],
                self._relation_key("$reply", RelationshipType.replacement): [],
            },
        )

        try:
            await cache.replace_thread(
                "!room:localhost",
                "$thread_root",
                [self._cache_source(root_event), self._cache_source(stale_reply)],
                validated_at=time.time(),
            )
            await cache.mark_thread_stale("!room:localhost", "$thread_root", reason="force_refetch")

            history = await fetch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
            recovered_history = await fetch_thread_history(
                recovered_client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
        finally:
            await cache.close()

        assert [message.body for message in history] == ["Root message", "Cached reply"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_STALE_CACHE
        assert history.diagnostics[THREAD_HISTORY_DEGRADED_DIAGNOSTIC] is True
        assert history.diagnostics[THREAD_HISTORY_ERROR_DIAGNOSTIC] == "scan failed"
        assert [message.body for message in recovered_history] == ["Root message", "Fresh reply"]
        assert recovered_history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_HOMESERVER

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("cache_state_side_effect", "cached_events_side_effect"),
        [
            (RuntimeError("db state read broken"), None),
            (
                None,
                RuntimeError("db event read broken"),
            ),
        ],
        ids=["thread_cache_state_read_failure", "thread_events_read_failure"],
    )
    async def test_fetch_thread_history_gracefully_degrades_when_cache_read_fails(
        self,
        cache_state_side_effect: RuntimeError | None,
        cached_events_side_effect: RuntimeError | None,
    ) -> None:
        """Cache metadata read errors should fail open to a successful homeserver fetch."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        reply_event = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Reply in thread",
            server_timestamp=2000,
            source_content={
                "body": "Reply in thread",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.thread): [reply_event],
                self._relation_key("$thread_root", RelationshipType.replacement): [],
                self._relation_key("$reply", RelationshipType.replacement): [],
            },
        )
        broken_cache = MagicMock(spec=SqliteEventCache)
        broken_cache.get_thread_cache_state = AsyncMock(
            side_effect=cache_state_side_effect,
            return_value=(
                ThreadCacheState(
                    validated_at=time.time(),
                    invalidated_at=None,
                    invalidation_reason=None,
                    room_invalidated_at=None,
                    room_invalidation_reason=None,
                )
                if cache_state_side_effect is None
                else None
            ),
        )
        broken_cache.get_thread_events = AsyncMock(side_effect=cached_events_side_effect, return_value=[])
        broken_cache.replace_thread_if_not_newer = AsyncMock(side_effect=RuntimeError("db broken"))
        history = await fetch_thread_history(client, "!room:localhost", "$thread_root", event_cache=broken_cache)
        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        broken_cache.replace_thread_if_not_newer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_incremental_thread_revalidation_ignores_runtime_age_but_not_room_staleness(
        self,
        tmp_path: Path,
    ) -> None:
        """Incremental append refresh should trust old caches unless room state is stale."""
        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()

        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        cached_reply = self._make_text_event(
            event_id="$reply1",
            sender="@agent:localhost",
            body="Cached reply",
            server_timestamp=2000,
            source_content={
                "body": "Cached reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        appended_reply = self._make_text_event(
            event_id="$reply2",
            sender="@agent:localhost",
            body="Incremental reply",
            server_timestamp=3000,
            source_content={
                "body": "Incremental reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        runtime_started_at = time.time() - 1

        await cache.replace_thread(
            "!room:localhost",
            "$thread_root",
            [self._cache_source(root_event), self._cache_source(cached_reply)],
            validated_at=runtime_started_at - 100,
        )
        await cache.mark_thread_stale("!room:localhost", "$thread_root", reason="sync_thread_mutation")
        pre_runtime_appended = await cache.append_event(
            "!room:localhost",
            "$thread_root",
            self._cache_source(appended_reply),
        )
        pre_runtime_revalidated = await cache.revalidate_thread_after_incremental_update(
            "!room:localhost",
            "$thread_root",
        )

        await cache.replace_thread(
            "!room:localhost",
            "$thread_root",
            [self._cache_source(root_event), self._cache_source(cached_reply)],
            validated_at=time.time(),
        )
        await cache.mark_room_threads_stale("!room:localhost", reason="sync_redaction_lookup_unavailable")
        await cache.mark_thread_stale("!room:localhost", "$thread_root", reason="sync_thread_mutation")
        room_stale_appended = await cache.append_event(
            "!room:localhost",
            "$thread_root",
            self._cache_source(appended_reply),
        )
        room_stale_revalidated = await cache.revalidate_thread_after_incremental_update(
            "!room:localhost",
            "$thread_root",
        )

        client = MagicMock()
        page = MagicMock(spec=nio.RoomMessagesResponse)
        page.chunk = [appended_reply, cached_reply, root_event]
        page.end = None
        client.room_messages = AsyncMock(return_value=page)

        try:
            history = await matrix_client_module.fetch_dispatch_thread_history(
                client,
                "!room:localhost",
                "$thread_root",
                event_cache=cache,
            )
        finally:
            await cache.close()

        assert pre_runtime_appended is True
        assert pre_runtime_revalidated is True
        assert room_stale_appended is True
        assert room_stale_revalidated is False
        assert [message.event_id for message in history] == ["$thread_root", "$reply1", "$reply2"]
        assert history.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_HOMESERVER

    @pytest.mark.asyncio
    async def test_fetch_thread_history_logs_refresh_diagnostics_for_cache_miss(self) -> None:
        """Homeserver refills should emit the phase-1 diagnostics line with miss metadata."""
        logger = MagicMock()
        refreshed_history = [
            ResolvedVisibleMessage.synthetic(
                sender="@user:localhost",
                body="refreshed",
                event_id="$thread_root",
                content={"body": "refreshed"},
            ),
        ]

        with (
            patch.object(matrix_client_module, "logger", logger),
            patch(
                "mindroom.matrix.client_thread_history._load_cached_thread_history_if_usable",
                new=AsyncMock(
                    return_value=(
                        None,
                        {
                            THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC: "no_cache_state",
                        },
                    ),
                ),
            ),
            patch(
                "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
                new=AsyncMock(
                    return_value=MagicMock(
                        history=refreshed_history,
                        event_sources=[{"event_id": "$thread_root"}],
                        fetch_ms=91.2,
                        room_scan_pages=7,
                        scanned_event_count=42,
                        resolution_ms=8.6,
                        sidecar_hydration_ms=4.4,
                    ),
                ),
            ),
            patch("mindroom.matrix.client_thread_history._store_thread_history_cache", new=AsyncMock()),
        ):
            history = await matrix_client_module.fetch_thread_history(
                AsyncMock(),
                "!room:localhost",
                "$thread_root",
                event_cache=_event_cache(),
                caller_label="cache_miss_test",
                coordinator_queue_wait_ms=45.6,
            )

        assert [message.event_id for message in history] == ["$thread_root"]
        refreshed_log = next(
            call
            for call in logger.info.call_args_list
            if call.args and call.args[0] == "matrix_cache_thread_history_refreshed"
        )
        assert refreshed_log.kwargs == {
            "room_id": "!room:localhost",
            "thread_id": "$thread_root",
            "caller_label": "cache_miss_test",
            "mode": "full_scan",
            "cache_read_ms": 0.0,
            "homeserver_fetch_ms": 91.2,
            "homeserver_scan_pages": 7,
            "homeserver_scanned_event_count": 42,
            "homeserver_thread_event_count": 1,
            "resolution_ms": 8.6,
            "sidecar_hydration_ms": 4.4,
            "coordinator_queue_wait_ms": 45.6,
            "cache_reject_reason": "no_cache_state",
            "thread_read_source": THREAD_HISTORY_SOURCE_HOMESERVER,
            "thread_read_degraded": False,
            "thread_read_error": None,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fetcher_name", "is_full_history"),
        [
            ("fetch_thread_history", True),
            ("fetch_thread_snapshot", False),
            ("fetch_dispatch_thread_history", True),
            ("fetch_dispatch_thread_snapshot", False),
        ],
    )
    async def test_thread_fetchers_log_refresh_diagnostics_for_cache_hit(
        self,
        fetcher_name: str,
        is_full_history: bool,
    ) -> None:
        """Usable cache hits should emit the same structured refresh diagnostics."""
        logger = MagicMock()
        cached_history = ThreadHistoryResult(
            [
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="cached",
                    event_id="$thread_root",
                    content={"body": "cached"},
                ),
            ],
            is_full_history=is_full_history,
            diagnostics={
                "cache_read_ms": 5.5,
                "resolution_ms": 3.2,
                "sidecar_hydration_ms": 1.4,
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_CACHE,
            },
        )
        homeserver_fetch = AsyncMock()

        with (
            patch.object(matrix_client_module, "logger", logger),
            patch(
                "mindroom.matrix.client_thread_history._load_cached_thread_history_if_usable",
                new=AsyncMock(return_value=(cached_history, None)),
            ),
            patch(
                "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
                new=homeserver_fetch,
            ),
        ):
            history = await getattr(matrix_client_module, fetcher_name)(
                AsyncMock(),
                "!room:localhost",
                "$thread_root",
                event_cache=_event_cache(),
                caller_label="cache_hit_test",
                coordinator_queue_wait_ms=34.5,
            )

        assert history == cached_history
        homeserver_fetch.assert_not_awaited()
        refreshed_log = next(
            call
            for call in logger.info.call_args_list
            if call.args and call.args[0] == "matrix_cache_thread_history_refreshed"
        )
        assert refreshed_log.kwargs == {
            "room_id": "!room:localhost",
            "thread_id": "$thread_root",
            "caller_label": "cache_hit_test",
            "mode": "cache_hit",
            "cache_read_ms": 5.5,
            "homeserver_fetch_ms": 0.0,
            "homeserver_scan_pages": 0,
            "homeserver_scanned_event_count": 0,
            "homeserver_thread_event_count": 0,
            "resolution_ms": 3.2,
            "sidecar_hydration_ms": 1.4,
            "coordinator_queue_wait_ms": 34.5,
            "cache_reject_reason": None,
            "thread_read_source": THREAD_HISTORY_SOURCE_CACHE,
            "thread_read_degraded": False,
            "thread_read_error": None,
        }

    @pytest.mark.asyncio
    async def test_fetch_thread_history_logs_stale_fallback_diagnostics(self) -> None:
        """Stale fallbacks should be visible in the structured refresh diagnostics."""
        logger = MagicMock()
        stale_history = ThreadHistoryResult(
            [
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="stale",
                    event_id="$thread_root",
                    content={"body": "stale"},
                ),
            ],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_STALE_CACHE,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
                THREAD_HISTORY_ERROR_DIAGNOSTIC: "homeserver unavailable",
            },
        )

        with (
            patch.object(matrix_client_module, "logger", logger),
            patch(
                "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
                new=AsyncMock(side_effect=RuntimeError("homeserver unavailable")),
            ),
            patch(
                "mindroom.matrix.client_thread_history._load_stale_cached_thread_history",
                new=AsyncMock(return_value=stale_history),
            ),
        ):
            history = await matrix_client_module.refresh_thread_history_from_source(
                AsyncMock(),
                "!room:localhost",
                "$thread_root",
                event_cache=_event_cache(),
                caller_label="stale_fallback_test",
                coordinator_queue_wait_ms=12.3,
            )

        assert history == stale_history
        refreshed_log = next(
            call
            for call in logger.info.call_args_list
            if call.args and call.args[0] == "matrix_cache_thread_history_refreshed"
        )
        assert refreshed_log.kwargs["caller_label"] == "stale_fallback_test"
        assert refreshed_log.kwargs["coordinator_queue_wait_ms"] == 12.3
        assert refreshed_log.kwargs["thread_read_source"] == THREAD_HISTORY_SOURCE_STALE_CACHE
        assert refreshed_log.kwargs["thread_read_degraded"] is True
        assert refreshed_log.kwargs["thread_read_error"] == "homeserver unavailable"
