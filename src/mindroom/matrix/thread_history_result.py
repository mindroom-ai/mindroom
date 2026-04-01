"""Shared thread-history metadata for Matrix dispatch fast paths."""

from __future__ import annotations

from typing import Any


class ThreadHistoryResult(list[dict[str, Any]]):
    """List subclass that preserves whether the history is already fully hydrated."""

    __slots__ = ("is_full_history",)

    def __init__(self, history: list[dict[str, Any]], *, is_full_history: bool) -> None:
        super().__init__(history)
        self.is_full_history = is_full_history


def thread_history_result(
    history: list[dict[str, Any]],
    *,
    is_full_history: bool,
) -> ThreadHistoryResult:
    """Wrap history with hydration metadata used by dispatch fast paths."""
    if isinstance(history, ThreadHistoryResult) and history.is_full_history == is_full_history:
        return history
    return ThreadHistoryResult(history, is_full_history=is_full_history)
