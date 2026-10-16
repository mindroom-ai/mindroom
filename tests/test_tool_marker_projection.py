"""Tool display markers stay in Matrix bodies: models get plain text, judges get tags."""

from __future__ import annotations

import nio
import pytest

from mindroom.dispatch_handoff import dispatch_prompt_for_event
from mindroom.judgment.conversation import message_judgment_text
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.memory._prompting import _build_conversation_messages
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.events import (
    ToolTraceEntry,
    body_with_tool_tags,
    body_without_tool_markers,
    build_tool_trace_content,
    message_tool_trace,
)

_TRACE = [
    ToolTraceEntry(
        type="tool_call_completed",
        tool_name="shell",
        args_preview="command=pytest -x",
        result_preview='3 "failed"',
    ),
    ToolTraceEntry(type="tool_call_started", tool_name="file", args_preview="path=src/auth.py", truncated=True),
]
_BODY = "Running tests.\n\n🔧 `shell` [1]\n\nFixing.\n\n🔧 `file` [2] ⏳"


def test_markers_are_stripped_only_from_traced_messages() -> None:
    """Without the tool trace there is no proof the lines are MindRoom chrome, so human text stays."""
    content = build_tool_trace_content(_TRACE) or {}
    assert "🔧" not in body_without_tool_markers(_BODY, content)
    assert "Fixing." in body_without_tool_markers(_BODY, content)
    assert body_without_tool_markers(_BODY, {"msgtype": "m.text"}) == _BODY


def test_message_tool_trace_reads_the_stored_entries() -> None:
    """The stored trace round-trips into entries, and absent metadata reads as None."""
    content = build_tool_trace_content(_TRACE) or {}
    assert message_tool_trace(content) == _TRACE
    assert message_tool_trace({"msgtype": "m.text"}) is None


@pytest.mark.parametrize("event", [{}, {"type": [], "tool_name": "shell"}, {"type": {}, "tool_name": "shell"}])
def test_malformed_tool_trace_reads_as_a_trace_without_entries(event: dict[str, object]) -> None:
    """Any room member can attach a broken trace, so readers must not raise on one."""
    content = {"msgtype": "m.text", "body": _BODY, "io.mindroom.tool_trace": {"events": [event]}}
    assert message_tool_trace(content) == []
    assert "🔧" not in body_without_tool_markers(_BODY, content)
    message = ResolvedVisibleMessage.synthetic(sender="@alice:localhost", body=_BODY, event_id="$m", content=content)
    assert "<tool" not in (message_judgment_text(message) or "")
    event = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$m",
            "sender": "@alice:localhost",
            "origin_server_ts": 1,
            "content": content,
        },
    )
    assert "Fixing." in dispatch_prompt_for_event(event)


@pytest.mark.parametrize(
    ("args", "result"),
    [
        ("command=curl -s 'https://api.github.com/repos/o/r/issues?state=open&per_page=5'", None),
        (
            "command=cat docker-compose.yml",
            "services:\n  db:\n    environment:\n      POSTGRES_PASSWORD: hunter2\n      POSTGRES_USER: app\n",
        ),
    ],
)
def test_judges_accept_tags_built_from_redacted_previews(args: str, result: str | None) -> None:
    """Judges refuse text the redactor would change, so a tag must not alter a redacted URL or multi-line output."""
    trace = [ToolTraceEntry(type="tool_call_completed", tool_name="shell", args_preview=args, result_preview=result)]
    message = ResolvedVisibleMessage.synthetic(
        sender="@mindroom_code:localhost",
        body="Checking.\n\n🔧 `shell` [1]\n\nDone.",
        event_id="$reply",
        content={"msgtype": "m.text", **(build_tool_trace_content(trace) or {})},
    )
    text = message_judgment_text(message) or ""
    assert "<tool" in text
    assert "hunter2" not in text
    assert redact_sensitive_text(text) == text


def test_oversized_marker_index_reads_as_unmatched() -> None:
    """An index past int()'s digit limit is untrusted text, so it drops like any unmatched marker."""
    body = "Prior text.\n\n🔧 `shell` [" + "9" * 4301 + "]\n\nDone."
    content = {"msgtype": "m.text", "body": body, "io.mindroom.tool_trace": {}}
    message = ResolvedVisibleMessage.synthetic(sender="@alice:localhost", body=body, event_id="$m", content=content)
    assert message_judgment_text(message) == "Prior text.\n\n\nDone."


def test_body_with_tool_tags_replaces_markers_in_place() -> None:
    """Each marker becomes its own trace entry's verbatim tag, and a marker without an entry disappears."""
    assert body_with_tool_tags(_BODY, _TRACE) == (
        "Running tests.\n\n"
        '<tool name="shell" input="command=pytest -x" output="3 "failed""/>\n\n'
        "Fixing.\n\n"
        '<tool name="file" input="path=src/auth.py" truncated="true"/>'
    )
    assert body_with_tool_tags(_BODY, _TRACE[:1]).endswith("Fixing.\n")


def test_memory_extraction_reads_replies_without_markers() -> None:
    """Memory extraction reads an agent reply's text, not its display markers."""
    message = ResolvedVisibleMessage.synthetic(
        sender="@mindroom_code:localhost",
        body=_BODY,
        event_id="$reply",
        content={"msgtype": "m.text", **(build_tool_trace_content(_TRACE) or {})},
    )
    messages = _build_conversation_messages([message], "next", "@alice:localhost")
    assert "🔧" not in messages[0]["content"]
    assert "Fixing." in messages[0]["content"]


def test_prompt_from_an_agent_reply_drops_markers() -> None:
    """A finished agent reply that wakes another agent becomes its prompt without display markers."""
    event = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$reply",
            "sender": "@mindroom_code:localhost",
            "origin_server_ts": 1,
            "content": {"msgtype": "m.text", "body": _BODY, **(build_tool_trace_content(_TRACE) or {})},
        },
    )
    prompt = dispatch_prompt_for_event(event)
    assert "🔧" not in prompt
    assert "Fixing." in prompt
