"""Persisted history compaction helpers."""

from mindroom.history.runtime import prepare_history_for_run
from mindroom.history.types import (
    CompactionDecision,
    CompactionLifecycle,
    CompactionLifecycleFailure,
    CompactionLifecycleStart,
    CompactionLifecycleSuccess,
    CompactionOutcome,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    OpportunisticCompactionRequest,
    PreparedHistoryState,
)

__all__ = [
    "CompactionDecision",
    "CompactionLifecycle",
    "CompactionLifecycleFailure",
    "CompactionLifecycleStart",
    "CompactionLifecycleSuccess",
    "CompactionOutcome",
    "HistoryPolicy",
    "HistoryScope",
    "HistoryScopeState",
    "OpportunisticCompactionRequest",
    "PreparedHistoryState",
    "prepare_history_for_run",
]
