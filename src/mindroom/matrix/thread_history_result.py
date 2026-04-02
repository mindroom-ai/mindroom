"""Shared thread-history metadata for Matrix dispatch fast paths."""

from __future__ import annotations

from typing import TypeVar

TMessage = TypeVar("TMessage")


class ThreadHistoryResult[TMessage](list[TMessage]):
    """List subclass that preserves whether the history is already fully hydrated."""

    __slots__ = ("is_full_history",)

    def __init__(self, history: list[TMessage], *, is_full_history: bool) -> None:
        super().__init__(history)
        self.is_full_history = is_full_history


def thread_history_result[TMessage](
    history: list[TMessage],
    *,
    is_full_history: bool,
) -> ThreadHistoryResult[TMessage]:
    """Wrap history with hydration metadata used by dispatch fast paths."""
    if isinstance(history, ThreadHistoryResult) and history.is_full_history == is_full_history:
        return history
    return ThreadHistoryResult(history, is_full_history=is_full_history)
