"""Small shared helpers for config validators."""

from __future__ import annotations


def duplicate_items(values: list[str]) -> list[str]:
    """Return duplicate items while preserving first duplicate order."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def validate_history_limit_choice(
    *,
    num_history_runs: int | None,
    num_history_messages: int | None,
) -> None:
    """Reject ambiguous history replay limit settings."""
    if num_history_runs is not None and num_history_messages is not None:
        msg = "num_history_runs and num_history_messages are mutually exclusive"
        raise ValueError(msg)
