## Summary

Top duplication candidates for `src/mindroom/history/interrupted_replay.py`:

- Matrix metadata list/prompt-map normalization is repeated in `ai.py`, `llm_request_logging.py`, `teams.py`, `history/storage.py`, `turn_store.py`, and this module.
- Agno agent/team session loading and missing-session construction are repeated in `history/runtime.py`, `teams.py`, and this module.
- Tool-call id normalization overlaps with OpenAI-compatible streaming id extraction, but the behavior is intentionally different enough to keep separate.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
InterruptedReplaySnapshot	class	lines 56-68	related-only	InterruptedReplaySnapshot dataclass replay snapshot metadata source_event_ids response_event_id	src/mindroom/turn_store.py:43; src/mindroom/handled_turns.py:69; src/mindroom/dispatch_handoff.py:99
_normalized_string_tuple	function	lines 71-78	duplicate-found	normalized_string_list matrix seen/source event ids list unique strings	src/mindroom/ai.py:364; src/mindroom/llm_request_logging.py:158; src/mindroom/teams.py:1139; src/mindroom/history/storage.py:277; src/mindroom/turn_store.py:173
tool_execution_call_id	function	lines 81-86	related-only	tool_call_id strip non-empty ToolExecution call id	src/mindroom/api/openai_compat.py:1126; src/mindroom/teams.py:2117; src/mindroom/teams.py:2149
_normalized_prompt_items	function	lines 89-96	duplicate-found	matrix source event prompts prompt map string key value normalization	src/mindroom/llm_request_logging.py:227; src/mindroom/turn_store.py:290; src/mindroom/handled_turns.py:761
split_interrupted_tool_trace	function	lines 99-124	related-only	cancelled tool trace completed interrupted format_tool_started_event format_tool_completed_event	src/mindroom/teams.py:1182; src/mindroom/tool_system/events.py:249; src/mindroom/tool_system/events.py:274
_render_interrupted_tool_trace	function	lines 127-136	none-found	interrupted before completion tool trace render context marker	src/mindroom/tool_system/events.py:329; src/mindroom/teams.py:1182
render_interrupted_replay_content	function	lines 139-152	none-found	interrupted replay content partial_text completed_tools interrupted_tools marker	src/mindroom/tool_system/events.py:329; src/mindroom/teams.py:2101
_interrupted_replay_metadata	function	lines 155-172	duplicate-found	build run metadata matrix_event_id matrix_seen_event_ids source prompts response event	src/mindroom/ai.py:374; src/mindroom/turn_store.py:187; src/mindroom/conversation_state_writer.py:95
build_interrupted_replay_run	function	lines 175-208	related-only	RunOutput TeamRunOutput messages metadata status completed	src/mindroom/api/openai_compat.py:1055; src/mindroom/teams.py:405
build_interrupted_replay_snapshot	function	lines 211-241	duplicate-found	parse persisted run metadata matrix source event ids prompts response event trace metadata	src/mindroom/turn_store.py:286; src/mindroom/llm_request_logging.py:218; src/mindroom/ai.py:406
persist_interrupted_replay_snapshot	function	lines 244-283	duplicate-found	get existing session or create new upsert run upsert session agent team	src/mindroom/history/runtime.py:1062; src/mindroom/teams.py:1124; src/mindroom/conversation_state_writer.py:95
persist_interrupted_replay	function	lines 286-316	related-only	scope_context guard build snapshot persist wrapper	src/mindroom/ai_runtime.py:227; src/mindroom/post_response_effects.py:37
_load_persisted_session	function	lines 319-327	duplicate-found	is_team choose get_team_session get_agent_session	src/mindroom/history/runtime.py:1062; src/mindroom/conversation_state_writer.py:102; src/mindroom/turn_store.py:371
_new_session	function	lines 330-353	duplicate-found	created_at datetime UTC AgentSession TeamSession metadata runs created updated	src/mindroom/history/runtime.py:1064; src/mindroom/teams.py:1126
```

## Findings

### 1. Matrix metadata normalization is repeated

`_normalized_string_tuple` at `src/mindroom/history/interrupted_replay.py:71` duplicates the same ordered, de-duplicating, non-empty string-list filter used by `_normalized_string_list` in `src/mindroom/ai.py:364` and `src/mindroom/llm_request_logging.py:158`.
Narrower variants also appear in `src/mindroom/teams.py:1139`, `src/mindroom/history/storage.py:277`, and `src/mindroom/turn_store.py:173`.

`_normalized_prompt_items` at `src/mindroom/history/interrupted_replay.py:89` repeats the source-prompt map filtering done in `src/mindroom/llm_request_logging.py:227`.
`src/mindroom/turn_store.py:290` forwards raw prompt maps to `HandledTurnState.create`, and `src/mindroom/handled_turns.py:761` performs related normalization for the same persisted source-prompt domain.

Differences to preserve:

- `interrupted_replay.py` returns tuples for snapshot immutability.
- `ai.py` and `llm_request_logging.py` return lists for metadata/log payloads.
- Some readers only need a set or list of seen ids and do not care about preserving tuple shape.

### 2. Matrix run metadata assembly/parsing has several local implementations

`_interrupted_replay_metadata` at `src/mindroom/history/interrupted_replay.py:155` builds Matrix linkage metadata from snapshot fields.
`build_matrix_run_metadata` in `src/mindroom/ai.py:374` builds live run metadata with the same keys for event id, seen ids, source ids, source prompts, and trace context fields such as room, thread, requester, correlation, tools schema, and model params.
`TurnStore._build_run_metadata_for_handled_turn` at `src/mindroom/turn_store.py:187` builds a smaller source-id/source-prompt subset.
`ConversationStateWriter.persist_response_event_id_in_session_run` at `src/mindroom/conversation_state_writer.py:95` mutates only the response-event id field.

`build_interrupted_replay_snapshot` at `src/mindroom/history/interrupted_replay.py:211` parses the same persisted Matrix linkage fields consumed by `TurnStore._persisted_turn_metadata_for_run` at `src/mindroom/turn_store.py:286` and `build_llm_request_log_context` at `src/mindroom/llm_request_logging.py:218`.

Differences to preserve:

- Interrupted replay needs cancellation markers and replay-state metadata that ordinary live runs must not carry.
- Live run metadata in `ai.py` injects defaults for `tools_schema` and `model_params`.
- Turn-store parsing requires an anchor event id and returns a `HandledTurnState`, not raw normalized metadata.

### 3. Agno session get-or-create logic is repeated

`persist_interrupted_replay_snapshot` at `src/mindroom/history/interrupted_replay.py:244`, `_load_persisted_session` at `src/mindroom/history/interrupted_replay.py:319`, and `_new_session` at `src/mindroom/history/interrupted_replay.py:330` implement the same agent/team branch used by `build_scope_session_context` in `src/mindroom/history/runtime.py:1062`.
The team-only missing-session branch in `src/mindroom/teams.py:1124` repeats the `TeamSession` creation part.
`ConversationStateWriter.persist_response_event_id_in_session_run` at `src/mindroom/conversation_state_writer.py:95` repeats the agent/team load branch before mutating run metadata.

Differences to preserve:

- `history/runtime.py` uses `HistoryScope` and `_scope_session_agent_id(scope)`.
- `interrupted_replay.py` receives a bare `scope_id` and an `is_team` boolean.
- `teams.py` creates only `TeamSession`.

### 4. Tool-call id normalization is related but not a refactor target

`tool_execution_call_id` at `src/mindroom/history/interrupted_replay.py:81` normalizes optional provider call ids by accepting only real strings and returning `None` when absent or blank.
`_extract_tool_call_id` in `src/mindroom/api/openai_compat.py:1126` also strips `tool_call_id`, but it coerces to `str` and raises when empty because streaming OpenAI-compatible events require a stable id.

The shared concept is clear, but the contract difference is important.
The optional helper is used by team interruption tracking in `src/mindroom/teams.py:2117` and `src/mindroom/teams.py:2149`, while OpenAI streaming should continue failing fast.

## Proposed Generalization

Add a small metadata/session helper only if this area is touched again:

- Put ordered string normalization and prompt-map normalization in a focused Matrix metadata module, for example `mindroom.history.metadata` or `mindroom.matrix.run_metadata`.
- Expose shape-specific wrappers such as `normalized_string_list`, `normalized_string_tuple`, and `normalized_prompt_items` so callers keep their current return types.
- Consider a `load_scope_session(storage, session_id, is_team)` and `new_scope_session(session_id, scope_id, is_team)` helper near `mindroom.agent_storage` or `mindroom.history.runtime` if more persistence code needs agent/team branching.

No immediate refactor is recommended from this audit alone.
The duplication is real, but most call sites are small and some have intentionally different return types or domain objects.

## Risk/tests

Risks:

- Shared metadata normalization could accidentally change ordering, de-duplication, tuple/list shape, or whether non-empty strings are required.
- Shared prompt-map parsing could change whether empty prompt strings are preserved.
- Shared session creation could use the wrong id field (`agent_id` versus `team_id`) or the wrong scope id for teams.

Tests that would need attention for a refactor:

- Unit tests for `build_matrix_run_metadata`, `build_interrupted_replay_snapshot`, and `render_interrupted_replay_content`.
- Turn-store tests covering persisted coalesced metadata and source prompt maps.
- Session persistence tests covering both agent and team interrupted replay, including missing-session creation.
- OpenAI-compatible streaming tests should keep `_extract_tool_call_id` raising on missing ids.
