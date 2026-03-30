"""Scoped persisted history replay and compaction."""

from mindroom.history.runtime import (
    clear_bound_agent_history_state,
    clear_prepared_history,
    compose_prompt_with_persisted_history,
    prepare_bound_agents_for_run,
    prepare_history_for_run,
    stream_with_bound_agent_history,
)
from mindroom.history.types import CompactionOutcome, CompactionState, HistoryPolicy, HistoryScope, PreparedHistory

__all__ = [
    "CompactionOutcome",
    "CompactionState",
    "HistoryPolicy",
    "HistoryScope",
    "PreparedHistory",
    "clear_bound_agent_history_state",
    "clear_prepared_history",
    "compose_prompt_with_persisted_history",
    "prepare_bound_agents_for_run",
    "prepare_history_for_run",
    "stream_with_bound_agent_history",
]
