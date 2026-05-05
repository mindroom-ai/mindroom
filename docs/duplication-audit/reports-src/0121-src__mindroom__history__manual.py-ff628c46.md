## Summary

Top duplication candidate: `request_compaction_before_next_reply` and `history.runtime._prepare_scope_state_for_run` both mark the same scoped `force_compact_before_next_run` flag by reading `HistoryScopeState`, replacing the force flag, writing it back, and upserting the session.
This is a small active duplication in persisted compaction-state mutation.

`_resolve_active_compaction_settings` is related to runtime preparation input resolution, but the behavior is not equivalent because manual compaction resolves team-vs-agent scope from a live `Agent.team_id` and supports room aliases for configured teams/agents.
`_validate_compaction_budget` is a thin wrapper over already-shared policy functions and has no meaningful standalone duplication.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ManualCompactionRequestResult	class	lines 27-31	none-found	ManualCompactionRequestResult; dataclass Result message session_state	src/mindroom/custom_tools/compact_context.py:49; src/mindroom/history/storage.py:93; src/mindroom/approval_manager.py:197
request_compaction_before_next_reply	function	lines 34-99	duplicate-found	request_compaction_before_next_reply; force_compact_before_next_run=True; read_scope_state write_scope_state upsert_session; add_pending_force_compaction_scope	src/mindroom/history/runtime.py:1403; src/mindroom/history/storage.py:93; src/mindroom/custom_tools/compact_context.py:49; src/mindroom/history/compaction.py:207
_resolve_active_compaction_settings	function	lines 102-134	related-only	resolve_runtime_model get_entity_compaction_config get_default_compaction_config team_id; _resolve_entity_preparation_inputs	src/mindroom/history/runtime.py:1309; src/mindroom/ai.py:753; src/mindroom/execution_preparation.py:773; src/mindroom/teams.py:1425; src/mindroom/teams.py:1486
_validate_compaction_budget	function	lines 137-153	related-only	resolve_history_execution_plan manual_compaction_unavailable_message describe_compaction_unavailability destructive_compaction_available	src/mindroom/history/policy.py:20; src/mindroom/history/policy.py:169; src/mindroom/history/runtime.py:684; src/mindroom/history/runtime.py:1415
```

## Findings

### 1. Scoped force-compaction state mutation is duplicated

`src/mindroom/history/manual.py:82-86` schedules manual compaction by reading the current scoped state, replacing `force_compact_before_next_run=True`, writing the scoped state back, and upserting the session.
`src/mindroom/history/runtime.py:1410-1414` repeats the same read/replace/write/upsert sequence after consuming a pending force-compaction scope from Agno `session_state`.

These are functionally the same persisted mutation: both request that the next run for one `HistoryScope` be treated as forced/manual compaction.
The only difference is trigger source.
`manual.py` does it immediately after a user/tool request and may also record the pending scope in `session_state`.
`runtime.py` does it when replaying a pending scope from `session_state` during run preparation.

The clear-side operation already has a dedicated helper, `clear_force_compaction_state` in `src/mindroom/history/storage.py:82-90`.
There is no matching `set_force_compaction_state` helper, so callers duplicate the set-side write pattern.

## Proposed Generalization

Add a small helper beside `clear_force_compaction_state` in `src/mindroom/history/storage.py`, for example:

```python
def set_force_compaction_state(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> HistoryScopeState:
    state = read_scope_state(session, scope)
    next_state = replace(state, force_compact_before_next_run=True)
    write_scope_state(session, scope, next_state)
    return next_state
```

Then update `manual.py` and `runtime.py` to call it and keep `storage.upsert_session(session)` at the call sites, because the storage handle and timing of persistence are owned by each caller.
This keeps the abstraction minimal and symmetric with `clear_force_compaction_state`.

No refactor is recommended for `_resolve_active_compaction_settings`.
It overlaps with `history.runtime._resolve_entity_preparation_inputs`, but only in low-level calls to `Config.resolve_runtime_model()` and `Config.get_*_compaction_config()`.
Manual compaction deliberately does not compute history settings, authored-compaction status, static prompt tokens, or execution plans there, and it has extra live-agent team fallback behavior.

No refactor is recommended for `_validate_compaction_budget`.
It correctly delegates the real budget calculation and user-facing unavailability message to `history.policy`.

## Risk/tests

Risk for the proposed force-state helper is low if the helper only centralizes the existing read/replace/write sequence and leaves session upsert behavior unchanged.
Tests should cover both direct manual requests and pending session-state consumption:

- `tests/test_compact_context.py` cases that assert the scoped force flag is persisted after `compact_context`.
- `tests/test_agno_history.py` cases that consume pending compaction scopes and assert `force_compact_before_next_run` transitions.

No production code was edited for this audit.
