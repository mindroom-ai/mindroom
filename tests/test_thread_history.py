"""Tests for thread history fetching, especially including thread root messages."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, call, patch

import nio
import pytest
from nio.api import RelationshipType

from mindroom.matrix.client import (
    _latest_thread_event_id,
    build_threaded_edit_content,
    fetch_thread_history,
)
from tests.conftest import make_visible_message

if TYPE_CHECKING:
    from collections.abc import Iterable


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
        event.source = {
            "type": "m.room.message",
            "content": source_content,
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
        client.room_messages = AsyncMock()

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
            key = (event_id, rel_type, event_type, direction, limit)
            value = relations.get(key, [])

            async def iterator() -> object:
                if isinstance(value, Exception):
                    raise value
                for event in value:
                    yield event

            return iterator()

        client.room_get_event_relations = MagicMock(side_effect=room_get_event_relations)
        return client

    @pytest.mark.asyncio
    async def test_fetch_thread_history_delegates_to_room_scan_fallback_helper(self) -> None:
        """Preserve the legacy room-scan path behind an explicit helper."""
        client = AsyncMock()
        expected_history = [make_visible_message(event_id="$thread_root", body="root")]

        with patch(
            "mindroom.matrix.client._fetch_thread_history_via_room_messages",
            new=AsyncMock(return_value=expected_history),
        ) as mock_fallback:
            history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert history == expected_history
        mock_fallback.assert_awaited_once_with(client, "!room:localhost", "$thread_root")

    @pytest.mark.asyncio
    async def test_fetch_thread_history_prefers_relations_fast_path(self) -> None:
        """Relations-first fetch should use the root event plus direct thread children."""
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
            body="Reply in thread",
            server_timestamp=2000,
            source_content={
                "body": "Reply in thread",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        unrelated_relation = self._make_text_event(
            event_id="$other_thread_reply",
            sender="@agent:localhost",
            body="Should be ignored",
            server_timestamp=2500,
            source_content={
                "body": "Should be ignored",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$other_root"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.thread): [thread_event, unrelated_relation],
                self._relation_key("$thread_root", RelationshipType.replacement): [],
                self._relation_key("$reply", RelationshipType.replacement): [],
            },
        )

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history[0].body == "Root message"
        assert history[1].body == "Reply in thread"
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_thread_history_uses_bundled_root_edit_without_replacement_lookup(self) -> None:
        """Bundled replacement data should update the root without another relations request."""
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
        replacement_calls = [
            call
            for call in client.room_get_event_relations.call_args_list
            if call.kwargs["rel_type"] == RelationshipType.replacement
        ]
        assert [call.args[1] for call in replacement_calls] == ["$reply"]

    @pytest.mark.asyncio
    async def test_fetch_thread_history_relations_path_applies_reply_edits_and_stream_status(self) -> None:
        """Relations-first fetch should apply reply edits and preserve stream metadata."""
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
        older_edit = self._make_text_event(
            event_id="$edit_a",
            sender="@agent:localhost",
            body="* partial",
            server_timestamp=3000,
            source_content={
                "body": "* partial",
                "m.new_content": {
                    "body": "Partial",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                    "io.mindroom.stream_status": "pending",
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
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
                self._relation_key("$thread_root", RelationshipType.replacement): [],
                self._relation_key("$reply", RelationshipType.replacement): [older_edit, newer_edit],
            },
        )

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [message.event_id for message in history] == ["$thread_root", "$reply"]
        assert history[1].body == "Final answer"
        assert history[1].content["body"] == "Final answer"
        assert history[1].stream_status == "completed"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_falls_back_when_relations_lookup_fails(self) -> None:
        """Relations fetch errors should fall back to the legacy room scan."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.thread): RuntimeError("unsupported"),
            },
        )
        fallback_history = [make_visible_message(event_id="$thread_root", body="fallback")]

        with patch(
            "mindroom.matrix.client._fetch_thread_history_via_room_messages",
            new=AsyncMock(return_value=fallback_history),
        ) as mock_fallback:
            history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert history == fallback_history
        mock_fallback.assert_awaited_once_with(client, "!room:localhost", "$thread_root")

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_uses_relations_fast_path(self) -> None:
        """The latest-thread lookup should inspect the newest child for edits."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        newest_thread_event = self._make_text_event(
            event_id="$reply_latest",
            sender="@agent:localhost",
            body="Newest reply",
            server_timestamp=3000,
            source_content={
                "body": "Newest reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key(
                    "$thread_root",
                    RelationshipType.thread,
                    direction=nio.MessageDirection.back,
                    limit=1,
                ): [newest_thread_event],
            },
        )

        event_id = await _latest_thread_event_id(client, "!room:localhost", "$thread_root")

        assert event_id == "$reply_latest"
        assert client.room_get_event_relations.call_args_list == [
            call(
                "!room:localhost",
                "$thread_root",
                rel_type=RelationshipType.thread,
                event_type="m.room.message",
                direction=nio.MessageDirection.back,
                limit=1,
            ),
            call(
                "!room:localhost",
                "$reply_latest",
                rel_type=RelationshipType.replacement,
                event_type="m.room.message",
                direction=nio.MessageDirection.back,
                limit=None,
            ),
        ]
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_stops_after_first_relation_event(self) -> None:
        """The latest-thread lookup should stop iterating thread children after one event."""
        newest_thread_event = self._make_text_event(
            event_id="$reply_latest",
            sender="@agent:localhost",
            body="Newest reply",
            server_timestamp=3000,
            source_content={
                "body": "Newest reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = MagicMock()
        client.room_messages = AsyncMock()
        second_event_requested = False

        async def thread_relations() -> object:
            nonlocal second_event_requested
            yield newest_thread_event
            second_event_requested = True
            msg = "should not request more than one relation event"
            raise AssertionError(msg)

        async def replacement_relations() -> object:
            if False:
                yield None

        def room_get_event_relations(
            _room_id: str,
            event_id: str,
            *,
            rel_type: RelationshipType | None = None,
            event_type: str | None = None,
            direction: nio.MessageDirection = nio.MessageDirection.back,
            limit: int | None = None,
        ) -> object:
            assert event_type == "m.room.message"
            assert direction == nio.MessageDirection.back
            if rel_type is RelationshipType.thread:
                assert event_id == "$thread_root"
                assert limit == 1
                return thread_relations()
            assert rel_type is RelationshipType.replacement
            assert event_id == "$reply_latest"
            assert limit is None
            return replacement_relations()

        client.room_get_event_relations = MagicMock(side_effect=room_get_event_relations)

        event_id = await _latest_thread_event_id(client, "!room:localhost", "$thread_root")

        assert event_id == "$reply_latest"
        assert second_event_requested is False
        assert client.room_get_event_relations.call_args_list[0] == call(
            "!room:localhost",
            "$thread_root",
            rel_type=RelationshipType.thread,
            event_type="m.room.message",
            direction=nio.MessageDirection.back,
            limit=1,
        )
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_returns_latest_edit_of_newest_child(self) -> None:
        """The fast path should return the newest visible edit event when present."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        newest_thread_event = self._make_text_event(
            event_id="$reply_latest",
            sender="@agent:localhost",
            body="Draft reply",
            server_timestamp=3000,
            source_content={
                "body": "Draft reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        newest_edit = self._make_text_event(
            event_id="$edit_latest",
            sender="@agent:localhost",
            body="* Final reply",
            server_timestamp=3100,
            source_content={
                "body": "* Final reply",
                "m.new_content": {"body": "Final reply"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply_latest"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key(
                    "$thread_root",
                    RelationshipType.thread,
                    direction=nio.MessageDirection.back,
                    limit=1,
                ): [newest_thread_event],
                self._relation_key("$reply_latest", RelationshipType.replacement): [newest_edit],
            },
        )

        event_id = await _latest_thread_event_id(client, "!room:localhost", "$thread_root")

        assert event_id == "$edit_latest"
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_returns_edit_relation_when_latest_relation_is_edit(self) -> None:
        """The fast path should treat an edit relation as the latest visible thread event."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        latest_edit = self._make_text_event(
            event_id="$edit_latest",
            sender="@agent:localhost",
            body="* Edited reply",
            server_timestamp=3100,
            source_content={
                "body": "* Edited reply",
                "m.new_content": {
                    "body": "Edited reply",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply_latest"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key(
                    "$thread_root",
                    RelationshipType.thread,
                    direction=nio.MessageDirection.back,
                    limit=1,
                ): [latest_edit],
            },
        )

        event_id = await _latest_thread_event_id(client, "!room:localhost", "$thread_root")

        assert event_id == "$edit_latest"
        client.room_get_event.assert_not_awaited()
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_build_threaded_edit_content_uses_latest_thread_event_id_for_fallback(self) -> None:
        """Threaded edits should preserve MSC3440 fallback semantics through the latest visible event."""
        client = AsyncMock()

        with (
            patch(
                "mindroom.matrix.client._latest_thread_event_id",
                new=AsyncMock(return_value="$latest"),
            ) as mock_latest,
            patch(
                "mindroom.matrix.client.format_message_with_mentions",
                return_value={"body": "edited"},
            ) as mock_format,
        ):
            content = await build_threaded_edit_content(
                client,
                room_id="!room:localhost",
                new_text="edited",
                thread_id="$thread_root",
                config=MagicMock(),
                runtime_paths=MagicMock(),
                sender_domain="localhost",
            )

        assert content == {"body": "edited"}
        mock_latest.assert_awaited_once_with(client, "!room:localhost", "$thread_root")
        assert mock_format.call_args.kwargs["latest_thread_event_id"] == "$latest"

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_returns_root_when_thread_has_no_children(self) -> None:
        """Empty threads should use the thread root as the MSC3440 fallback target."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key(
                    "$thread_root",
                    RelationshipType.thread,
                    direction=nio.MessageDirection.back,
                    limit=1,
                ): [],
            },
        )

        event_id = await _latest_thread_event_id(client, "!room:localhost", "$thread_root")

        assert event_id == "$thread_root"

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_returns_latest_root_edit_when_no_children(self) -> None:
        """A root edit should become the latest visible event when there are no children."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Original root",
            server_timestamp=1000,
            source_content={"body": "Original root"},
        )
        root_edit = self._make_text_event(
            event_id="$root_edit",
            sender="@user:localhost",
            body="* Edited root",
            server_timestamp=1100,
            source_content={
                "body": "* Edited root",
                "m.new_content": {"body": "Edited root"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_root"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key("$thread_root", RelationshipType.replacement): [root_edit],
            },
        )

        event_id = await _latest_thread_event_id(client, "!room:localhost", "$thread_root")

        assert event_id == "$root_edit"
        client.room_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_falls_back_to_history_on_relations_error(self) -> None:
        """Latest-thread lookup should only materialize history when relations lookup fails."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key(
                    "$thread_root",
                    RelationshipType.thread,
                    direction=nio.MessageDirection.back,
                    limit=1,
                ): RuntimeError("unsupported"),
            },
        )

        with patch(
            "mindroom.matrix.client.fetch_thread_history",
            new=AsyncMock(return_value=[make_visible_message(event_id="$reply_latest")]),
        ) as mock_fetch_history:
            event_id = await _latest_thread_event_id(client, "!room:localhost", "$thread_root")

        assert event_id == "$reply_latest"
        mock_fetch_history.assert_awaited_once_with(client, "!room:localhost", "$thread_root")

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_falls_back_to_history_on_child_replacement_error(self) -> None:
        """Replacement lookup failures after a successful child lookup should not escape."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        newest_thread_event = self._make_text_event(
            event_id="$reply_latest",
            sender="@agent:localhost",
            body="Newest reply",
            server_timestamp=3000,
            source_content={
                "body": "Newest reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key(
                    "$thread_root",
                    RelationshipType.thread,
                    direction=nio.MessageDirection.back,
                    limit=1,
                ): [newest_thread_event],
                self._relation_key("$reply_latest", RelationshipType.replacement): RuntimeError("unsupported"),
            },
        )

        with patch(
            "mindroom.matrix.client.fetch_thread_history",
            new=AsyncMock(return_value=[make_visible_message(event_id="$reply_visible")]),
        ) as mock_fetch_history:
            event_id = await _latest_thread_event_id(client, "!room:localhost", "$thread_root")

        assert event_id == "$reply_visible"
        mock_fetch_history.assert_awaited_once_with(client, "!room:localhost", "$thread_root")

    @pytest.mark.asyncio
    async def test_latest_thread_event_id_falls_back_to_history_on_root_replacement_error(self) -> None:
        """Root replacement lookup failures should degrade to history instead of aborting sends."""
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Root message",
            server_timestamp=1000,
            source_content={"body": "Root message"},
        )
        client = self._make_relations_client(
            root_event=root_event,
            relations={
                self._relation_key(
                    "$thread_root",
                    RelationshipType.thread,
                    direction=nio.MessageDirection.back,
                    limit=1,
                ): [],
                self._relation_key("$thread_root", RelationshipType.replacement): RuntimeError("unsupported"),
            },
        )

        with patch(
            "mindroom.matrix.client.fetch_thread_history",
            new=AsyncMock(return_value=[make_visible_message(event_id="$root_visible")]),
        ) as mock_fetch_history:
            event_id = await _latest_thread_event_id(client, "!room:localhost", "$thread_root")

        assert event_id == "$root_visible"
        mock_fetch_history.assert_awaited_once_with(client, "!room:localhost", "$thread_root")

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
                    "url": "mxc://server/edit-sidecar",
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

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [edit_only_event]
        response.end = None
        client.room_messages.return_value = response

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert history == []

    @pytest.mark.asyncio
    async def test_fetch_thread_history_skips_unrelated_missing_edit_before_body_extraction(self) -> None:
        """Avoid edit-body extraction for missing originals unrelated to this thread."""
        client = AsyncMock()

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

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [unrelated_edit]
        response.end = None
        client.room_messages.return_value = response

        with patch("mindroom.matrix.client.extract_edit_body", new_callable=AsyncMock) as mock_extract_edit_body:
            history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert history == []
        mock_extract_edit_body.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_thread_history_edit_only_event_still_visible(self) -> None:
        """Synthesize a history entry when only edit events are returned."""
        client = AsyncMock()

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

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [edit_only_event]
        response.end = None
        client.room_messages.return_value = response

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert len(history) == 1
        assert history[0].event_id == "$missing_original"
        assert history[0].body == "Final answer"

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
