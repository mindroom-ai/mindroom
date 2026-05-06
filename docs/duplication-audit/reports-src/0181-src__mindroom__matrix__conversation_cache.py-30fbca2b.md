## Summary

Top duplication candidates:

1. `MatrixConversationCache._fetch_*_from_client` and `_refresh_dispatch_thread_snapshot_for_startup_prewarm` repeat the same Matrix thread-fetch adapter shape with only the underlying fetch function and fixed labels varying.
2. `MatrixConversationCache.get_thread_messages` repeats the thread read mode selection already implemented by `ThreadReadPolicy.read_thread`.
3. Cached event/edit projection is related to visible-message edit application elsewhere, but the return types and semantics differ enough that no immediate refactor is recommended.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_TurnEventLookup	class	lines 61-66	none-found	_TurnEventLookup turn event lookup memoized fetched_event_source lookup_fill_persisted	none
resolve_thread_root_event_id_for_client	async_function	lines 84-124	related-only	resolve_thread_root_event_id_for_client resolve_event_thread_id lookup_thread_id_from_conversation_cache fetch_event_info_for_client	src/mindroom/matrix/thread_membership.py:485; src/mindroom/matrix/thread_membership.py:530; src/mindroom/matrix/thread_room_scan.py:66
ConversationCacheProtocol	class	lines 127-225	not-a-behavior-symbol	ConversationCacheProtocol get_thread_messages notify_outbound_message protocol	none
ConversationCacheProtocol.turn_scope	method	lines 130-131	not-a-behavior-symbol	protocol turn_scope AbstractAsyncContextManager	none
ConversationCacheProtocol.get_event	async_method	lines 133-134	not-a-behavior-symbol	protocol get_event EventLookupResult	none
ConversationCacheProtocol.get_thread_messages	async_method	lines 136-145	not-a-behavior-symbol	protocol get_thread_messages full_history dispatch_safe	none
ConversationCacheProtocol.get_thread_snapshot	async_method	lines 147-154	not-a-behavior-symbol	protocol get_thread_snapshot	none
ConversationCacheProtocol.get_thread_history	async_method	lines 156-163	not-a-behavior-symbol	protocol get_thread_history	none
ConversationCacheProtocol.get_dispatch_thread_snapshot	async_method	lines 165-172	not-a-behavior-symbol	protocol get_dispatch_thread_snapshot	none
ConversationCacheProtocol.get_dispatch_thread_history	async_method	lines 174-181	not-a-behavior-symbol	protocol get_dispatch_thread_history	none
ConversationCacheProtocol.get_thread_id_for_event	async_method	lines 183-184	not-a-behavior-symbol	protocol get_thread_id_for_event	none
ConversationCacheProtocol.get_latest_thread_event_id_if_needed	async_method	lines 186-195	not-a-behavior-symbol	protocol get_latest_thread_event_id_if_needed	none
ConversationCacheProtocol.notify_outbound_message	method	lines 197-207	not-a-behavior-symbol	protocol notify_outbound_message	none
ConversationCacheProtocol.notify_outbound_event	method	lines 209-210	not-a-behavior-symbol	protocol notify_outbound_event	none
ConversationCacheProtocol.notify_outbound_redaction	method	lines 212-216	not-a-behavior-symbol	protocol notify_outbound_redaction	none
ConversationCacheProtocol.append_live_event	async_method	lines 218-225	not-a-behavior-symbol	protocol append_live_event	none
_apply_cached_latest_edit	async_function	lines 228-274	related-only	get_latest_edit extract_edit_body apply latest edit content origin_server_ts	src/mindroom/matrix/client_visible_messages.py:401; src/mindroom/matrix/stale_stream_cleanup.py:978
_cached_room_get_event_response	async_function	lines 277-294	related-only	RoomGetEventResponse.from_dict cached event latest edit	src/mindroom/matrix/client_visible_messages.py:401; src/mindroom/matrix/stale_stream_cleanup.py:963
_cached_room_get_event	async_function	lines 297-351	related-only	room_get_event normalize_nio_event_for_cache event_cache.get_event	src/mindroom/approval_transport.py:343; src/mindroom/custom_tools/matrix_api.py:1267; src/mindroom/matrix/stale_stream_cleanup.py:963; src/mindroom/matrix/thread_membership.py:530
MatrixConversationCache	class	lines 355-971	related-only	MatrixConversationCache facade ThreadReadPolicy ThreadOutboundWritePolicy ThreadLiveWritePolicy ThreadSyncWritePolicy	src/mindroom/matrix/cache/thread_reads.py:41; src/mindroom/matrix/cache/thread_writes.py:438; src/mindroom/matrix/cache/thread_writes.py:1109
MatrixConversationCache.__post_init__	method	lines 372-403	none-found	ThreadReadPolicy ThreadMutationResolver ThreadOutboundWritePolicy ThreadLiveWritePolicy ThreadSyncWritePolicy	none
MatrixConversationCache._require_client	method	lines 405-410	related-only	runtime.client is None Matrix client is not ready	src/mindroom/bot.py:644; src/mindroom/bot.py:659
MatrixConversationCache._trusted_sender_ids	method	lines 412-417	none-found	active_internal_sender_ids runtime.config runtime_paths	none
MatrixConversationCache.turn_scope	async_method	lines 420-434	none-found	ContextVar asynccontextmanager turn_scope memoize event thread reads	none
MatrixConversationCache._copy_thread_read_result	method	lines 437-443	related-only	thread_history_result list(result) diagnostics is_full_history	src/mindroom/matrix/cache/thread_reads.py:79; src/mindroom/matrix/client_thread_history.py:76
MatrixConversationCache._read_thread_memoized	async_method	lines 445-470	none-found	_turn_thread_read_cache ThreadReadCacheKey read_thread memoized	none
MatrixConversationCache.get_event	async_method	lines 472-523	none-found	_turn_event_cache _cached_room_get_event _persist_lookup_fill lookup_fill_persisted	none
MatrixConversationCache._persist_lookup_fill	async_method	lines 525-553	related-only	queue_room_update store_event matrix_cache_store_room_get_event	src/mindroom/approval_transport.py:346; src/mindroom/matrix/cache/thread_write_cache_ops.py:85; src/mindroom/matrix/cache/thread_writes.py:1136
MatrixConversationCache._persist_lookup_fill.<locals>.persist_lookup_event	nested_async_function	lines 535-536	related-only	event_cache.store_event fetched_event_source	src/mindroom/approval_transport.py:346
MatrixConversationCache._event_info_for_thread_resolution	async_method	lines 555-564	related-only	get_event persist_lookup_fill False EventInfo.from_event	src/mindroom/matrix/thread_membership.py:530; src/mindroom/matrix/thread_bookkeeping.py:301
MatrixConversationCache._fetch_thread_history_from_client	async_method	lines 566-584	duplicate-found	fetch_thread_history cache_write_guard_started_at trusted_sender_ids coordinator_queue_wait_ms	src/mindroom/matrix/conversation_cache.py:586; src/mindroom/matrix/conversation_cache.py:606; src/mindroom/matrix/conversation_cache.py:626; src/mindroom/matrix/conversation_cache.py:646
MatrixConversationCache._fetch_thread_snapshot_from_client	async_method	lines 586-604	duplicate-found	fetch_thread_snapshot cache_write_guard_started_at trusted_sender_ids coordinator_queue_wait_ms	src/mindroom/matrix/conversation_cache.py:566; src/mindroom/matrix/conversation_cache.py:606; src/mindroom/matrix/conversation_cache.py:626; src/mindroom/matrix/conversation_cache.py:646
MatrixConversationCache._fetch_dispatch_thread_history_from_client	async_method	lines 606-624	duplicate-found	fetch_dispatch_thread_history cache_write_guard_started_at trusted_sender_ids coordinator_queue_wait_ms	src/mindroom/matrix/conversation_cache.py:566; src/mindroom/matrix/conversation_cache.py:586; src/mindroom/matrix/conversation_cache.py:626; src/mindroom/matrix/conversation_cache.py:646
MatrixConversationCache._fetch_dispatch_thread_snapshot_from_client	async_method	lines 626-644	duplicate-found	fetch_dispatch_thread_snapshot cache_write_guard_started_at trusted_sender_ids coordinator_queue_wait_ms	src/mindroom/matrix/conversation_cache.py:566; src/mindroom/matrix/conversation_cache.py:586; src/mindroom/matrix/conversation_cache.py:606; src/mindroom/matrix/conversation_cache.py:646
MatrixConversationCache._refresh_dispatch_thread_snapshot_for_startup_prewarm	async_method	lines 646-663	duplicate-found	fetch_dispatch_thread_snapshot startup_thread_prewarm coordinator_queue_wait_ms 0.0	src/mindroom/matrix/conversation_cache.py:626
MatrixConversationCache._startup_thread_prewarm_ids	async_method	lines 665-705	none-found	get_recent_room_thread_ids get_room_threads_page startup prewarm top-up	none
MatrixConversationCache.prewarm_recent_room_threads	async_method	lines 707-753	none-found	prewarm_recent_room_threads pending_thread_ids worker_count threads_warmed threads_failed	none
MatrixConversationCache.prewarm_recent_room_threads.<locals>.worker	nested_async_function	lines 724-741	none-found	pending_thread_ids pop startup prewarm worker outcome aborted failed warmed	none
MatrixConversationCache._prewarm_one_startup_thread	async_method	lines 755-792	none-found	_prewarm_one_startup_thread startup_thread_prewarm_thread_failed aborted warmed failed	none
MatrixConversationCache.get_thread_snapshot	async_method	lines 794-808	related-only	get_thread_snapshot _read_thread_memoized full_history False dispatch_safe False	src/mindroom/matrix/cache/thread_reads.py:170
MatrixConversationCache.get_thread_history	async_method	lines 810-824	related-only	get_thread_history _read_thread_memoized full_history True dispatch_safe False	src/mindroom/matrix/cache/thread_reads.py:150
MatrixConversationCache.get_thread_messages	async_method	lines 826-854	duplicate-found	get_thread_messages full_history dispatch_safe mode selection read_thread	src/mindroom/matrix/cache/thread_reads.py:128
MatrixConversationCache.get_dispatch_thread_snapshot	async_method	lines 856-870	related-only	get_dispatch_thread_snapshot _read_thread_memoized full_history False dispatch_safe True	src/mindroom/matrix/cache/thread_reads.py:160
MatrixConversationCache.get_dispatch_thread_history	async_method	lines 872-886	related-only	get_dispatch_thread_history _read_thread_memoized full_history True dispatch_safe True	src/mindroom/matrix/cache/thread_reads.py:140
MatrixConversationCache.get_thread_id_for_event	async_method	lines 888-899	related-only	get_thread_id_for_event cache exception fallback None	src/mindroom/matrix/thread_bookkeeping.py:301; src/mindroom/matrix/thread_membership.py:485; src/mindroom/matrix/thread_room_scan.py:66
MatrixConversationCache.get_latest_thread_event_id_if_needed	async_method	lines 901-917	none-found	get_latest_thread_event_id_if_needed _reads delegate	none
MatrixConversationCache.notify_outbound_message	method	lines 919-926	none-found	notify_outbound_message _outbound delegate	none
MatrixConversationCache.notify_outbound_event	method	lines 928-934	none-found	notify_outbound_event _outbound delegate	none
MatrixConversationCache.notify_outbound_redaction	method	lines 936-938	none-found	notify_outbound_redaction _outbound delegate	none
MatrixConversationCache.append_live_event	async_method	lines 940-948	none-found	append_live_event _live delegate	none
MatrixConversationCache.apply_redaction	async_method	lines 950-952	none-found	apply_redaction _live delegate	none
MatrixConversationCache.cache_sync_timeline	method	lines 954-964	none-found	cache_sync_timeline _sync delegate raise_on_cache_write_failure	none
MatrixConversationCache.cache_sync_timeline_for_certification	async_method	lines 966-971	none-found	cache_sync_timeline_for_certification _sync delegate SyncCacheWriteResult	none
```

## Findings

### 1. Thread fetch adapter wrappers repeat the same behavior

`MatrixConversationCache._fetch_thread_history_from_client` (`src/mindroom/matrix/conversation_cache.py:566`), `_fetch_thread_snapshot_from_client` (`src/mindroom/matrix/conversation_cache.py:586`), `_fetch_dispatch_thread_history_from_client` (`src/mindroom/matrix/conversation_cache.py:606`), `_fetch_dispatch_thread_snapshot_from_client` (`src/mindroom/matrix/conversation_cache.py:626`), and `_refresh_dispatch_thread_snapshot_for_startup_prewarm` (`src/mindroom/matrix/conversation_cache.py:646`) all:

- record `fetch_started_at = time.time()`;
- require the current Matrix client;
- pass `room_id`, `thread_id`, `self.runtime.event_cache`, `cache_write_guard_started_at`, `self._trusted_sender_ids()`, and `caller_label` into a `client_thread_history` fetch function;
- pass a coordinator wait value, either from the caller or the fixed startup prewarm value `0.0`.

The behavior is functionally the same adapter from facade state into a thread-history fetcher.
The differences to preserve are the fetch function selected and the startup-prewarm fixed `caller_label`/`coordinator_queue_wait_ms`.

### 2. Thread read mode routing is implemented in two layers

`MatrixConversationCache.get_thread_messages` (`src/mindroom/matrix/conversation_cache.py:826`) selects among snapshot/history and dispatch/non-dispatch branches from `full_history` and `dispatch_safe`.
`ThreadReadPolicy.read_thread` (`src/mindroom/matrix/cache/thread_reads.py:128`) independently performs the same four-way selection to choose the underlying fetcher and cache-barrier name.

The facade version routes to named convenience methods, while `ThreadReadPolicy` routes to fetchers.
The behavior is duplicated at the mode-selection level, and the two branches must stay aligned for all combinations of `full_history` and `dispatch_safe`.

### 3. Cached latest-edit projection is related but not a direct duplicate

`_apply_cached_latest_edit` (`src/mindroom/matrix/conversation_cache.py:228`) applies the latest cached edit to a raw event source before reconstructing a `RoomGetEventResponse`.
`_apply_latest_edits_to_messages` (`src/mindroom/matrix/client_visible_messages.py:401`) applies edit bodies to `ResolvedVisibleMessage` objects, and `_fetch_message_data_for_event_id` (`src/mindroom/matrix/stale_stream_cleanup.py:951`) resolves an edit body after `room_get_event`.

All three share `extract_edit_body` and latest-edit semantics, but they operate on different shapes: raw event dicts, visible message records, and requester-resolution messages.
This is related behavior rather than a strong duplication candidate.

## Proposed Generalization

1. Add a private helper in `src/mindroom/matrix/conversation_cache.py`, for example `_fetch_thread_from_client(fetcher, room_id, thread_id, caller_label, coordinator_queue_wait_ms)`, that contains the common `time.time()`, `self._require_client()`, `event_cache`, and `trusted_sender_ids` argument wiring.
2. Keep the existing public/private wrapper methods as thin named adapters because `ThreadReadPolicy` currently receives those callbacks explicitly.
3. For startup prewarm, call the same helper with `fetch_dispatch_thread_snapshot`, `caller_label="startup_thread_prewarm"`, and `coordinator_queue_wait_ms=0.0`.
4. Simplify `get_thread_messages` to call `_read_thread_memoized(...)` directly with the caller's `full_history` and `dispatch_safe` flags, leaving the canonical four-way fetch selection in `ThreadReadPolicy.read_thread`.

No broad architecture refactor is recommended.

## Risk/tests

The wrapper helper change is low risk if the fetcher signatures remain uniform.
Tests should cover all four combinations of `full_history` and `dispatch_safe` through `get_thread_messages`, plus startup prewarm preserving `caller_label="startup_thread_prewarm"` and `coordinator_queue_wait_ms=0.0`.

No production code was edited.
