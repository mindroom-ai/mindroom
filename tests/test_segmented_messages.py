"""Tests for lossless Matrix Markdown segmentation."""

from mindroom.matrix.message_builder import build_matrix_edit_content, markdown_to_html
from mindroom.matrix.segmented_messages import (
    _SEGMENT_TARGET_EDIT_BYTES,
    _SEGMENT_TARGET_PLAINTEXT_BYTES,
    _estimated_event_size,
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

    segmented = segment_matrix_content(content, room_encrypted=False)

    assert segmented is not None
    parts = [segmented.first, *segmented.continuations]
    assert len(parts) >= 2
    assert "".join(part["body"] for part in parts) == body
    assert segmented.first["m.relates_to"] == content["m.relates_to"]
    assert all(part["format"] == "org.matrix.custom.html" for part in parts)
    assert all(part["formatted_body"] for part in parts)
    assert all(_estimated_event_size(part) <= _SEGMENT_TARGET_PLAINTEXT_BYTES for part in parts)


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
