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
        # Create mock client
        client = AsyncMock()

        # Create mock events
        # 1. The thread root message (user's original message)
        root_event = MagicMock(spec=nio.RoomMessageText)
        root_event.event_id = "$thread_root"
        root_event.sender = "@user:localhost"
        root_event.body = "look up Feynman on Wikipedia"
        root_event.server_timestamp = 1000
        root_event.source = {
            "type": "m.room.message",
            "content": {"body": "look up Feynman on Wikipedia"},
        }

        # 2. Router's message in the thread
        router_event = MagicMock(spec=nio.RoomMessageText)
        router_event.event_id = "$router_msg"
        router_event.sender = "@mindroom_news:localhost"
        router_event.body = "@mindroom_research:localhost could you help with this?"
        router_event.server_timestamp = 2000
        router_event.source = {
            "type": "m.room.message",
            "content": {
                "body": "@mindroom_research:localhost could you help with this?",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        }

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
        assert history[0]["event_id"] == "$thread_root"
        assert history[0]["body"] == "look up Feynman on Wikipedia"
        assert history[0]["sender"] == "@user:localhost"

        assert history[1]["event_id"] == "$router_msg"
        assert history[1]["body"] == "@mindroom_research:localhost could you help with this?"
        assert history[1]["sender"] == "@mindroom_news:localhost"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_only_thread_messages(self) -> None:
        """Test that fetch_thread_history only includes messages from the specific thread."""
        # Create mock client
        client = AsyncMock()

        # Create mock events
        # Thread root
        root_event = MagicMock(spec=nio.RoomMessageText)
        root_event.event_id = "$thread1"
        root_event.sender = "@user:localhost"
        root_event.body = "First thread"
        root_event.server_timestamp = 1000
        root_event.source = {
            "type": "m.room.message",
            "content": {"body": "First thread"},
        }

        # Message in thread 1
        thread1_msg = MagicMock(spec=nio.RoomMessageText)
        thread1_msg.event_id = "$msg1"
        thread1_msg.sender = "@agent:localhost"
        thread1_msg.body = "Reply in thread 1"
        thread1_msg.server_timestamp = 2000
        thread1_msg.source = {
            "type": "m.room.message",
            "content": {
                "body": "Reply in thread 1",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread1",
                },
            },
        }

        # Message in different thread
        other_thread_msg = MagicMock(spec=nio.RoomMessageText)
        other_thread_msg.event_id = "$msg2"
        other_thread_msg.sender = "@agent:localhost"
        other_thread_msg.body = "Reply in different thread"
        other_thread_msg.server_timestamp = 3000
        other_thread_msg.source = {
            "type": "m.room.message",
            "content": {
                "body": "Reply in different thread",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread2",  # Different thread
                },
            },
        }

        # Regular room message (not in any thread)
        room_msg = MagicMock(spec=nio.RoomMessageText)
        room_msg.event_id = "$room_msg"
        room_msg.sender = "@user:localhost"
        room_msg.body = "Regular room message"
        room_msg.server_timestamp = 4000
        room_msg.source = {
            "type": "m.room.message",
            "content": {"body": "Regular room message"},
        }

        # Mock response with all messages
        mock_response = MagicMock(spec=nio.RoomMessagesResponse)
        mock_response.chunk = [other_thread_msg, thread1_msg, room_msg, root_event]
        mock_response.end = None

        client.room_messages.return_value = mock_response

        # Fetch thread history for thread1
        history = await fetch_thread_history(client, "!room:localhost", "$thread1")

        # Should only include thread1 messages
        assert len(history) == 2
        assert history[0]["event_id"] == "$thread1"
        assert history[1]["event_id"] == "$msg1"

        # Should not include other thread or room messages
        event_ids = [msg["event_id"] for msg in history]
        assert "$msg2" not in event_ids
        assert "$room_msg" not in event_ids

    @pytest.mark.asyncio
    async def test_fetch_thread_history_empty_thread(self) -> None:
        """Test fetch_thread_history with a thread that has no replies yet."""
        client = AsyncMock()

        # Only the root message exists
        root_event = MagicMock(spec=nio.RoomMessageText)
        root_event.event_id = "$thread_root"
        root_event.sender = "@user:localhost"
        root_event.body = "New thread"
        root_event.server_timestamp = 1000
        root_event.source = {
            "type": "m.room.message",
            "content": {"body": "New thread"},
        }

        # Mock response
        mock_response = MagicMock(spec=nio.RoomMessagesResponse)
        mock_response.chunk = [root_event]
        mock_response.end = None

        client.room_messages.return_value = mock_response

        # Fetch thread history
        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        # Should only include the root message
        assert len(history) == 1
        assert history[0]["event_id"] == "$thread_root"
        assert history[0]["body"] == "New thread"

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
