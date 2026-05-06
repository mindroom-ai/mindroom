## Summary

Top duplication candidates for `src/mindroom/matrix/stale_stream_cleanup.py`:

1. Synthetic `ResolvedVisibleMessage` construction and raw Matrix event normalization overlap with thread-history snapshot helpers in `src/mindroom/matrix/client_thread_history.py`.
2. Stream metadata classification and metadata preservation overlap with `src/mindroom/matrix/large_messages.py`, `src/mindroom/matrix/cache/thread_writes.py`, and `src/mindroom/execution_preparation.py`.
3. Restart-interrupted note stripping/checking partially overlaps with `src/mindroom/streaming.py`.

The room-level stale cleanup orchestration, requester reply-chain resolution, and stop-reaction cleanup are mostly specialized to restart recovery and do not have a clear active duplicate flow elsewhere.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
InterruptedThread	class	lines 75-84	none-found	InterruptedThread interrupted thread target_event_id partial_text auto resume	src/mindroom/streaming.py:193; src/mindroom/stop.py:328; src/mindroom/conversation_resolver.py:98
_MessageState	class	lines 88-99	none-found	latest_body latest_timestamp latest_event_id stream_status stop_reaction_event_ids	src/mindroom/matrix/client_visible_messages.py:26; src/mindroom/matrix/cache/thread_writes.py:43; src/mindroom/stop.py:258
_requester_resolution_message	function	lines 102-121	duplicate-found	ResolvedVisibleMessage.synthetic sender body timestamp content thread_id	src/mindroom/matrix/client_thread_history.py:174; src/mindroom/matrix/client_thread_history.py:725; src/mindroom/api/openai_compat.py:695
cleanup_stale_streaming_messages	async_function	lines 124-164	none-found	cleanup stale streaming messages joined rooms get_joined_rooms	src/mindroom/tool_approval.py:331; src/mindroom/approval_manager.py:367; src/mindroom/matrix/room_cleanup.py:1
auto_resume_interrupted_threads	async_function	lines 167-227	none-found	auto resume interrupted threads send_message_result resume prompt notify_outbound_message	src/mindroom/scheduling.py:811; src/mindroom/hooks/sender.py:81; src/mindroom/custom_tools/subagents.py:265
_cleanup_room_stale_streaming_messages	async_function	lines 230-318	none-found	scan room stale messages cleanup candidate restart interrupted note repair metadata	src/mindroom/tool_approval.py:331; src/mindroom/approval_manager.py:367; src/mindroom/matrix/room_cleanup.py:1
_repair_restart_marked_message_metadata	async_function	lines 321-361	related-only	non terminal stream metadata repair edit stale message terminal stream content	src/mindroom/delivery_gateway.py:483; src/mindroom/streaming.py:225; src/mindroom/execution_preparation.py:278
_cleanup_one_stale_message	async_function	lines 364-413	related-only	build_restart_interrupted_body redact stop reactions InterruptedThread	src/mindroom/streaming.py:193; src/mindroom/stop.py:258; src/mindroom/stop.py:441
_cleanup_candidate_message	async_function	lines 416-453	none-found	best effort cleanup one stale candidate rate limit delay warning	src/mindroom/approval_manager.py:367; src/mindroom/tool_approval.py:331; src/mindroom/matrix/room_cleanup.py:1
_scan_room_message_states	async_function	lines 456-510	related-only	resolve_latest_visible_messages requester ids latest thread event ids	src/mindroom/matrix/client_visible_messages.py:453; src/mindroom/matrix/client_thread_history.py:265; src/mindroom/matrix/thread_bookkeeping.py:204
_assign_latest_thread_event_ids	function	lines 513-533	duplicate-found	latest_visible_thread_event_id_by_thread all messages thread latest event	src/mindroom/matrix/thread_projection.py:305; src/mindroom/matrix/cache/thread_cache_helpers.py:23; src/mindroom/matrix/client_thread_history.py:1057
_collect_room_history_events	async_function	lines 536-591	related-only	room_messages pagination back chunk lookback record reactions	src/mindroom/matrix/client_thread_history.py:265; src/mindroom/matrix/conversation_cache.py:669; src/mindroom/approval_manager.py:367
_merge_bot_resolved_message_states	function	lines 594-614	none-found	merge bot resolved messages cleanup state requester fallback thread	src/mindroom/matrix/client_visible_messages.py:453; src/mindroom/matrix/client_thread_history.py:281
_merge_resolved_message_state	function	lines 617-634	related-only	copy ResolvedVisibleMessage fields latest body timestamp content stream_status	src/mindroom/matrix/client_visible_messages.py:350; src/mindroom/matrix/cache/thread_writes.py:438
_scanned_message_data_by_event_id	async_function	lines 637-672	duplicate-found	scanned message data by event id EventInfo resolve_thread_ids_for_event_infos synthetic	src/mindroom/matrix/client_thread_history.py:174; src/mindroom/matrix/client_thread_history.py:725; src/mindroom/matrix/thread_bookkeeping.py:204
_scanned_message_requires_exact_requester_fetch	function	lines 675-679	none-found	m.new_content reply_to_event_id exact requester fetch	none
_derive_requester_ids_for_bot_messages	async_function	lines 682-739	related-only	effective requester ids bot messages reply chain cache trusted sender ids	src/mindroom/turn_controller.py:221; src/mindroom/authorization.py:145; src/mindroom/dispatch_handoff.py:212
_resolve_requester_for_bot_message	async_function	lines 742-784	none-found	resolve requester bot message reply_to_event_id load scanned fetched	none
_resolve_requester_for_event_id	async_function	lines 787-847	related-only	follow reply chain internal sender effective requester max depth	src/mindroom/matrix/reply_chain.py:1; src/mindroom/turn_controller.py:221; src/mindroom/authorization.py:145
_load_message_data_for_requester_resolution	async_function	lines 850-874	none-found	load message data requester resolution scanned fetched resolved	none
_resolve_requester_from_internal_reply	async_function	lines 877-922	related-only	internal sender reply edge real requester recursive	src/mindroom/turn_controller.py:221; src/mindroom/dispatch_handoff.py:212; src/mindroom/authorization.py:145
_load_scanned_or_fetched_message_data	async_function	lines 925-948	none-found	load scanned or fetched message data cache fallback exact fetch	none
_fetch_message_data_for_event_id	async_function	lines 951-1022	duplicate-found	room_get_event extract_edit_body extract_and_resolve_message synthetic fallback	src/mindroom/matrix/client_thread_history.py:725; src/mindroom/matrix/client_thread_history.py:174; src/mindroom/matrix/client_visible_messages.py:420
_as_string_keyed_dict	function	lines 1025-1035	related-only	normalize string keyed dict content keys isinstance key str	src/mindroom/matrix/client_thread_history.py:180; src/mindroom/matrix/large_messages.py:55; src/mindroom/matrix/message_content.py:192
_is_internal_sender	function	lines 1038-1044	related-only	active_internal_sender_ids sender is internal	src/mindroom/authorization.py:83; src/mindroom/authorization.py:138; src/mindroom/turn_controller.py:233
_cleanup_trusted_sender_ids	function	lines 1047-1056	related-only	active_internal_sender_ids trusted sender ids add bot user id	src/mindroom/matrix/client_visible_messages.py:140; src/mindroom/matrix/conversation_cache.py:414
_effective_requester_for_message	function	lines 1059-1069	related-only	get_effective_sender_id_for_reply_permissions content original sender	src/mindroom/turn_controller.py:221; src/mindroom/authorization.py:145; src/mindroom/dispatch_handoff.py:212
_record_stop_reaction	function	lines 1072-1096	related-only	m.reaction stop button target event id sender bot user	src/mindroom/stop.py:372; src/mindroom/stop.py:395; src/mindroom/interactive.py:476
_edit_stale_message	async_function	lines 1099-1158	duplicate-found	format_message_with_mentions edit_message_result notify_outbound_message preserve visible body	src/mindroom/streaming.py:823; src/mindroom/matrix/client_delivery.py:412; src/mindroom/delivery_gateway.py:531
_preserved_cleanup_content	function	lines 1161-1176	duplicate-found	preserve io.mindroom metadata m.mentions original sender long_text exclusion	src/mindroom/matrix/large_messages.py:36; src/mindroom/matrix/large_messages.py:55; src/mindroom/matrix/visible_body.py:75
_has_non_terminal_stream_status	function	lines 1179-1184	duplicate-found	nonterminal stream status pending streaming terminal statuses	src/mindroom/matrix/large_messages.py:59; src/mindroom/matrix/cache/thread_writes.py:33; src/mindroom/execution_preparation.py:278
_terminal_stream_content	function	lines 1187-1191	related-only	set stream status error terminal content	src/mindroom/delivery_gateway.py:483; src/mindroom/streaming.py:225
_redact_stop_reactions	async_function	lines 1194-1243	related-only	redact stop reactions room_redact relation ids history ids	src/mindroom/stop.py:258; src/mindroom/stop.py:441; src/mindroom/bot.py:1770
_get_stop_reaction_event_ids_from_relations	async_function	lines 1246-1279	related-only	room_get_event_relations annotation m.reaction stop keys bot sender	src/mindroom/stop.py:372; src/mindroom/interactive.py:736; src/mindroom/commands/config_confirmation.py:320
_iter_reaction_relation_events	async_function	lines 1282-1295	none-found	room_get_event_relations RelationshipType.annotation m.reaction iterator	none
_extract_partial_text	function	lines 1298-1303	duplicate-found	build_restart_interrupted_body removesuffix restart interrupted note clean partial	src/mindroom/streaming.py:140; src/mindroom/streaming.py:193
_truncate_partial_text	function	lines 1306-1311	related-only	truncate text limit ellipsis strip	src/mindroom/streaming.py:126; src/mindroom/agent_descriptions.py:1
_select_threads_to_resume	function	lines 1314-1341	none-found	newest unique threaded interruptions max_resumes latest_by_key	none
_has_restart_interrupted_note	function	lines 1344-1346	duplicate-found	body endswith restart interrupted response note	src/mindroom/streaming.py:130; src/mindroom/streaming.py:140
_is_cleanup_candidate	function	lines 1349-1356	duplicate-found	stream_status pending streaming completed restart note cleanup candidate	src/mindroom/matrix/large_messages.py:59; src/mindroom/matrix/cache/thread_writes.py:33; src/mindroom/execution_preparation.py:278
_is_recent_timestamp	function	lines 1359-1362	related-only	current time ms timestamp recency guard	src/mindroom/approval_manager.py:1218; src/mindroom/bot.py:1345
_is_older_than_cleanup_window	function	lines 1365-1368	related-only	current time ms timestamp lookback window	src/mindroom/approval_manager.py:1218; src/mindroom/approval_transport.py:56
_chunk_reaches_cleanup_lookback_limit	function	lines 1371-1381	related-only	oldest event server_timestamp cleanup lookback window	src/mindroom/matrix/client_thread_history.py:265; src/mindroom/matrix/conversation_cache.py:669
_build_auto_resume_content	function	lines 1384-1418	duplicate-found	build message content mention agent display name matrix.to original sender thread reply	src/mindroom/matrix/mentions.py:251; src/mindroom/voice_handler.py:404; src/mindroom/matrix/client_delivery.py:436
_entity_display_name	function	lines 1421-1427	duplicate-found	agent team display_name fallback agent_name	src/mindroom/orchestration/runtime.py:417; src/mindroom/topic_generator.py:50; src/mindroom/api/openai_compat.py:883
_agent_name_for_bot_user_id	function	lines 1430-1444	related-only	extract_agent_name fallback username config get_ids	src/mindroom/thread_utils.py:106; src/mindroom/bot.py:1438; src/mindroom/turn_controller.py:261
```

## Findings

### 1. Synthetic visible-message construction is repeated

`_requester_resolution_message` (`src/mindroom/matrix/stale_stream_cleanup.py:102`) normalizes arbitrary content/body/timestamp data into `ResolvedVisibleMessage.synthetic`.
The same shape appears in `_snapshot_message_dict` (`src/mindroom/matrix/client_thread_history.py:174`) and the non-text branch of `_resolve_room_message_event` (`src/mindroom/matrix/client_thread_history.py:725`).
`_scanned_message_data_by_event_id` (`src/mindroom/matrix/stale_stream_cleanup.py:637`) and `_fetch_message_data_for_event_id` (`src/mindroom/matrix/stale_stream_cleanup.py:951`) also repeat raw event-source normalization before building that object.

Why this is duplicated: all paths coerce Matrix event data into a lightweight visible message with sender, body, timestamp, event ID, content, and thread ID.

Differences to preserve: stale cleanup sometimes accepts missing body as an empty string, rejects non-string content keys in `_as_string_keyed_dict`, and sometimes intentionally omits sidecar hydration when using scanned room history.

### 2. Stream metadata classification and preservation are split across modules

`_has_non_terminal_stream_status` (`src/mindroom/matrix/stale_stream_cleanup.py:1179`) and `_is_cleanup_candidate` (`src/mindroom/matrix/stale_stream_cleanup.py:1349`) duplicate the "pending/streaming means non-terminal" concept also present in `_NONTERMINAL_STREAM_STATUSES` and `_is_nonterminal_stream_content` (`src/mindroom/matrix/large_messages.py:59`) and `_NONTERMINAL_STREAM_STATUSES` (`src/mindroom/matrix/cache/thread_writes.py:33`).
`src/mindroom/execution_preparation.py:278` also classifies pending/streaming as partial replies.

`_preserved_cleanup_content` (`src/mindroom/matrix/stale_stream_cleanup.py:1161`) overlaps with the passthrough metadata filters in `src/mindroom/matrix/large_messages.py:36` and `src/mindroom/matrix/large_messages.py:55`.

Why this is duplicated: multiple Matrix paths need the same stream-status vocabulary and "which MindRoom metadata survives a transformed event" policy.

Differences to preserve: stale cleanup excludes `io.mindroom.long_text` and rewrites stream status to `error`; large-message previewing has sidecar-specific passthrough rules; thread cache uses the status to decide cache behavior.

### 3. Restart-interrupted note detection/removal is duplicated with streaming helpers

`_has_restart_interrupted_note` (`src/mindroom/matrix/stale_stream_cleanup.py:1344`) repeats one branch of `is_interrupted_partial_reply` (`src/mindroom/streaming.py:130`).
`_extract_partial_text` (`src/mindroom/matrix/stale_stream_cleanup.py:1298`) repeats part of `clean_partial_reply_text` (`src/mindroom/streaming.py:140`) but specifically uses `build_restart_interrupted_body` (`src/mindroom/streaming.py:193`) to normalize placeholder behavior.

Why this is duplicated: streaming owns terminal visible notes, while stale cleanup needs a restart-only variant for auto-resume previews.

Differences to preserve: cleanup should only treat the restart note as repair/auto-resume state, not generic cancellation or error notes.

### 4. Outbound edit/send with mention formatting repeats a standard delivery shape

`_edit_stale_message` (`src/mindroom/matrix/stale_stream_cleanup.py:1099`) repeats the common sequence of `format_message_with_mentions`, Matrix delivery, and `conversation_cache.notify_outbound_message` seen in `StreamingResponse._prepare_delivery`/commit paths (`src/mindroom/streaming.py:823`, `src/mindroom/streaming.py:908`) and delivery helpers (`src/mindroom/matrix/client_delivery.py:412`).
`_build_auto_resume_content` (`src/mindroom/matrix/stale_stream_cleanup.py:1384`) overlaps with other "mention an agent by display name and Matrix ID" builders, especially mention formatting in `src/mindroom/matrix/mentions.py:251`.

Why this is duplicated: stale cleanup builds low-level Matrix content directly because it needs restart-specific metadata preservation and thread targeting.

Differences to preserve: cleanup edits must drop warmup visible-body metadata before formatting, then restore canonical visible body when needed; auto-resume messages are router-authored system relays with `ORIGINAL_SENDER_KEY`.

## Proposed Generalization

1. Add a small visible-message factory in `src/mindroom/matrix/client_visible_messages.py`, for example `synthetic_visible_message_from_event_parts(...)`, and migrate `_requester_resolution_message` plus matching thread-history synthetic builders to it.
2. Move shared stream-status predicates to a focused stream metadata helper, or export a small helper from `streaming.py`, so non-terminal status checks use one constant set.
3. Add a restart-note helper in `streaming.py`, for example `has_restart_interrupted_note(text)` and `clean_restart_interrupted_text(text)`, if stale cleanup and streaming continue to share the note vocabulary.
4. Keep stop-reaction cleanup local to `stale_stream_cleanup.py`; the overlap with `StopManager` is lifecycle-related but not a clean abstraction candidate.
5. Do not generalize the full stale-cleanup orchestration; the room scan, requester resolution, edit repair, and auto-resume flow are specialized and would become less clear behind a broad abstraction.

## Risk/tests

Primary risks are Matrix content regressions: losing `m.mentions`, `ORIGINAL_SENDER_KEY`, stream status, thread IDs, or visible-body sidecar metadata.
Focused tests should cover synthetic visible-message construction from scanned and fetched events, restart-note detection/removal, non-terminal stream-status classification, and cleanup edit content preservation.
Existing stale-stream cleanup tests should also assert that auto-resume content still mentions the target agent and preserves the original requester.
