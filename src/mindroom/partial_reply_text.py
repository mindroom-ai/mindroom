"""Shared partial-reply marker text helpers."""

from __future__ import annotations

PROGRESS_PLACEHOLDER = "Thinking..."
CANCELLED_RESPONSE_NOTE = "**[Response cancelled by user]**"
INTERRUPTED_RESPONSE_NOTE = "**[Response interrupted]**"
RESTART_INTERRUPTED_RESPONSE_NOTE = "**[Response interrupted by service restart]**"
STREAM_ERROR_RESPONSE_NOTE_PREFIX = "**[Response interrupted by an error"


def format_stream_error_note(error: Exception) -> str:
    """Return a concise user-facing note for stream-time exceptions."""
    normalized_error = " ".join(str(error).split())
    if not normalized_error:
        return f"{STREAM_ERROR_RESPONSE_NOTE_PREFIX}. Please retry.]**"
    if len(normalized_error) > 220:
        normalized_error = f"{normalized_error[:219]}…"
    return f"{STREAM_ERROR_RESPONSE_NOTE_PREFIX}: {normalized_error}]**"


def is_interrupted_partial_reply(text: object) -> bool:
    """Return True when text carries a terminal interrupted partial-reply marker."""
    if not isinstance(text, str):
        return False
    trimmed_text = text.rstrip()
    return trimmed_text.endswith(
        (
            CANCELLED_RESPONSE_NOTE,
            INTERRUPTED_RESPONSE_NOTE,
            RESTART_INTERRUPTED_RESPONSE_NOTE,
            " [cancelled]",
            " [error]",
        ),
    ) or (STREAM_ERROR_RESPONSE_NOTE_PREFIX in trimmed_text)


def clean_partial_reply_text(text: str) -> str:
    """Strip partial-reply status notes from persisted text."""
    cleaned = text.rstrip()

    for marker in (
        " [cancelled]",
        " [error]",
        CANCELLED_RESPONSE_NOTE,
        INTERRUPTED_RESPONSE_NOTE,
        RESTART_INTERRUPTED_RESPONSE_NOTE,
    ):
        if cleaned.endswith(marker):
            cleaned = cleaned[: -len(marker)].rstrip()

    if STREAM_ERROR_RESPONSE_NOTE_PREFIX in cleaned:
        cleaned = cleaned.split(STREAM_ERROR_RESPONSE_NOTE_PREFIX, 1)[0].rstrip()

    if cleaned == PROGRESS_PLACEHOLDER or not cleaned or not any(char.isalnum() for char in cleaned):
        return ""
    return cleaned
