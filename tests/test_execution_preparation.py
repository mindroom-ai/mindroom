"""Tests for history-message preparation helpers."""

from __future__ import annotations

from defusedxml.ElementTree import fromstring

from mindroom.execution_preparation import _collect_history_messages, build_matrix_prompt_with_thread_history
from mindroom.tool_system.events import _TOOL_TRACE_KEY
from tests.conftest import make_visible_message


def test_collect_history_messages_appends_tool_trace_to_body() -> None:
    """History collection should append rendered tool trace blocks to message bodies."""
    messages = [
        make_visible_message(
            sender="@alice:localhost",
            body="Worked on it",
            content={
                _TOOL_TRACE_KEY: {
                    "version": 2,
                    "events": [
                        {
                            "type": "tool_call_completed",
                            "tool_name": "run_shell_command",
                            "args_preview": "cmd=pwd",
                            "result_preview": "/app",
                        },
                        {
                            "type": "tool_call_started",
                            "tool_name": "save_file",
                            "args_preview": "file_name=a.py",
                        },
                    ],
                },
            },
        ),
    ]

    collected = _collect_history_messages(
        messages,
        max_messages=None,
        max_message_length=None,
        missing_sender_label=None,
    )

    assert collected == [
        (
            "@alice:localhost",
            "Worked on it\n\n"
            "[tool:run_shell_command completed]\n"
            "  args: cmd=pwd\n"
            "  result: /app\n"
            "[tool:save_file started]\n"
            "  args: file_name=a.py\n"
            "  result: <not yet returned>",
        ),
    ]


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


def test_collect_history_messages_surfaces_tool_only_message() -> None:
    """Tool-only messages should no longer be dropped when the narrative body is empty."""
    messages = [
        make_visible_message(
            sender="@alice:localhost",
            content={
                _TOOL_TRACE_KEY: {
                    "version": 2,
                    "events": [
                        {
                            "type": "tool_call_started",
                            "tool_name": "run_shell_command",
                            "args_preview": "cmd=sleep 600",
                        },
                    ],
                },
            },
        ),
    ]

    collected = _collect_history_messages(
        messages,
        max_messages=None,
        max_message_length=None,
        missing_sender_label=None,
    )

    assert collected == [
        (
            "@alice:localhost",
            "[tool:run_shell_command started]\n  args: cmd=sleep 600\n  result: <not yet returned>",
        ),
    ]


def test_collect_history_messages_truncates_final_rendered_body() -> None:
    """Length caps should apply after tool traces are appended to the visible body."""
    messages = [
        make_visible_message(
            sender="@alice:localhost",
            body="ok",
            content={
                _TOOL_TRACE_KEY: {
                    "version": 2,
                    "events": [
                        {
                            "type": "tool_call_completed",
                            "tool_name": "run_shell_command",
                            "result_preview": "/app",
                        },
                    ],
                },
            },
        ),
    ]

    collected = _collect_history_messages(
        messages,
        max_messages=None,
        max_message_length=20,
        missing_sender_label=None,
    )

    assert collected[0][0] == "@alice:localhost"
    assert collected[0][1].startswith("ok\n\n[tool:")
    assert collected[0][1].endswith("…")
    assert len(collected[0][1]) == 20


def test_build_matrix_prompt_with_thread_history_truncates_tool_enriched_body_to_max_length() -> None:
    """Rendered Matrix history bodies should respect max_message_length after trace text is appended."""
    thread_history = [
        make_visible_message(
            sender="@alice:localhost",
            body="ok",
            content={
                _TOOL_TRACE_KEY: {
                    "version": 2,
                    "events": [
                        {
                            "type": "tool_call_completed",
                            "tool_name": "run_shell_command",
                            "args_preview": "cmd=echo 1234",
                            "result_preview": "x" * 5000,
                        },
                    ],
                },
            },
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
    assert message.text.startswith("ok\n\n[tool:run_shell_command completed]")
    assert message.text.endswith("…")
    assert len(message.text) <= 200
