"""Cancellation/interruption note markers must stay compatible with stale-stream recovery matching.

The streaming layer owns the terminal note text; ``stale_stream_cleanup`` owns the
suffix matchers that decide whether a visible reply is restart-resumable. These
tests pin the contract through the production builders so marker text cannot
drift on either side.
"""

from __future__ import annotations

from typing import Literal

import pytest

from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.matrix.stale_stream_cleanup import (
    _has_generic_interrupted_note as has_generic_interrupted_note,
)
from mindroom.matrix.stale_stream_cleanup import (
    _has_restart_interrupted_note as has_restart_interrupted_note,
)
from mindroom.matrix.stale_stream_cleanup import (
    _has_resumable_interrupted_note as has_resumable_interrupted_note,
)
from mindroom.matrix.stale_stream_cleanup import _MessageState as MessageState
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
    """Marker text is a wire contract: recovery suffix matching breaks if it drifts."""
    assert CANCELLED_RESPONSE_NOTE == "**[Response cancelled by user]**"
    assert INTERRUPTED_RESPONSE_NOTE == "**[Response interrupted]**"
    assert RESTART_INTERRUPTED_RESPONSE_NOTE == "**[Response interrupted by service restart]**"
    assert STREAM_ERROR_RESPONSE_NOTE == "**[Response interrupted by an error"


def test_marker_constants_are_suffix_matched_only_by_their_own_cleanup_matcher() -> None:
    """Each note constant must be recognized by its own cleanup matcher and no other."""
    assert has_restart_interrupted_note(RESTART_INTERRUPTED_RESPONSE_NOTE)
    assert not has_restart_interrupted_note(INTERRUPTED_RESPONSE_NOTE)
    assert not has_restart_interrupted_note(CANCELLED_RESPONSE_NOTE)

    assert has_generic_interrupted_note(INTERRUPTED_RESPONSE_NOTE)
    assert not has_generic_interrupted_note(RESTART_INTERRUPTED_RESPONSE_NOTE)
    assert not has_generic_interrupted_note(CANCELLED_RESPONSE_NOTE)


def test_user_stop_cancelled_note_is_neither_restart_interrupted_nor_resumable() -> None:
    """A user-stopped response must never be auto-resumed after a restart."""
    body, stream_status = build_cancelled_response_update("Partial answer", cancel_source="user_stop")

    assert stream_status == STREAM_STATUS_CANCELLED
    assert body.endswith(CANCELLED_RESPONSE_NOTE)
    assert not has_restart_interrupted_note(body)
    assert not has_generic_interrupted_note(body)
    assert not has_resumable_interrupted_note(MessageState(latest_body=body, stream_status=stream_status))


def test_restart_interrupted_note_is_resumable() -> None:
    """A sync-restart cancellation must surface as a restart-resumable interruption."""
    body, stream_status = build_cancelled_response_update("Partial answer", cancel_source="sync_restart")

    assert stream_status == STREAM_STATUS_ERROR
    assert body.endswith(RESTART_INTERRUPTED_RESPONSE_NOTE)
    assert has_restart_interrupted_note(body)
    assert not has_generic_interrupted_note(body)
    assert has_resumable_interrupted_note(MessageState(latest_body=body, stream_status=stream_status))


@pytest.mark.parametrize("stream_status", [None, STREAM_STATUS_ERROR, STREAM_STATUS_INTERRUPTED])
def test_restart_note_is_resumable_without_a_wire_status_too(stream_status: str | None) -> None:
    """The restart note alone proves interruption, so a missing status still resumes."""
    body = build_restart_interrupted_body("Partial answer")

    assert has_resumable_interrupted_note(MessageState(latest_body=body, stream_status=stream_status))


@pytest.mark.parametrize(
    "stream_status",
    [STREAM_STATUS_CANCELLED, STREAM_STATUS_COMPLETED, STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING],
)
def test_restart_note_under_a_non_error_status_is_not_resumed(stream_status: str) -> None:
    """A restart-noted body under a cancelled/completed/in-flight status must not resume."""
    body = build_restart_interrupted_body("Partial answer")

    assert not has_resumable_interrupted_note(MessageState(latest_body=body, stream_status=stream_status))


@pytest.mark.parametrize("stream_status", [STREAM_STATUS_ERROR, STREAM_STATUS_INTERRUPTED])
def test_generic_interrupted_note_is_resumable_under_error_statuses(stream_status: str) -> None:
    """The generic interrupted note resumes only when the wire status is error-like."""
    body, built_status = build_cancelled_response_update("Partial answer", cancel_source="interrupted")

    assert built_status == STREAM_STATUS_ERROR
    assert has_generic_interrupted_note(body)
    assert not has_restart_interrupted_note(body)
    assert has_resumable_interrupted_note(MessageState(latest_body=body, stream_status=stream_status))


@pytest.mark.parametrize("stream_status", [None, STREAM_STATUS_CANCELLED, STREAM_STATUS_COMPLETED])
def test_generic_interrupted_note_without_an_error_status_is_not_resumed(stream_status: str | None) -> None:
    """Unlike the restart note, the generic note requires an error-like wire status."""
    body, _ = build_cancelled_response_update("Partial answer", cancel_source="interrupted")

    assert not has_resumable_interrupted_note(MessageState(latest_body=body, stream_status=stream_status))


def test_stream_error_note_is_not_restart_resumable() -> None:
    """Error-interrupted bodies carry only the error prefix and must not auto-resume."""
    body = format_stream_error_note(RuntimeError("provider exploded"))

    assert body.startswith(STREAM_ERROR_RESPONSE_NOTE)
    assert not has_restart_interrupted_note(body)
    assert not has_generic_interrupted_note(body)
    assert not has_resumable_interrupted_note(MessageState(latest_body=body, stream_status=STREAM_STATUS_ERROR))


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
    assert has_resumable_interrupted_note(MessageState(latest_body=body, stream_status=STREAM_STATUS_ERROR))
