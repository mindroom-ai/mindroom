"""Persisted history compaction helpers."""

from importlib import import_module
from typing import Any

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
    PostResponseCompactionCheck,
    PreparedHistoryState,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
)

_LAZY_EXPORTS = {
    "PreparedScopeHistory": "mindroom.history.runtime",
    "ScopeSessionContext": "mindroom.history.runtime",
    "add_pending_force_compaction_scope": "mindroom.history.storage",
    "apply_replay_plan": "mindroom.history.runtime",
    "close_agent_runtime_sqlite_dbs": "mindroom.history.runtime",
    "close_team_runtime_sqlite_dbs": "mindroom.history.runtime",
    "compute_prompt_token_breakdown": "mindroom.history.compaction",
    "create_scope_session_storage": "mindroom.history.runtime",
    "estimate_preparation_static_tokens": "mindroom.history.runtime",
    "estimate_preparation_static_tokens_for_team": "mindroom.history.runtime",
    "finalize_history_preparation": "mindroom.history.runtime",
    "manual_compaction_unavailable_message": "mindroom.history.policy",
    "open_bound_scope_session_context": "mindroom.history.runtime",
    "open_resolved_scope_session_context": "mindroom.history.runtime",
    "open_scope_session_context": "mindroom.history.runtime",
    "prepare_bound_scope_history": "mindroom.history.runtime",
    "prepare_history_for_run": "mindroom.history.runtime",
    "prepare_scope_history": "mindroom.history.runtime",
    "read_scope_seen_event_ids": "mindroom.history.storage",
    "read_scope_state": "mindroom.history.storage",
    "resolve_bound_team_scope_context": "mindroom.history.runtime",
    "resolve_history_execution_plan": "mindroom.history.policy",
    "update_scope_seen_event_ids": "mindroom.history.storage",
    "write_scope_state": "mindroom.history.storage",
}

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
    "PostResponseCompactionCheck",
    "PreparedHistoryState",
    "PreparedScopeHistory",
    "ResolvedHistorySettings",
    "ResolvedReplayPlan",
    "ScopeSessionContext",
    "add_pending_force_compaction_scope",
    "apply_replay_plan",
    "close_agent_runtime_sqlite_dbs",
    "close_team_runtime_sqlite_dbs",
    "compute_prompt_token_breakdown",
    "create_scope_session_storage",
    "estimate_preparation_static_tokens",
    "estimate_preparation_static_tokens_for_team",
    "finalize_history_preparation",
    "manual_compaction_unavailable_message",
    "open_bound_scope_session_context",
    "open_resolved_scope_session_context",
    "open_scope_session_context",
    "prepare_bound_scope_history",
    "prepare_history_for_run",
    "prepare_scope_history",
    "read_scope_seen_event_ids",
    "read_scope_state",
    "resolve_bound_team_scope_context",
    "resolve_history_execution_plan",
    "update_scope_seen_event_ids",
    "write_scope_state",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
