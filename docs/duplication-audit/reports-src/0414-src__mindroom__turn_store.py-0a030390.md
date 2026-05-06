Summary: `TurnStore` has two meaningful duplication candidates.
The strongest is repeated Matrix source-event metadata parsing/normalization across `turn_store.py`, `ai.py`, `history/interrupted_replay.py`, `handled_turns.py`, and `agents.py`.
The second is repeated session-context setup for loading and deleting persisted runs inside `TurnStore`.
Several public methods are intentional thin ledger/state-writer delegations and are not useful refactor targets by themselves.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
LoadedTurnRecord	class	lines 31-37	related-only	LoadedTurnRecord loaded turn record edit regeneration context	tests and src references via rg; src/mindroom/edit_regenerator.py:141
_PersistedTurnMetadata	class	lines 41-52	duplicate-found	persisted turn metadata source_event_ids response_event_id source_event_prompts	src/mindroom/history/interrupted_replay.py:48, src/mindroom/history/interrupted_replay.py:223, src/mindroom/handled_turns.py:693
_PersistedTurnMetadata.is_coalesced	method	lines 50-52	related-only	is_coalesced len source_event_ids	src/mindroom/handled_turns.py checked via rg, src/mindroom/edit_regenerator.py:216
_LoadPersistedTurnMetadataRequest	class	lines 56-62	not-a-behavior-symbol	load persisted turn metadata request dataclass	none
_RemoveStaleRunsRequest	class	lines 66-72	not-a-behavior-symbol	remove stale runs request dataclass	none
TurnStoreDeps	class	lines 76-83	not-a-behavior-symbol	TurnStoreDeps collaborators dataclass	src/mindroom/bot.py:419
TurnStore	class	lines 87-477	duplicate-found	TurnStore persisted run metadata handled turn ledger stale runs	src/mindroom/handled_turns.py:693, src/mindroom/agents.py:718, src/mindroom/history/interrupted_replay.py:223
TurnStore.__post_init__	method	lines 93-98	related-only	HandledTurnLedger construction tracking_base_path	src/mindroom/bot.py:419, src/mindroom/handled_turns.py checked
TurnStore.record_turn	method	lines 100-107	related-only	record handled turn visible echo sources	src/mindroom/handled_turns.py:563, src/mindroom/turn_controller.py:1142
TurnStore.record_turn_record	method	lines 109-111	related-only	record_handled_turn_record ledger delegation	src/mindroom/edit_regenerator.py:85
TurnStore.is_handled	method	lines 113-115	related-only	has_responded is_handled source_event_id	src/mindroom/turn_controller.py:1149
TurnStore.visible_echo_for_source	method	lines 117-119	related-only	get_visible_echo_event_id visible echo source	src/mindroom/turn_controller.py:731, src/mindroom/handled_turns.py:1065
TurnStore.record_visible_echo	method	lines 121-123	related-only	record_visible_echo echo event id	src/mindroom/turn_controller.py:749
TurnStore.visible_echo_for_sources	method	lines 125-127	related-only	visible_echo_event_id_for_sources source_event_ids	src/mindroom/handled_turns.py:563, src/mindroom/turn_controller.py:1142
TurnStore.get_turn_record	method	lines 129-131	related-only	get_turn_record ledger source_event_id	src/mindroom/edit_regenerator.py:141
TurnStore.response_history_scope	method	lines 133-143	related-only	history_scope team_history_scope response_action kind	src/mindroom/conversation_state_writer.py checked, src/mindroom/turn_controller.py:1781
TurnStore.attach_response_context	method	lines 145-157	related-only	with_response_context response_owner history_scope conversation_target	src/mindroom/turn_controller.py:1124, src/mindroom/turn_controller.py:1779
TurnStore.build_run_metadata	method	lines 159-184	duplicate-found	MATRIX_SOURCE_EVENT_IDS_METADATA_KEY additional_source_event_ids normalized string list	src/mindroom/ai.py:395, src/mindroom/history/interrupted_replay.py:164, src/mindroom/agents.py:745
TurnStore._build_run_metadata_for_handled_turn	method	lines 187-198	duplicate-found	MATRIX_SOURCE_EVENT_IDS_METADATA_KEY MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY metadata build	src/mindroom/history/interrupted_replay.py:164, src/mindroom/ai.py:418
TurnStore.load_turn	method	lines 200-254	related-only	load_turn merge ledger persisted metadata HandledTurnRecord	src/mindroom/edit_regenerator.py:141, src/mindroom/handled_turns.py:895
TurnStore.remove_stale_runs_for_edit	method	lines 256-283	related-only	remove stale runs exact turn record fallback edited message	src/mindroom/edit_regenerator.py:295, src/mindroom/agents.py:718
TurnStore._persisted_turn_metadata_for_run	method	lines 285-310	duplicate-found	parse run metadata source event ids prompts response event	src/mindroom/history/interrupted_replay.py:223, src/mindroom/handled_turns.py:895, src/mindroom/ai.py:395
TurnStore._latest_matching_persisted_turn_metadata	method	lines 312-337	duplicate-found	latest matching run metadata created_at run_index metadata filter	src/mindroom/agents.py:718, src/mindroom/history/interrupted_replay.py:223
TurnStore._load_persisted_turn_metadata	method	lines 339-389	duplicate-found	session_contexts create_session_id build_message_target build_execution_identity create_storage get session	src/mindroom/turn_store.py:391, src/mindroom/conversation_state_writer.py:99, src/mindroom/history/runtime.py:815
TurnStore._remove_stale_runs_for_edited_message	method	lines 391-434	duplicate-found	session_contexts create_session_id build_message_target build_execution_identity create_storage remove_run_by_event_id	src/mindroom/turn_store.py:339, src/mindroom/agents.py:718
TurnStore._remove_stale_runs_for_turn_record	method	lines 436-477	duplicate-found	remove_run_by_event_id source_event_ids storage session_type build_execution_identity	src/mindroom/agents.py:718, src/mindroom/conversation_state_writer.py:99
```

Findings:

1. Matrix turn-run metadata parsing and source-event normalization are repeated.
`TurnStore.build_run_metadata` and `_build_run_metadata_for_handled_turn` serialize `matrix_source_event_ids` and `matrix_source_event_prompts` at `src/mindroom/turn_store.py:159` and `src/mindroom/turn_store.py:187`.
`TurnStore._persisted_turn_metadata_for_run` parses `matrix_event_id`, `matrix_source_event_ids`, `matrix_source_event_prompts`, and `matrix_response_event_id` at `src/mindroom/turn_store.py:285`.
The same metadata keys are serialized and parsed for interrupted replay in `src/mindroom/history/interrupted_replay.py:164` and `src/mindroom/history/interrupted_replay.py:223`.
`src/mindroom/ai.py:395` has another local list-normalizer for source event IDs, and `src/mindroom/agents.py:745` repeats a looser list-of-strings extraction when removing runs.
`src/mindroom/handled_turns.py:693` provides canonical source-event ID normalization with duplicate removal, but the persisted-run metadata paths do not share one helper.
The behavior is functionally the same: normalize Matrix event IDs from metadata, preserve ordering, drop invalid values, and carry prompt/response linkage.
Differences to preserve: `ai.py` returns lists for metadata, `history/interrupted_replay.py` returns tuples and prompt item tuples, and `TurnStore` falls back to the anchor event ID when `matrix_source_event_ids` is missing or invalid.

2. `TurnStore` repeats persisted session access setup for thread and room fallback sessions.
`TurnStore._load_persisted_turn_metadata` builds `(thread_id, session_id)` and `(None, room session_id)` candidates, deduplicates session IDs, builds a message target, clears the thread root for the room session, builds execution identity, creates storage, loads an agent/team session, and closes storage at `src/mindroom/turn_store.py:339`.
`TurnStore._remove_stale_runs_for_edited_message` repeats the same candidate-session setup and storage lifecycle before calling `remove_run_by_event_id` at `src/mindroom/turn_store.py:391`.
The behavior is duplicated inside one module, not just similar: both scan the same possible persisted sessions for one edited source event and requester.
Differences to preserve: load mode needs the opened session and must compare newest run metadata; removal mode calls `remove_run_by_event_id` and logs per removed session.

3. Agent/team session selection is repeated in several modules.
`TurnStore._load_persisted_turn_metadata` chooses `get_team_session` or `get_agent_session` from `SessionType` at `src/mindroom/turn_store.py:372`.
`src/mindroom/agents.py:718` does the same inside `remove_run_by_event_id`, `src/mindroom/conversation_state_writer.py:99` does it while persisting response event IDs, and `src/mindroom/history/runtime.py:815` / `src/mindroom/history/runtime.py:1062` do it for history preparation.
This is a small duplicated branch around the same Agno storage API.
Differences to preserve: some call sites branch on `SessionType`, while history runtime branches on `HistoryScope.kind`.

Proposed generalization:

1. Add a small focused helper module only if editing this area again, for example `mindroom.matrix_run_metadata`, with pure helpers to normalize source event IDs, normalize prompt maps, serialize handled-turn metadata, and parse persisted turn metadata.
It should preserve the current anchor fallback behavior for `TurnStore` rather than forcing all consumers into the same return shape.
2. Inside `TurnStore`, extract the repeated edited-message candidate-session setup into a private iterator/context helper that yields `session_id`, candidate target, execution identity, storage, and session type, while keeping the load/delete behavior as separate callbacks or loops.
3. Consider a tiny `get_session_for_type(storage, session_id, session_type)` helper near `agent_storage.py` if another change touches these branches; do not refactor all current call sites just for this audit.

Risk/tests:

Metadata normalization affects edit regeneration, interrupted replay, coalesced batches, and stale run cleanup.
Tests should cover invalid and duplicate `matrix_source_event_ids`, missing source IDs with anchor fallback, prompt map filtering, response event ID preservation, and both agent and team sessions.
The session-iterator extraction should be covered by tests where `thread_id` is `None`, where thread and room session IDs are identical, and where both thread and room sessions contain matching runs with different `created_at` values.
No production code was edited.
