"""Persisted history compaction helpers."""

from mindroom.history import agno_team_patch
from mindroom.history.manual import request_compaction_before_next_reply
from mindroom.history.policy import (
    context_budget_after_reserve,
    resolve_history_execution_plan,
)
from mindroom.history.prompt_tokens import (
    StaticTokenEstimator,
    agent_static_token_estimator,
    agent_tool_definition_payloads_for_logging,
    compute_prompt_token_breakdown,
    team_static_token_estimator,
    team_tool_definition_payloads_for_logging,
)
from mindroom.history.runtime import (
    HistoryPreparationInputs,
    PreparedScopeHistory,
    ScopeSessionContext,
    apply_replay_plan,
    close_agent_runtime_state_dbs,
    close_team_runtime_state_dbs,
    create_scope_session_storage,
    finalize_history_preparation,
    note_prepared_history_timing,
    open_bound_scope_session_context,
    open_resolved_scope_session_context,
    prepare_bound_scope_history,
    prepare_scope_history,
    resolve_agent_preparation_inputs,
    resolve_bound_team_scope_context,
)
from mindroom.history.storage import (
    has_pending_force_compaction_scope,
    read_scope_seen_event_ids,
    read_scope_state,
    update_scope_seen_event_ids,
)
from mindroom.history.types import (
    CompactionDecision,
    CompactionLifecycle,
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionOutcome,
    CompactionReplyOutcome,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeMetadata,
    PreparedHistoryState,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
)

# Applied on package import so every entry point that touches persisted history
# gets the Team roleful-input and inline-media dedupe patch before any Agno run.
agno_team_patch.apply_patch()

__all__ = [
    "CompactionDecision",
    "CompactionLifecycle",
    "CompactionLifecycleFailure",
    "CompactionLifecycleProgress",
    "CompactionLifecycleStart",
    "CompactionOutcome",
    "CompactionReplyOutcome",
    "HistoryPolicy",
    "HistoryPreparationInputs",
    "HistoryScope",
    "HistoryScopeMetadata",
    "PreparedHistoryState",
    "PreparedScopeHistory",
    "ResolvedHistorySettings",
    "ResolvedReplayPlan",
    "ScopeSessionContext",
    "StaticTokenEstimator",
    "agent_static_token_estimator",
    "agent_tool_definition_payloads_for_logging",
    "apply_replay_plan",
    "close_agent_runtime_state_dbs",
    "close_team_runtime_state_dbs",
    "compute_prompt_token_breakdown",
    "context_budget_after_reserve",
    "create_scope_session_storage",
    "finalize_history_preparation",
    "has_pending_force_compaction_scope",
    "note_prepared_history_timing",
    "open_bound_scope_session_context",
    "open_resolved_scope_session_context",
    "prepare_bound_scope_history",
    "prepare_scope_history",
    "read_scope_seen_event_ids",
    "read_scope_state",
    "request_compaction_before_next_reply",
    "resolve_agent_preparation_inputs",
    "resolve_bound_team_scope_context",
    "resolve_history_execution_plan",
    "team_static_token_estimator",
    "team_tool_definition_payloads_for_logging",
    "update_scope_seen_event_ids",
]
