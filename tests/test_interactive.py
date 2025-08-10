"""Tests for the interactive Q&A system using Matrix reactions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom import interactive
from mindroom.config import AgentConfig, Config, ModelConfig


@pytest.fixture
def mock_client() -> AsyncMock:
    """Create a mock Matrix client."""
    client = AsyncMock()
    client.user_id = "@mindroom_test:localhost"
    return client


class TestInteractiveFunctions:
    """Test cases for interactive functions."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
            agents={
                "test_agent": AgentConfig(display_name="Test Agent", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

    def test_should_create_interactive_question(self) -> None:
        """Test detection of interactive code blocks."""
        # Should detect - standard format
        assert interactive.should_create_interactive_question("Here's a question:\n```interactive\n{}\n```")

        assert interactive.should_create_interactive_question(
            'Text before\n```interactive\n{"question": "test"}\n```\nText after',
        )

        # Should detect - newline format (agent mistake)
        assert interactive.should_create_interactive_question("Here's a question:\n```\ninteractive\n{}\n```")

        # Should detect - without checkmark
        assert interactive.should_create_interactive_question("Here's a question:\n```interactive\n{}\n```")

        # Should not detect
        assert not interactive.should_create_interactive_question("Regular message without code block")

        assert not interactive.should_create_interactive_question("```python\nprint('hello')\n```")

    @pytest.mark.asyncio
    async def test_handle_interactive_response_valid_json(self, mock_client: AsyncMock) -> None:
        """Test creating interactive question from valid JSON."""
        # Clear any existing questions
        interactive._active_questions.clear()

        # Mock room_send responses
        mock_reaction_response = MagicMock(spec=nio.RoomSendResponse)
        mock_reaction_response.event_id = "$react123"

        mock_client.room_send.side_effect = [
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

        # Test the new approach
        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        formatted_text, option_map, options = response.formatted_text, response.option_map, response.options_list

        # Should format the message correctly
        assert "Let me help you decide." in formatted_text
        assert "Based on your choice" in formatted_text
        assert "What approach would you prefer?" in formatted_text
        assert "1. 🚀 Fast and automated" in formatted_text
        assert "2. 🔍 Careful and manual" in formatted_text
        assert "```interactive" not in formatted_text

        # Should extract options correctly
        assert option_map is not None
        assert options is not None
        assert option_map["🚀"] == "fast"
        assert option_map["🔍"] == "careful"
        assert option_map["1"] == "fast"
        assert option_map["2"] == "careful"

        # Register the question
        event_id = "$question123"
        interactive.register_interactive_question(event_id, "!room:localhost", "$thread123", option_map, "test_agent")

        # Should create question
        assert event_id in interactive._active_questions
        question = interactive._active_questions[event_id]
        assert question.room_id == "!room:localhost"
        assert question.thread_id == "$thread123"
        assert question.options["🚀"] == "fast"
        assert question.options["🔍"] == "careful"
        assert question.options["1"] == "fast"
        assert question.options["2"] == "careful"

        # Add reaction buttons
        await interactive.add_reaction_buttons(mock_client, "!room:localhost", event_id, options)

        # Should have added reactions
        assert mock_client.room_send.call_count == 2

    @pytest.mark.asyncio
    async def test_handle_interactive_response_invalid_json(self, mock_client: AsyncMock) -> None:  # noqa: ARG002
        """Test handling invalid JSON in interactive block."""
        interactive._active_questions.clear()

        response_text = """Here's a question:

```interactive
{invalid json}
```"""

        # Test the new approach - should return original text when JSON is invalid
        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        formatted_text, option_map, options = response.formatted_text, response.option_map, response.options_list

        # Should return original text unchanged
        assert formatted_text == response_text
        assert option_map is None
        assert options is None

        # Should not create any question
        assert len(interactive._active_questions) == 0

    @pytest.mark.asyncio
    async def test_handle_interactive_response_missing_options(self, mock_client: AsyncMock) -> None:  # noqa: ARG002
        """Test handling JSON without options."""
        interactive._active_questions.clear()

        response_text = """Question:

```interactive
{
    "question": "What now?",
    "options": []
}
```"""

        # Test the new approach - should return original text when options are empty
        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        formatted_text, option_map, options = response.formatted_text, response.option_map, response.options_list

        # Should return original text unchanged when no options
        assert formatted_text == response_text
        assert option_map is None
        assert options is None

        # Should not create any question when options are empty
        assert len(interactive._active_questions) == 0

    @pytest.mark.asyncio
    async def test_handle_reaction_valid_response(self, mock_client: AsyncMock) -> None:
        """Test handling a valid reaction response."""
        interactive._active_questions.clear()

        # Set up an active question
        interactive._active_questions["$question123"] = interactive.InteractiveQuestion(
            room_id="!room:localhost",
            thread_id="$thread123",
            options={"🚀": "fast", "🐢": "slow", "1": "fast", "2": "slow"},
            creator_agent="test_agent",
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

        result = await interactive.handle_reaction(mock_client, event, "test_agent", self.config)

        # Should return the selected value and thread_id
        assert result == ("fast", "$thread123")

        # Should NOT send confirmation (user's reaction is the response)
        mock_client.room_send.assert_not_called()

        # Question should be removed
        assert "$question123" not in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_reaction_unknown_event(self, mock_client: AsyncMock) -> None:
        """Test handling reaction to unknown event."""
        interactive._active_questions.clear()

        # Create reaction event for non-existent question
        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@user:localhost"
        event.reacts_to = "$unknown123"
        event.key = "👍"

        result = await interactive.handle_reaction(mock_client, event, "test_agent", self.config)

        # Should return None for unknown reaction
        assert result is None

        # Should not send anything
        mock_client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_reaction_bot_own_reaction(self, mock_client: AsyncMock) -> None:
        """Test that bot ignores its own reactions."""
        interactive._active_questions.clear()

        # Set up a question
        interactive._active_questions["$question123"] = interactive.InteractiveQuestion(
            room_id="!room:localhost",
            thread_id=None,
            options={"✅": "yes"},
            creator_agent="test_agent",
        )

        # Create reaction event from bot itself
        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@mindroom_test:localhost"  # Bot's own ID
        event.reacts_to = "$question123"
        event.key = "✅"

        result = await interactive.handle_reaction(mock_client, event, "test_agent", self.config)

        # Should return None (ignoring own reaction)
        assert result is None

        # Should not send anything
        mock_client.room_send.assert_not_called()

        # Question should still be active
        assert "$question123" in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_text_response_valid(self, mock_client: AsyncMock) -> None:
        """Test handling valid text responses (1, 2, 3)."""
        interactive._active_questions.clear()

        # Set up an active question
        interactive._active_questions["$question123"] = interactive.InteractiveQuestion(
            room_id="!room:localhost",
            thread_id="$thread123",
            options={"1": "first", "2": "second", "3": "third"},
            creator_agent="test_agent",
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

        # Should NOT send confirmation (user's message is the response)
        mock_client.room_send.assert_not_called()

        # Question should be removed
        assert "$question123" not in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_text_response_invalid(self, mock_client: AsyncMock) -> None:
        """Test that invalid text responses are ignored."""
        interactive._active_questions.clear()

        # Set up a question
        interactive._active_questions["$question123"] = interactive.InteractiveQuestion(
            room_id="!room:localhost",
            thread_id=None,
            options={"1": "one", "2": "two"},
            creator_agent="test_agent",
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
    async def test_handle_interactive_response_newline_format(self, mock_client: AsyncMock) -> None:
        """Test creating interactive question from JSON with newline format."""
        # Clear any existing questions
        interactive._active_questions.clear()

        # Mock room_send responses
        mock_reaction_response = MagicMock(spec=nio.RoomSendResponse)
        mock_reaction_response.event_id = "$react456"

        mock_client.room_send.side_effect = [
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

        # Test the new approach
        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        formatted_text, option_map, options = response.formatted_text, response.option_map, response.options_list

        # Should format despite the newline format
        assert "Let me help." in formatted_text
        assert "Choose an option:" in formatted_text
        assert "1. ✅ Yes" in formatted_text
        assert "```" not in formatted_text
        assert "interactive" not in formatted_text

        assert option_map is not None
        assert options is not None
        assert option_map["✅"] == "yes"
        assert option_map["1"] == "yes"

        # Register the question
        event_id = "$question456"
        interactive.register_interactive_question(event_id, "!room:localhost", None, option_map, "test_agent")

        # Should create question despite the format
        assert event_id in interactive._active_questions
        question = interactive._active_questions[event_id]
        assert question.room_id == "!room:localhost"
        assert question.options["✅"] == "yes"
        assert question.options["1"] == "yes"

    @pytest.mark.asyncio
    async def test_complete_flow(self, mock_client: AsyncMock) -> None:
        """Test complete flow from AI response to user reaction."""
        interactive._active_questions.clear()

        # Mock all room_send responses for reactions
        mock_reaction_send = MagicMock(spec=nio.RoomSendResponse)
        mock_reaction_send.event_id = "$r123"

        mock_client.room_send.side_effect = [
            mock_reaction_send,  # First reaction
            mock_reaction_send,  # Second reaction
            mock_reaction_send,  # Third reaction
        ]

        # Step 1: Process AI response with interactive JSON
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

        # Test the new approach
        response = interactive.parse_and_format_interactive(ai_response, extract_mapping=True)
        formatted_text, option_map, options = response.formatted_text, response.option_map, response.options_list

        # Verify formatting
        assert "I can help you with that task." in formatted_text
        assert "How would you like me to proceed?" in formatted_text
        assert "1. ⚡ Quick mode" in formatted_text
        assert "2. 🔍 Detailed analysis" in formatted_text
        assert "3. 📊 Show statistics" in formatted_text
        assert "Just let me know your preference!" in formatted_text

        # Register the question
        event_id = "$q123"
        interactive.register_interactive_question(
            event_id,
            "!room:localhost",
            "$thread123",
            option_map or {},
            "test_agent",
        )

        # Verify question was created
        assert event_id in interactive._active_questions
        question = interactive._active_questions[event_id]
        assert question.room_id == "!room:localhost"
        assert question.thread_id == "$thread123"
        assert len(question.options) == 6  # 3 emojis + 3 numbers

        # Add reaction buttons
        await interactive.add_reaction_buttons(mock_client, "!room:localhost", event_id, options or [])

        # Should have added 3 reactions
        assert mock_client.room_send.call_count == 3

        # Step 2: User reacts with emoji
        room = MagicMock()
        room.room_id = "!room:localhost"

        reaction_event = MagicMock(spec=nio.ReactionEvent)
        reaction_event.sender = "@user:localhost"
        reaction_event.reacts_to = event_id
        reaction_event.key = "🔍"

        result = await interactive.handle_reaction(mock_client, reaction_event, "test_agent", self.config)

        # Verify reaction was processed
        assert result == ("detailed", "$thread123")  # Thread ID from the question
        assert event_id not in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_interactive_response_with_checkmark(self, mock_client: AsyncMock) -> None:
        """Test creating interactive question from JSON with trailing checkmark."""
        # Clear any existing questions
        interactive._active_questions.clear()

        # Mock room_send responses
        mock_reaction_response = MagicMock(spec=nio.RoomSendResponse)
        mock_reaction_response.event_id = "$react789"

        mock_client.room_send.side_effect = [
            mock_reaction_response,  # Reaction
        ]

        response_text = """Let's play rock paper scissors!

```interactive
{
    "question": "What do you choose?",
    "options": [
        {"emoji": "🪨", "label": "Rock", "value": "rock"}
    ]
}
```"""

        # Test the new approach
        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        formatted_text, option_map, options = response.formatted_text, response.option_map, response.options_list

        # Should format the message correctly even with checkmark
        assert "Let's play rock paper scissors!" in formatted_text
        assert "What do you choose?" in formatted_text
        assert "1. 🪨 Rock" in formatted_text
        assert "```interactive" not in formatted_text
        # No checkmark anymore

        # Should extract options correctly
        assert option_map is not None
        assert options is not None
        assert option_map["🪨"] == "rock"
        assert option_map["1"] == "rock"

        # Register the question
        event_id = "$question789"
        interactive.register_interactive_question(event_id, "!room:localhost", "$thread123", option_map, "test_agent")

        # Should create question
        assert event_id in interactive._active_questions
        question = interactive._active_questions[event_id]
        assert question.room_id == "!room:localhost"
        assert question.thread_id == "$thread123"
        assert question.options["🪨"] == "rock"
        assert question.options["1"] == "rock"
