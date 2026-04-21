"""Tests for history-message preparation helpers."""

from __future__ import annotations

from defusedxml.ElementTree import fromstring

from mindroom.execution_preparation import _collect_history_messages, build_matrix_prompt_with_thread_history
from tests.conftest import make_visible_message


def test_collect_history_messages_keeps_visible_body_only() -> None:
    """History collection should ignore Matrix tool-trace metadata and keep only the visible body."""
    messages = [
        make_visible_message(
            sender="@alice:localhost",
            body="Worked on it",
            content={"io.mindroom.tool_trace": {"version": 2, "events": [{"tool_name": "run_shell_command"}]}},
        ),
    ]

    collected = _collect_history_messages(
        messages,
        max_messages=None,
        max_message_length=None,
        missing_sender_label=None,
    )

    assert collected == [("@alice:localhost", "Worked on it")]


def test_collect_history_messages_leaves_no_trace_messages_unchanged() -> None:
    """No-trace history collection should remain byte-identical to the prior output."""
    messages = [make_visible_message(sender="@alice:localhost", body="Earlier context")]

    collected = _collect_history_messages(
        messages,
        max_messages=None,
        max_message_length=None,
        missing_sender_label=None,
    )

    assert collected == [("@alice:localhost", "Earlier context")]


def test_collect_history_messages_drops_tool_only_message() -> None:
    """Tool-only metadata should not be promoted into visible history context."""
    messages = [
        make_visible_message(
            sender="@alice:localhost",
            content={"io.mindroom.tool_trace": {"version": 2, "events": [{"tool_name": "run_shell_command"}]}},
        ),
    ]

    collected = _collect_history_messages(
        messages,
        max_messages=None,
        max_message_length=None,
        missing_sender_label=None,
    )

    assert collected == []


def test_collect_history_messages_truncates_visible_body() -> None:
    """Length caps should apply only to the visible body content."""
    messages = [
        make_visible_message(
            sender="@alice:localhost",
            body="ok" * 20,
            content={"io.mindroom.tool_trace": {"version": 2, "events": [{"tool_name": "run_shell_command"}]}},
        ),
    ]

    collected = _collect_history_messages(
        messages,
        max_messages=None,
        max_message_length=20,
        missing_sender_label=None,
    )

    assert collected[0][0] == "@alice:localhost"
    assert collected[0][1].startswith("okok")
    assert collected[0][1].endswith("…")
    assert len(collected[0][1]) == 20


def test_build_matrix_prompt_with_thread_history_truncates_visible_body_to_max_length() -> None:
    """Rendered Matrix history bodies should respect max_message_length using only visible text."""
    thread_history = [
        make_visible_message(
            sender="@alice:localhost",
            body="ok" * 200,
            content={"io.mindroom.tool_trace": {"version": 2, "events": [{"tool_name": "run_shell_command"}]}},
        ),
    ]

    prompt = build_matrix_prompt_with_thread_history(
        "Follow-up",
        thread_history,
        max_message_length=200,
        current_sender="@bob:localhost",
    )

    conversation_xml = prompt.split("Previous conversation in this thread:\n", 1)[1].split("\n\nCurrent message:\n", 1)[
        0
    ]
    conversation = fromstring(conversation_xml)
    message = conversation.find("msg")

    assert message is not None
    assert message.text is not None
    assert message.text.startswith("okok")
    assert message.text.endswith("…")
    assert len(message.text) <= 200
