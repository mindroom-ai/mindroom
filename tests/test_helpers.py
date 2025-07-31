"""Shared test helpers for Matrix AI bot tests."""

from unittest.mock import AsyncMock

from mindroom.bot import Bot


def mock_room_messages_empty(bot: Bot) -> None:
    """Mock room_messages to return empty thread history."""
    mock_room_messages = AsyncMock()
    mock_room_messages.return_value = AsyncMock(chunk=[], end=None)
    bot.client.room_messages = mock_room_messages


def mock_room_messages_with_history(bot: Bot, thread_id: str, messages: list[tuple[str, str, str]]) -> None:
    """Mock room_messages to return specific thread history.

    Args:
        bot: The bot instance
        thread_id: The thread root event ID
        messages: List of (sender, body, event_id) tuples
    """
    mock_room_messages = AsyncMock()

    # Create mock events
    mock_events = []
    for sender, body, event_id in messages:
        mock_event = AsyncMock()
        mock_event.sender = sender
        mock_event.body = body
        mock_event.event_id = event_id
        mock_event.server_timestamp = 1234567890
        mock_event.source = {
            "type": "m.room.message",
            "content": {
                "body": body,
                "m.relates_to": {
                    "event_id": thread_id,
                    "rel_type": "m.thread",
                },
            },
        }
        mock_events.append(mock_event)

    mock_room_messages.return_value = AsyncMock(chunk=mock_events, end=None)
    bot.client.room_messages = mock_room_messages
