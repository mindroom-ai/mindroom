"""Tests for lossless Matrix Markdown segmentation."""

from mindroom.matrix.message_builder import build_matrix_edit_content, markdown_to_html
from mindroom.matrix.segmented_messages import (
    _SEGMENT_TARGET_EDIT_BYTES,
    _SEGMENT_TARGET_PLAINTEXT_BYTES,
    _estimated_event_size,
    _render_segment_html,
    _unclosed_fence_start,
    segment_matrix_content,
)


def _body() -> str:
    """Return a body large enough to exercise paragraph segmentation."""
    return "\n\n".join(
        f"## Section {index}\n\n**value {index}** with [source](https://example.com)." for index in range(900)
    )


def test_plaintext_segments_preserve_markdown_body() -> None:
    """A regular oversized response is split into rich-text events losslessly."""
    body = _body()
    content = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": markdown_to_html(body),
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
    }

    segmented = segment_matrix_content(
        content,
        room_encrypted=False,
        continuation_thread_id="$thread",
        continuation_reply_to_event_id="$cause",
    )

    assert segmented is not None
    parts = [segmented.first, *segmented.continuations]
    assert len(parts) >= 2
    assert "".join(part["body"] for part in parts) == body
    assert segmented.first["m.relates_to"] == content["m.relates_to"]
    assert all(part["format"] == "org.matrix.custom.html" for part in parts)
    assert all(part["formatted_body"] for part in parts)
    assert all(_estimated_event_size(part) <= _SEGMENT_TARGET_PLAINTEXT_BYTES for part in parts)


def test_continuations_use_thread_fallback_relation() -> None:
    """Continuations never repeat a genuine reply to the turn's source event.

    Replay recovery counts every non-fallback reply to a source as a visible
    response of the turn; one segmented answer must produce exactly one.
    """
    body = _body()
    genuine_reply = {
        "rel_type": "m.thread",
        "event_id": "$thread",
        "is_falling_back": False,
        "m.in_reply_to": {"event_id": "$cause"},
    }
    content = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": markdown_to_html(body),
        "m.relates_to": genuine_reply,
    }

    segmented = segment_matrix_content(
        content,
        room_encrypted=False,
        continuation_thread_id="$thread",
        continuation_reply_to_event_id="$cause",
    )

    assert segmented is not None
    assert segmented.first["m.relates_to"] == genuine_reply
    assert segmented.continuations
    for continuation in segmented.continuations:
        assert continuation["m.relates_to"] == {
            "rel_type": "m.thread",
            "event_id": "$thread",
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": "$cause"},
        }


def test_room_mode_continuations_carry_no_reply_relation() -> None:
    """Outside threads a continuation is a bare message, not a second reply."""
    body = _body()
    content = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": markdown_to_html(body),
        "m.relates_to": {"m.in_reply_to": {"event_id": "$cause"}},
    }

    segmented = segment_matrix_content(content, room_encrypted=False)

    assert segmented is not None
    assert segmented.continuations
    assert all("m.relates_to" not in part for part in segmented.continuations)
    assert "".join(part["body"] for part in [segmented.first, *segmented.continuations]) == body


def test_edit_segments_use_replace_then_thread_replies() -> None:
    """An oversized edit keeps the first replacement and threads continuations."""
    body = _body()
    content = build_matrix_edit_content(
        event_id="$placeholder",
        new_content={
            "msgtype": "m.text",
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": markdown_to_html(body),
        },
    )

    segmented = segment_matrix_content(
        content,
        room_encrypted=False,
        continuation_thread_id="$thread",
        continuation_reply_to_event_id="$placeholder",
    )

    assert segmented is not None
    parts = [segmented.first["m.new_content"], *segmented.continuations]
    assert "".join(part["body"] for part in parts) == body
    assert segmented.first["m.relates_to"]["rel_type"] == "m.replace"
    assert all(part["m.relates_to"]["rel_type"] == "m.thread" for part in segmented.continuations)
    assert all(
        _estimated_event_size(part) <= _SEGMENT_TARGET_EDIT_BYTES
        for part in [segmented.first, *segmented.continuations]
    )


def test_large_tool_trace_does_not_disable_visible_markdown() -> None:
    """Internal tool metadata must not force a small completed answer into a sidecar."""
    body = "\n\n".join(f"### Finding {index}\n\n**value**" for index in range(90))
    content = build_matrix_edit_content(
        event_id="$placeholder",
        new_content={
            "msgtype": "m.text",
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": markdown_to_html(body),
            "io.mindroom.tool_trace": {
                "version": 2,
                "events": [
                    {
                        "type": "tool_call_completed",
                        "tool_name": "synthetic_tool",
                        "result_preview": "x" * 500,
                    }
                    for _ in range(100)
                ],
            },
        },
    )

    segmented = segment_matrix_content(content, room_encrypted=False)

    assert segmented is not None
    first = segmented.first["m.new_content"]
    parts = [first, *segmented.continuations]
    assert "".join(part["body"] for part in parts) == body
    assert "io.mindroom.tool_trace" not in first
    assert first["format"] == "org.matrix.custom.html"
    assert first["formatted_body"]


