Summary: One narrow duplication candidate was found: Matrix event-id list normalization from run metadata is repeated in `src/mindroom/history/storage.py` and `src/mindroom/teams.py`.
Most other symbols in `storage.py` are related to broader metadata/session persistence patterns elsewhere, but the behavior is scope-specific enough that a shared abstraction would not clearly reduce complexity.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
read_scope_state	function	lines 31-34	related-only	read_scope_state HistoryScopeState scope metadata	src/mindroom/history/compaction.py:213; src/mindroom/history/runtime.py:174; src/mindroom/history/manual.py:83
read_scope_states	function	lines 37-51	related-only	MINDROOM_COMPACTION_METADATA_KEY version states metadata	src/mindroom/history/storage.py:366; src/mindroom/history/storage.py:430; src/mindroom/metadata_merge.py:9
write_scope_state	function	lines 54-79	related-only	write_scope_state metadata version states pop	src/mindroom/history/runtime.py:179; src/mindroom/history/manual.py:85; src/mindroom/conversation_state_writer.py:111
clear_force_compaction_state	function	lines 82-90	related-only	force_compact_before_next_run replace write_scope_state	src/mindroom/history/runtime.py:161; src/mindroom/history/runtime.py:1415
add_pending_force_compaction_scope	function	lines 93-108	none-found	pending_compaction_scope_keys session_state compact_context	src/mindroom/history/manual.py:88; src/mindroom/custom_tools/compact_context.py:58
consume_pending_force_compaction_scope	function	lines 111-144	none-found	pending_compaction_scope_keys consume session_data session_state	src/mindroom/history/runtime.py:1411; src/mindroom/history/manual.py:88
strip_transient_enrichment_from_session	function	lines 147-181	related-only	strip_transient_enrichment memory_prompt transient_system_context storage.upsert_session	src/mindroom/response_runner.py:501; src/mindroom/post_response_effects.py:249
_strip_transient_system_context_from_run	function	lines 184-202	none-found	member_responses transient_system_context RunOutput TeamRunOutput	src/mindroom/history/compaction.py:1349; src/mindroom/teams.py:1148
_strip_transient_system_context_from_messages	function	lines 205-228	none-found	system message content transient_system_context strip messages	src/mindroom/ai.py:737; src/mindroom/response_runner.py:293
_remove_transient_system_context	function	lines 231-248	none-found	remove transient_system_context stripped_block replacements	src/mindroom/response_runner.py:293; src/mindroom/history/storage.py:205
read_scope_seen_event_ids	function	lines 251-260	related-only	read_scope_seen_event_ids run_seen_event_ids scope_for_run	src/mindroom/history/compaction.py:539; src/mindroom/teams.py:1139
seen_event_ids_for_runs	function	lines 263-268	related-only	seen_event_ids_for_runs run_seen_event_ids	src/mindroom/history/compaction.py:539; src/mindroom/teams.py:1139
run_seen_event_ids	function	lines 271-283	duplicate-found	MATRIX_SEEN_EVENT_IDS_METADATA_KEY MATRIX_RESPONSE_EVENT_ID_METADATA_KEY run metadata	src/mindroom/teams.py:1139; src/mindroom/turn_store.py:171; src/mindroom/turn_store.py:285
update_scope_seen_event_ids	function	lines 286-304	related-only	update_scope_seen_event_ids normalized_event_ids union metadata	src/mindroom/teams.py:1116; src/mindroom/history/compaction.py:542
metadata_with_merged_seen_event_ids	function	lines 307-320	related-only	metadata_with_merged_seen_event_ids merge scope states deep_merge_metadata	src/mindroom/history/compaction.py:663; src/mindroom/metadata_merge.py:9
_parse_state	function	lines 323-333	related-only	HistoryScopeState parse metadata last_compacted_at last_summary_model	src/mindroom/history/types.py:50; src/mindroom/history/storage.py:336
_state_to_metadata	function	lines 336-346	related-only	HistoryScopeState to metadata force_compact last_compacted	src/mindroom/history/types.py:145; src/mindroom/history/storage.py:323
_state_is_empty	function	lines 349-355	none-found	HistoryScopeState empty force_compact_before_next_run	src/mindroom/history/storage.py:61; src/mindroom/history/storage.py:70
_read_preserved_scope_seen_event_ids	function	lines 358-359	related-only	preserved scope seen event ids	src/mindroom/history/storage.py:251; src/mindroom/history/storage.py:286
_read_scope_seen_event_states	function	lines 362-363	related-only	read scope seen event states metadata	src/mindroom/history/storage.py:37; src/mindroom/history/storage.py:366
_read_scope_seen_event_states_from_metadata	function	lines 366-386	related-only	MINDROOM_MATRIX_HISTORY_METADATA_KEY seen_event_ids states version	src/mindroom/history/storage.py:37; src/mindroom/turn_store.py:285; src/mindroom/teams.py:1139
_write_scope_seen_event_states	function	lines 389-390	related-only	write scope seen event states session.metadata	src/mindroom/conversation_state_writer.py:111; src/mindroom/history/storage.py:54
_metadata_with_scope_seen_event_states	function	lines 393-414	related-only	metadata_with_scope_seen_event_states version states metadata pop	src/mindroom/history/storage.py:54; src/mindroom/metadata_merge.py:9
_state_with_seen_event_ids	function	lines 417-427	related-only	state_with_seen_event_ids sorted event ids raw state	src/mindroom/teams.py:1139; src/mindroom/turn_store.py:171
_valid_matrix_history_metadata	function	lines 430-436	related-only	valid metadata version key dict	src/mindroom/history/storage.py:37; src/mindroom/metadata_merge.py:9
_merge_scope_seen_event_states	function	lines 439-446	related-only	merge scope seen event states set update	src/mindroom/metadata_merge.py:9; src/mindroom/history/storage.py:286
_scope_for_run	function	lines 449-458	none-found	TeamRunOutput team_id RunOutput agent_id HistoryScope	src/mindroom/history/runtime.py:151; src/mindroom/turn_store.py:312
```

Findings:

1. `run_seen_event_ids` duplicates Matrix seen-event-id list extraction already present in team response persistence.
   `src/mindroom/history/storage.py:271` reads `run.metadata`, filters `MATRIX_SEEN_EVENT_IDS_METADATA_KEY` to non-empty strings, and also includes `MATRIX_RESPONSE_EVENT_ID_METADATA_KEY`.
   `src/mindroom/teams.py:1139` implements the same non-empty string filtering for `MATRIX_SEEN_EVENT_IDS_METADATA_KEY` on a metadata dict, but intentionally returns a list and does not include the response event id.
   `src/mindroom/turn_store.py:171` has a similar inline filter for source event ids before appending additional ids, but it uses `MATRIX_SOURCE_EVENT_IDS_METADATA_KEY`, so it is related normalization rather than the same domain field.
   Difference to preserve: `run_seen_event_ids` must return a set and include the response event id, while `_run_metadata_seen_event_ids` must return only explicit seen-event ids from a metadata dict.

2. Versioned scoped metadata parsing/writing is repeated in shape inside `storage.py`, but not clearly duplicated elsewhere under `src`.
   `read_scope_states` / `write_scope_state` manage `MINDROOM_COMPACTION_METADATA_KEY` with `version` and `states`.
   `_read_scope_seen_event_states_from_metadata` / `_metadata_with_scope_seen_event_states` manage `MINDROOM_MATRIX_HISTORY_METADATA_KEY` with the same envelope shape.
   The envelope mechanics are similar, but the payload semantics differ: compaction state stores `HistoryScopeState`, while matrix history state stores preserved seen-event ids and preserves any existing per-scope state fields.
   This is an internal pattern, not a strong cross-module duplication candidate.

3. Response-event metadata mutation is related but not a duplicate of scoped history storage.
   `src/mindroom/conversation_state_writer.py:91` mutates one run's metadata to add `MATRIX_RESPONSE_EVENT_ID_METADATA_KEY` and upserts the session.
   `strip_transient_enrichment_from_session` in `src/mindroom/history/storage.py:147` similarly locates one persisted run, mutates it, and upserts the session.
   The traversal criteria and mutation are different enough that extracting a shared helper would likely obscure the call sites.

Proposed generalization:

Introduce a small metadata helper only if the Matrix event-id normalization keeps spreading.
The minimal shape would be a pure function such as `matrix_metadata_string_list(metadata: dict[str, Any] | None, key: str) -> list[str]` in a metadata-focused module, then have `run_seen_event_ids`, `teams._run_metadata_seen_event_ids`, and the source-event-id append path in `turn_store` use it.
No broader refactor is recommended for the scoped versioned metadata envelopes because the current duplication is local, behavior-specific, and preserves subtly different merge/delete semantics.

Risk/tests:

If the event-id normalization helper is introduced, tests should cover non-dict metadata, non-list metadata values, empty strings, non-string list members, ordering for list-returning callers, and inclusion of `MATRIX_RESPONSE_EVENT_ID_METADATA_KEY` in `run_seen_event_ids`.
If the scoped metadata envelopes are ever generalized, tests must verify stale version rejection, preservation of unrelated metadata, deletion when states are empty, and preservation of existing matrix-history per-scope fields when only `seen_event_ids` changes.
