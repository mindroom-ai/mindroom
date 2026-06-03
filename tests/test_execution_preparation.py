"""Tests for history-message preparation helpers."""

from __future__ import annotations

from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.execution_preparation import (
    _build_thread_history_messages,
    _build_unseen_context_messages,
    _fallback_static_token_budget,
)
from mindroom.tool_system.events import ToolTraceEntry, build_tool_trace_content
from tests.conftest import make_visible_message


def _config() -> Config:
    return Config.model_validate({})


def _tool_trace_content() -> dict[str, object]:
    content = build_tool_trace_content(
        [ToolTraceEntry(type="tool_call_completed", tool_name="run_shell_command")],
    )
    assert content is not None
    return content


def test_fallback_static_token_budget_preserves_context_window_bounds() -> None:
    """Fallback static budgeting should keep missing and reserve-clamped bounds."""
    assert _fallback_static_token_budget(context_window=None, reserve_tokens=100) is None
    assert _fallback_static_token_budget(context_window=0, reserve_tokens=100) is None
    assert _fallback_static_token_budget(context_window=1_000, reserve_tokens=800) == 500
    assert _fallback_static_token_budget(context_window=1_000, reserve_tokens=100) == 900


def test_fallback_thread_history_caps_long_messages_without_dropping_them() -> None:
    """Oversized Matrix fallback messages should stay in context with a capped body."""
    long_body = "x" * 201
    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@alice:localhost",
                body=long_body,
                event_id="$long",
            ),
        ],
        response_sender_id="@mindroom_team:localhost",
        config=_config(),
        max_message_length=200,
    )

    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[0].content == f"@alice:localhost: {'x' * 199}…"
    assert long_body not in str(messages[0].content)
    assert messages[1].content == "Current request"


def test_fallback_thread_history_strips_visible_tool_markers_from_assistant_context() -> None:
    """Visible Matrix tool markers should not train the model to echo fake tool calls."""
    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@mindroom_code:localhost",
                body=(
                    "Checking status.\n\n"
                    "🔧 `run_shell_command` [1]\n\n"
                    "Still checking.\n\n"
                    "🔧 `read_file` [2]\n\n"
                    "---\n\n"
                    "Done."
                ),
                event_id="$assistant",
            ),
        ],
        response_sender_id="@mindroom_code:localhost",
        config=_config(),
    )

    assert messages[0].role == "assistant"
    assert messages[0].content == "Checking status.\n\n\nStill checking.\n\n\nDone."
    assert "🔧" not in str(messages[0].content)


def test_fallback_thread_history_drops_marker_only_messages_from_context() -> None:
    """Marker-only visible messages should not become empty assistant context turns."""
    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@mindroom_code:localhost",
                body="🔧 `run_shell_command` [1]\n\n🔧 `read_file` [2]",
                event_id="$markers",
            ),
        ],
        response_sender_id="@mindroom_code:localhost",
        config=_config(),
    )

    assert len(messages) == 1
    assert messages[0].content == "Current request"


def test_fallback_thread_history_preserves_user_authored_tool_marker_text() -> None:
    """Human-authored marker-shaped text is conversation content, not MindRoom display chrome."""
    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@alice:localhost",
                body="Please see:\n\n🔧 `run_shell_command` [1]\n\nActual content",
                event_id="$user",
            ),
        ],
        response_sender_id="@mindroom_code:localhost",
        config=_config(),
    )

    assert messages[0].role == "user"
    assert messages[0].content == "@alice:localhost: Please see:\n\n🔧 `run_shell_command` [1]\n\nActual content"


def test_fallback_thread_history_strips_structured_tool_markers_from_labeled_context() -> None:
    """Structured MindRoom tool trace metadata identifies marker lines as display chrome."""
    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@mindroom_research:localhost",
                body="Please see:\n\n🔧 `run_shell_command` [1]\n\nActual content",
                event_id="$agent",
                content={
                    "body": "Please see:\n\n🔧 `run_shell_command` [1]\n\nActual content",
                    **_tool_trace_content(),
                },
            ),
        ],
        response_sender_id="@mindroom_code:localhost",
        config=_config(),
    )

    assert messages[0].role == "user"
    assert messages[0].content == "@mindroom_research:localhost: Please see:\n\n\nActual content"


def test_unseen_context_keeps_self_sent_relayed_user_message() -> None:
    """A tool-relayed user message from the agent account should remain user context."""
    thread_history = [
        make_visible_message(
            sender="@mindroom_code:localhost",
            body="@mindroom_missing_agent Please investigate this",
            event_id="$spawn-root",
            content={
                "body": "@mindroom_missing_agent Please investigate this",
                ORIGINAL_SENDER_KEY: "@alice:localhost",
            },
        ),
        make_visible_message(
            sender="@alice:localhost",
            body="What happened?",
            event_id="$question",
        ),
    ]

    messages, unseen_event_ids = _build_unseen_context_messages(
        "What happened?",
        thread_history,
        seen_event_ids=set(),
        current_event_id="$question",
        active_event_ids=(),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id="@alice:localhost",
        config=_config(),
    )

    assert unseen_event_ids == ["$spawn-root"]
    assert messages[0].role == "user"
    assert messages[0].content == "@alice:localhost: @mindroom_missing_agent Please investigate this"


def test_unseen_context_keeps_unpersisted_self_sent_message() -> None:
    """A self-sent Matrix event not known to persisted history should remain visible context."""
    thread_history = [
        make_visible_message(
            sender="@mindroom_code:localhost",
            body="@mindroom_missing_agent Please investigate this",
            event_id="$spawn-root",
        ),
        make_visible_message(
            sender="@alice:localhost",
            body="What happened?",
            event_id="$question",
        ),
    ]

    messages, unseen_event_ids = _build_unseen_context_messages(
        "What happened?",
        thread_history,
        seen_event_ids=set(),
        current_event_id="$question",
        active_event_ids=(),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id="@alice:localhost",
        config=_config(),
    )

    assert unseen_event_ids == ["$spawn-root"]
    assert messages[0].role == "assistant"
    assert messages[0].content == "@mindroom_missing_agent Please investigate this"


def test_unseen_context_skips_persisted_self_sent_response_event() -> None:
    """A self-sent Matrix event already represented in persisted history should not be duplicated."""
    thread_history = [
        make_visible_message(
            sender="@mindroom_code:localhost",
            body="Persisted assistant answer",
            event_id="$answer",
        ),
        make_visible_message(
            sender="@alice:localhost",
            body="What next?",
            event_id="$question",
        ),
    ]

    messages, unseen_event_ids = _build_unseen_context_messages(
        "What next?",
        thread_history,
        seen_event_ids={"$answer"},
        current_event_id="$question",
        active_event_ids=(),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id="@alice:localhost",
        config=_config(),
    )

    assert unseen_event_ids == []
    assert len(messages) == 1
    assert messages[0].content == 'Current message:\n<msg from="@alice:localhost"><![CDATA[What next?]]></msg>'
