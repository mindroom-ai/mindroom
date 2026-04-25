"""Tests for the interactive Q&A system using Matrix reactions."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import interactive
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths


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
        interactive._active_questions.clear()
        interactive._dirty_question_ids.clear()
        interactive._deleted_question_ids.clear()
        interactive._persistence_file = None
        interactive._persistence_lock_file = None
        runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
        self.config = bind_runtime_paths(
            Config(
                agents={
                    "test_agent": AgentConfig(display_name="Test Agent", rooms=["#test:example.org"]),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
            runtime_paths,
        )

    def teardown_method(self) -> None:
        """Reset interactive module state between tests."""
        interactive._active_questions.clear()
        interactive._dirty_question_ids.clear()
        interactive._deleted_question_ids.clear()
        interactive._persistence_file = None
        interactive._persistence_lock_file = None

    def test_should_create_interactive_question(self) -> None:
        """Test detection of interactive code blocks."""
        # Should detect - standard format
        assert interactive.should_create_interactive_question("Here's a question:\n```interactive\n{}\n```")

        assert interactive.should_create_interactive_question(
            'Text before\n```interactive\n{"question": "test"}\n```\nText after',
        )

        # Should detect - newline format (agent mistake)
        assert interactive.should_create_interactive_question("Here's a question:\n```\ninteractive\n{}\n```")

        # Should detect - common LLM variations
        assert interactive.should_create_interactive_question("Here's a question:\n``` interactive\n{}\n```")
        assert interactive.should_create_interactive_question("Here's a question:\n```Interactive\n{}\n```")
        assert interactive.should_create_interactive_question("Here's a question:\n    ```interactive\n{}\n    ```")
        assert interactive.should_create_interactive_question("Here's a question:\n```interactive json\n{}\n```")
        assert interactive.should_create_interactive_question(
            'Please choose: ```interactive\n{"question": "test"}\n```',
        )

        # Should detect - without checkmark
        assert interactive.should_create_interactive_question("Here's a question:\n```interactive\n{}\n```")

        # Should not detect
        assert not interactive.should_create_interactive_question("Regular message without code block")

        assert not interactive.should_create_interactive_question("```python\nprint('hello')\n```")
        assert not interactive.should_create_interactive_question('```\n{"question": "test"}\n```')
        assert not interactive.should_create_interactive_question("```interactive.py\nprint('hello')\n```")
        assert not interactive.should_create_interactive_question("```\ninteractive = True\nprint('hello')\n```")

    @pytest.mark.parametrize(
        "response_text",
        [
            """Please choose.

``` interactive
{
    "question": "Which option?",
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
```""",
            """Please choose.

```Interactive
{
    "question": "Which option?",
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
```""",
            """Please choose.

    ```interactive
{
    "question": "Which option?",
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
    ```""",
            """Please choose.

```interactive json
{
    "question": "Which option?",
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
```""",
        ],
    )
    def test_parse_and_format_interactive_matches_common_variants(self, response_text: str) -> None:
        """Parser should handle common interactive fence variants."""
        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert "Please choose." in response.formatted_text
        assert "Which option?" in response.formatted_text
        assert "1. ✅ Approve" in response.formatted_text
        assert "```" not in response.formatted_text
        assert response.option_map == {"✅": "approve", "1": "approve"}
        assert response.options_list == [{"emoji": "✅", "label": "Approve", "value": "approve"}]

    def test_parse_and_format_interactive_accepts_inline_intro_before_fence(self) -> None:
        """Parser should handle prose immediately before the opening fence."""
        response_text = """Please choose: ```interactive
{
    "question": "Which option?",
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
```"""

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert response.formatted_text.startswith("Please choose:")
        assert "Which option?" in response.formatted_text
        assert "1. ✅ Approve" in response.formatted_text
        assert response.option_map == {"✅": "approve", "1": "approve"}
        assert response.options_list == [{"emoji": "✅", "label": "Approve", "value": "approve"}]

    def test_parse_and_format_interactive_logs_warning_when_block_does_not_match(self) -> None:
        """Malformed interactive-looking blocks should log a warning."""
        response_text = 'Malformed block: ```interactive {"question": "test"}```'

        with patch.object(interactive.logger, "warning") as mock_warning:
            response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert response.formatted_text == response_text
        assert response.option_map is None
        assert response.options_list is None
        mock_warning.assert_called_once()
        assert mock_warning.call_args.args == ("Interactive block not parsed",)
        assert "interactive" in mock_warning.call_args.kwargs["preview"].lower()

    @pytest.mark.parametrize(
        "response_text",
        [
            """To make the widget interactive, update the example.

