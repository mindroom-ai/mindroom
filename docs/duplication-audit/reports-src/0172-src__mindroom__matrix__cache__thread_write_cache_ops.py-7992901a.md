# Summary

Top duplication candidates for `src/mindroom/matrix/cache/thread_write_cache_ops.py`:

- `append_event_to_cache` mirrors the same append-then-revalidate thread-cache mutation implemented by both SQLite and PostgreSQL cache backends, with the wrapper adding fail-open logging and the backends duplicating the normalized write operation.
- `_fail_closed_thread_invalidation` and `_fail_closed_room_invalidation` are near-identical fail-closed stale-marker recovery flows that differ only by scope, cache method, and log fields.
- `queue_room_cache_update` and `queue_thread_cache_update` are thin pass-throughs over the write coordinator, and adjacent scheduling code in `thread_writes.py` repeats room/thread fail-open queue wrapping with only thread context differences.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ThreadMutationCacheOps	class	lines 20-375	related-only	ThreadMutationCacheOps event_cache_write_coordinator thread cache ops	src/mindroom/matrix/cache/thread_writes.py:124; src/mindroom/matrix/cache/thread_reads.py:41; src/mindroom/matrix/conversation_cache.py:525
ThreadMutationCacheOps.__init__	method	lines 23-30	related-only	logger_getter runtime BotRuntimeView init	src/mindroom/matrix/cache/thread_reads.py:44
ThreadMutationCacheOps.logger	method	lines 33-35	related-only	logger_getter facade-bound logger	src/mindroom/matrix/cache/thread_reads.py:61
ThreadMutationCacheOps.cache_runtime_available	method	lines 37-43	related-only	cache_runtime_available durable_writes_available event_cache_write_coordinator	src/mindroom/bot.py:1095; src/mindroom/matrix/cache/event_cache.py:31; src/mindroom/matrix/cache/sqlite_event_cache.py:410; src/mindroom/matrix/cache/postgres_event_cache.py:683
ThreadMutationCacheOps.cache_runtime_diagnostics	method	lines 45-49	related-only	runtime_diagnostics cache_backend none sync certification	src/mindroom/matrix/cache/event_cache.py:153; src/mindroom/matrix/cache/sqlite_event_cache.py:414; src/mindroom/matrix/cache/postgres_event_cache.py:692; src/mindroom/matrix/sync_certification.py:163
ThreadMutationCacheOps.pending_durable_write_room_ids	method	lines 51-55	related-only	pending_durable_write_room_ids pending invalidation room ids	src/mindroom/matrix/cache/event_cache.py:156; src/mindroom/matrix/cache/sqlite_event_cache.py:422; src/mindroom/matrix/cache/postgres_event_cache.py:696
ThreadMutationCacheOps.queue_pending_durable_write_flushes	method	lines 57-71	related-only	flush_pending_durable_writes queue_room_cache_update pending durable writes	src/mindroom/matrix/cache/sqlite_event_cache.py:426; src/mindroom/matrix/cache/postgres_event_cache.py:700; src/mindroom/matrix/cache/thread_writes.py:1210
ThreadMutationCacheOps.queue_room_cache_update	method	lines 73-92	related-only	queue_room_update queue_room_cache_update room barrier	src/mindroom/matrix/cache/write_coordinator.py:660; src/mindroom/matrix/conversation_cache.py:540; src/mindroom/matrix/cache/thread_writes.py:572; src/mindroom/matrix/cache/thread_writes.py:1125
ThreadMutationCacheOps.queue_thread_cache_update	method	lines 94-115	related-only	queue_thread_update queue_thread_cache_update thread barrier	src/mindroom/matrix/cache/write_coordinator.py:699; src/mindroom/matrix/cache/thread_reads.py:117; src/mindroom/matrix/cache/thread_writes.py:630; src/mindroom/matrix/cache/thread_writes.py:738
ThreadMutationCacheOps.store_events_batch	async_method	lines 117-138	related-only	store_events_batch failure_message raise_on_failure	src/mindroom/matrix/cache/event_cache.py:91; src/mindroom/matrix/cache/sqlite_event_cache.py:602; src/mindroom/matrix/cache/postgres_event_cache.py:979; src/mindroom/matrix/cache/thread_writes.py:1044
ThreadMutationCacheOps.redact_cached_event	async_method	lines 140-162	related-only	redact_event redacted_event_id failure_message	src/mindroom/matrix/cache/event_cache.py:143; src/mindroom/matrix/cache/sqlite_event_cache.py:796; src/mindroom/matrix/cache/postgres_event_cache.py:1205; src/mindroom/matrix/cache/thread_writes.py:226
ThreadMutationCacheOps.invalidate_after_redaction	async_method	lines 164-190	related-only	invalidate_after_redaction MutationThreadImpactState redaction lookup unavailable	src/mindroom/matrix/cache/thread_writes.py:203; src/mindroom/matrix/thread_bookkeeping.py
ThreadMutationCacheOps.invalidate_known_thread	async_method	lines 192-218	related-only	mark_thread_stale fail_closed_thread_invalidation stale marker	src/mindroom/matrix/cache/event_cache.py:124; src/mindroom/matrix/cache/sqlite_event_cache.py:720; src/mindroom/matrix/cache/postgres_event_cache.py:1103; src/mindroom/matrix/cache/thread_writes.py:157
ThreadMutationCacheOps.invalidate_room_threads	async_method	lines 220-243	related-only	mark_room_threads_stale fail_closed_room_invalidation room stale marker	src/mindroom/matrix/cache/event_cache.py:127; src/mindroom/matrix/cache/sqlite_event_cache.py:734; src/mindroom/matrix/cache/postgres_event_cache.py:1129; src/mindroom/matrix/cache/thread_writes.py:149
ThreadMutationCacheOps.append_event_to_cache	async_method	lines 245-295	duplicate-found	append_event revalidate_thread_after_incremental_update append_failed incremental update	src/mindroom/matrix/cache/sqlite_event_cache.py:747; src/mindroom/matrix/cache/sqlite_event_cache.py:764; src/mindroom/matrix/cache/postgres_event_cache.py:1153; src/mindroom/matrix/cache/postgres_event_cache.py:1171; src/mindroom/matrix/cache/thread_writes.py:163; src/mindroom/matrix/cache/thread_writes.py:769
ThreadMutationCacheOps._disable_cache_after_fail_closed_invalidation	method	lines 297-304	related-only	disable stale_marker_failed scope reason	src/mindroom/matrix/cache/sqlite_event_cache.py:434; src/mindroom/matrix/cache/postgres_event_cache.py:713
ThreadMutationCacheOps._fail_closed_thread_invalidation	async_method	lines 306-341	duplicate-found	fail_closed_thread_invalidation invalidate_thread backend unavailable stale marker	src/mindroom/matrix/cache/thread_write_cache_ops.py:343; src/mindroom/matrix/cache/event_cache.py:118; src/mindroom/matrix/cache/sqlite_event_cache.py:695; src/mindroom/matrix/cache/postgres_event_cache.py:1076
ThreadMutationCacheOps._fail_closed_room_invalidation	async_method	lines 343-375	duplicate-found	fail_closed_room_invalidation invalidate_room_threads backend unavailable stale marker	src/mindroom/matrix/cache/thread_write_cache_ops.py:306; src/mindroom/matrix/cache/event_cache.py:121; src/mindroom/matrix/cache/sqlite_event_cache.py:708; src/mindroom/matrix/cache/postgres_event_cache.py:1090
```

# Findings

## 1. Backend append and revalidation logic is duplicated across cache implementations

`ThreadMutationCacheOps.append_event_to_cache` at `src/mindroom/matrix/cache/thread_write_cache_ops.py:245` performs the high-level mutation sequence: append an event to an existing cached thread, log if the raw thread cache is missing, then revalidate the thread after the incremental update.
The concrete backend operations are implemented twice with the same behavior shape:

- `src/mindroom/matrix/cache/sqlite_event_cache.py:747` normalizes an event and calls `sqlite_event_cache_threads.append_existing_thread_event`, while `src/mindroom/matrix/cache/sqlite_event_cache.py:764` wraps `revalidate_thread_after_incremental_update_locked`.
- `src/mindroom/matrix/cache/postgres_event_cache.py:1153` normalizes an event and calls `postgres_event_cache_threads.append_existing_thread_event`, while `src/mindroom/matrix/cache/postgres_event_cache.py:1171` wraps `revalidate_thread_after_incremental_update_locked`.

The behavior is functionally the same: both backends normalize the event, return `False` when there is no cached thread to append into, and expose a separate revalidation write for a successful append.
Differences to preserve are backend-specific connection types, namespace arguments for PostgreSQL, and PostgreSQL transient-failure handling inside `_write_operation`.

## 2. Thread and room fail-closed invalidation handlers are near duplicates

`_fail_closed_thread_invalidation` at `src/mindroom/matrix/cache/thread_write_cache_ops.py:306` and `_fail_closed_room_invalidation` at `src/mindroom/matrix/cache/thread_write_cache_ops.py:343` share the same control flow:

- Try an eager destructive invalidation after a stale-marker write fails.
- If the stale-marker failure was `EventCacheBackendUnavailableError`, log that the marker is pending and keep the cache enabled.
- For other invalidation failures, log and disable the cache using `stale_marker_failed:<scope>:<room_id>:<reason>`.

The only meaningful differences are the invalidation method (`invalidate_thread` versus `invalidate_room_threads`), the thread ID log field, scope formatting, and message text.
This is true duplication inside the primary file.

## 3. Room and thread queueing wrappers have duplicated shape but limited refactor value

`queue_room_cache_update` at `src/mindroom/matrix/cache/thread_write_cache_ops.py:73` and `queue_thread_cache_update` at `src/mindroom/matrix/cache/thread_write_cache_ops.py:94` are parallel wrappers over `EventCacheWriteCoordinator.queue_room_update` and `queue_thread_update`.
Adjacent scheduling helpers in `src/mindroom/matrix/cache/thread_writes.py:595` and the preceding room helper around `src/mindroom/matrix/cache/thread_writes.py:530` repeat fail-open scheduling with identical cancellation/error logging, differing mostly by whether `thread_id` is included and whether the room or thread coordinator wrapper is called.

This duplication is real but low impact.
Keeping explicit room and thread methods makes the barrier choice clear at call sites.

# Proposed Generalization

1. No broad refactor recommended for `ThreadMutationCacheOps` as part of this audit.
2. If touching the fail-closed code, extract a private `_fail_closed_invalidation(...)` helper in `thread_write_cache_ops.py` that accepts the invalidate coroutine factory, scope string, log context, and two log messages.
3. If touching backend append behavior, consider adding an event-cache protocol method such as `append_event_and_revalidate(room_id, thread_id, event) -> bool` implemented by both backends, so `append_event_to_cache` owns only fail-open logging and no longer coordinates two backend writes.
4. Leave `queue_room_cache_update` and `queue_thread_cache_update` separate unless another caller repeats the same coordinator pass-through shape with richer behavior.

# Risk/tests

- A fail-closed helper would need focused unit coverage for three branches: successful destructive invalidation, backend-unavailable stale marker with no cache disable, and destructive invalidation failure that disables the cache.
- A backend-level `append_event_and_revalidate` helper would need SQLite and PostgreSQL cache tests covering missing-thread append, successful append, and revalidation failure behavior.
- The PostgreSQL backend records pending stale markers on backend-unavailable errors; any generalization must preserve that behavior and must not swallow `EventCacheBackendUnavailableError` before the runtime records pending invalidations.
