"""Tests for the interactive Q&A system using Matrix reactions."""

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

import mindroom.interactive as interactive


@pytest.fixture
def mock_client():
    """Create a mock Matrix client."""
    client = AsyncMock()
    client.user_id = "@mindroom_test:localhost"
    return client


class TestInteractiveFunctions:
    """Test cases for interactive functions."""

    def test_should_create_interactive_question(self):
        """Test detection of interactive code blocks."""
        # Should detect
        assert interactive.should_create_interactive_question("Here's a question:\n```interactive\n{}\n```")

        assert interactive.should_create_interactive_question(
            'Text before\n```interactive\n{"question": "test"}\n```\nText after'
        )

        # Should not detect
        assert not interactive.should_create_interactive_question("Regular message without code block")

        assert not interactive.should_create_interactive_question("```python\nprint('hello')\n```")

    @pytest.mark.asyncio
    async def test_handle_interactive_response_valid_json(self, mock_client):
        """Test creating interactive question from valid JSON."""
        # Clear any existing questions
        interactive._active_questions.clear()

        # Mock room_send responses
        mock_text_response = MagicMock(spec=nio.RoomSendResponse)
        mock_question_response = MagicMock(spec=nio.RoomSendResponse)
        mock_question_response.event_id = "$question123"
        mock_reaction_response = MagicMock(spec=nio.RoomSendResponse)

        # Setup reaction responses with event_id
        mock_reaction_response.event_id = "$react123"

        mock_client.room_send.side_effect = [
            mock_text_response,  # Clean text send
            mock_question_response,  # Question send
            mock_reaction_response,  # Reactions
            mock_reaction_response,
        ]

        response_text = """Let me help you decide.

```interactive
{
    "question": "What approach would you prefer?",
    "type": "preference",
    "options": [
        {"emoji": "🚀", "label": "Fast and automated", "value": "fast"},
        {"emoji": "🔍", "label": "Careful and manual", "value": "careful"}
    ]
}
```

Based on your choice, I'll proceed accordingly."""

        await interactive.handle_interactive_response(mock_client, "!room:localhost", "$thread123", response_text)

        # Should send clean text first
        first_call = mock_client.room_send.call_args_list[0]
        assert "Let me help you decide." in first_call[1]["content"]["body"]
        assert "Based on your choice" in first_call[1]["content"]["body"]
        assert "```interactive" not in first_call[1]["content"]["body"]

        # Should create question
        assert "$question123" in interactive._active_questions
        room_id, thread_id, options = interactive._active_questions["$question123"]
        assert room_id == "!room:localhost"
        assert thread_id == "$thread123"
        assert options["🚀"] == "fast"
        assert options["🔍"] == "careful"
        assert options["1"] == "fast"
        assert options["2"] == "careful"

    @pytest.mark.asyncio
    async def test_handle_interactive_response_invalid_json(self, mock_client):
        """Test handling invalid JSON in interactive block."""
        interactive._active_questions.clear()

        mock_response = MagicMock(spec=nio.RoomSendResponse)
        mock_client.room_send.return_value = mock_response

        response_text = """Here's a question:

```interactive
{invalid json}
```"""

        await interactive.handle_interactive_response(mock_client, "!room:localhost", None, response_text)

        # Should send the original response
        mock_client.room_send.assert_called_once()
        call_args = mock_client.room_send.call_args[1]
        assert "Here's a question:" in call_args["content"]["body"]

        # Should not create any question
        assert len(interactive._active_questions) == 0

    @pytest.mark.asyncio
    async def test_handle_interactive_response_missing_options(self, mock_client):
        """Test handling JSON without options."""
        interactive._active_questions.clear()

        mock_response = MagicMock(spec=nio.RoomSendResponse)
        mock_client.room_send.return_value = mock_response

        response_text = """Question:

```interactive
{
    "question": "What now?",
    "options": []
}
```"""

        await interactive.handle_interactive_response(mock_client, "!room:localhost", None, response_text)

        # Should send the clean response
        mock_client.room_send.assert_called_once()

        # Should not create any question
        assert len(interactive._active_questions) == 0

    @pytest.mark.asyncio
    async def test_handle_reaction_valid_response(self, mock_client):
        """Test handling a valid reaction response."""
        interactive._active_questions.clear()

        # Set up an active question
        interactive._active_questions["$question123"] = (
            "!room:localhost",
            "$thread123",
            {"🚀": "fast", "🐢": "slow", "1": "fast", "2": "slow"},
        )

        # Mock confirmation send
        mock_client.room_send.return_value = MagicMock(spec=nio.RoomSendResponse)

        # Create reaction event
        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@user:localhost"
        event.reacts_to = "$question123"
        event.key = "🚀"

        await interactive.handle_reaction(mock_client, room, event)

        # Should send confirmation
        mock_client.room_send.assert_called_once()
        call_args = mock_client.room_send.call_args[1]
        assert "✅ You selected: fast" in call_args["content"]["body"]

        # Question should be removed
        assert "$question123" not in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_reaction_unknown_event(self, mock_client):
        """Test handling reaction to unknown event."""
        interactive._active_questions.clear()

        # Create reaction event for non-existent question
        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@user:localhost"
        event.reacts_to = "$unknown123"
        event.key = "👍"

        await interactive.handle_reaction(mock_client, room, event)

        # Should not send anything
        mock_client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_reaction_bot_own_reaction(self, mock_client):
        """Test that bot ignores its own reactions."""
        interactive._active_questions.clear()

        # Set up a question
        interactive._active_questions["$question123"] = ("!room:localhost", None, {"✅": "yes"})

        # Create reaction event from bot itself
        room = MagicMock()
        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@mindroom_test:localhost"  # Bot's own ID
        event.reacts_to = "$question123"
        event.key = "✅"

        await interactive.handle_reaction(mock_client, room, event)

        # Should not send anything
        mock_client.room_send.assert_not_called()

        # Question should still be active
        assert "$question123" in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_text_response_valid(self, mock_client):
        """Test handling valid text responses (1, 2, 3)."""
        interactive._active_questions.clear()

        # Set up an active question
        interactive._active_questions["$question123"] = (
            "!room:localhost",
            "$thread123",
            {"1": "first", "2": "second", "3": "third"},
        )

        # Mock confirmation send
        mock_client.room_send.return_value = MagicMock(spec=nio.RoomSendResponse)

        # Create message event
        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.RoomMessageText)
        event.sender = "@user:localhost"
        event.body = "2"
        event.source = {"content": {"m.relates_to": {"rel_type": "m.thread", "event_id": "$thread123"}}}

        await interactive.handle_text_response(mock_client, room, event)

        # Should send confirmation
        mock_client.room_send.assert_called_once()
        call_args = mock_client.room_send.call_args[1]
        assert "✅ You selected: second" in call_args["content"]["body"]

        # Question should be removed
        assert "$question123" not in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_text_response_invalid(self, mock_client):
        """Test that invalid text responses are ignored."""
        interactive._active_questions.clear()

        # Set up a question
        interactive._active_questions["$question123"] = ("!room:localhost", None, {"1": "one", "2": "two"})

        room = MagicMock()
        room.room_id = "!room:localhost"

        # Test various invalid responses
        invalid_bodies = ["hello", "12", "0", "4", "yes", ""]

        for body in invalid_bodies:
            event = MagicMock(spec=nio.RoomMessageText)
            event.sender = "@user:localhost"
            event.body = body
            event.source = {"content": {}}

            await interactive.handle_text_response(mock_client, room, event)

            # Should never send anything
            mock_client.room_send.assert_not_called()

        # Question should still be active
        assert "$question123" in interactive._active_questions

    @pytest.mark.asyncio
    async def test_create_interactive_question_too_many_options(self, mock_client):
        """Test that too many options get truncated to 5."""
        interactive._active_questions.clear()

        mock_response = MagicMock(spec=nio.RoomSendResponse)
        mock_response.event_id = "$question123"
        mock_client.room_send.return_value = mock_response

        options = [
            {"emoji": "1️⃣", "label": "Option 1", "value": "opt1"},
            {"emoji": "2️⃣", "label": "Option 2", "value": "opt2"},
            {"emoji": "3️⃣", "label": "Option 3", "value": "opt3"},
            {"emoji": "4️⃣", "label": "Option 4", "value": "opt4"},
            {"emoji": "5️⃣", "label": "Option 5", "value": "opt5"},
            {"emoji": "6️⃣", "label": "Option 6", "value": "opt6"},  # This should be truncated
        ]

        await interactive.create_interactive_question(mock_client, "!room:localhost", None, "Choose one:", options)

        _, _, saved_options = interactive._active_questions["$question123"]
        # Should only have 5 options (plus their numeric equivalents)
        numeric_keys = [k for k in saved_options if k.isdigit()]
        assert len(numeric_keys) == 5

    @pytest.mark.asyncio
    async def test_complete_flow(self, mock_client):
        """Test complete flow from AI response to user reaction."""
        interactive._active_questions.clear()

        # Mock all room_send responses
        mock_text_send = MagicMock(spec=nio.RoomSendResponse)
        mock_question_send = MagicMock(spec=nio.RoomSendResponse)
        mock_question_send.event_id = "$q123"
        mock_reaction_send = MagicMock(spec=nio.RoomSendResponse)
        mock_reaction_send.event_id = "$r123"
        mock_confirm_send = MagicMock(spec=nio.RoomSendResponse)

        # Use a function to return responses in order
        call_count = 0

        def room_send_side_effect(*args, **kwargs):
            nonlocal call_count
            responses = [
                mock_text_send,  # Initial text
                mock_question_send,  # Question
                mock_reaction_send,  # First reaction
                mock_reaction_send,  # Second reaction
                mock_reaction_send,  # Third reaction
                mock_confirm_send,  # Confirmation
            ]
            if call_count < len(responses):
                response = responses[call_count]
                call_count += 1
                return response
            return mock_confirm_send  # Default response

        mock_client.room_send.side_effect = room_send_side_effect

        # Step 1: AI sends response with interactive JSON
        ai_response = """I can help you with that task.

```interactive
{
    "question": "How would you like me to proceed?",
    "type": "approach",
    "options": [
        {"emoji": "⚡", "label": "Quick mode", "value": "quick"},
        {"emoji": "🔍", "label": "Detailed analysis", "value": "detailed"},
        {"emoji": "📊", "label": "Show statistics", "value": "stats"}
    ]
}
```

Just let me know your preference!"""

        await interactive.handle_interactive_response(mock_client, "!room:localhost", "$thread123", ai_response)

        # Verify question was created
        assert "$q123" in interactive._active_questions
        room_id, thread_id, options = interactive._active_questions["$q123"]
        assert room_id == "!room:localhost"
        assert thread_id == "$thread123"
        assert len(options) == 6  # 3 emojis + 3 numbers

        # Step 2: User reacts with emoji
        room = MagicMock()
        room.room_id = "!room:localhost"

        reaction_event = MagicMock(spec=nio.ReactionEvent)
        reaction_event.sender = "@user:localhost"
        reaction_event.reacts_to = "$q123"
        reaction_event.key = "🔍"

        await interactive.handle_reaction(mock_client, room, reaction_event)

        # Verify confirmation was sent
        assert "$q123" not in interactive._active_questions
        # We expect 6 calls: 1 clean text + 1 question + 3 reactions + 1 confirmation
        assert mock_client.room_send.call_count == 6