def _fence_balance(text: str) -> int:
    """Return the net number of unclosed fence openers in one chunk."""
    return sum(1 for line in text.splitlines() if line.lstrip().startswith(("```", "~~~"))) % 2


def test_fenced_code_block_is_not_split_mid_block() -> None:
    """Every segment must be self-contained Markdown, fences included."""
    prefix = "\n\n".join(f"## Lead {index}\n\nword " * 40 for index in range(30))
    fence = "```python\n" + "".join(f"value_{index} = {index}\n" for index in range(150)) + "```"
    suffix = "\n\n".join(f"## Tail {index}\n\nword " * 40 for index in range(30))
    body = f"{prefix}\n\n{fence}\n\n{suffix}"
    content = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": markdown_to_html(body),
    }

    segmented = segment_matrix_content(content, room_encrypted=True)

    assert segmented is not None
    parts = [segmented.first, *segmented.continuations]
    assert len(parts) >= 2
    assert "".join(part["body"] for part in parts) == body
    assert all(_fence_balance(part["body"]) == 0 for part in parts)
    assert sum(fence in part["body"] for part in parts) == 1


def test_fence_spanning_the_whole_budget_falls_back_to_sidecar() -> None:
    """A code block no cut can keep whole stays on the sidecar path."""
    body = "```\n" + "x" * (_SEGMENT_TARGET_PLAINTEXT_BYTES * 2) + "\n```"
    content = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": markdown_to_html(body),
    }

    assert segment_matrix_content(content, room_encrypted=False) is None


def test_mention_pills_survive_segmentation() -> None:
    """Re-rendering a chunk must not drop the link its plain body cannot carry."""
    lead = "\n\n".join(f"## Section {index}\n\n**value {index}**" for index in range(600))
    body = f"{lead}\n\nThanks @alice:localhost for the review."
    content = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": (
            markdown_to_html(lead)
            + '<p>Thanks <a href="https://matrix.to/#/@alice:localhost">@Alice</a> for the review.</p>'
        ),
        "m.mentions": {"user_ids": ["@alice:localhost"]},
    }

    segmented = segment_matrix_content(content, room_encrypted=False)

    assert segmented is not None
    parts = [segmented.first, *segmented.continuations]
    assert "".join(part["body"] for part in parts) == body
    mentioned = [part for part in parts if "@alice:localhost" in part["body"]]
    assert len(mentioned) == 1
    assert '<a href="https://matrix.to/#/@alice:localhost">@Alice</a>' in mentioned[0]["formatted_body"]
    assert segmented.first["m.mentions"] == {"user_ids": ["@alice:localhost"]}
    assert all("m.mentions" not in part for part in segmented.continuations)


def test_prefix_overlapping_mentions_do_not_nest_pills() -> None:
    """A user ID that prefixes another must not match inside an inserted pill."""
    body = "Thanks @alice:localhost and @alice:localhost2."
    content = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": (
            '<p>Thanks <a href="https://matrix.to/#/@alice:localhost">@Alice</a> and '
            '<a href="https://matrix.to/#/@alice:localhost2">@Alice Two</a>.</p>'
        ),
        "m.mentions": {"user_ids": ["@alice:localhost", "@alice:localhost2"]},
    }

    rendered = _render_segment_html(content, body)

    assert '<a href="https://matrix.to/#/@alice:localhost">@Alice</a>' in rendered
    assert '<a href="https://matrix.to/#/@alice:localhost2">@Alice Two</a>' in rendered
    assert rendered.count("<a ") == 2


def test_unclosed_fence_requires_same_marker_and_length() -> None:
    """Only a same-character fence of at least the opening length closes."""
    opened = "```python\ncode\n"
    assert _unclosed_fence_start(opened + "~~~\nmore\n", 0, len(opened) + 8) == 0
    assert _unclosed_fence_start("````\ncode\n```\nstill open\n", 0, 24) == 0
    closed = opened + "```   \n"
    assert _unclosed_fence_start(closed, 0, len(closed)) is None
    longer_closes = opened + "````\n"
    assert _unclosed_fence_start(longer_closes, 0, len(longer_closes)) is None
