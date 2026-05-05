## Summary

Top duplication candidate: the `full_history` plus `dispatch_safe` four-way dispatch is repeated in `ThreadReadPolicy.read_thread` and `MatrixConversationCache.get_thread_messages`.
No broader refactor is recommended because the duplicate is small and each layer selects different concrete targets.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ThreadHistoryFetcher	class	lines 27-38	related-only	ThreadHistoryFetcher fetch_thread_history_from_client coordinator_queue_wait_ms	src/mindroom/matrix/conversation_cache.py:566; src/mindroom/matrix/conversation_cache.py:589; src/mindroom/matrix/conversation_cache.py:609; src/mindroom/matrix/conversation_cache.py:629; src/mindroom/matrix/client_thread_history.py:766; src/mindroom/matrix/client_thread_history.py:820; src/mindroom/matrix/client_thread_history.py:875; src/mindroom/matrix/client_thread_history.py:930
ThreadHistoryFetcher.__call__	method	lines 30-38	related-only	caller_label coordinator_queue_wait_ms fetcher protocol signature	src/mindroom/matrix/conversation_cache.py:566; src/mindroom/matrix/client_thread_history.py:766; src/mindroom/matrix/client_thread_history.py:820; src/mindroom/matrix/client_thread_history.py:875; src/mindroom/matrix/client_thread_history.py:930
ThreadReadPolicy	class	lines 41-215	related-only	ThreadReadPolicy read_thread get_latest_thread_event_id_if_needed cache facade	src/mindroom/matrix/conversation_cache.py:366; src/mindroom/matrix/conversation_cache.py:460; src/mindroom/matrix/conversation_cache.py:901
ThreadReadPolicy.__init__	method	lines 44-59	related-only	ThreadReadPolicy constructor fetch_thread_history_from_client fetch_dispatch_thread_snapshot	src/mindroom/matrix/conversation_cache.py:374; src/mindroom/matrix/conversation_cache.py:566; src/mindroom/matrix/conversation_cache.py:589; src/mindroom/matrix/conversation_cache.py:609; src/mindroom/matrix/conversation_cache.py:629
ThreadReadPolicy.logger	method	lines 62-64	none-found	logger_getter facade-bound logger property	none
ThreadReadPolicy._coordinator	method	lines 66-67	related-only	event_cache_write_coordinator coordinator runtime	src/mindroom/matrix/cache/write_coordinator.py:45; src/mindroom/matrix/conversation_cache.py:374
ThreadReadPolicy._wait_for_pending_thread_cache_updates	async_method	lines 69-77	related-only	wait_for_thread_idle ignore_cancelled_room_fences thread cache idle	src/mindroom/matrix/cache/write_coordinator.py:828; src/mindroom/matrix/cache/write_coordinator.py:115
ThreadReadPolicy._full_history_result	method	lines 79-89	related-only	thread_history_result is_full_history diagnostics copy	src/mindroom/matrix/conversation_cache.py:435
ThreadReadPolicy._run_thread_read	async_method	lines 91-126	related-only	run_thread_update coordinator_queue_wait_ms fetcher load	src/mindroom/matrix/cache/write_coordinator.py:724; src/mindroom/matrix/conversation_cache.py:445
ThreadReadPolicy._run_thread_read.<locals>.load	nested_async_function	lines 102-112	related-only	coordinator_queue_wait_ms fetcher full_history _full_history_result	src/mindroom/matrix/client_thread_history.py:775; src/mindroom/matrix/client_thread_history.py:829; src/mindroom/matrix/client_thread_history.py:884; src/mindroom/matrix/client_thread_history.py:939
ThreadReadPolicy.read_thread	async_method	lines 128-178	duplicate-found	full_history dispatch_safe get_thread_messages four way selection	src/mindroom/matrix/conversation_cache.py:802; src/mindroom/matrix/conversation_cache.py:818; src/mindroom/matrix/conversation_cache.py:832; src/mindroom/matrix/conversation_cache.py:864; src/mindroom/matrix/conversation_cache.py:880
ThreadReadPolicy.get_latest_thread_event_id_if_needed	async_method	lines 180-215	related-only	get_latest_thread_event_id_if_needed latest_visible_thread_event_id stale cache fallback	src/mindroom/matrix/conversation_cache.py:901; src/mindroom/matrix/cache/thread_cache_helpers.py:23; src/mindroom/thread_summary.py:396; src/mindroom/delivery_gateway.py:544
```

## Findings

1. `ThreadReadPolicy.read_thread` duplicates the four-way read-mode branching in `MatrixConversationCache.get_thread_messages`.
   `ThreadReadPolicy.read_thread` maps `(full_history, dispatch_safe)` to one of four fetchers and coordinator operation names at `src/mindroom/matrix/cache/thread_reads.py:141`.
   `MatrixConversationCache.get_thread_messages` maps the same boolean pair to one of four public cache methods at `src/mindroom/matrix/conversation_cache.py:832`.
   The behavior is structurally duplicated, but the targets differ: the facade preserves named public entrypoints and per-turn memoization, while the policy selects client fetchers and coordinator labels.

## Proposed Generalization

No refactor recommended for this file.
If this grows, the minimal generalization would be a tiny typed read-mode table local to the cache package that maps `(full_history, dispatch_safe)` to mode metadata.
It would need separate facade and policy targets to avoid coupling public methods to private fetchers.

## Risk/Tests

The main risk is mode drift: one branch could route a `full_history` or `dispatch_safe` combination differently between the facade and the read policy.
Relevant tests would cover all four `get_thread_messages` combinations and verify that `ThreadReadPolicy.read_thread` selects the matching fetcher and coordinator operation name.
Latest-event fallback behavior is centralized in `ThreadReadPolicy.get_latest_thread_event_id_if_needed`; call sites are related consumers, not duplicate implementations.
