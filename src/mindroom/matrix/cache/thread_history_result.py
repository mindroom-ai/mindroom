"""Shared thread-history metadata for Matrix dispatch fast paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.matrix.client import ResolvedVisibleMessage

type ThreadHistoryDiagnosticValue = str | int | float | bool

THREAD_HISTORY_SOURCE_DIAGNOSTIC = "thread_read_source"
THREAD_HISTORY_SOURCE_CACHE = "cache"
THREAD_HISTORY_SOURCE_HOMESERVER = "homeserver"


class ThreadHistoryResult(list["ResolvedVisibleMessage"]):
    """List subclass that preserves whether the history is already fully hydrated."""

    __slots__ = ("diagnostics", "is_full_history")

    def __init__(
        self,
        history: list[ResolvedVisibleMessage],
        *,
        is_full_history: bool,
        diagnostics: Mapping[str, ThreadHistoryDiagnosticValue] | None = None,
    ) -> None:
        super().__init__(history)
        self.is_full_history = is_full_history
        self.diagnostics = dict(diagnostics or {})


def thread_history_result(
    history: list[ResolvedVisibleMessage],
    *,
    is_full_history: bool,
    diagnostics: Mapping[str, ThreadHistoryDiagnosticValue] | None = None,
) -> ThreadHistoryResult:
    """Wrap history with hydration metadata used by dispatch fast paths."""
    if isinstance(history, ThreadHistoryResult):
        history.is_full_history = is_full_history
        history.diagnostics = dict(history.diagnostics if diagnostics is None else diagnostics)
        return history
    return ThreadHistoryResult(
        history,
        is_full_history=is_full_history,
        diagnostics=diagnostics,
    )


def thread_history_read_source(history: ThreadHistoryResult) -> str | None:
    """Return the logical source that produced one thread-history result."""
    source = history.diagnostics.get(THREAD_HISTORY_SOURCE_DIAGNOSTIC)
    return source if isinstance(source, str) else None
