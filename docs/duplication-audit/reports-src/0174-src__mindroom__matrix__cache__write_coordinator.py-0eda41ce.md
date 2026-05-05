## Summary

No meaningful duplication found.
The closest related code is `CoalescingGate`, which also owns per-key queued work and wake/drain task lifecycle, and `KnowledgeRefreshScheduler`, which coalesces pending refresh requests behind an active task.
Neither duplicates the primary module's room-wide versus same-thread Matrix cache write ordering, cancelled room fence handling, idle waiters, or fallback task maps.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_QueuedRoomFence	class	lines 25-28	none-found	cancelled room fence queued barrier	none
_QueuedUpdate	class	lines 32-41	related-only	queued update task start_signal coalesce_key	src/mindroom/coalescing.py:72; src/mindroom/coalescing.py:82; src/mindroom/knowledge/refresh_scheduler.py:151
_QueuedUpdateState	class	lines 48-51	related-only	coalesced_update_count coalesce_log_context update_coro_factory	src/mindroom/knowledge/refresh_scheduler.py:151; src/mindroom/coalescing.py:450
_RoomSchedulerState	class	lines 55-59	related-only	per room scheduler entries active_threads waiters	src/mindroom/coalescing.py:82; src/mindroom/response_lifecycle.py:110
EventCacheWriteCoordinator	class	lines 62-129	related-only	Protocol queue_room_update queue_thread_update wait_for_idle	src/mindroom/matrix/cache/thread_write_cache_ops.py:73; src/mindroom/matrix/cache/thread_reads.py:73
EventCacheWriteCoordinator.queue_room_update	method	lines 65-76	related-only	queue_room_update protocol cache update	src/mindroom/matrix/cache/thread_write_cache_ops.py:73
EventCacheWriteCoordinator.run_room_update	async_method	lines 78-85	none-found	run_room_update ordered barrier	none
EventCacheWriteCoordinator.queue_thread_update	method	lines 87-99	related-only	queue_thread_update protocol cache update	src/mindroom/matrix/cache/thread_write_cache_ops.py:94
EventCacheWriteCoordinator.run_thread_update	async_method	lines 101-110	none-found	run_thread_update ignore_cancelled_room_fences	none
EventCacheWriteCoordinator.wait_for_room_idle	async_method	lines 112-113	related-only	wait_for_room_idle drain idle	src/mindroom/coalescing.py:472
EventCacheWriteCoordinator.wait_for_thread_idle	async_method	lines 115-126	related-only	wait_for_thread_idle idle same thread	src/mindroom/matrix/cache/thread_reads.py:73
EventCacheWriteCoordinator.close	async_method	lines 128-129	related-only	close drain background tasks drain_all	src/mindroom/coalescing.py:472
_EventCacheWriteCoordinator	class	lines 133-882	related-only	room thread scheduler coalescing idle waiters	src/mindroom/coalescing.py:145; src/mindroom/knowledge/refresh_scheduler.py:151; src/mindroom/response_lifecycle.py:110
_EventCacheWriteCoordinator._next_entry_sequence	method	lines 147-150	none-found	next sequence queued entry	none
_EventCacheWriteCoordinator._pending_tasks	method	lines 152-153	related-only	filter not task.done pending tasks	src/mindroom/coalescing.py:169; src/mindroom/coalescing.py:480
_EventCacheWriteCoordinator._pending_chain_length	method	lines 155-156	related-only	pending chain length task.done	src/mindroom/coalescing.py:480
_EventCacheWriteCoordinator._pending_entry_tasks	method	lines 158-160	related-only	pending entry tasks dict.fromkeys dedupe	src/mindroom/coalescing.py:480
_EventCacheWriteCoordinator._room_pending_tasks	method	lines 162-167	related-only	room pending tasks fallback entries	src/mindroom/coalescing.py:480
_EventCacheWriteCoordinator._emit_idle_wait_timing	method	lines 169-186	related-only	emit_timing_event idle wait timing	src/mindroom/coalescing.py:439; src/mindroom/coalescing.py:753
_EventCacheWriteCoordinator._room_state	method	lines 188-189	related-only	get or create per key state	src/mindroom/coalescing.py:217; src/mindroom/response_lifecycle.py:146
_EventCacheWriteCoordinator._find_entry_index	method	lines 191-199	none-found	find entry by identity	none
_EventCacheWriteCoordinator._prune_done_task_maps	method	lines 201-217	related-only	prune done task maps task.done pop	src/mindroom/coalescing.py:169; src/mindroom/knowledge/refresh_scheduler.py:165
_EventCacheWriteCoordinator._wake_waiters	method	lines 219-226	related-only	wake waiters futures set_result event wake	src/mindroom/coalescing.py:412
_EventCacheWriteCoordinator._discard_waiter	method	lines 228-232	none-found	discard waiter future state waiters	none
_EventCacheWriteCoordinator._cleanup_room_state	method	lines 234-241	related-only	cleanup empty state maps pop	src/mindroom/coalescing.py:743; src/mindroom/response_lifecycle.py:134
_EventCacheWriteCoordinator._start_entry	method	lines 243-263	related-only	start entry set active maps signal	src/mindroom/coalescing.py:400; src/mindroom/knowledge/refresh_scheduler.py:156
_EventCacheWriteCoordinator._drop_leading_room_fences	method	lines 265-267	none-found	drop leading room fences	none
_EventCacheWriteCoordinator._reevaluate_entry	method	lines 269-302	none-found	reevaluate entry room barrier same thread predecessor	none
_EventCacheWriteCoordinator._reevaluate_room	method	lines 304-338	none-found	reevaluate room queued_thread_predecessors active_room active_threads	none
_EventCacheWriteCoordinator._coalescible_pending_entry	method	lines 340-359	related-only	coalescible pending entry same order lane	src/mindroom/knowledge/refresh_scheduler.py:151; src/mindroom/coalescing.py:450
_EventCacheWriteCoordinator._coalesce_pending_update	method	lines 361-384	related-only	replace pending update coalesce latest request	src/mindroom/knowledge/refresh_scheduler.py:151
_EventCacheWriteCoordinator._log_coalesced_update_if_needed	method	lines 386-408	related-only	log coalesced update dropped_update_count	src/mindroom/coalescing.py:417; src/mindroom/coalescing.py:753
_EventCacheWriteCoordinator._release_active_entry	method	lines 410-433	related-only	release active task maps pop identity	src/mindroom/knowledge/refresh_scheduler.py:165; src/mindroom/coalescing.py:743
_EventCacheWriteCoordinator._remove_finished_entry	method	lines 435-446	none-found	remove finished entry cancelled room fence	none
_EventCacheWriteCoordinator._finish_entry	method	lines 448-463	related-only	done callback finish entry cleanup reevaluate wake	src/mindroom/knowledge/refresh_scheduler.py:163; src/mindroom/knowledge/refresh_scheduler.py:165; src/mindroom/coalescing.py:743
_EventCacheWriteCoordinator._queue_update	method	lines 465-586	related-only	queue update create task start signal coalesce timing	src/mindroom/coalescing.py:450; src/mindroom/knowledge/refresh_scheduler.py:156
_EventCacheWriteCoordinator._queue_update.<locals>.run_update	nested_async_function	lines 500-511	related-only	nested async run update bound log context	src/mindroom/coalescing.py:657
_EventCacheWriteCoordinator._queue_update.<locals>.run_when_scheduled	nested_async_function	lines 515-517; lines 522-564	related-only	await start signal then run task; timed scheduled run outcome timing emit	src/mindroom/coalescing.py:497; src/mindroom/coalescing.py:617; src/mindroom/coalescing.py:753
_EventCacheWriteCoordinator._await_idle_task	async_method	lines 588-606	related-only	await pending task ignore noncaller cancellation log failure	src/mindroom/coalescing.py:480
_EventCacheWriteCoordinator._room_is_idle	method	lines 608-616	related-only	is idle no gates no active tasks	src/mindroom/coalescing.py:169
_EventCacheWriteCoordinator._thread_is_idle	method	lines 618-639	related-only	thread idle queued entries room barrier same thread	src/mindroom/response_lifecycle.py:117
_EventCacheWriteCoordinator._fallback_room_tasks	method	lines 641-648	related-only	fallback room active tasks dedupe	src/mindroom/coalescing.py:480
_EventCacheWriteCoordinator._fallback_thread_tasks	method	lines 650-658	related-only	fallback thread active tasks dedupe	src/mindroom/coalescing.py:480
_EventCacheWriteCoordinator.queue_room_update	method	lines 660-682	related-only	public queue_room_update delegate _queue_update	src/mindroom/matrix/cache/thread_write_cache_ops.py:73
_EventCacheWriteCoordinator.run_room_update	async_method	lines 684-697	related-only	run public room update await queued task	src/mindroom/matrix/cache/thread_write_cache_ops.py:73
_EventCacheWriteCoordinator.queue_thread_update	method	lines 699-722	related-only	public queue_thread_update delegate _queue_update	src/mindroom/matrix/cache/thread_write_cache_ops.py:94
_EventCacheWriteCoordinator.run_thread_update	async_method	lines 724-742	related-only	run public thread update await queued task	src/mindroom/matrix/cache/thread_write_cache_ops.py:94
_EventCacheWriteCoordinator._wait_for_room_idle_without_timing	async_method	lines 744-770	related-only	wait loop idle future waiter fallback tasks	src/mindroom/coalescing.py:472; src/mindroom/coalescing.py:497
_EventCacheWriteCoordinator._wait_for_room_idle_with_timing	async_method	lines 772-819	related-only	wait loop idle timing pending tasks waiter	src/mindroom/coalescing.py:472; src/mindroom/coalescing.py:497; src/mindroom/coalescing.py:753
_EventCacheWriteCoordinator.wait_for_room_idle	async_method	lines 821-826	related-only	public wait room idle timing dispatch	src/mindroom/coalescing.py:472
_EventCacheWriteCoordinator.wait_for_thread_idle	async_method	lines 828-874	related-only	wait thread idle waiter fallback tasks	src/mindroom/coalescing.py:472; src/mindroom/response_lifecycle.py:117
_EventCacheWriteCoordinator.close	async_method	lines 876-882	related-only	drain owned tasks clear state	src/mindroom/coalescing.py:472
```

## Findings

No real duplication found.

Related scheduler/coalescing patterns checked:

- `src/mindroom/coalescing.py:145` defines a per-key queue state machine with drain tasks, wake events, idle detection, and shutdown draining.
  This is conceptually related to `_EventCacheWriteCoordinator`, but it batches inbound Matrix events by `(room, thread, sender)` and owns debounce/grace timing.
  It does not implement room-scoped write barriers, same-thread parallelism rules, cancelled room fences, start futures, or waiter futures.
- `src/mindroom/knowledge/refresh_scheduler.py:151` coalesces repeated refresh requests by keeping only the latest pending request while one task is active, then starts the pending request from the done callback at `src/mindroom/knowledge/refresh_scheduler.py:165`.
  This overlaps with "replace not-yet-run work with the latest request", but the ordering model is single active task per knowledge target and has no Matrix room/thread lane semantics.
- `src/mindroom/response_lifecycle.py:110` tracks per-thread lifecycle locks and active response signals.
  It is related to per-target serialization and idle/active detection, but uses `asyncio.Lock` ownership rather than queued update entries, callback completion, or room/thread cache write barriers.
- `src/mindroom/matrix/cache/thread_write_cache_ops.py:73` and `src/mindroom/matrix/cache/thread_write_cache_ops.py:94` are wrapper methods that forward cache writes into this coordinator.
  They are callers, not duplicate implementations.

## Proposed Generalization

No refactor recommended.
The related code shares broad async scheduling vocabulary, but the primary module's ordering semantics are specialized and do not appear repeated elsewhere under `./src`.

## Risk/tests

If this coordinator is changed later, tests should focus on room update exclusivity, same-thread serialization, cross-thread parallelism, room barriers blocking thread writes, cancelled room fences, coalesced pending updates, idle waits, and cleanup of task maps after cancellation or failure.
No production code was edited for this audit.
