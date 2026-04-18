"""Shared thread-history metadata for Matrix dispatch fast paths."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

type ThreadHistoryDiagnosticValue = str | int | float | bool

THREAD_HISTORY_SOURCE_DIAGNOSTIC = "thread_read_source"
THREAD_HISTORY_SOURCE_CACHE = "cache"
THREAD_HISTORY_SOURCE_HOMESERVER = "homeserver"
THREAD_HISTORY_SOURCE_STALE_CACHE = "stale_cache"
THREAD_HISTORY_ERROR_DIAGNOSTIC = "thread_read_error"
THREAD_HISTORY_DEGRADED_DIAGNOSTIC = "thread_read_degraded"


@dataclass(slots=True, eq=False)
class ThreadHistoryResult(Sequence["ResolvedVisibleMessage"]):  # noqa: PLW1641
    """Sequence wrapper that preserves whether the history is already fully hydrated."""

    messages: list[ResolvedVisibleMessage]
    is_full_history: bool
    diagnostics: dict[str, ThreadHistoryDiagnosticValue] = field(default_factory=dict)

    def __iter__(self) -> Iterator[ResolvedVisibleMessage]:
        """Iterate over wrapped visible messages."""
        return iter(self.messages)

    def __len__(self) -> int:
        """Return the number of wrapped visible messages."""
        return len(self.messages)

    @overload
    def __getitem__(self, index: int) -> ResolvedVisibleMessage: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[ResolvedVisibleMessage]: ...

    def __getitem__(self, index: int | slice) -> ResolvedVisibleMessage | Sequence[ResolvedVisibleMessage]:
        """Return one wrapped message or one sliced view of the history."""
        return self.messages[index]

    def __eq__(self, other: object) -> bool:
        """Compare history results by visible-message contents for list-style behavior."""
        if isinstance(other, ThreadHistoryResult):
            return self.messages == other.messages
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes, bytearray)):
            return self.messages == list(other)
        return NotImplemented


def thread_history_result(
    history: Sequence[ResolvedVisibleMessage],
    *,
    is_full_history: bool,
    diagnostics: Mapping[str, ThreadHistoryDiagnosticValue] | None = None,
) -> ThreadHistoryResult:
    """Wrap history with hydration metadata used by dispatch fast paths."""
    resolved_diagnostics = dict(diagnostics or {})
    if isinstance(history, ThreadHistoryResult):
        if diagnostics is None:
            resolved_diagnostics = dict(history.diagnostics)
        return ThreadHistoryResult(
            messages=list(history),
            is_full_history=is_full_history,
            diagnostics=resolved_diagnostics,
        )
    return ThreadHistoryResult(
        messages=list(history),
        is_full_history=is_full_history,
        diagnostics=resolved_diagnostics,
    )
