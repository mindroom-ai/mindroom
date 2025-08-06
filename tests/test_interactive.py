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
        # Should detect - standard format
        assert interactive.should_create_interactive_question("Here's a question:\n```interactive\n{}\n```")

        assert interactive.should_create_interactive_question(
            'Text before\n```interactive\n{"question": "test"}\n```\nText after'
        )

        # Should detect - newline format (agent mistake)
        assert interactive.should_create_interactive_question("Here's a question:\n```\ninteractive\n{}\n```")

        # Should not detect
        assert not interactive.should_create_interactive_question("Regular message without code block")

        assert not interactive.should_create_interactive_question("```python\nprint('hello')\n```")

    @pytest.mark.asyncio
    async def test_handle_interactive_response_valid_json(self, mock_client):
        """Test creating interactive question from valid JSON."""
        # Clear any existing questions
        interactive._active_questions.clear()

        # Mock room_send responses
        mock_question_response = MagicMock(spec=nio.RoomSendResponse)
        mock_question_response.event_id = "$question123"
        mock_reaction_response = MagicMock(spec=nio.RoomSendResponse)

        # Setup reaction responses with event_id
        mock_reaction_response.event_id = "$react123"

        mock_client.room_send.side_effect = [
            mock_question_response,  # Question send (now combined)
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

        await interactive.handle_interactive_response(
            mock_client, "!room:localhost", "$thread123", response_text, "test_agent"
        )

        # Should send formatted message with question
        first_call = mock_client.room_send.call_args_list[0]
        assert "Let me help you decide." in first_call[1]["content"]["body"]
        assert "Based on your choice" in first_call[1]["content"]["body"]
        assert "What approach would you prefer?" in first_call[1]["content"]["body"]
        assert "🚀 Fast and automated" in first_call[1]["content"]["body"]
        assert "🔍 Careful and manual" in first_call[1]["content"]["body"]
        assert "```interactive" not in first_call[1]["content"]["body"]

        # Should create question
        assert "$question123" in interactive._active_questions
        room_id, thread_id, options, agent_name = interactive._active_questions["$question123"]
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

        await interactive.handle_interactive_response(mock_client, "!room:localhost", None, response_text, "test_agent")

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

        await interactive.handle_interactive_response(mock_client, "!room:localhost", None, response_text, "test_agent")

        # Should not send anything or create any question when options are empty
        mock_client.room_send.assert_not_called()
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
            "test_agent",
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

        await interactive.handle_reaction(mock_client, room, event, "test_agent")

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

        await interactive.handle_reaction(mock_client, room, event, "test_agent")

        # Should not send anything
        mock_client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_reaction_bot_own_reaction(self, mock_client):
        """Test that bot ignores its own reactions."""
        interactive._active_questions.clear()

        # Set up a question
        interactive._active_questions["$question123"] = ("!room:localhost", None, {"✅": "yes"}, "test_agent")

        # Create reaction event from bot itself
        room = MagicMock()
        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@mindroom_test:localhost"  # Bot's own ID
        event.reacts_to = "$question123"
        event.key = "✅"

        await interactive.handle_reaction(mock_client, room, event, "test_agent")

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
            "test_agent",
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

        await interactive.handle_text_response(mock_client, room, event, "test_agent")

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
        interactive._active_questions["$question123"] = (
            "!room:localhost",
            None,
            {"1": "one", "2": "two"},
            "test_agent",
        )

        room = MagicMock()
        room.room_id = "!room:localhost"

        # Test various invalid responses
        invalid_bodies = ["hello", "12", "0", "4", "yes", ""]

        for body in invalid_bodies:
            event = MagicMock(spec=nio.RoomMessageText)
            event.sender = "@user:localhost"
            event.body = body
            event.source = {"content": {}}

            await interactive.handle_text_response(mock_client, room, event, "test_agent")

            # Should never send anything
            mock_client.room_send.assert_not_called()

        # Question should still be active
        assert "$question123" in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_interactive_response_newline_format(self, mock_client):
        """Test creating interactive question from JSON with newline format."""
        # Clear any existing questions
        interactive._active_questions.clear()

        # Mock room_send responses
        mock_question_response = MagicMock(spec=nio.RoomSendResponse)
        mock_question_response.event_id = "$question456"
        mock_reaction_response = MagicMock(spec=nio.RoomSendResponse)
        mock_reaction_response.event_id = "$react456"

        mock_client.room_send.side_effect = [
            mock_question_response,  # Question send (now combined)
            mock_reaction_response,  # Reaction
        ]

        # Test with newline format (agent mistake)
        response_text = """Let me help.

```
interactive
{
    "question": "Choose an option:",
    "options": [
        {"emoji": "✅", "label": "Yes", "value": "yes"}
    ]
}
```"""

        await interactive.handle_interactive_response(mock_client, "!room:localhost", None, response_text, "test_agent")

        # Should create question despite the format
        assert "$question456" in interactive._active_questions
        room_id, thread_id, options, agent_name = interactive._active_questions["$question456"]
        assert room_id == "!room:localhost"
        assert options["✅"] == "yes"
        assert options["1"] == "yes"

    @pytest.mark.asyncio
    async def test_handle_interactive_response_streaming_mode(self, mock_client):
        """Test creating interactive question in streaming mode (editing existing message)."""
        interactive._active_questions.clear()

        # Mock room_send responses for edit and reactions
        mock_edit_response = MagicMock(spec=nio.RoomSendResponse)
        mock_edit_response.event_id = "$edit123"
        mock_reaction_response = MagicMock(spec=nio.RoomSendResponse)

        mock_client.room_send.side_effect = [
            mock_edit_response,  # Edit message
            mock_reaction_response,  # First reaction
            mock_reaction_response,  # Second reaction
        ]

        response_text = """I'll help you with that.

```interactive
{
    "question": "How should I proceed?",
    "options": [
        {"emoji": "⚡", "label": "Fast", "value": "fast"},
        {"emoji": "🐢", "label": "Slow", "value": "slow"}
    ]
}
```"""

        # Simulate streaming mode with existing event_id
        existing_event_id = "$existing123"
        await interactive.handle_interactive_response(
            mock_client,
            "!room:localhost",
            "$thread123",
            response_text,
            "test_agent",
            response_already_sent=True,
            event_id=existing_event_id,
        )

        # Should edit the existing message
        first_call = mock_client.room_send.call_args_list[0]
        assert first_call[1]["content"]["m.relates_to"]["rel_type"] == "m.replace"
        assert first_call[1]["content"]["m.relates_to"]["event_id"] == existing_event_id

        # Check edited content
        assert "I'll help you with that." in first_call[1]["content"]["body"]
        assert "How should I proceed?" in first_call[1]["content"]["body"]
        assert "⚡ Fast" in first_call[1]["content"]["body"]
        assert "```interactive" not in first_call[1]["content"]["body"]

        # Should store question with existing event_id
        assert existing_event_id in interactive._active_questions

    @pytest.mark.asyncio
    async def test_complete_flow(self, mock_client):
        """Test complete flow from AI response to user reaction."""
        interactive._active_questions.clear()

        # Mock all room_send responses
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
                mock_question_send,  # Question (now combined)
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

        await interactive.handle_interactive_response(
            mock_client, "!room:localhost", "$thread123", ai_response, "test_agent"
        )

        # Verify question was created
        assert "$q123" in interactive._active_questions
        room_id, thread_id, options, agent_name = interactive._active_questions["$q123"]
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

        await interactive.handle_reaction(mock_client, room, reaction_event, "test_agent")

        # Verify confirmation was sent
        assert "$q123" not in interactive._active_questions
        # We expect 5 calls: 1 question + 3 reactions + 1 confirmation
        assert mock_client.room_send.call_count == 5
