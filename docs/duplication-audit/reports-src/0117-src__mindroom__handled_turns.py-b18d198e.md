Summary: Top duplication candidates are durable JSON/YAML file replacement with fsync and advisory locking, repeated non-empty string/event-id normalization, and repeated Matrix run metadata parsing for source event IDs and prompt maps.
No production code was edited.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_SerializedHistoryScope	class	lines 24-28	related-only	HistoryScope serialization kind scope_id	src/mindroom/conversation_state_writer.py:66; src/mindroom/history/types.py:19
_SerializedConversationTarget	class	lines 31-39	related-only	MessageTarget room_id source_thread_id resolved_thread_id session_id	src/mindroom/message_target.py:14; src/mindroom/tool_system/runtime_context.py:434
_SerializedHandledTurnRecord	class	lines 42-56	none-found	handled turn persisted response_event_id visible_echo_event_id source_event_prompts	none
HandledTurnState	class	lines 63-228	related-only	HandledTurnState source_event_ids response_event_id is_coalesced	src/mindroom/turn_store.py:45; src/mindroom/history/interrupted_replay.py:65
HandledTurnState.create	method	lines 77-105	related-only	normalize source_event_ids response_event_id source_event_prompts	src/mindroom/turn_store.py:290; src/mindroom/history/interrupted_replay.py:218
HandledTurnState.from_source_event_id	method	lines 108-132	none-found	from_source_event_id single source event handled turn	none
HandledTurnState.anchor_event_id	method	lines 135-137	related-only	source_event_ids[-1] anchor event	src/mindroom/turn_store.py:290
HandledTurnState.is_coalesced	method	lines 140-142	duplicate-found	is_coalesced len(source_event_ids) > 1	src/mindroom/turn_store.py:52; src/mindroom/handled_turns.py:249
HandledTurnState.with_response_event_id	method	lines 144-156	related-only	with_response_event_id copy handled turn response_event_id	src/mindroom/history/turn_recorder.py:38
HandledTurnState.with_visible_echo_event_id	method	lines 158-170	related-only	visible echo event id copy handled turn	src/mindroom/turn_store.py:100
HandledTurnState.with_source_event_prompts	method	lines 172-187	related-only	source_event_prompts refreshed prompts handled turn	src/mindroom/turn_controller.py:1647
HandledTurnState.with_request_context	method	lines 189-206	related-only	requester_id correlation_id with request context	src/mindroom/turn_controller.py:1119; src/mindroom/turn_controller.py:1666
HandledTurnState.with_response_context	method	lines 208-228	related-only	response context history_scope conversation_target	src/mindroom/turn_store.py:147
HandledTurnRecord	class	lines 232-251	related-only	HandledTurnRecord source_event_ids response_event_id completed	src/mindroom/turn_store.py:45; src/mindroom/edit_regenerator.py:86
HandledTurnRecord.is_coalesced	method	lines 249-251	duplicate-found	is_coalesced len(source_event_ids) > 1	src/mindroom/turn_store.py:52; src/mindroom/handled_turns.py:140
HandledTurnLedger	class	lines 255-690	related-only	ledger record handled turn json lock responded	src/mindroom/turn_store.py:94; src/mindroom/interactive.py:114
HandledTurnLedger.__post_init__	method	lines 265-271	related-only	initialize paths load cleanup lock file	src/mindroom/oauth/state.py:24; src/mindroom/interactive.py:293
HandledTurnLedger.record_handled_turn	method	lines 273-294	related-only	record terminal handled turn save ledger	src/mindroom/turn_store.py:100; src/mindroom/turn_controller.py:337
HandledTurnLedger.record_handled_turn_record	method	lines 296-318	related-only	record exact handled turn record anchor	src/mindroom/edit_regenerator.py:86; src/mindroom/turn_store.py:110
HandledTurnLedger.record_visible_echo	method	lines 320-345	related-only	record visible echo preserve existing record	src/mindroom/turn_store.py:117; src/mindroom/turn_controller.py:731
HandledTurnLedger.has_responded	method	lines 347-352	related-only	has_responded is_handled completed record	src/mindroom/turn_store.py:114; src/mindroom/agents.py:745
HandledTurnLedger.get_response_event_id	method	lines 354-358	related-only	get response_event_id from metadata record	src/mindroom/history/storage.py:280; src/mindroom/conversation_state_writer.py:91
HandledTurnLedger.get_visible_echo_event_id	method	lines 360-364	related-only	get visible echo event id source event	src/mindroom/turn_store.py:117; src/mindroom/turn_controller.py:731
HandledTurnLedger.visible_echo_event_id_for_sources	method	lines 366-373	related-only	visible echo for source_event_ids first existing	src/mindroom/turn_store.py:125
HandledTurnLedger.get_turn_record	method	lines 375-396	related-only	build HandledTurnRecord from persisted metadata	src/mindroom/turn_store.py:213; src/mindroom/edit_regenerator.py:149
HandledTurnLedger._load_responses	method	lines 398-401	duplicate-found	load json under file lock	src/mindroom/interactive.py:266; src/mindroom/oauth/state.py:35
HandledTurnLedger._save_responses_locked	method	lines 403-423	duplicate-found	NamedTemporaryFile json.dump fsync replace fsync directory	src/mindroom/matrix/state.py:188; src/mindroom/interactive.py:188; src/mindroom/tool_system/output_files.py:415
HandledTurnLedger._cleanup_old_events	method	lines 425-438	related-only	cleanup old records max age max count timestamp	src/mindroom/oauth/state.py:77; src/mindroom/memory/auto_flush.py:305
HandledTurnLedger._file_lock	method	lines 441-448	duplicate-found	fcntl flock LOCK_EX LOCK_SH unlock contextmanager	src/mindroom/interactive.py:266; src/mindroom/oauth/state.py:35; src/mindroom/codex_model.py:123
HandledTurnLedger._read_responses_file_locked	method	lines 450-520	related-only	read json normalize corrupt quarantine invalid entries	src/mindroom/oauth/state.py:58; src/mindroom/credentials.py:142
HandledTurnLedger._quarantine_corrupt_responses_file_locked	method	lines 522-529	duplicate-found	corrupt file replace corrupt timestamp	src/mindroom/oauth/state.py:58
HandledTurnLedger._fsync_base_path	method	lines 531-537	duplicate-found	fsync directory after atomic replace	src/mindroom/matrix/state.py:207; src/mindroom/interactive.py:201
HandledTurnLedger._visible_echo_for_sources	method	lines 539-545	none-found	first visible_echo_event_id for source ids	none
HandledTurnLedger._persist_handled_turn_locked	method	lines 547-586	related-only	persist handled turn for each source id serialized record	src/mindroom/turn_store.py:100; src/mindroom/history/storage.py:307
HandledTurnLedger._normalized_prompt_map	method	lines 588-600	related-only	preserve existing prompt map if explicit missing	src/mindroom/turn_store.py:290; src/mindroom/llm_request_logging.py:228
HandledTurnLedger._normalized_response_owner	method	lines 602-615	related-only	normalize or preserve existing string field	src/mindroom/handled_turns.py:632; src/mindroom/handled_turns.py:647
HandledTurnLedger._normalized_history_scope	method	lines 617-630	related-only	normalize or preserve existing HistoryScope	src/mindroom/conversation_state_writer.py:66
HandledTurnLedger._normalized_requester_id	method	lines 632-645	duplicate-found	normalize or preserve existing optional string field	src/mindroom/handled_turns.py:602; src/mindroom/handled_turns.py:647
HandledTurnLedger._normalized_correlation_id	method	lines 647-660	duplicate-found	normalize or preserve existing optional string field	src/mindroom/handled_turns.py:602; src/mindroom/handled_turns.py:632
HandledTurnLedger._normalized_conversation_target	method	lines 662-675	related-only	normalize or preserve existing MessageTarget	src/mindroom/message_target.py:42; src/mindroom/tool_system/runtime_context.py:434
HandledTurnLedger._normalized_anchor_event_id	method	lines 677-690	related-only	normalize anchor or preserve existing fallback source_event_ids[-1]	src/mindroom/turn_store.py:290
_normalize_source_event_ids	function	lines 693-702	duplicate-found	deduplicate strings preserving order source_event_ids	 src/mindroom/llm_request_logging.py:218; src/mindroom/ai.py:406; src/mindroom/history/interrupted_replay.py:222; src/mindroom/attachments.py:628
_normalized_event_id	function	lines 705-707	duplicate-found	non-empty string or None event_id	src/mindroom/commands/handler.py:175; src/mindroom/approval_events.py:128; src/mindroom/approval_transport.py:254; src/mindroom/matrix/media.py:93
_normalized_response_owner	function	lines 710-712	duplicate-found	non-empty string or None optional id	src/mindroom/api/oauth.py:258; src/mindroom/api/credentials.py:200
_normalized_requester_id	function	lines 715-717	duplicate-found	non-empty string or None requester_id	src/mindroom/api/credentials.py:200; src/mindroom/approval_transport.py:254
_normalized_correlation_id	function	lines 720-722	duplicate-found	non-empty string or None correlation_id	src/mindroom/api/oauth.py:258; src/mindroom/knowledge/registry.py:277
_normalized_history_scope	function	lines 725-733	related-only	validate HistoryScope kind scope_id	src/mindroom/conversation_state_writer.py:66; src/mindroom/history/types.py:19
_normalized_conversation_target	function	lines 736-756	related-only	validate MessageTarget and session_id	src/mindroom/message_target.py:42; src/mindroom/message_target.py:56
_explicit_prompt_map_for_sources	function	lines 759-771	duplicate-found	filter prompt map to string source event ids	src/mindroom/llm_request_logging.py:228; src/mindroom/history/interrupted_replay.py:224
_serialized_record	function	lines 774-821	related-only	build persisted record dict from normalized dataclass fields	src/mindroom/history/interrupted_replay.py:155; src/mindroom/tool_system/tool_calls.py:167
_responses_file_path	function	lines 824-833	related-only	validate filename path service/agent name	src/mindroom/credentials.py:131; src/mindroom/config/models.py:451
_cleaned_responses	function	lines 836-852	related-only	remove stale timestamp records max_age max_events	src/mindroom/oauth/state.py:77; src/mindroom/memory/auto_flush.py:305
_ResponseGroup	class	lines 856-861	not-a-behavior-symbol	group container for cleanup	none
_response_groups	function	lines 864-884	related-only	group records by source_event_ids latest timestamp	src/mindroom/history/storage.py:263
_normalize_serialized_record	function	lines 887-948	related-only	normalize old and new persisted record schema legacy keys	src/mindroom/history/interrupted_replay.py:218; src/mindroom/turn_store.py:290
_source_event_ids_for_record	function	lines 951-963	related-only	parse source_event_ids list fallback event_id	src/mindroom/turn_store.py:290; src/mindroom/history/interrupted_replay.py:224
_prompt_map_for_record	function	lines 966-979	duplicate-found	filter source_event_prompts dict to string values	src/mindroom/llm_request_logging.py:228; src/mindroom/history/interrupted_replay.py:224
_anchor_event_id_for_record	function	lines 982-990	related-only	anchor_event_id fallback last source id	src/mindroom/turn_store.py:290
_response_owner_for_record	function	lines 993-997	related-only	read normalized response_owner from record	none
_requester_id_for_record	function	lines 1000-1004	duplicate-found	read optional normalized string from record	src/mindroom/handled_turns.py:993; src/mindroom/handled_turns.py:1007
_correlation_id_for_record	function	lines 1007-1011	duplicate-found	read optional normalized string from record	src/mindroom/handled_turns.py:993; src/mindroom/handled_turns.py:1000
_history_scope_for_record	function	lines 1014-1025	related-only	parse HistoryScope from dict kind scope_id	src/mindroom/conversation_state_writer.py:66
_conversation_target_for_record	function	lines 1028-1051	related-only	parse MessageTarget from dict with legacy thread_id	src/mindroom/message_target.py:42; src/mindroom/tool_system/runtime_context.py:434
_response_event_id_for_record	function	lines 1054-1062	related-only	read response_event_id with legacy response_id	src/mindroom/history/storage.py:280
_visible_echo_event_id_for_record	function	lines 1065-1073	related-only	read visible_echo_event_id with legacy visible_echo_response_id	none
_completed_for_record	function	lines 1076-1078	none-found	completed flag default true for record	none
```

Findings:

1. Durable atomic file persistence is duplicated.
   `HandledTurnLedger._save_responses_locked` writes JSON to a temporary file, flushes and fsyncs it, replaces the target, fsyncs the containing directory, and removes leftover temp files.
   The same behavior appears in `src/mindroom/matrix/state.py:188` for YAML Matrix state and in `src/mindroom/interactive.py:188` for interactive question JSON.
   `src/mindroom/tool_system/output_files.py:415` also uses the same temporary-file, flush, fsync, replace pattern.
   Differences to preserve: handled turns and interactive questions write JSON, Matrix state writes YAML, and handled turns currently rely on the caller already holding the cross-process lock.

2. Advisory file locking and corrupt-file quarantine are repeated.
   `HandledTurnLedger._file_lock` wraps `fcntl.flock` with exclusive/shared modes and unlocks in `finally`.
   `src/mindroom/interactive.py:266` and `src/mindroom/oauth/state.py:35` do the same lock/unlock lifecycle, while `src/mindroom/codex_model.py:123` repeats the exclusive-lock subset.
   `HandledTurnLedger._quarantine_corrupt_responses_file_locked` renames bad JSON to a `.corrupt-*` path, which overlaps with `src/mindroom/oauth/state.py:58`.
   Differences to preserve: handled turns support shared reads, OAuth state only uses exclusive writes, and OAuth uses a human timestamp plus UUID collision guard while handled turns use `time_ns()`.

3. Optional string and Matrix metadata normalization is duplicated.
   `_normalized_event_id`, `_normalized_requester_id`, `_normalized_correlation_id`, and `_normalized_response_owner` all implement the same "non-empty string or None" behavior found in `src/mindroom/commands/handler.py:175`, `src/mindroom/approval_events.py:128`, `src/mindroom/approval_transport.py:254`, `src/mindroom/api/oauth.py:258`, and `src/mindroom/api/credentials.py:200`.
   `_normalize_source_event_ids`, `_explicit_prompt_map_for_sources`, and `_prompt_map_for_record` overlap with source-event and prompt-map parsing in `src/mindroom/llm_request_logging.py:218`, `src/mindroom/llm_request_logging.py:228`, `src/mindroom/ai.py:406`, and `src/mindroom/history/interrupted_replay.py:222`.
   Differences to preserve: handled turns dedupe source event IDs while preserving order; some other metadata paths return lists rather than tuples or sets.

4. Small intra-module duplication exists in handled-turn shape helpers.
   `HandledTurnState.is_coalesced`, `HandledTurnRecord.is_coalesced`, and `src/mindroom/turn_store.py:52` all use `len(source_event_ids) > 1`.
   `_normalized_response_owner`, `_normalized_requester_id`, and `_normalized_correlation_id` are identical except for field names, and the matching `*_for_record` helpers repeat the same get-and-normalize shape.
   This is real duplication, but the code is small and local enough that extraction would mostly reduce lines rather than clarify behavior.

Proposed generalization:

1. Add a small filesystem helper module, for example `src/mindroom/persistence/atomic_file.py`, with `atomic_write_text(path, write_callback)` or narrowly typed JSON/YAML helpers plus `fsync_directory(path)`.
2. Add a focused `file_lock(path, exclusive: bool)` context manager only if the next touched persistence module needs shared/exclusive locking; avoid migrating all callers in one patch.
3. Add a `non_empty_str(value: object) -> str | None` helper only in a module that already owns validation utilities, then replace callers opportunistically when touching those modules.
4. Keep handled-turn source-event and prompt-map normalization local unless a dedicated Matrix run-metadata parser is introduced; the tuple/list/set differences make a broad helper easy to misuse.
5. Do not extract the `is_coalesced` property or one-line record field readers unless a larger typed handled-turn metadata object already exists.

Risk/tests:

The persistence helper has the highest payoff but also the highest regression risk because fsync and cleanup semantics are durability-sensitive.
Tests should cover successful atomic replacement, temp-file cleanup on write failure, directory fsync OSError behavior if preserved, and JSON/YAML serializer differences.
Locking tests should cover shared-read versus exclusive-write behavior if a common lock helper is introduced.
String and Matrix metadata normalization tests should preserve empty-string filtering, order-preserving dedupe, tuple/list return shape, legacy key fallback, and prompt-map filtering to tracked source event IDs only.
