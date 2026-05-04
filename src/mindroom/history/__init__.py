"""Persisted history compaction helpers."""

from mindroom.history.compaction import (
    normalize_compaction_budget_tokens,
)
from mindroom.history.manual import (
    MANUAL_COMPACTION_SUCCESS_MESSAGE,
    ManualCompactionRequestResult,
    request_compaction_before_next_reply,
)
from mindroom.history.policy import (
    context_budget_after_reserve,
    manual_compaction_unavailable_message,
    resolve_history_execution_plan,
)
from mindroom.history.provider_request import (
    agent_tool_definition_payloads_for_logging,
    compute_prompt_token_breakdown,
    team_tool_definition_payloads_for_logging,
)
from mindroom.history.runtime import (
    PreparedScopeHistory,
    ScopeSessionContext,
    apply_replay_plan,
    close_agent_runtime_state_dbs,
    close_team_runtime_state_dbs,
    create_scope_session_storage,
    estimate_preparation_static_tokens,
    estimate_preparation_static_tokens_for_team,
    finalize_history_preparation,
    note_prepared_history_timing,
    open_bound_scope_session_context,
    open_resolved_scope_session_context,
    prepare_bound_scope_history,
    prepare_history_for_run,
    prepare_scope_history,
    resolve_bound_team_scope_context,
)
from mindroom.history.storage import (
    read_scope_seen_event_ids,
    strip_transient_enrichment_from_session,
    update_scope_seen_event_ids,
)
from mindroom.history.types import (
    CompactionDecision,
    CompactionLifecycle,
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionLifecycleSuccess,
    CompactionOutcome,
    CompactionReplyOutcome,
    HistoryScope,
    PreparedHistoryState,
    ResolvedReplayPlan,
)

__all__ = [
    "CompactionDecision",
    "CompactionLifecycle",
    "CompactionLifecycleFailure",
    "CompactionLifecycleProgress",
    "CompactionLifecycleStart",
    "CompactionLifecycleSuccess",
    "CompactionOutcome",
    "CompactionReplyOutcome",
    "HistoryScope",
    "MANUAL_COMPACTION_SUCCESS_MESSAGE",
    "ManualCompactionRequestResult",
    "PreparedHistoryState",
    "PreparedScopeHistory",
    "ResolvedReplayPlan",
    "ScopeSessionContext",
    "agent_tool_definition_payloads_for_logging",
    "apply_replay_plan",
    "close_agent_runtime_state_dbs",
    "close_team_runtime_state_dbs",
    "compute_prompt_token_breakdown",
    "context_budget_after_reserve",
    "create_scope_session_storage",
    "estimate_preparation_static_tokens",
    "estimate_preparation_static_tokens_for_team",
    "finalize_history_preparation",
    "manual_compaction_unavailable_message",
    "normalize_compaction_budget_tokens",
    "note_prepared_history_timing",
    "open_bound_scope_session_context",
    "open_resolved_scope_session_context",
    "prepare_bound_scope_history",
    "prepare_history_for_run",
    "prepare_scope_history",
    "read_scope_seen_event_ids",
    "request_compaction_before_next_reply",
    "resolve_history_execution_plan",
    "resolve_bound_team_scope_context",
    "strip_transient_enrichment_from_session",
    "team_tool_definition_payloads_for_logging",
    "update_scope_seen_event_ids",
]
