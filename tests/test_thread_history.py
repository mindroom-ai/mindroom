"""Tests for thread history fetching, especially including thread root messages."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.client import fetch_thread_history


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
        # Order doesn't matter, history is sorted by timestamp.
        mock_response.chunk = [router_event, root_event]
        # No more messages
        mock_response.end = None
        client.room_messages.return_value = mock_response

        # Fetch thread history
        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        # Verify both messages are included in chronological order.
        assert len(history) == 2
        assert history[0]["event_id"] == "$thread_root"
        assert history[0]["body"] == "look up Feynman on Wikipedia"
        assert history[0]["sender"] == "@user:localhost"
        assert history[1]["event_id"] == "$router_msg"
        assert history[1]["body"] == "@mindroom_research:localhost could you help with this?"
        assert history[1]["sender"] == "@mindroom_news:localhost"

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

        # Mock response with all message types.
        mock_response = MagicMock(spec=nio.RoomMessagesResponse)
        mock_response.chunk = [other_thread_msg, thread1_msg, room_msg, root_event]
        mock_response.end = None
        client.room_messages.return_value = mock_response

        # Fetch thread history for thread1 only.
        history = await fetch_thread_history(client, "!room:localhost", "$thread1")

        # Should only include thread1 messages.
        assert len(history) == 2
        assert history[0]["event_id"] == "$thread1"
        assert history[1]["event_id"] == "$msg1"
        # Should not include other thread or room messages.
        event_ids = [msg["event_id"] for msg in history]
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

        # Should only include the root message.
        assert len(history) == 1
        assert history[0]["event_id"] == "$thread_root"
        assert history[0]["body"] == "New thread"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_continues_pagination_after_empty_page(self) -> None:
        """Thread history pagination must continue when a page has no thread events."""
        client = AsyncMock()

        unrelated_event = self._make_text_event(
            event_id="$other",
            sender="@user:localhost",
            body="Unrelated room message",
            server_timestamp=3000,
            source_content={"body": "Unrelated room message"},
        )
        root_event = self._make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Thread root",
            server_timestamp=1000,
            source_content={"body": "Thread root"},
        )
        thread_reply = self._make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Reply in thread",
            server_timestamp=2000,
            source_content={
                "body": "Reply in thread",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )

        page_one = MagicMock(spec=nio.RoomMessagesResponse)
        page_one.chunk = [unrelated_event]
        page_one.end = "next_token"
        page_two = MagicMock(spec=nio.RoomMessagesResponse)
        page_two.chunk = [thread_reply, root_event]
        page_two.end = None
        client.room_messages.side_effect = [page_one, page_two]

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert [msg["event_id"] for msg in history] == ["$thread_root", "$reply"]
        assert client.room_messages.await_count == 2

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

        assert [msg["event_id"] for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[1]["body"] == "Final answer"
        assert history[1]["content"]["body"] == "Final answer"

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

        assert [msg["event_id"] for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[1]["body"] == "Final answer"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_edit_without_thread_ignored(self) -> None:
        """Ignore edits that do not include thread metadata in m.new_content."""
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
                    "body": "Should be ignored",
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

        assert [msg["event_id"] for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[1]["body"] == "Original answer"

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
        assert history[0]["event_id"] == "$missing_original"
        assert history[0]["body"] == "Final answer"

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
        assert [msg["event_id"] for msg in history] == ["$thread_root", "$agent_msg"]
        assert history[1]["body"] == "Final answer"

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
        assert [msg["event_id"] for msg in history] == ["$thread_root", "$agent_msg"]
