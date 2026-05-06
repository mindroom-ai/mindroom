Summary: The strongest duplication candidates are the visible-message projection flow shared with `client_visible_messages.py`, the four public thread fetch wrappers that differ only by hydration/stale-fallback policy, and the raw event-id extractor duplicated in `thread_projection.py`.
The `/threads` page error shape is intentionally shared by callers and does not need production-code movement from this module.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_ThreadHistoryFetchResult	class	lines 55-64	none-found	_thread history fetch result dataclass fetch_ms room_scan_pages scanned_event_count	src/mindroom/matrix/client_thread_history.py:985
_ThreadEventSourceScanResult	class	lines 68-73	none-found	thread event source scan result page_count scanned_event_count	src/mindroom/matrix/thread_room_scan.py:36
_thread_history_result	function	lines 76-83	related-only	thread_history_result ThreadHistoryResult wrapper is_full_history diagnostics	src/mindroom/matrix/cache/thread_history_result.py:60; src/mindroom/matrix/cache/thread_reads.py:82
_log_thread_history_refresh	function	lines 86-114	none-found	matrix_cache_thread_history_refreshed thread_read_source coordinator_queue_wait_ms	none
RoomThreadsPageError	class	lines 117-130	related-only	RoomThreadsPageError errcode retry_after_ms response	custom_tools/matrix_conversation_operations.py:539; custom_tools/matrix_room.py:322
RoomThreadsPageError.__init__	method	lines 120-130	related-only	errcode retry_after_ms exception response attributes	custom_tools/matrix_conversation_operations.py:539; custom_tools/matrix_room.py:322
_room_threads_page_error_from_response	function	lines 133-141	none-found	ErrorResponse retry_after_ms status_code room threads	none
_room_threads_page_error_from_exception	function	lines 144-148	related-only	ClientError TimeoutError transport error message	custom_tools/matrix_room.py:333
_is_room_message_event	function	lines 151-154	related-only	event source type m.room.message room message filter	custom_tools/matrix_conversation_operations.py:390; thread_summary.py:224; bot_room_lifecycle.py:156
_room_message_fallback_body	function	lines 157-167	duplicate-found	event fallback body RoomMessageText RoomMessageNotice content body	src/mindroom/matrix/client_visible_messages.py:283
_snapshot_message_dict	function	lines 170-193	duplicate-found	ResolvedVisibleMessage synthetic visible_body_from_event_source EventInfo thread_id	src/mindroom/matrix/client_thread_history.py:718; src/mindroom/matrix/client_visible_messages.py:296
_parse_room_message_event	function	lines 196-202	related-only	parse_event m.room.message event source nio Event	src/mindroom/matrix/media.py:84
_parse_visible_text_message_event	function	lines 205-210	related-only	parse visible text notice event RoomMessageText RoomMessageNotice	src/mindroom/matrix/client_visible_messages.py:453
_event_source_for_cache	function	lines 213-215	related-only	normalize_nio_event_for_cache wrapper event source for cache	src/mindroom/matrix/cache/thread_writes.py:327
_event_id_from_source	function	lines 218-221	duplicate-found	event_id from source Mapping str	src/mindroom/matrix/thread_projection.py:206; src/mindroom/matrix/thread_membership.py:412; src/mindroom/matrix/thread_bookkeeping.py:413
_bundled_replacement_source	function	lines 224-253	duplicate-found	bundled replacement m.relations m.replace latest_event event	src/mindroom/matrix/client_visible_messages.py:230
_resolve_thread_history_from_event_sources_timed	async_function	lines 256-331	duplicate-found	resolve latest visible messages edits EventInfo sort thread messages	src/mindroom/matrix/client_visible_messages.py:453; src/mindroom/matrix/thread_projection.py:210
_load_stale_cached_thread_history	async_function	lines 334-403	related-only	stale cache get_thread_events degraded diagnostics stale_cache	src/mindroom/matrix/cache/thread_reads.py:208
_resolve_cached_thread_history	async_function	lines 406-435	related-only	resolve cached thread history invalidate cache on corruption	src/mindroom/matrix/cache/thread_writes.py:315
_cache_reject_diagnostics	function	lines 438-459	none-found	cache_validated_at cache_invalidated_at room_cache_invalidation_reason	none
_load_cached_thread_history_if_usable	async_function	lines 462-531	related-only	thread_cache_rejection_reason get_thread_cache_state get_thread_events	src/mindroom/matrix/cache/thread_cache_helpers.py:30; src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:62
_invalidate_thread_cache_entry	async_function	lines 534-548	related-only	invalidate_thread warning failed cache invalidation	src/mindroom/matrix/cache/thread_write_cache_ops.py:315
_fetch_thread_history_with_events	async_function	lines 551-568	none-found	fetch thread history with events wrapper	none
refresh_thread_history_from_source	async_function	lines 571-676	related-only	refresh thread history cache store stale fallback diagnostics	src/mindroom/matrix/cache/thread_reads.py:101
_store_thread_history_cache	async_function	lines 679-706	related-only	replace_thread_if_not_newer Event cache write failed	src/mindroom/matrix/cache/sqlite_event_cache.py:664; src/mindroom/matrix/cache/postgres_event_cache.py:1044
_thread_history_fetch_is_cacheable	function	lines 709-715	duplicate-found	cacheable contains root event_id thread_id	src/mindroom/matrix/thread_projection.py:206
_resolve_thread_history_message	async_function	lines 718-763	duplicate-found	resolve visible message extract_and_resolve_message synthetic visible body	src/mindroom/matrix/client_visible_messages.py:155; src/mindroom/matrix/client_visible_messages.py:207
fetch_thread_history	async_function	lines 766-817	duplicate-found	fetch thread history cache hit refresh stale fallback hydrate_sidecars	src/mindroom/matrix/client_thread_history.py:820; src/mindroom/matrix/client_thread_history.py:875; src/mindroom/matrix/client_thread_history.py:930
fetch_thread_snapshot	async_function	lines 820-872	duplicate-found	fetch thread snapshot cache hit refresh hydrate_sidecars false	src/mindroom/matrix/client_thread_history.py:766; src/mindroom/matrix/client_thread_history.py:875; src/mindroom/matrix/client_thread_history.py:930
fetch_dispatch_thread_history	async_function	lines 875-927	duplicate-found	fetch dispatch thread history cache hit refresh allow_stale_fallback false	src/mindroom/matrix/client_thread_history.py:766; src/mindroom/matrix/client_thread_history.py:820; src/mindroom/matrix/client_thread_history.py:930
fetch_dispatch_thread_snapshot	async_function	lines 930-982	duplicate-found	fetch dispatch thread snapshot cache hit refresh allow_stale_fallback false hydrate false	src/mindroom/matrix/client_thread_history.py:766; src/mindroom/matrix/client_thread_history.py:820; src/mindroom/matrix/client_thread_history.py:875
_fetch_thread_history_via_room_messages_with_events	async_function	lines 985-1015	related-only	fetch room messages scan resolve timed ThreadHistoryFetchResult	src/mindroom/matrix/thread_room_scan.py:36
_record_scanned_room_message_source	function	lines 1018-1040	duplicate-found	record latest edit skip edits normalize event source	src/mindroom/matrix/client_thread_history.py:284; src/mindroom/matrix/client_visible_messages.py:464
_resolve_scanned_thread_message_sources	async_function	lines 1043-1078	related-only	resolve thread ids for event infos ordered scanned sources	src/mindroom/matrix/thread_membership.py:371; src/mindroom/matrix/thread_projection.py:239
_fetch_thread_event_sources_via_room_messages	async_function	lines 1081-1155	related-only	room_messages pagination direction back RoomMessagesResponse scan pages	src/mindroom/matrix/stale_stream_cleanup.py:549; src/mindroom/thread_summary.py:224; src/mindroom/bot_room_lifecycle.py:156
get_room_threads_page	async_function	lines 1158-1189	related-only	room_get_threads _send RoomThreadsResponse page_token	custom_tools/matrix_conversation_operations.py:533; custom_tools/matrix_room.py:316; src/mindroom/matrix/conversation_cache.py:682
```

## Findings

1. Visible-message fallback and projection are duplicated across thread history and visible-message helpers.
   `client_thread_history._room_message_fallback_body` at `src/mindroom/matrix/client_thread_history.py:157` is the same behavior as `client_visible_messages._event_fallback_body` at `src/mindroom/matrix/client_visible_messages.py:283`.
   `_snapshot_message_dict` and the non-text branch of `_resolve_thread_history_message` at `src/mindroom/matrix/client_thread_history.py:170` and `src/mindroom/matrix/client_thread_history.py:741` repeat the same synthetic `ResolvedVisibleMessage` construction: normalize source content, derive `EventInfo.thread_id`, call `visible_body_from_event_source`, refresh stream status.
   Differences to preserve: snapshot mode must not hydrate sidecars, while `_resolve_thread_history_message` hydrates encrypted/sidecar content before constructing the synthetic message.

2. Bundled replacement extraction duplicates the candidate traversal already present in visible-message previews.
   `_bundled_replacement_source` at `src/mindroom/matrix/client_thread_history.py:224` manually walks `unsigned -> m.relations -> m.replace` and tries `event`, `latest_event`, then the replacement object.
   `client_visible_messages._bundled_replacement_candidates` at `src/mindroom/matrix/client_visible_messages.py:230` performs the same Matrix relation traversal and string-key normalization for `latest_event`, `event`, and the replacement object.
   Differences to preserve: thread history validates candidates by parsing visible `RoomMessageText`/`RoomMessageNotice` events and currently checks only `unsigned`, while the preview helper also checks top-level relations and does async body resolution.

3. The four public fetch functions are near-identical policy wrappers.
   `fetch_thread_history`, `fetch_thread_snapshot`, `fetch_dispatch_thread_history`, and `fetch_dispatch_thread_snapshot` at `src/mindroom/matrix/client_thread_history.py:766`, `:820`, `:875`, and `:930` all attempt `_load_cached_thread_history_if_usable`, log a cache hit, and otherwise call `refresh_thread_history_from_source`.
   The only behavior parameters are `hydrate_sidecars`, `allow_stale_fallback`, and the warning text for cache-read failures.
   `conversation_cache` adds another layer of repeated wrappers at `src/mindroom/matrix/conversation_cache.py:573`, `:586`, `:606`, and `:626`, but those are thin dependency adapters rather than the core duplication.

4. Raw event-id extraction is duplicated.
   `_event_id_from_source` at `src/mindroom/matrix/client_thread_history.py:218` is identical to `thread_projection._event_id_from_source` at `src/mindroom/matrix/thread_projection.py:206`.
   Similar inline forms appear in thread membership and bookkeeping code.
   This is small but active because thread history already imports other projection helpers from `thread_projection.py`.

5. Thread history resolution duplicates part of `resolve_latest_visible_messages`.
   `_resolve_thread_history_from_event_sources_timed` at `src/mindroom/matrix/client_thread_history.py:256` and `resolve_latest_visible_messages` at `src/mindroom/matrix/client_visible_messages.py:453` both collect latest edits with `_record_latest_thread_edit`, skip edit events, resolve visible messages, and apply edits with `_apply_latest_edits_to_messages`.
   Differences to preserve: thread history accepts raw event sources, supports bundled replacement sources, supports no-hydration snapshots, records input ordering/related-event ordering, and requires a thread id for sorting and edit filtering.

## Proposed Generalization

1. Move the fallback body helper and a small `resolved_visible_message_from_event_source` helper into `client_visible_messages.py` or a focused `visible_message_projection.py`.
2. Reuse a single bundled replacement candidate iterator from `client_visible_messages.py`, with a synchronous validation wrapper in thread history if needed.
3. Replace the four public fetch wrappers with one private `_fetch_thread_history_with_policy(...)` helper parameterized by `hydrate_sidecars`, `allow_stale_fallback`, and cache-read warning text.
4. Export or colocate one raw `event_id_from_source` helper in `thread_projection.py`, then use it from thread history.
5. Defer deeper merging of `_resolve_thread_history_from_event_sources_timed` and `resolve_latest_visible_messages` unless tests are added around bundled replacements, stale cache recovery, snapshot mode, and edit synthesis.

## Risk/tests

The visible-message refactor is moderate risk because it touches encrypted/sidecar hydration, trusted sender metadata, edit application, and stream status refresh.
Tests should cover text, notice, encrypted or non-text message sources, bundled edits, trusted-stream body metadata, and snapshot mode without sidecar hydration.
The fetch-wrapper policy extraction is low risk if kept private and table-driven tests assert the four current public functions still pass the same `hydrate_sidecars` and `allow_stale_fallback` values to `refresh_thread_history_from_source`.
The event-id helper consolidation is very low risk.
No production code was edited for this audit.
