"""Cancellation and interruption note markers are a wire contract.

Replies already in Matrix carry these notes, and reading a reply back
recognizes them by their text. These tests pin the text through the
production builders so it cannot drift.
"""

from __future__ import annotations

from mindroom.streaming import _STREAM_ERROR_RESPONSE_NOTE as STREAM_ERROR_RESPONSE_NOTE
from mindroom.streaming import (
    CANCELLED_RESPONSE_NOTE,
    INTERRUPTED_RESPONSE_NOTE,
    RESTART_INTERRUPTED_RESPONSE_NOTE,
    build_restart_interrupted_body,
)


def test_marker_constants_keep_their_wire_text() -> None:
    """Marker text is a wire contract: reading a reply back breaks if it drifts."""
    assert CANCELLED_RESPONSE_NOTE == "**[Response cancelled by user]**"
    assert INTERRUPTED_RESPONSE_NOTE == "**[Response interrupted]**"
    assert RESTART_INTERRUPTED_RESPONSE_NOTE == "**[Response interrupted by service restart]**"
    assert STREAM_ERROR_RESPONSE_NOTE == "**[Response interrupted by an error"


def test_placeholder_only_restart_body_is_the_bare_restart_note() -> None:
    """A restart note on a placeholder-only stream replaces the placeholder."""
    body = build_restart_interrupted_body("Thinking...")

    assert body == RESTART_INTERRUPTED_RESPONSE_NOTE
