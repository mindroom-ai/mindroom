"""Tests for shared dispatch event content extraction."""

from __future__ import annotations

from mindroom.dispatch_handoff import PreparedTextEvent, event_content_dict


def test_event_content_dict_returns_matrix_content_mapping() -> None:
    """Return the source content mapping without copying it."""
    content: dict[str, object] = {"body": "hello", "msgtype": "m.text"}
    event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$event",
        body="hello",
        source={"content": content},
    )

    assert event_content_dict(event) is content


def test_event_content_dict_ignores_non_mapping_content() -> None:
    """Return None when Matrix content is not a mapping."""
    event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$event",
        body="hello",
        source={"content": "not a mapping"},
    )

    assert event_content_dict(event) is None
