"""Shared Matrix thread-read diagnostic keys.

This module is a deliberate dependency-cycle breaker between the cache package (which produces
``ThreadHistoryResult`` diagnostics) and ``thread_membership`` (which consumes them for root proofs).

Diagnostic semantics:

1. ``thread_read_source`` distinguishes ``cache`` (trusted snapshot), ``homeserver`` (fresh fetch),
   ``stale_cache`` (advisory fallback after a failed refetch), and ``degraded`` (empty fail-open result
   from a dispatch timeout).

2. ``is_thread_history_degraded`` is the planning-level check: both ``stale_cache`` and ``degraded``
   reads count as degraded and must not be memoized or treated as authoritative context.

3. ``is_thread_history_source_degraded`` is the stricter proof-level check: only the explicit
   ``degraded`` source disqualifies a history from serving as thread-root proof, because a stale
   snapshot still proves children existed while an empty degraded read proves nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

THREAD_HISTORY_SOURCE_DIAGNOSTIC = "thread_read_source"
THREAD_HISTORY_SOURCE_CACHE = "cache"
THREAD_HISTORY_SOURCE_HOMESERVER = "homeserver"
THREAD_HISTORY_SOURCE_STALE_CACHE = "stale_cache"
THREAD_HISTORY_SOURCE_DEGRADED = "degraded"
THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC = "cache_reject_reason"
THREAD_HISTORY_ERROR_DIAGNOSTIC = "thread_read_error"
THREAD_HISTORY_DEGRADED_DIAGNOSTIC = "thread_read_degraded"


@runtime_checkable
class _SupportsThreadDiagnostics(Protocol):
    """Minimal boundary-safe shape for thread histories that expose read diagnostics.

    Keep this structural to avoid importing ThreadHistoryResult here and creating a cache
    package dependency cycle.
    """

    diagnostics: Mapping[str, object]


def is_thread_history_degraded(thread_history: object) -> bool:
    """Return whether one thread-history read explicitly degraded."""
    if not isinstance(thread_history, _SupportsThreadDiagnostics):
        return False
    diagnostics = thread_history.diagnostics
    return (
        diagnostics.get(THREAD_HISTORY_DEGRADED_DIAGNOSTIC) is True
        or diagnostics.get(THREAD_HISTORY_SOURCE_DIAGNOSTIC) == THREAD_HISTORY_SOURCE_DEGRADED
    )


def is_thread_history_source_degraded(thread_history: object) -> bool:
    """Return whether the read source itself is the explicit degraded fallback."""
    return (
        isinstance(thread_history, _SupportsThreadDiagnostics)
        and thread_history.diagnostics.get(THREAD_HISTORY_SOURCE_DIAGNOSTIC) == THREAD_HISTORY_SOURCE_DEGRADED
    )
