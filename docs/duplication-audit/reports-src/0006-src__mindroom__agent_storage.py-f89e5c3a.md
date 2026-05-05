## Summary

The main duplication candidate is Agno session deserialization from `BaseDb.get_session()` results.
`agent_storage.py` provides typed helpers for agent and team sessions, but `ai_runtime.py` and `memory/auto_flush.py` repeat parts of the same raw-object-to-session coercion.

DB path construction is mostly centralized through `create_state_storage()` and reused by agents/history code.
The culture storage helper has a similar directory/create/`SqliteDb` shape to `_create_sqlite_state_storage()`, but its lack of `session_table` is a small intentional difference and not a strong refactor target by itself.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
get_agent_runtime_state_dbs	function	lines 34-39	none-found	"agent.db BaseDb LearningMachine learning.db runtime state db close"	src/mindroom/history/runtime.py:1234, src/mindroom/history/runtime.py:1247, src/mindroom/agents.py:661
create_state_storage	function	lines 42-55	related-only	"create_state_storage SqliteDb session_table state_root sessions learning"	src/mindroom/agents.py:1012, src/mindroom/agents.py:1070, src/mindroom/history/runtime.py:1195
_create_sqlite_state_storage	function	lines 58-68	related-only	"SqliteDb mkdir parents session_table db_file storage_name.db"	src/mindroom/agent_storage.py:112, src/mindroom/matrix/cache/sqlite_event_cache.py:74
create_session_storage	function	lines 71-85	related-only	"create_session_storage resolve_agent_runtime sessions agent_sessions"	src/mindroom/history/runtime.py:1195, src/mindroom/conversation_state_writer.py:62, src/mindroom/memory/auto_flush.py:347
_create_agent_state_db	function	lines 88-109	none-found	"resolve_agent_runtime state_root create_state_storage execution_identity"	src/mindroom/history/runtime.py:1195, src/mindroom/runtime_resolution.py:202
create_culture_storage	function	lines 112-116	related-only	"culture mkdir SqliteDb culture_name.db storage_path"	src/mindroom/agent_storage.py:58, src/mindroom/agents.py:820
get_agent_session	function	lines 119-128	duplicate-found	"get_session SessionType.AGENT AgentSession from_dict raw_session dict"	src/mindroom/memory/auto_flush.py:338, src/mindroom/memory/auto_flush.py:347, src/mindroom/ai_runtime.py:177
get_team_session	function	lines 131-140	duplicate-found	"get_session SessionType.TEAM TeamSession from_dict raw_session dict"	src/mindroom/ai_runtime.py:177, src/mindroom/conversation_state_writer.py:101, src/mindroom/teams.py:1124
```

## Findings

### 1. Session deserialization is duplicated outside `agent_storage.py`

- `src/mindroom/agent_storage.py:119` and `src/mindroom/agent_storage.py:131` load a raw Agno session by `SessionType`, accept already-instantiated `AgentSession`/`TeamSession`, deserialize dict payloads with `from_dict()`, and return `None` for missing or unexpected values.
- `src/mindroom/ai_runtime.py:177` repeats the dict-to-`AgentSession`/`TeamSession` branch for queued-notice cleanup after `storage.get_session()` at `src/mindroom/ai_runtime.py:208`.
- `src/mindroom/memory/auto_flush.py:338` repeats the agent-only coercion branch, and `_load_agent_session()` calls `storage.get_session(session_id, SessionType.AGENT)` directly at `src/mindroom/memory/auto_flush.py:361`.

These are functionally the same deserialization boundary for Agno persisted sessions.
The differences to preserve are that `ai_runtime.py` accepts both session types based on a `SessionType` parameter, while `memory/auto_flush.py` is agent-only and currently does not need team support.

### 2. State DB construction is centralized; related call shapes are not independent duplication

- `src/mindroom/agent_storage.py:42` and `src/mindroom/agent_storage.py:58` are the canonical path creation plus `SqliteDb` construction helpers.
- `src/mindroom/agents.py:1012` and `src/mindroom/agents.py:1070` correctly call `create_state_storage()` for session and learning stores using different `subdir`/`session_table` values.
- `src/mindroom/history/runtime.py:1195` routes agent scope storage through `create_session_storage()` and team scope storage through `create_state_storage()`.

These call sites repeat the same conceptual parameters, but they delegate to the shared helper instead of reimplementing filesystem and `SqliteDb` setup.
No additional generalization is recommended from this audit.

### 3. Culture DB setup is only structurally similar

- `src/mindroom/agent_storage.py:112` creates `storage_path / "culture"` and returns `SqliteDb(db_file=...)`.
- `_create_sqlite_state_storage()` at `src/mindroom/agent_storage.py:58` creates a subdirectory and returns `SqliteDb(session_table=..., db_file=...)`.

This is similar path/SQLite setup, but culture storage intentionally does not pass a session table.
Extracting a lower-level helper would save little code and would obscure the semantic distinction between Agno session DBs and culture DBs.

## Proposed Generalization

Add one small typed helper in `src/mindroom/agent_storage.py`, for example `get_session(storage, session_id, session_type) -> AgentSession | TeamSession | None`, implemented by sharing the current `get_agent_session()` and `get_team_session()` deserialization behavior.
Then update `ai_runtime.py` cleanup and `memory/auto_flush.py` to call the centralized helper instead of locally coercing raw sessions.

No refactor is recommended for DB construction or culture storage.

## Risk/tests

The main risk is accidentally changing how unexpected raw session values are handled.
Tests should cover dict payload deserialization, already-instantiated sessions, missing sessions, and unexpected payload types for both `SessionType.AGENT` and `SessionType.TEAM`.
Queued-notice cleanup tests around `src/mindroom/ai_runtime.py` and auto-flush session loading tests around `src/mindroom/memory/auto_flush.py` would need attention if the helper is introduced.
