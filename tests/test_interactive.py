"""Tests for interactive response parsing and Matrix reaction buttons."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import interactive


@pytest.fixture
def mock_client() -> AsyncMock:
    """Create a mock Matrix client."""
    client = AsyncMock()
    client.user_id = "@mindroom_test:localhost"
    return client


class TestInteractiveFunctions:
    """Test pure interactive formatting and Matrix button delivery."""

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
        assert response.interactive_metadata is not None
        assert response.interactive_metadata.question_text == "Which option?"
        assert response.interactive_metadata.option_labels == {"✅": "Approve", "1": "Approve"}

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
        assert response.interactive_metadata is not None
        assert response.interactive_metadata.question_text == "Which option?"
        assert response.interactive_metadata.option_labels == {"✅": "Approve", "1": "Approve"}

    def test_parse_and_format_interactive_renders_question_in_place(self) -> None:
        """The question should replace the block where it was written, not move to the end."""
        response_text = """Lock the decision:

```interactive
{
    "question": "Which option?",
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
```

Whichever you pick, tell the other tool."""

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert response.formatted_text == (
            "Lock the decision:\n"
            "\n"
            "Which option?\n"
            "\n"
            "1. ✅ Approve\n"
            "\n"
            "React with an emoji or type the number to respond.\n"
            "\n"
            "Whichever you pick, tell the other tool."
        )
        assert response.option_map == {"✅": "approve", "1": "approve"}

    def test_parse_and_format_interactive_renders_extra_blocks_as_plain_text(self) -> None:
        """Only the first block gets reactions; later blocks render readably instead of as raw JSON."""
        response_text = """First question:

```interactive
{
    "question": "Which option?",
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
```

Second question:

```interactive
{
    "question": "What next?",
    "options": [
        {"emoji": "🔎", "label": "Verify", "value": "verify"},
        {"emoji": "✋", "label": "Hold", "value": "hold"}
    ]
}
```"""

        with patch.object(interactive.logger, "warning") as mock_warning:
            response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert response.formatted_text == (
            "First question:\n"
            "\n"
            "Which option?\n"
            "\n"
            "1. ✅ Approve\n"
            "\n"
            "React with an emoji or type the number to respond.\n"
            "\n"
            "Second question:\n"
            "\n"
            "What next?\n"
            "\n"
            "1. 🔎 Verify\n"
            "2. ✋ Hold"
        )
        assert response.option_map == {"✅": "approve", "1": "approve"}
        assert response.interactive_metadata is not None
        assert response.interactive_metadata.question_text == "Which option?"
        mock_warning.assert_called_once()
        assert mock_warning.call_args.args == (
            "Multiple interactive blocks in one response; only the first gets reaction buttons",
        )
        assert mock_warning.call_args.kwargs["block_count"] == 2

    def test_parse_and_format_interactive_ignores_invalid_options_shape(self) -> None:
        """Malformed option containers should not crash the interactive parser."""
        response_text = """```interactive
{
    "question": "Which option?",
    "options": "approve"
}
```"""

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert response.formatted_text == response_text
        assert response.option_map is None

    def test_parse_and_format_interactive_defaults_null_option_fields(self) -> None:
        """Explicit null option fields should use the same defaults as missing fields."""
        response_text = """```interactive
{
    "question": "Which option?",
    "options": [
        {"emoji": null, "label": null, "value": null}
    ]
}
```"""

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert response.formatted_text == (
            "Which option?\n\n1. ❓ Option\n\nReact with an emoji or type the number to respond."
        )
        assert response.option_map == {"❓": "option", "1": "option"}
        assert response.options_list == [{"emoji": "❓", "label": "Option", "value": "option"}]
        assert response.interactive_metadata is not None
        assert response.interactive_metadata.option_labels == {"❓": "Option", "1": "Option"}

    def test_parse_and_format_interactive_removes_malformed_extra_blocks(self) -> None:
        """Malformed extra blocks should not leak raw interactive fences into the message."""
        response_text = """First question:

```interactive
{
    "question": "Which option?",
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
```

Broken extra:

```interactive
{
    "question": "Bad extra",
    "options":
}
```

Last question:

```interactive
{
    "question": "What next?",
    "options": [
        {"emoji": "🔎", "label": "Verify", "value": "verify"}
    ]
}
```"""

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert "```interactive" not in response.formatted_text
        assert '"question": "Bad extra"' not in response.formatted_text
        assert response.formatted_text == (
            "First question:\n"
            "\n"
            "Which option?\n"
            "\n"
            "1. ✅ Approve\n"
            "\n"
            "React with an emoji or type the number to respond.\n"
            "\n"
            "Broken extra:\n"
            "\n"
            "\n"
            "\n"
            "Last question:\n"
            "\n"
            "What next?\n"
            "\n"
            "1. 🔎 Verify"
        )

    def test_parse_and_format_interactive_removes_unmatched_malformed_extra_blocks(self) -> None:
        """Malformed extra fences that do not match the parser regex should not leak after a valid block."""
        response_text = """First question:

```interactive
{
    "question": "Which option?",
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
```

Broken extra: ```interactive {"question": "Bad extra"}```"""

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert "```interactive" not in response.formatted_text
        assert '"question": "Bad extra"' not in response.formatted_text
        assert response.formatted_text == (
            "First question:\n"
            "\n"
            "Which option?\n"
            "\n"
            "1. ✅ Approve\n"
            "\n"
            "React with an emoji or type the number to respond.\n"
            "\n"
            "Broken extra:"
        )

    def test_parse_and_format_interactive_defaults_null_question_text(self) -> None:
        """Explicit JSON null question text should use the default prompt."""
        response_text = """```interactive
{
    "question": null,
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
```"""

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)

        assert response.interactive_metadata is not None
        assert response.interactive_metadata.question_text == interactive._DEFAULT_QUESTION
        assert interactive._DEFAULT_QUESTION in response.formatted_text

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

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_add_reaction_buttons_uses_each_option_emoji(self, mock_client: AsyncMock) -> None:
        """Each visible option becomes one Matrix annotation."""
        mock_client.room_send.return_value = MagicMock(spec=nio.RoomSendResponse)

        await interactive.add_reaction_buttons(
            mock_client,
            "!room:localhost",
            "$question",
            [{"emoji": "🚀"}, {"emoji": "🔍"}],
        )

        assert [call.kwargs["content"] for call in mock_client.room_send.await_args_list] == [
            {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$question",
                    "key": "🚀",
                },
            },
            {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$question",
                    "key": "🔍",
                },
            },
        ]

    def test_build_selection_prompt_anchors_question_and_escapes_user_text(self) -> None:
        """Selection prompts anchor the durable question without markdown fences."""
        selection = interactive.InteractiveSelection(
            question_event_id="$question:localhost",
            question_text='Choose safely\n```json\n{"breakout": true}\n```',
            selection_key="✅",
            selected_label="Approve",
            selected_value="approve",
            thread_id="$thread:localhost",
        )

        prompt = interactive.build_selection_prompt(selection)

        assert "question_event_id" in prompt
        assert "$question:localhost" in prompt
        assert "question_text" in prompt
        assert "selected_option" in prompt
        assert '"key": "✅"' in prompt
        assert '"label": "Approve"' in prompt
        assert '"value": "approve"' in prompt
        assert "Use the question_event_id and question_text" in prompt
        assert "\n```" not in prompt
