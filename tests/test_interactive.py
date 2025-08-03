"""Tests for the interactive Q&A system using Matrix reactions."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.interactive import InteractiveManager, InteractiveQuestion


@pytest.fixture
def mock_client():
    """Create a mock Matrix client."""
    client = AsyncMock()
    client.user_id = "@mindroom_test:localhost"
    return client


@pytest.fixture
def interactive_manager(mock_client):
    """Create an InteractiveManager instance for testing."""
    with patch("asyncio.create_task"):
        return InteractiveManager(mock_client, "test")


class TestInteractiveQuestion:
    """Test cases for InteractiveQuestion dataclass."""

    def test_interactive_question_creation(self):
        """Test creating an InteractiveQuestion."""
        handler = AsyncMock()
        question = InteractiveQuestion(
            event_id="$test123",
            room_id="!room:localhost",
            thread_id="$thread123",
            question="What's your choice?",
            options={"🚀": "fast", "🐢": "slow", "1": "fast", "2": "slow"},
            handler=handler,
            expires_at=datetime.now() + timedelta(minutes=30),
        )

        assert question.event_id == "$test123"
        assert question.room_id == "!room:localhost"
        assert question.thread_id == "$thread123"
        assert question.question == "What's your choice?"
        assert question.options["🚀"] == "fast"
        assert question.options["1"] == "fast"
        assert len(question.bot_reaction_ids) == 0
        assert len(question.responded_users) == 0


class TestInteractiveManager:
    """Test cases for InteractiveManager class."""

    @pytest.mark.asyncio
    async def test_ask_interactive_success(self, interactive_manager, mock_client):
        """Test successfully sending an interactive question."""
        # Mock successful room_send responses
        mock_message_response = MagicMock(spec=nio.RoomSendResponse)
        mock_message_response.event_id = "$question123"

        mock_reaction_response = MagicMock(spec=nio.RoomSendResponse)
        mock_reaction_response.event_id = "$reaction123"

        mock_client.room_send.side_effect = [
            mock_message_response,  # Message send
            mock_reaction_response,  # First reaction
            mock_reaction_response,  # Second reaction
        ]

        handler = AsyncMock()
        options = [
            ("🚀", "Fast", "fast"),
            ("🐢", "Slow", "slow"),
        ]

        event_id = await interactive_manager.ask_interactive(
            room_id="!room:localhost",
            thread_id="$thread123",
            question="How fast do you want to go?",
            options=options,
            handler=handler,
            timeout_minutes=30,
        )

        assert event_id == "$question123"
        assert "$question123" in interactive_manager.active_questions

        question = interactive_manager.active_questions["$question123"]
        assert question.question == "How fast do you want to go?"
        assert question.options["🚀"] == "fast"
        assert question.options["1"] == "fast"
        assert question.options["🐢"] == "slow"
        assert question.options["2"] == "slow"

    @pytest.mark.asyncio
    async def test_ask_interactive_too_many_options(self, interactive_manager, mock_client):
        """Test that too many options get truncated to 5."""
        mock_response = MagicMock(spec=nio.RoomSendResponse)
        mock_response.event_id = "$question123"
        mock_client.room_send.return_value = mock_response

        handler = AsyncMock()
        options = [
            ("1️⃣", "Option 1", "opt1"),
            ("2️⃣", "Option 2", "opt2"),
            ("3️⃣", "Option 3", "opt3"),
            ("4️⃣", "Option 4", "opt4"),
            ("5️⃣", "Option 5", "opt5"),
            ("6️⃣", "Option 6", "opt6"),  # This should be truncated
        ]

        await interactive_manager.ask_interactive(
            room_id="!room:localhost",
            thread_id=None,
            question="Choose one:",
            options=options,
            handler=handler,
        )

        question = interactive_manager.active_questions["$question123"]
        # Should only have 5 options (plus their numeric equivalents)
        assert len([k for k in question.options if k.isdigit()]) == 5

    @pytest.mark.asyncio
    async def test_on_reaction_valid_response(self, interactive_manager, mock_client):
        """Test handling a valid reaction response."""
        # Set up an active question
        handler = AsyncMock()
        question = InteractiveQuestion(
            event_id="$question123",
            room_id="!room:localhost",
            thread_id="$thread123",
            question="Choose speed:",
            options={"🚀": "fast", "🐢": "slow"},
            handler=handler,
            expires_at=datetime.now() + timedelta(minutes=30),
        )
        interactive_manager.active_questions["$question123"] = question

        # Mock confirmation send
        mock_client.room_send.return_value = MagicMock(spec=nio.RoomSendResponse)

        # Create reaction event
        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@user:localhost"
        event.reacts_to = "$question123"
        event.key = "🚀"

        await interactive_manager.on_reaction(room, event)

        # Handler should be called with the correct value
        handler.assert_called_once_with("!room:localhost", "@user:localhost", "fast", "$thread123")

        # User should be marked as responded
        assert "@user:localhost" in question.responded_users

    @pytest.mark.asyncio
    async def test_on_reaction_expired_question(self, interactive_manager, mock_client):
        """Test handling reaction to an expired question."""
        # Set up an expired question
        handler = AsyncMock()
        question = InteractiveQuestion(
            event_id="$question123",
            room_id="!room:localhost",
            thread_id=None,
            question="Old question",
            options={"✅": "yes", "❌": "no"},
            handler=handler,
            expires_at=datetime.now() - timedelta(minutes=1),  # Expired
            bot_reaction_ids={"✅": "$react1", "❌": "$react2"},
        )
        interactive_manager.active_questions["$question123"] = question

        # Mock redact for cleanup
        mock_client.room_redact.return_value = MagicMock()

        # Create reaction event
        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@user:localhost"
        event.reacts_to = "$question123"
        event.key = "✅"

        await interactive_manager.on_reaction(room, event)

        # Handler should NOT be called
        handler.assert_not_called()

        # Question should be cleaned up
        assert "$question123" not in interactive_manager.active_questions

        # Bot reactions should be redacted
        assert mock_client.room_redact.call_count == 2

    @pytest.mark.asyncio
    async def test_on_reaction_duplicate_response(self, interactive_manager):
        """Test that users can't respond twice."""
        # Set up a question with existing response
        handler = AsyncMock()
        question = InteractiveQuestion(
            event_id="$question123",
            room_id="!room:localhost",
            thread_id=None,
            question="Choose one:",
            options={"A": "a", "B": "b"},
            handler=handler,
            expires_at=datetime.now() + timedelta(minutes=30),
            responded_users={"@user:localhost"},  # Already responded
        )
        interactive_manager.active_questions["$question123"] = question

        # Create reaction event from same user
        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@user:localhost"
        event.reacts_to = "$question123"
        event.key = "B"

        await interactive_manager.on_reaction(room, event)

        # Handler should NOT be called
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_reaction_bot_own_reaction(self, interactive_manager, mock_client):
        """Test that bot ignores its own reactions."""
        # Set up a question
        handler = AsyncMock()
        question = InteractiveQuestion(
            event_id="$question123",
            room_id="!room:localhost",
            thread_id=None,
            question="Choose:",
            options={"✅": "yes"},
            handler=handler,
            expires_at=datetime.now() + timedelta(minutes=30),
        )
        interactive_manager.active_questions["$question123"] = question

        # Create reaction event from bot itself
        room = MagicMock()
        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@mindroom_test:localhost"  # Bot's own ID
        event.reacts_to = "$question123"
        event.key = "✅"

        await interactive_manager.on_reaction(room, event)

        # Handler should NOT be called
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_text_response_valid(self, interactive_manager, mock_client):
        """Test handling valid text responses (1, 2, 3)."""
        # Set up an active question
        handler = AsyncMock()
        question = InteractiveQuestion(
            event_id="$question123",
            room_id="!room:localhost",
            thread_id="$thread123",
            question="Pick a number:",
            options={"1": "first", "2": "second", "3": "third"},
            handler=handler,
            expires_at=datetime.now() + timedelta(minutes=30),
        )
        interactive_manager.active_questions["$question123"] = question

        # Mock confirmation send
        mock_client.room_send.return_value = MagicMock(spec=nio.RoomSendResponse)

        # Create message event
        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.RoomMessageText)
        event.sender = "@user:localhost"
        event.body = "2"
        event.source = {"content": {"m.relates_to": {"rel_type": "m.thread", "event_id": "$thread123"}}}

        await interactive_manager.on_text_response(room, event)

        # Handler should be called with the correct value
        handler.assert_called_once_with("!room:localhost", "@user:localhost", "second", "$thread123")

        # User should be marked as responded
        assert "@user:localhost" in question.responded_users

    @pytest.mark.asyncio
    async def test_on_text_response_invalid(self, interactive_manager):
        """Test that invalid text responses are ignored."""
        # Set up a question
        handler = AsyncMock()
        question = InteractiveQuestion(
            event_id="$question123",
            room_id="!room:localhost",
            thread_id=None,
            question="Pick:",
            options={"1": "one", "2": "two"},
            handler=handler,
            expires_at=datetime.now() + timedelta(minutes=30),
        )
        interactive_manager.active_questions["$question123"] = question

        room = MagicMock()
        room.room_id = "!room:localhost"

        # Test various invalid responses
        invalid_bodies = ["hello", "12", "0", "4", "yes", ""]

        for body in invalid_bodies:
            event = MagicMock(spec=nio.RoomMessageText)
            event.sender = "@user:localhost"
            event.body = body
            event.source = {"content": {}}

            await interactive_manager.on_text_response(room, event)

            # Handler should never be called
            handler.assert_not_called()

    def test_should_create_interactive_question(self):
        """Test detection of interactive code blocks."""
        # Should detect
        assert InteractiveManager.should_create_interactive_question("Here's a question:\n```interactive\n{}\n```")

        assert InteractiveManager.should_create_interactive_question(
            'Text before\n```interactive\n{"question": "test"}\n```\nText after'
        )

        # Should not detect
        assert not InteractiveManager.should_create_interactive_question("Regular message without code block")

        assert not InteractiveManager.should_create_interactive_question("```python\nprint('hello')\n```")

    @pytest.mark.asyncio
    async def test_handle_interactive_response_valid_json(self, interactive_manager, mock_client):
        """Test creating interactive question from valid JSON."""
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

        await interactive_manager.handle_interactive_response("!room:localhost", "$thread123", response_text)

        # Should send clean text first
        first_call = mock_client.room_send.call_args_list[0]
        assert "Let me help you decide." in first_call[1]["content"]["body"]
        assert "Based on your choice" in first_call[1]["content"]["body"]
        assert "```interactive" not in first_call[1]["content"]["body"]

        # Should create question
        assert "$question123" in interactive_manager.active_questions
        question = interactive_manager.active_questions["$question123"]
        assert question.question == "What approach would you prefer?"
        assert question.options["🚀"] == "fast"
        assert question.options["🔍"] == "careful"

    @pytest.mark.asyncio
    async def test_handle_interactive_response_invalid_json(self, interactive_manager, mock_client):
        """Test handling invalid JSON in interactive block."""
        mock_response = MagicMock(spec=nio.RoomSendResponse)
        mock_client.room_send.return_value = mock_response

        response_text = """Here's a question:

```interactive
{invalid json}
```"""

        await interactive_manager.handle_interactive_response("!room:localhost", None, response_text)

        # Should send the original response
        mock_client.room_send.assert_called_once()
        call_args = mock_client.room_send.call_args[1]
        assert "Here's a question:" in call_args["content"]["body"]

        # Should not create any question
        assert len(interactive_manager.active_questions) == 0

    @pytest.mark.asyncio
    async def test_handle_interactive_response_missing_options(self, interactive_manager, mock_client):
        """Test handling JSON without options."""
        mock_response = MagicMock(spec=nio.RoomSendResponse)
        mock_client.room_send.return_value = mock_response

        response_text = """Question:

```interactive
{
    "question": "What now?",
    "options": []
}
```"""

        await interactive_manager.handle_interactive_response("!room:localhost", None, response_text)

        # Should send the clean response
        mock_client.room_send.assert_called_once()

        # Should not create any question
        assert len(interactive_manager.active_questions) == 0

    @pytest.mark.asyncio
    async def test_generic_handler_confirmation_type(self, interactive_manager, mock_client):
        """Test the generic handler for confirmation type questions."""
        mock_response = MagicMock(spec=nio.RoomSendResponse)
        mock_client.room_send.return_value = mock_response

        handler = interactive_manager._create_generic_handler("confirmation")

        # Test "yes" response
        await handler("!room:localhost", "@user:localhost", "yes", "$thread123")
        call_args = mock_client.room_send.call_args[1]
        assert "Great! Proceeding" in call_args["content"]["body"]

        mock_client.reset_mock()

        # Test "no" response
        await handler("!room:localhost", "@user:localhost", "no", "$thread123")
        call_args = mock_client.room_send.call_args[1]
        assert "won't proceed" in call_args["content"]["body"]

    @pytest.mark.asyncio
    async def test_cleanup_expired_question(self, interactive_manager, mock_client):
        """Test cleaning up an expired question."""
        # Add an expired question
        expired_question = InteractiveQuestion(
            event_id="$expired",
            room_id="!room:localhost",
            thread_id=None,
            question="Old question",
            options={"Y": "yes"},
            handler=AsyncMock(),
            expires_at=datetime.now() - timedelta(minutes=1),
            bot_reaction_ids={"Y": "$react1"},
        )

        interactive_manager.active_questions["$expired"] = expired_question

        # Mock room_redact
        mock_client.room_redact.return_value = MagicMock()

        # Clean up the expired question directly
        await interactive_manager._cleanup_question(expired_question)

        # Expired question should be removed
        assert "$expired" not in interactive_manager.active_questions

        # Bot reaction should be redacted
        mock_client.room_redact.assert_called_once()

    def test_cleanup(self, interactive_manager):
        """Test cleanup method clears active questions."""
        # Add some questions
        interactive_manager.active_questions["$q1"] = MagicMock()
        interactive_manager.active_questions["$q2"] = MagicMock()

        interactive_manager.cleanup()

        assert len(interactive_manager.active_questions) == 0


@pytest.mark.asyncio
async def test_interactive_integration_flow(mock_client):
    """Test complete flow from AI response to user reaction."""
    with patch("asyncio.create_task"):
        manager = InteractiveManager(mock_client, "assistant")

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

    await manager.handle_interactive_response("!room:localhost", "$thread123", ai_response)

    # Verify question was created
    assert "$q123" in manager.active_questions
    question = manager.active_questions["$q123"]
    assert question.question == "How would you like me to proceed?"
    assert len(question.options) == 6  # 3 emojis + 3 numbers

    # Step 2: User reacts with emoji
    room = MagicMock()
    room.room_id = "!room:localhost"

    reaction_event = MagicMock(spec=nio.ReactionEvent)
    reaction_event.sender = "@user:localhost"
    reaction_event.reacts_to = "$q123"
    reaction_event.key = "🔍"

    await manager.on_reaction(room, reaction_event)

    # Verify handler was called and confirmation sent
    assert "@user:localhost" in question.responded_users
    # We expect 7 calls: 1 clean text + 1 question + 3 reactions + 1 handler response + 1 confirmation
    assert mock_client.room_send.call_count == 7
