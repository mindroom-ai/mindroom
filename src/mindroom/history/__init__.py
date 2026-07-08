"""Persisted history compaction helpers.

Re-exports resolve lazily (PEP 562) so slim entry points that only need leaf
history types (config load, the sandbox runner, the tool registry) do not drag
in the history runtime and, through model loading, every provider SDK (#1436).
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.history import agno_team_patch as agno_team_patch
    from mindroom.history.manual import request_compaction_before_next_reply as request_compaction_before_next_reply
    from mindroom.history.policy import (
        context_budget_after_reserve as context_budget_after_reserve,
    )
    from mindroom.history.policy import (
        resolve_history_execution_plan as resolve_history_execution_plan,
    )
    from mindroom.history.prompt_tokens import (
        StaticTokenEstimator as StaticTokenEstimator,
    )
    from mindroom.history.prompt_tokens import (
        agent_static_token_estimator as agent_static_token_estimator,
    )
    from mindroom.history.prompt_tokens import (
        agent_tool_definition_payloads_for_logging as agent_tool_definition_payloads_for_logging,
    )
    from mindroom.history.prompt_tokens import (
        team_static_token_estimator as team_static_token_estimator,
    )
    from mindroom.history.prompt_tokens import (
        team_tool_definition_payloads_for_logging as team_tool_definition_payloads_for_logging,
    )
    from mindroom.history.runtime import (
        HistoryPreparationInputs as HistoryPreparationInputs,
    )
    from mindroom.history.runtime import (
        PreparedScopeHistory as PreparedScopeHistory,
    )
    from mindroom.history.runtime import (
        ScopeSessionContext as ScopeSessionContext,
    )
    from mindroom.history.runtime import (
        apply_replay_plan as apply_replay_plan,
    )
    from mindroom.history.runtime import (
        close_agent_runtime_state_dbs as close_agent_runtime_state_dbs,
    )
    from mindroom.history.runtime import (
        close_team_runtime_state_dbs as close_team_runtime_state_dbs,
    )
    from mindroom.history.runtime import (
        create_scope_session_storage as create_scope_session_storage,
    )
    from mindroom.history.runtime import (
        finalize_history_preparation as finalize_history_preparation,
    )
    from mindroom.history.runtime import (
        note_prepared_history_timing as note_prepared_history_timing,
    )
    from mindroom.history.runtime import (
        open_bound_scope_session_context as open_bound_scope_session_context,
    )
    from mindroom.history.runtime import (
        open_resolved_scope_session_context as open_resolved_scope_session_context,
    )
    from mindroom.history.runtime import (
        prepare_bound_scope_history as prepare_bound_scope_history,
    )
    from mindroom.history.runtime import (
        prepare_scope_history as prepare_scope_history,
    )
    from mindroom.history.runtime import (
        resolve_agent_preparation_inputs as resolve_agent_preparation_inputs,
    )
    from mindroom.history.runtime import (
        resolve_bound_team_scope_context as resolve_bound_team_scope_context,
    )
    from mindroom.history.storage import (
        has_pending_force_compaction_scope as has_pending_force_compaction_scope,
    )
    from mindroom.history.storage import (
        read_scope_seen_event_ids as read_scope_seen_event_ids,
    )
    from mindroom.history.storage import (
        read_scope_state as read_scope_state,
    )
    from mindroom.history.storage import (
        update_scope_seen_event_ids as update_scope_seen_event_ids,
    )
    from mindroom.history.types import (
        CompactionDecision as CompactionDecision,
    )
    from mindroom.history.types import (
        CompactionLifecycle as CompactionLifecycle,
    )
    from mindroom.history.types import (
        CompactionLifecycleFailure as CompactionLifecycleFailure,
    )
    from mindroom.history.types import (
        CompactionLifecycleProgress as CompactionLifecycleProgress,
    )
    from mindroom.history.types import (
        CompactionLifecycleStart as CompactionLifecycleStart,
    )
    from mindroom.history.types import (
        CompactionOutcome as CompactionOutcome,
    )
    from mindroom.history.types import (
        CompactionReplyOutcome as CompactionReplyOutcome,
    )
    from mindroom.history.types import (
        HistoryPolicy as HistoryPolicy,
    )
    from mindroom.history.types import (
        HistoryScope as HistoryScope,
    )
    from mindroom.history.types import (
        HistoryScopeMetadata as HistoryScopeMetadata,
    )
    from mindroom.history.types import (
        PreparedHistoryState as PreparedHistoryState,
    )
    from mindroom.history.types import (
        ResolvedHistorySettings as ResolvedHistorySettings,
    )
    from mindroom.history.types import (
        ResolvedReplayPlan as ResolvedReplayPlan,
    )

_SUBMODULE_BY_ATTRIBUTE = {
    "agno_team_patch": "agno_team_patch",
    "request_compaction_before_next_reply": "manual",
    "context_budget_after_reserve": "policy",
    "resolve_history_execution_plan": "policy",
    "StaticTokenEstimator": "prompt_tokens",
    "agent_static_token_estimator": "prompt_tokens",
    "agent_tool_definition_payloads_for_logging": "prompt_tokens",
    "team_static_token_estimator": "prompt_tokens",
    "team_tool_definition_payloads_for_logging": "prompt_tokens",
    "HistoryPreparationInputs": "runtime",
    "PreparedScopeHistory": "runtime",
    "ScopeSessionContext": "runtime",
    "apply_replay_plan": "runtime",
    "close_agent_runtime_state_dbs": "runtime",
    "close_team_runtime_state_dbs": "runtime",
    "create_scope_session_storage": "runtime",
    "finalize_history_preparation": "runtime",
    "note_prepared_history_timing": "runtime",
    "open_bound_scope_session_context": "runtime",
    "open_resolved_scope_session_context": "runtime",
    "prepare_bound_scope_history": "runtime",
    "prepare_scope_history": "runtime",
    "resolve_agent_preparation_inputs": "runtime",
    "resolve_bound_team_scope_context": "runtime",
    "has_pending_force_compaction_scope": "storage",
    "read_scope_seen_event_ids": "storage",
    "read_scope_state": "storage",
    "update_scope_seen_event_ids": "storage",
    "CompactionDecision": "types",
    "CompactionLifecycle": "types",
    "CompactionLifecycleFailure": "types",
    "CompactionLifecycleProgress": "types",
    "CompactionLifecycleStart": "types",
    "CompactionOutcome": "types",
    "CompactionReplyOutcome": "types",
    "HistoryPolicy": "types",
    "HistoryScope": "types",
    "HistoryScopeMetadata": "types",
    "PreparedHistoryState": "types",
    "ResolvedHistorySettings": "types",
    "ResolvedReplayPlan": "types",
}


def __getattr__(name: str) -> object:
    submodule_name = _SUBMODULE_BY_ATTRIBUTE.get(name)
    if submodule_name is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    module = import_module(f"{__name__}.{submodule_name}")
    if name == submodule_name:
        return module
    return getattr(module, name)
