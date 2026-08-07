"""Tests for thread history fetching, especially including thread root messages."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import nio
import pytest
from nio.responses import RoomThreadsError, RoomThreadsResponse

import mindroom.matrix.client_thread_history as matrix_client_module
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.matrix.client import ResolvedVisibleMessage, RoomThreadsPageError, get_room_threads_page
from mindroom.matrix.client_thread_history import (
    _fetch_thread_history_via_room_messages_with_events,
    _resolve_thread_history_from_event_sources_timed,
)
from mindroom.matrix.client_visible_messages import ThreadEditCandidates
from mindroom.matrix.membership_fence import UNCERTIFIED_MEMBERSHIP_EPOCH
from mindroom.matrix.room_history_reads import _event_source_for_cache, _group_scanned_sources_by_thread
from mindroom.matrix.thread_projection import ordered_event_ids_from_scanned_event_sources
from mindroom.thread_utils import get_agents_in_thread
from tests.conftest import bind_runtime_paths, make_event_cache_mock, make_visible_message, test_runtime_paths
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from nio.api import RelationshipType

    from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache


def _event_cache() -> AsyncMock:
    return make_event_cache_mock()


def test_thread_agent_detection_uses_actual_persisted_ids(tmp_path: Path) -> None:
    """Thread continuation should use current actual Matrix IDs and ignore generated fallbacks."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        runtime_paths,
    )
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "actual_router", "general": "actual_general"},
    )
    history = [
        make_visible_message(sender="@actual_general:localhost", body="Current agent reply"),
        make_visible_message(sender="@mindroom_general:localhost", body="Stale generated-looking reply"),
    ]

    agents = get_agents_in_thread(history, config, runtime_paths)

    assert [agent.full_id for agent in agents] == ["@actual_general:localhost"]


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
        grouped, _unresolved_opaque = await _group_scanned_sources_by_thread(
            room_id="!room:localhost",
            thread_root_ids=("$room_root",),
            edit_candidates=ThreadEditCandidates(),
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

        assert [source["event_id"] for source in grouped["$room_root"]] == ["$room_root"]

    @pytest.mark.asyncio
    async def test_room_scan_revisits_inherited_replies_until_fixpoint(self) -> None:
        """Cold room scans should retain descendants even when they sort before their threaded parent."""
        grouped, _unresolved_opaque = await _group_scanned_sources_by_thread(
            room_id="!room:localhost",
            thread_root_ids=("$root",),
            edit_candidates=ThreadEditCandidates(),
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

        assert {source["event_id"] for source in grouped["$root"]} == {"$root", "$z-parent", "$a-child"}

    @pytest.mark.asyncio
    async def test_room_scan_promotes_transitive_plain_reply_chain(self) -> None:
        """Cold room scans should keep a plain-reply chain inside the same thread transitively."""
        grouped, _unresolved_opaque = await _group_scanned_sources_by_thread(
            room_id="!room:localhost",
            thread_root_ids=("$root",),
            edit_candidates=ThreadEditCandidates(),
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

        assert [source["event_id"] for source in grouped["$root"]] == ["$root", "$thread_reply", "$plain1", "$plain2"]

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
            )
        ).history

        event_ids = [message.event_id for message in history]
        assert event_ids == ["$root", "$explicit", "$zzz_parent", "$aaa_child"]

    @pytest.mark.asyncio
    async def test_resolve_thread_history_keeps_same_timestamp_reference_descendant_after_parent(self) -> None:
        """Same-timestamp reference descendants should sort after their related parent."""
        client = AsyncMock()

        resolution = await _resolve_thread_history_from_event_sources_timed(
            client,
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
        )
        history = resolution.messages

        assert [message.event_id for message in history] == ["$root", "$zzz_parent", "$aaa_child"]

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

        resolution = await _resolve_thread_history_from_event_sources_timed(
            client,
            thread_id="$thread_root",
            event_sources=[_event_source_for_cache(root_event), _event_source_for_cache(edit_only_event)],
        )
        history = resolution.messages

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
            resolution = await _resolve_thread_history_from_event_sources_timed(
                client,
                thread_id="$thread_root",
                event_sources=[_event_source_for_cache(root_event), _event_source_for_cache(unrelated_edit)],
            )
        history = resolution.messages

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

        resolution = await _resolve_thread_history_from_event_sources_timed(
            client,
            thread_id="$thread_root",
            event_sources=[_event_source_for_cache(root_event), _event_source_for_cache(edit_only_event)],
        )
        history = resolution.messages

        assert [message.event_id for message in history] == ["$thread_root", "$missing_original"]
        assert history[1].body == "Final answer"

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
        await _replace_thread(cache, room_id, thread_id, events)

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
    async def test_refresh_reports_a_failed_store_instead_of_claiming_cache_success(self) -> None:
        """A store that installs nothing must stay explicit and never read as cache success.

        The bounded retry this replaced is gone: replacement no longer conflicts, so there is
        nothing to re-attempt. One fetch, one store attempt, and an honest diagnostic.
        """
        event_cache = _event_cache()
        event_cache.replace_thread.return_value = False
        fetch_result = matrix_client_module._ThreadHistoryFetchResult(
            history=[
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="homeserver fallback",
                    event_id="$thread_root",
                    content={"body": "homeserver fallback"},
                ),
            ],
            event_sources=[{"event_id": "$thread_root"}],
            fetch_ms=1.0,
            room_scan_pages=1,
            scanned_event_count=1,
            resolution_ms=1.0,
            sidecar_hydration_ms=0.0,
        )

        with patch(
            "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
            new=AsyncMock(return_value=fetch_result),
        ) as fetch:
            history = await matrix_client_module.refresh_thread_history_from_source(
                AsyncMock(),
                "!room:localhost",
                "$thread_root",
                event_cache=event_cache,
                allow_stale_fallback=False,
            )

        assert fetch.await_count == 1
        assert [message.body for message in history] == ["homeserver fallback"]
        assert history.diagnostics["cache_store_written"] is False
        # A refused store is not a write fault; the diagnostics have to keep the two apart.
        assert history.diagnostics["cache_store_failed"] is False

    @pytest.mark.asyncio
    async def test_source_refresh_survives_membership_epoch_cache_failure(self) -> None:
        """Authoritative history remains available while every derived cache write is rejected."""
        event_cache = _event_cache()
        event_cache.room_membership_epoch.side_effect = RuntimeError("cache unavailable")
        event_cache.replace_thread.return_value = False
        fetch_result = matrix_client_module._ThreadHistoryFetchResult(
            history=[
                ResolvedVisibleMessage.synthetic(
                    sender="@user:localhost",
                    body="fresh",
                    event_id="$thread_root",
                    content={"body": "fresh"},
                ),
            ],
            event_sources=[{"event_id": "$thread_root"}],
            fetch_ms=1.0,
            room_scan_pages=1,
            scanned_event_count=1,
            resolution_ms=1.0,
            sidecar_hydration_ms=0.0,
        )

        with patch(
            "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
            new=AsyncMock(return_value=fetch_result),
        ):
            history = await matrix_client_module.refresh_thread_history_from_source(
                AsyncMock(),
                "!room:localhost",
                "$thread_root",
                event_cache=event_cache,
                allow_stale_fallback=False,
            )

        assert [message.event_id for message in history] == ["$thread_root"]
        # The fetch no longer carries an epoch -- it writes nothing durable --
        # so the uncertified generation has to reach the one write that remains.
        assert event_cache.replace_thread.await_args.kwargs["expected_membership_epoch"] == UNCERTIFIED_MEMBERSHIP_EPOCH
