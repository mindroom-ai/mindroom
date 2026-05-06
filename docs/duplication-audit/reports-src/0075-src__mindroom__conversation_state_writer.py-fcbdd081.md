## Summary

Top duplication candidates are the ad hoc team scope ID construction in `ConversationStateWriter.team_history_scope`, the `HistoryScope.kind` to `SessionType` mapping, and the agent/team session retrieval branch used before mutating session runs.
The storage creation path mostly delegates to canonical storage helpers and does not contain meaningful duplicate implementation beyond expected orchestration.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ConversationStateWriterDeps	class	lines 27-33	not-a-behavior-symbol	ConversationStateWriterDeps dependency dataclass runtime logger runtime_paths agent_name	none
ConversationStateWriter	class	lines 37-117	related-only	ConversationStateWriter history scope storage response_event_id session run metadata	src/mindroom/history/runtime.py:1125; src/mindroom/response_runner.py:359; src/mindroom/response_lifecycle.py:254
ConversationStateWriter.history_scope	method	lines 42-46	related-only	HistoryScope agent team config.teams agent_name resolve_history_scope	src/mindroom/history/runtime.py:326; src/mindroom/history/storage.py:453
ConversationStateWriter.session_type_for_scope	method	lines 48-50	duplicate-found	SessionType TEAM AGENT scope.kind team agent	src/mindroom/history/runtime.py:171; src/mindroom/history/compaction.py:210; src/mindroom/history/compaction.py:659; src/mindroom/response_lifecycle.py:277
ConversationStateWriter.team_history_scope	method	lines 52-60	duplicate-found	team_ join sorted agent names ad hoc team scope build_team_user_id	src/mindroom/history/runtime.py:980; src/mindroom/history/runtime.py:1287; src/mindroom/memory/_policy.py:93
ConversationStateWriter.create_storage	method	lines 62-89	related-only	create_scope_session_storage create_session_storage HistoryScope normalized scope agent team storage	src/mindroom/history/runtime.py:1195; src/mindroom/agent_storage.py:71; src/mindroom/history/runtime.py:1125
ConversationStateWriter.persist_response_event_id_in_session_run	method	lines 91-117	related-only	matrix_response_event_id run.metadata get_agent_session get_team_session upsert_session	src/mindroom/history/storage.py:251; src/mindroom/history/storage.py:271; src/mindroom/agents.py:718; src/mindroom/ai_runtime.py:201
```

## Findings

### 1. Ad hoc team scope ID construction is duplicated

`src/mindroom/conversation_state_writer.py:57-60` converts Matrix IDs to agent/member names, sorts them, and builds `team_<name+name>` for an ad hoc team history scope.
`src/mindroom/history/runtime.py:1287-1291` builds the same `team_<sorted agent ids joined by +>` string for bound team history scopes.
`src/mindroom/memory/_policy.py:93-95` builds the same string for team memory user IDs.

The behavior is functionally the same stable team identifier scheme, but the inputs differ.
`conversation_state_writer.py` starts from `MatrixID` values and falls back to Matrix usernames, while `history/runtime.py` starts from Agno `Agent.id` values and returns `None` if no names are available.
`memory/_policy.py` is memory-scoped naming, so it may intentionally mirror rather than share the history implementation.

### 2. Scope/session type mapping is repeated

`src/mindroom/conversation_state_writer.py:48-50` maps `HistoryScope(kind="team")` to `SessionType.TEAM` and all other scopes to `SessionType.AGENT`.
Equivalent branching appears where code derives a session type from concrete Agno session classes in `src/mindroom/history/runtime.py:171`, `src/mindroom/history/compaction.py:210`, and `src/mindroom/history/compaction.py:659`.
`src/mindroom/response_lifecycle.py:277-285` and `src/mindroom/conversation_state_writer.py:101-105` also repeat the related `SessionType.TEAM` branch to choose `get_team_session` versus `get_agent_session`.

The behavior is small but central.
The current duplication is not large enough to force a refactor, but future changes to supported scope/session kinds would need multiple edits.

### 3. Response event ID metadata write has related run-metadata behavior, not a clear duplicate

`src/mindroom/conversation_state_writer.py:101-117` retrieves the correct session, finds the matching `RunOutput` or `TeamRunOutput` by `run_id`, writes `MATRIX_RESPONSE_EVENT_ID_METADATA_KEY`, and upserts the session.
Nearby code reads the same metadata key in `src/mindroom/history/storage.py:271-283`, removes runs by Matrix metadata in `src/mindroom/agents.py:718-750`, and mutates persisted runs for queued-notice cleanup in `src/mindroom/ai_runtime.py:201-220`.

These are related persisted-run metadata mutations, but they do not duplicate the same write semantics.
The response event ID writer targets one run by run ID and is intentionally idempotent.

## Proposed Generalization

If refactoring later, the smallest useful helper would be in `mindroom.history.types` or `mindroom.history.runtime`:

- `session_type_for_history_scope(scope: HistoryScope) -> SessionType`
- `build_ad_hoc_team_scope_id(agent_names: list[str]) -> str | None`
- optionally `get_session_for_type(storage: BaseDb, session_id: str, session_type: SessionType) -> AgentSession | TeamSession | None`

No immediate refactor is recommended for `persist_response_event_id_in_session_run`; it is a focused state-write operation with only related readers and removers elsewhere.

## Risk/tests

The main behavior risk is changing stable team scope IDs, which would orphan existing persisted team history or memory.
Tests should cover named teams versus ad hoc teams, sorted member-name stability, Matrix username fallback, and storage/session type selection for both agent and team scopes.
For response event persistence, tests should cover missing sessions, empty runs, mismatched run IDs, existing matching metadata, and both `RunOutput` and `TeamRunOutput`.
