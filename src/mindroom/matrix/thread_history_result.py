"""Shared thread-history metadata for Matrix dispatch fast paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.matrix.client import ResolvedVisibleMessage

type ThreadHistoryDiagnosticValue = str | int | float | bool


class ThreadHistoryResult(list["ResolvedVisibleMessage"]):
    """List subclass that preserves whether the history is already fully hydrated."""

    __slots__ = ("diagnostics", "is_full_history", "thread_version")

    def __init__(
        self,
        history: list[ResolvedVisibleMessage],
        *,
        is_full_history: bool,
        thread_version: int | None = None,
        diagnostics: Mapping[str, ThreadHistoryDiagnosticValue] | None = None,
    ) -> None:
        super().__init__(history)
        self.is_full_history = is_full_history
        self.thread_version = thread_version
        self.diagnostics = dict(diagnostics or {})


def thread_history_result(
    history: list[ResolvedVisibleMessage],
    *,
    is_full_history: bool,
    thread_version: int | None = None,
    diagnostics: Mapping[str, ThreadHistoryDiagnosticValue] | None = None,
) -> ThreadHistoryResult:
    """Wrap history with hydration metadata used by dispatch fast paths."""
    if isinstance(history, ThreadHistoryResult):
        history.is_full_history = is_full_history
        history.thread_version = history.thread_version if thread_version is None else thread_version
        history.diagnostics = dict(history.diagnostics if diagnostics is None else diagnostics)
        return history
    return ThreadHistoryResult(
        history,
        is_full_history=is_full_history,
        thread_version=thread_version,
        diagnostics=diagnostics,
    )
