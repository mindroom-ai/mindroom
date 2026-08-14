"""Compatibility boundaries for shipped journal schemas."""

from __future__ import annotations


def validate_interactive_question_columns(existing_columns: frozenset[str]) -> None:
    """Refuse the old question-claim schema instead of guessing its ownership."""
    if "claimed_source_event_id" not in existing_columns:
        return
    msg = (
        "This event journal uses the incompatible pre-selection schema; "
        "recreate the event journal database before starting MindRoom"
    )
    raise RuntimeError(msg)