```python
interactive = True
print("hello")
```""",
            """```interactive.py
print("hello")
```""",
            """```
interactive = True
print("hello")
```""",
        ],
    )
    def test_parse_and_format_interactive_skips_false_positive_warnings(self, response_text: str) -> None:
        """Non-interactive code blocks should not log interactive warnings."""
        with patch.object(interactive.logger, "warning") as mock_warning:
            response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert response.formatted_text == response_text
        assert response.option_map is None
        assert response.options_list is None
        mock_warning.assert_not_called()

    def test_parse_and_format_interactive_skips_warning_for_closing_fence_followed_by_prose(self) -> None:
        """Closing fences should not be treated as interactive openings."""
        response_text = """Docs:
```
text
```
interactive"""

        with patch.object(interactive.logger, "warning") as mock_warning:
            response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert response.formatted_text == response_text
        assert response.option_map is None
        assert response.options_list is None
        mock_warning.assert_not_called()

    @pytest.mark.parametrize("payload", ["[]", "true", "42"])
    def test_parse_and_format_interactive_rejects_non_object_json_payloads(self, payload: str) -> None:
        """Interactive payloads must decode to objects."""
        response_text = f"```Interactive\n{payload}\n```"

        with patch.object(interactive.logger, "warning") as mock_warning:
            response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert response.formatted_text == response_text
        assert response.option_map is None
        assert response.options_list is None
        mock_warning.assert_called_once()
        assert mock_warning.call_args.args == ("Interactive JSON payload must be an object",)

    def test_parse_interactive_with_prior_fenced_blocks(self) -> None:
        """Interactive parsing should not be confused by earlier fenced blocks."""
        response_text = """Here is code:

```python
print("hello")
```

And sample JSON:

```json
{"ok": true}
```

