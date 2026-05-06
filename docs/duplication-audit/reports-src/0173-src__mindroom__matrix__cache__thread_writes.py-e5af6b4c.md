## Summary

Top duplication candidate: event lookup-batch normalization and room grouping is repeated in both durable cache backends, while `ThreadSyncWritePolicy` and outbound reaction bookkeeping build batches in the same shape before delegating to `ThreadMutationCacheOps.store_events_batch`.
No other meaningful cross-file duplication was found for this primary file; most matches are facade delegates in `conversation_cache.py`, shared primitives already extracted into `thread_bookkeeping.py` / `thread_write_cache_ops.py`, or deliberately distinct live/outbound/sync policy paths.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_collect_sync_timeline_cache_updates	function	lines 37-65	related-only	collect_sync_timeline_cache_updates sync timeline redaction threaded plain events	src/mindroom/matrix/conversation_cache.py:954; src/mindroom/bot.py:997
_collect_sync_event_cache_update	function	lines 68-75	related-only	normalize_nio_event_for_cache event_id sync cache update	src/mindroom/matrix/conversation_cache.py:340; src/mindroom/matrix/client_thread_history.py:215
_threaded_sync_event_cache_update	function	lines 78-89	related-only	is_thread_affecting_relation EventInfo normalize_nio_event_for_cache	src/mindroom/matrix/thread_bookkeeping.py:254; src/mindroom/matrix/conversation_cache.py:340
_outbound_streaming_edit_coalesce_context	function	lines 92-114	none-found	outbound streaming edit coalesce STREAM_STATUS_PENDING STREAM_STATUS_STREAMING	none
_mutation_reason	function	lines 117-121	none-found	context suffix mutation reason live_thread_mutation sync_redaction	none
_apply_thread_message_mutation	async_function	lines 124-177	related-only	resolve_thread_impact_for_mutation invalidate_known_thread append_event_to_cache invalidate_room_threads	src/mindroom/matrix/cache/thread_write_cache_ops.py:164; src/mindroom/matrix/thread_bookkeeping.py:254
_resolve_thread_redaction_mutation_impact	async_function	lines 180-200	related-only	resolve_redaction_thread_impact failure_message outbound live sync	src/mindroom/matrix/thread_bookkeeping.py:134; src/mindroom/matrix/thread_bookkeeping.py:214; src/mindroom/custom_tools/matrix_api.py:709
_apply_thread_redaction_mutation	async_function	lines 203-244	related-only	redact_cached_event invalidate_after_redaction MutationThreadImpactState	src/mindroom/matrix/cache/thread_write_cache_ops.py:139; src/mindroom/matrix/cache/thread_write_cache_ops.py:164
ThreadOutboundWritePolicy	class	lines 247-654	related-only	notify_outbound_event notify_outbound_message notify_outbound_redaction	src/mindroom/matrix/conversation_cache.py:932; src/mindroom/matrix/conversation_cache.py:939; src/mindroom/matrix/conversation_cache.py:945
ThreadOutboundWritePolicy.__init__	method	lines 250-259	not-a-behavior-symbol	resolver cache_ops require_client constructor	none
ThreadOutboundWritePolicy._emit_outbound_schedule_timing	method	lines 261-283	none-found	Event cache outbound schedule timing matrix_cache_notify_outbound_event	none
ThreadOutboundWritePolicy._apply_outbound_event_notification	async_method	lines 285-308	related-only	outbound resolve_thread_impact_for_mutation apply_thread_message_mutation	src/mindroom/matrix/thread_bookkeeping.py:254
ThreadOutboundWritePolicy.notify_outbound_event	method	lines 310-436	related-only	notify_outbound_event normalize outbound reaction coalesce queue room thread update	src/mindroom/matrix/conversation_cache.py:939
ThreadOutboundWritePolicy.notify_outbound_message	method	lines 438-458	related-only	notify_outbound_message m.room.message content event_id	src/mindroom/matrix/conversation_cache.py:932; src/mindroom/custom_tools/matrix_api.py:682
ThreadOutboundWritePolicy._normalize_outbound_event_source	method	lines 460-482	related-only	normalize_event_source_for_cache sender origin_server_ts outbound	src/mindroom/matrix/cache/event_normalization.py:15; src/mindroom/matrix/cache/sqlite_event_cache.py:610; src/mindroom/matrix/cache/postgres_event_cache.py:987
ThreadOutboundWritePolicy._apply_outbound_redaction_notification	async_method	lines 484-502	related-only	outbound redaction resolve impact apply mutation	src/mindroom/matrix/thread_bookkeeping.py:214; src/mindroom/custom_tools/matrix_api.py:709
ThreadOutboundWritePolicy.notify_outbound_redaction	method	lines 504-538	related-only	notify_outbound_redaction schedule fail open room redaction	src/mindroom/matrix/conversation_cache.py:945
ThreadOutboundWritePolicy._schedule_fail_open_room_update	method	lines 540-593	related-only	queue_room_cache_update cancelled_message failure_message warning	src/mindroom/matrix/cache/thread_write_cache_ops.py:73; src/mindroom/matrix/cache/thread_writes.py:595
ThreadOutboundWritePolicy._schedule_fail_open_room_update.<locals>.safe_update	nested_async_function	lines 553-569	related-only	safe_update CancelledError warning failure_message room_id	src/mindroom/matrix/cache/thread_writes.py:609
ThreadOutboundWritePolicy._schedule_fail_open_thread_update	method	lines 595-654	related-only	queue_thread_cache_update cancelled_message failure_message warning	src/mindroom/matrix/cache/thread_write_cache_ops.py:94; src/mindroom/matrix/cache/thread_writes.py:540
ThreadOutboundWritePolicy._schedule_fail_open_thread_update.<locals>.safe_update	nested_async_function	lines 609-627	related-only	safe_update CancelledError warning failure_message thread_id	src/mindroom/matrix/cache/thread_writes.py:553
ThreadLiveWritePolicy	class	lines 657-943	related-only	append_live_event apply_redaction live cache policy	src/mindroom/matrix/conversation_cache.py:948; src/mindroom/matrix/conversation_cache.py:956
ThreadLiveWritePolicy.__init__	method	lines 660-667	not-a-behavior-symbol	resolver cache_ops constructor	none
ThreadLiveWritePolicy._resolve_live_event_impact	async_method	lines 669-681	related-only	live resolve_thread_impact_for_mutation event_info event_id	src/mindroom/matrix/thread_bookkeeping.py:254
ThreadLiveWritePolicy._append_live_event_without_timing	async_method	lines 683-743	related-only	live append without timing invalidate_room_threads append_event_to_cache	src/mindroom/matrix/cache/thread_writes.py:820; src/mindroom/matrix/cache/thread_write_cache_ops.py:204
ThreadLiveWritePolicy._append_live_event_without_timing.<locals>.append_and_invalidate	nested_async_function	lines 725-736	related-only	append_and_invalidate apply_thread_message_mutation queue_thread_cache_update	src/mindroom/matrix/cache/thread_writes.py:760
ThreadLiveWritePolicy._append_live_threaded_event_with_timing	async_method	lines 745-818	related-only	live threaded timing invalidate append append_failed metrics	src/mindroom/matrix/cache/thread_writes.py:683; src/mindroom/timing.py:1
ThreadLiveWritePolicy._append_live_threaded_event_with_timing.<locals>.append_and_invalidate	nested_async_function	lines 760-788	related-only	append_metrics invalidate_known_thread append_event_to_cache live_append_failed	src/mindroom/matrix/cache/thread_writes.py:725; src/mindroom/matrix/cache/thread_write_cache_ops.py:204
ThreadLiveWritePolicy._append_live_event_with_timing	async_method	lines 820-881	related-only	live event timing impact_resolution room_level unknown threaded	src/mindroom/matrix/cache/thread_writes.py:683; src/mindroom/timing.py:1
ThreadLiveWritePolicy.append_live_event	async_method	lines 883-906	related-only	cache_runtime_available timing_enabled append_live_event	src/mindroom/matrix/conversation_cache.py:948
ThreadLiveWritePolicy.apply_redaction	async_method	lines 908-943	related-only	apply_redaction resolve redaction queue thread room cache update	src/mindroom/matrix/conversation_cache.py:956; src/mindroom/custom_tools/matrix_api.py:709
ThreadLiveWritePolicy.apply_redaction.<locals>.redact_and_invalidate	nested_async_function	lines 922-929	related-only	redact_and_invalidate apply_thread_redaction_mutation	src/mindroom/matrix/cache/thread_write_cache_ops.py:164
ThreadSyncWritePolicy	class	lines 946-1238	related-only	cache_sync_timeline certification sync write policy	src/mindroom/matrix/conversation_cache.py:954; src/mindroom/bot.py:997
ThreadSyncWritePolicy.__init__	method	lines 949-956	not-a-behavior-symbol	resolver cache_ops constructor	none
ThreadSyncWritePolicy._persist_threaded_sync_events	async_method	lines 958-992	related-only	sync threaded events resolve impact apply message mutation	src/mindroom/matrix/thread_bookkeeping.py:254; src/mindroom/matrix/cache/thread_writes.py:285
ThreadSyncWritePolicy._apply_sync_redactions	async_method	lines 994-1022	related-only	sync redactions resolve redaction impact apply redaction mutation	src/mindroom/matrix/thread_bookkeeping.py:214; src/mindroom/matrix/cache/thread_writes.py:484
ThreadSyncWritePolicy._persist_room_sync_timeline_updates	async_method	lines 1024-1079	duplicate-found	store_events_batch plain_batch threaded_batch normalize group by room	src/mindroom/matrix/cache/sqlite_event_cache.py:602; src/mindroom/matrix/cache/postgres_event_cache.py:979; src/mindroom/matrix/cache/thread_write_cache_ops.py:117
ThreadSyncWritePolicy._group_sync_timeline_updates	method	lines 1081-1107	related-only	response.rooms.join timeline.events group room_plain_events room_threaded_events	src/mindroom/bot.py:995; src/mindroom/matrix/conversation_cache.py:954
ThreadSyncWritePolicy.cache_sync_timeline	method	lines 1109-1139	related-only	queue sync timeline room cache update lambda plain threaded redactions	src/mindroom/matrix/conversation_cache.py:954
ThreadSyncWritePolicy._limited_sync_timeline_room_ids	method	lines 1142-1167	none-found	limited sync timeline room ids validation joined rooms timeline.limited	none
ThreadSyncWritePolicy._cache_task_errors	method	lines 1170-1184	related-only	asyncio.gather return_exceptions CancelledError KeyboardInterrupt SystemExit	src/mindroom/bot.py:1299; src/mindroom/bot.py:1311
ThreadSyncWritePolicy.cache_sync_timeline_for_certification	async_method	lines 1186-1238	related-only	SyncCacheWriteResult limited_room_ids pending_durable_write_room_ids cache certification	src/mindroom/bot.py:995; src/mindroom/matrix/sync_certification.py:124
```

## Findings

### 1. Event lookup-batch normalization and room grouping is repeated in both durable cache backends

- Primary path: `src/mindroom/matrix/cache/thread_writes.py:1024` builds `plain_batch` and `threaded_batch` as `(event_id, room_id, event_source)` tuples, then delegates persistence through `ThreadMutationCacheOps.store_events_batch`.
- Shared wrapper: `src/mindroom/matrix/cache/thread_write_cache_ops.py:117` provides fail-open logging around backend `store_events_batch`.
- Duplicate backend behavior: `src/mindroom/matrix/cache/sqlite_event_cache.py:602` and `src/mindroom/matrix/cache/postgres_event_cache.py:979` both skip disabled/empty input, compute `cached_at = time.time()`, normalize each event with `normalize_event_source_for_cache(event_data, event_id=event_id)`, group by `room_id`, and call the backend-specific persist operation per room.

Why this is duplicated: the backend methods perform the same event-shape validation-adjacent transform and room grouping before diverging only at the persistence function and namespace argument.

Differences to preserve: SQLite calls `sqlite_event_cache_events.persist_lookup_events` without namespace; Postgres calls `postgres_event_cache_events.persist_lookup_events` with `namespace=self._runtime.namespace`.
The write-operation wrappers are backend-specific and should remain at the IO edge.

## Proposed Generalization

Extract a small pure helper in a cache-focused module such as `src/mindroom/matrix/cache/event_batching.py`:

1. Accept `list[tuple[str, str, dict[str, Any]]]`.
2. Return `dict[str, list[tuple[str, dict[str, Any]]]]`.
3. Normalize through `normalize_event_source_for_cache(event_data, event_id=event_id)`.
4. Keep `cached_at` and backend write operations in each backend method.

No refactor is recommended for the thread write policies themselves in this report.
The room/thread fail-open schedulers are similar, but their room-vs-thread queue signatures and log context differ enough that merging them would add parameter complexity for only two local call sites.
The live timed/untimed append paths duplicate some mutation ordering, but they intentionally trade clarity for timing instrumentation and already share `_apply_thread_message_mutation` where practical.

## Risk/tests

Behavior risk for the proposed backend helper is low if it is pure and covered with unit tests for mixed-room batches, empty batches, and event normalization with explicit `event_id`.
Regression tests should cover both SQLite and Postgres `store_events_batch` paths or the extracted helper plus one integration-style backend test per implementation.
No production code was edited.
