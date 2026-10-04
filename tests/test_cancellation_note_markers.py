"""Cancellation and interruption note markers must stay compatible with stale-stream cleanup matching.

The streaming layer owns the terminal note text; ``stale_stream_cleanup`` owns the
suffix matcher that decides whether a visible reply already carries the restart
note. These tests pin the contract through the production builders so marker
text cannot drift on either side.
"""

from __future__ import annotations

from typing import Literal

import pytest

from mindroom.constants import STREAM_STATUS_CANCELLED, STREAM_STATUS_ERROR
from mindroom.matrix.stale_stream_cleanup import (
    _has_restart_interrupted_note as has_restart_interrupted_note,
)
from mindroom.streaming import _CANCELLED_RESPONSE_NOTE as CANCELLED_RESPONSE_NOTE
from mindroom.streaming import _STREAM_ERROR_RESPONSE_NOTE as STREAM_ERROR_RESPONSE_NOTE
from mindroom.streaming import (
    INTERRUPTED_RESPONSE_NOTE,
    RESTART_INTERRUPTED_RESPONSE_NOTE,
    build_cancelled_response_update,
    build_restart_interrupted_body,
)
from mindroom.streaming import _format_stream_error_note as format_stream_error_note

_CancelSource = Literal["user_stop", "sync_restart", "interrupted"]


def test_marker_constants_keep_their_cleanup_matched_text() -> None:
    """Marker text is a wire contract: cleanup suffix matching breaks if it drifts."""
    assert CANCELLED_RESPONSE_NOTE == "**[Response cancelled by user]**"
    assert INTERRUPTED_RESPONSE_NOTE == "**[Response interrupted]**"
    assert RESTART_INTERRUPTED_RESPONSE_NOTE == "**[Response interrupted by service restart]**"
    assert STREAM_ERROR_RESPONSE_NOTE == "**[Response interrupted by an error"


def test_only_the_restart_note_matches_the_restart_matcher() -> None:
    """The cleanup matcher recognizes the restart note and no other note."""
    assert has_restart_interrupted_note(RESTART_INTERRUPTED_RESPONSE_NOTE)
    assert not has_restart_interrupted_note(INTERRUPTED_RESPONSE_NOTE)
    assert not has_restart_interrupted_note(CANCELLED_RESPONSE_NOTE)
    assert not has_restart_interrupted_note(format_stream_error_note(RuntimeError("provider exploded")))


@pytest.mark.parametrize(
    ("cancel_source", "expected_note", "expected_status"),
    [
        ("user_stop", CANCELLED_RESPONSE_NOTE, STREAM_STATUS_CANCELLED),
        ("sync_restart", RESTART_INTERRUPTED_RESPONSE_NOTE, STREAM_STATUS_ERROR),
        ("interrupted", INTERRUPTED_RESPONSE_NOTE, STREAM_STATUS_ERROR),
    ],
)
def test_cancelled_updates_end_with_their_own_note(
    cancel_source: _CancelSource,
    expected_note: str,
    expected_status: str,
) -> None:
    """Each cancellation provenance ends the partial text with its note and status."""
    body, stream_status = build_cancelled_response_update("Partial answer", cancel_source=cancel_source)

    assert body == f"Partial answer\n\n{expected_note}"
    assert stream_status == expected_status
    assert has_restart_interrupted_note(body) is (cancel_source == "sync_restart")


@pytest.mark.parametrize(
    ("cancel_source", "expected_note"),
    [
        ("user_stop", CANCELLED_RESPONSE_NOTE),
        ("sync_restart", RESTART_INTERRUPTED_RESPONSE_NOTE),
        ("interrupted", INTERRUPTED_RESPONSE_NOTE),
    ],
)
def test_placeholder_only_bodies_collapse_to_the_bare_note(
    cancel_source: _CancelSource,
    expected_note: str,
) -> None:
    """A cancellation before any visible chunk leaves exactly the bare note."""
    body, _ = build_cancelled_response_update("Thinking...", cancel_source=cancel_source)

    assert body == expected_note


def test_placeholder_only_restart_body_is_the_bare_restart_note() -> None:
    """A restart cleanup of a placeholder-only stream must still suffix-match."""
    body = build_restart_interrupted_body("Thinking...")

    assert body == RESTART_INTERRUPTED_RESPONSE_NOTE
    assert has_restart_interrupted_note(body)