```interactive
{
    "question": "Choose one:",
    "options": [
        {"emoji": "1️⃣", "label": "One", "value": "one"}
    ]
}
```
"""

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        formatted_text, option_map, options = response.formatted_text, response.option_map, response.options_list

        assert "Choose one:" in formatted_text
        assert "1. 1️⃣ One" in formatted_text
        assert "```interactive" not in formatted_text
        assert option_map is not None
        assert options is not None
        assert option_map["1"] == "one"
        assert option_map["1️⃣"] == "one"

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

        with patch.object(interactive.logger, "warning") as mock_warning:
            # Test the new approach - should return original text when JSON is invalid
            response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
            formatted_text, option_map, options = response.formatted_text, response.option_map, response.options_list

        # Should return original text unchanged
        assert formatted_text == response_text
        assert option_map is None
        assert options is None
        mock_warning.assert_called_once()
        assert mock_warning.call_args.args == ("Interactive JSON parse failed",)
        assert "invalid json" in mock_warning.call_args.kwargs["preview"]

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
        interactive._active_questions["$question123"] = interactive._InteractiveQuestion(
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

        result = await interactive.handle_reaction(
            mock_client,
            event,
            "test_agent",
            self.config,
            runtime_paths_for(self.config),
        )

        assert result == interactive.InteractiveSelection(
            question_event_id="$question123",
            selection_key="🚀",
            selected_value="fast",
            thread_id="$thread123",
        )

        # Should NOT send confirmation (user's reaction is the response)
        mock_client.room_send.assert_not_called()

        # Question should be removed
        assert "$question123" not in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_reaction_accepts_old_persisted_question(self, mock_client: AsyncMock) -> None:
        """Questions should stay answerable even when their timestamp is old."""
        interactive._active_questions.clear()
        interactive._active_questions["$question123"] = interactive._InteractiveQuestion(
            room_id="!room:localhost",
            thread_id="$thread123",
            options={"🚀": "fast", "1": "fast"},
            creator_agent="test_agent",
            created_at=0.0,
        )

        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@user:localhost"
        event.reacts_to = "$question123"
        event.key = "🚀"

        result = await interactive.handle_reaction(
            mock_client,
            event,
            "test_agent",
            self.config,
            runtime_paths_for(self.config),
        )

        assert result == interactive.InteractiveSelection(
            question_event_id="$question123",
            selection_key="🚀",
            selected_value="fast",
            thread_id="$thread123",
        )
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

        result = await interactive.handle_reaction(
            mock_client,
            event,
            "test_agent",
            self.config,
            runtime_paths_for(self.config),
        )

        # Should return None for unknown reaction
        assert result is None

        # Should not send anything
        mock_client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_reaction_bot_own_reaction(self, mock_client: AsyncMock) -> None:
        """Test that bot ignores its own reactions."""
        interactive._active_questions.clear()

        # Set up a question
        interactive._active_questions["$question123"] = interactive._InteractiveQuestion(
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

        result = await interactive.handle_reaction(
            mock_client,
            event,
            "test_agent",
            self.config,
            runtime_paths_for(self.config),
        )

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
        interactive._active_questions["$question123"] = interactive._InteractiveQuestion(
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

        await interactive.handle_text_response(
            mock_client,
            room,
            event,
            "test_agent",
            resolved_thread_id="$thread123",
        )

        # Should NOT send confirmation (user's message is the response)
        mock_client.room_send.assert_not_called()

        # Question should be removed
        assert "$question123" not in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_text_response_accepts_old_persisted_question(self, mock_client: AsyncMock) -> None:
        """Old questions should stay answerable by text reply."""
        interactive._active_questions.clear()
        interactive._active_questions["$question123"] = interactive._InteractiveQuestion(
            room_id="!room:localhost",
            thread_id="$thread123",
            options={"1": "first"},
            creator_agent="test_agent",
            created_at=0.0,
        )

        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.RoomMessageText)
        event.sender = "@user:localhost"
        event.body = "1"
        event.source = {"content": {"m.relates_to": {"rel_type": "m.thread", "event_id": "$thread123"}}}

        result = await interactive.handle_text_response(
            mock_client,
            room,
            event,
            "test_agent",
            resolved_thread_id="$thread123",
        )

        assert result == interactive.InteractiveSelection(
            question_event_id="$question123",
            selection_key="1",
            selected_value="first",
            thread_id="$thread123",
        )
        assert "$question123" not in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_text_response_refreshes_from_disk_before_matching(
        self,
        mock_client: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Text replies should not consume questions another process already removed from disk."""
        interactive.init_persistence(tmp_path)
        persistence_file = tmp_path / "tracking" / "interactive_questions.json"
        interactive.register_interactive_question(
            "$question123",
            "!room:localhost",
            "$thread123",
            {"1": "first"},
            "test_agent",
        )
        persistence_file.write_text("{}")

        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.RoomMessageText)
        event.sender = "@user:localhost"
        event.body = "1"
        event.source = {"content": {"m.relates_to": {"rel_type": "m.thread", "event_id": "$thread123"}}}

        result = await interactive.handle_text_response(
            mock_client,
            room,
            event,
            "test_agent",
            resolved_thread_id="$thread123",
        )

        assert result is None
        assert interactive._active_questions == {}

    @pytest.mark.asyncio
    async def test_handle_text_response_invalid(self, mock_client: AsyncMock) -> None:
        """Test that invalid text responses are ignored."""
        interactive._active_questions.clear()

        # Set up a question
        interactive._active_questions["$question123"] = interactive._InteractiveQuestion(
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

            result = await interactive.handle_text_response(
                mock_client,
                room,
                event,
                "test_agent",
                resolved_thread_id=None,
            )

            assert result is None
            mock_client.room_send.assert_not_called()

        assert "$question123" in interactive._active_questions

    @pytest.mark.asyncio
    async def test_handle_text_response_uses_resolved_thread_id_for_plain_reply(
        self,
        mock_client: AsyncMock,
    ) -> None:
        """Plain numeric replies should match threaded prompts via canonical resolved thread scope."""
        interactive._active_questions.clear()
        interactive._active_questions["$question123"] = interactive._InteractiveQuestion(
            room_id="!room:localhost",
            thread_id="$thread123",
            options={"1": "first"},
            creator_agent="test_agent",
        )

        room = MagicMock()
        room.room_id = "!room:localhost"

        event = MagicMock(spec=nio.RoomMessageText)
        event.sender = "@user:localhost"
        event.body = "1"
        event.source = {
            "content": {
                "body": "1",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-reply:localhost"}},
            },
        }

        result = await interactive.handle_text_response(
            mock_client,
            room,
            event,
            "test_agent",
            resolved_thread_id="$thread123",
        )

        assert result == interactive.InteractiveSelection(
            question_event_id="$question123",
            selection_key="1",
            selected_value="first",
            thread_id="$thread123",
        )
        mock_client.room_send.assert_not_called()
        assert "$question123" not in interactive._active_questions

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

        result = await interactive.handle_reaction(
            mock_client,
            reaction_event,
            "test_agent",
            self.config,
            runtime_paths_for(self.config),
        )

        assert result == interactive.InteractiveSelection(
            question_event_id=event_id,
            selection_key="🔍",
            selected_value="detailed",
            thread_id="$thread123",
        )
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

    @pytest.mark.asyncio
    async def test_interactive_question_persistence_reload_and_consume(
        self,
        mock_client: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Registered questions should survive reload and be removed from disk when consumed."""
        interactive.init_persistence(tmp_path)
        persistence_file = tmp_path / "tracking" / "interactive_questions.json"
        option_map = {"✅": "yes", "1": "yes"}

        interactive.register_interactive_question(
            "$question123",
            "!room:localhost",
            "$thread123",
            option_map,
            "test_agent",
        )

        persisted = json.loads(persistence_file.read_text())
        assert persisted["$question123"]["creator_agent"] == "test_agent"
        assert persisted["$question123"]["created_at"] > 0

        interactive._active_questions.clear()
        interactive.init_persistence(tmp_path)

        restored = interactive._active_questions["$question123"]
        assert restored.room_id == "!room:localhost"
        assert restored.thread_id == "$thread123"
        assert restored.options == option_map
        assert restored.creator_agent == "test_agent"
        assert restored.created_at > 0

        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@user:localhost"
        event.reacts_to = "$question123"
        event.key = "✅"

        result = await interactive.handle_reaction(
            mock_client,
            event,
            "test_agent",
            self.config,
            runtime_paths_for(self.config),
        )

        assert result == interactive.InteractiveSelection(
            question_event_id="$question123",
            selection_key="✅",
            selected_value="yes",
            thread_id="$thread123",
        )
        assert interactive._active_questions == {}
        assert json.loads(persistence_file.read_text()) == {}

    def test_clear_interactive_question_persists_removal_across_reload(self, tmp_path: Path) -> None:
        """Clearing a question should remove it from disk so reloads do not resurrect it."""
        interactive.init_persistence(tmp_path)
        persistence_file = tmp_path / "tracking" / "interactive_questions.json"

        interactive.register_interactive_question(
            "$question123",
            "!room:localhost",
            "$thread123",
            {"1": "yes", "✅": "yes"},
            "test_agent",
        )

        interactive.clear_interactive_question("$question123")

        assert json.loads(persistence_file.read_text()) == {}

        interactive._cleanup()
        interactive.init_persistence(tmp_path)

        assert interactive._active_questions == {}

    def test_init_persistence_keeps_old_questions(self, tmp_path: Path) -> None:
        """Old persisted questions should still load on startup."""
        persistence_file = tmp_path / "tracking" / "interactive_questions.json"
        persistence_file.parent.mkdir(parents=True, exist_ok=True)
        persistence_file.write_text(
            json.dumps(
                {
                    "$question123": {
                        "room_id": "!room:localhost",
                        "thread_id": "$thread123",
                        "options": {"1": "yes"},
                        "creator_agent": "test_agent",
                        "created_at": 0.0,
                    },
                },
            ),
        )

        interactive.init_persistence(tmp_path)

        restored = interactive._active_questions["$question123"]
        assert restored.thread_id == "$thread123"
        assert restored.options == {"1": "yes"}
        assert restored.created_at == 0.0

    def test_init_persistence_starts_fresh_on_corrupt_json(self, tmp_path: Path) -> None:
        """Corrupt persistence should be cleared so future saves can recover."""
        persistence_file = tmp_path / "tracking" / "interactive_questions.json"
        persistence_file.parent.mkdir(parents=True, exist_ok=True)
        persistence_file.write_text("{not valid json")

        interactive.init_persistence(tmp_path)

        assert interactive._active_questions == {}
        assert not persistence_file.exists()

        interactive.register_interactive_question(
            "$question123",
            "!room:localhost",
            "$thread123",
            {"1": "yes"},
            "test_agent",
        )

        persisted = json.loads(persistence_file.read_text())
        assert persisted["$question123"]["creator_agent"] == "test_agent"

    def test_init_persistence_starts_fresh_on_missing_required_field(self, tmp_path: Path) -> None:
        """Structurally corrupt persistence should be cleared so future saves can recover."""
        persistence_file = tmp_path / "tracking" / "interactive_questions.json"
        persistence_file.parent.mkdir(parents=True, exist_ok=True)
        persistence_file.write_text(
            json.dumps(
                {
                    "$question123": {
                        "room_id": "!room:localhost",
                        "thread_id": "$thread123",
                        "options": {"1": "yes"},
                        "created_at": time.time(),
                    },
                },
            ),
        )

        interactive.init_persistence(tmp_path)

        assert interactive._active_questions == {}
        assert not persistence_file.exists()

    def test_save_keeps_existing_file_when_atomic_write_is_interrupted(self, tmp_path: Path) -> None:
        """A failed temp-file write should leave the last committed JSON untouched."""
        interactive.init_persistence(tmp_path)
        persistence_file = tmp_path / "tracking" / "interactive_questions.json"
        interactive.register_interactive_question(
            "$question123",
            "!room:localhost",
            "$thread123",
            {"1": "yes"},
            "test_agent",
        )
        original_contents = persistence_file.read_text()

        class _InterruptedWriteError(RuntimeError):
            """Sentinel write failure used to simulate an interrupted persistence attempt."""

        def _partial_dump(*args: object, **kwargs: object) -> None:  # noqa: ARG001
            file_obj = args[1]
            assert hasattr(file_obj, "write")
            file_obj.write("{")
            file_obj.flush()
            raise _InterruptedWriteError

        with patch("mindroom.interactive.json.dump", side_effect=_partial_dump):
            interactive.register_interactive_question(
                "$question456",
                "!room:localhost",
                "$thread123",
                {"2": "no"},
                "test_agent",
            )

        assert persistence_file.read_text() == original_contents

        interactive._cleanup()
        interactive.init_persistence(tmp_path)

        assert set(interactive._active_questions) == {"$question123"}

    def test_save_merges_with_existing_file_when_local_snapshot_is_stale(self, tmp_path: Path) -> None:
        """Saving a new local question should preserve questions already persisted by another process."""
        persistence_file = tmp_path / "tracking" / "interactive_questions.json"
        persistence_file.parent.mkdir(parents=True, exist_ok=True)
        persistence_file.write_text(
            json.dumps(
                {
                    "$question123": {
                        "room_id": "!room:localhost",
                        "thread_id": "$thread123",
                        "options": {"1": "yes"},
                        "creator_agent": "test_agent",
                        "created_at": time.time(),
                    },
                },
            ),
        )

        interactive._persistence_file = persistence_file
        interactive._persistence_lock_file = tmp_path / "tracking" / "interactive_questions.lock"
        interactive._active_questions = {
            "$question456": interactive._InteractiveQuestion(
                room_id="!room:localhost",
                thread_id="$thread123",
                options={"2": "no"},
                creator_agent="test_agent",
            ),
        }
        interactive._dirty_question_ids.add("$question456")

        with interactive._thread_lock:
            interactive._save_active_questions_locked()

        persisted = json.loads(persistence_file.read_text())
        assert set(persisted) == {"$question123", "$question456"}

    @pytest.mark.asyncio
    async def test_handle_reaction_refreshes_from_disk_before_consuming(
        self,
        mock_client: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Reactions should not consume questions another process already removed from disk."""
        interactive.init_persistence(tmp_path)
        persistence_file = tmp_path / "tracking" / "interactive_questions.json"
        interactive.register_interactive_question(
            "$question123",
            "!room:localhost",
            "$thread123",
            {"✅": "yes", "1": "yes"},
            "test_agent",
        )
        persistence_file.write_text("{}")

        event = MagicMock(spec=nio.ReactionEvent)
        event.sender = "@user:localhost"
        event.reacts_to = "$question123"
        event.key = "✅"

        result = await interactive.handle_reaction(
            mock_client,
            event,
            "test_agent",
            self.config,
            runtime_paths_for(self.config),
        )

        assert result is None
        assert interactive._active_questions == {}

    def test_save_rebuilds_from_in_memory_snapshot_when_file_is_corrupt(self, tmp_path: Path) -> None:
        """A corrupt persisted file should be rebuilt from the full live in-memory snapshot."""
        interactive.init_persistence(tmp_path)
        persistence_file = tmp_path / "tracking" / "interactive_questions.json"

        interactive.register_interactive_question(
            "$question123",
            "!room:localhost",
            "$thread123",
            {"1": "yes"},
            "test_agent",
        )

        persistence_file.write_text("{not valid json")

        interactive.register_interactive_question(
            "$question456",
            "!room:localhost",
            "$thread123",
            {"2": "no"},
            "test_agent",
        )

        persisted = json.loads(persistence_file.read_text())
        assert set(persisted) == {"$question123", "$question456"}
