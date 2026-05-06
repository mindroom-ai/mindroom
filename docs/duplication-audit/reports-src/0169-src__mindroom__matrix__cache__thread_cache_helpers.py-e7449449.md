## Summary

No meaningful duplication found.
The primary module already centralizes durable thread-cache acceptance policy for thread-history reads and agent-message snapshot reads.
Nearby code has related thread-tail selection and timestamp freshness checks, but those helpers preserve different semantics and are not direct duplicates of this module's behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ThreadCacheStateLike	class	lines 13-20	related-only	ThreadCacheStateLike ThreadCacheState validated_at invalidated_at room_invalidated_at	src/mindroom/matrix/cache/event_cache.py:12, src/mindroom/matrix/cache/sqlite_event_cache_threads.py:119, src/mindroom/matrix/cache/postgres_event_cache_threads.py:146
latest_visible_thread_event_id	function	lines 23-27	related-only	latest_visible_thread_event_id visible_event_id event_id thread latest_event_id	src/mindroom/matrix/cache/thread_reads.py:180, src/mindroom/matrix/thread_projection.py:305, src/mindroom/matrix/stale_stream_cleanup.py:519
thread_cache_rejection_reason	function	lines 30-43	none-found	thread_cache_rejection_reason cache_never_validated thread_invalidated_after_validation room_invalidated_after_validation usable cache_state	src/mindroom/matrix/client_thread_history.py:462, src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:53, src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:54, src/mindroom/matrix/cache/sqlite_event_cache_threads.py:260, src/mindroom/matrix/cache/postgres_event_cache_threads.py:284
thread_cache_state_is_usable	function	lines 46-50	none-found	thread_cache_state_is_usable usable thread_cache_rejection_reason cache_state	src/mindroom/matrix/cache/__init__.py:28, src/mindroom/matrix/cache/__init__.py:59
```

## Findings

No real duplication found for this primary file.

`ThreadCacheStateLike` mirrors the fields on `ThreadCacheState` in `src/mindroom/matrix/cache/event_cache.py:12`.
This is a structural typing contract rather than duplicated behavior, and it keeps the pure helper independent of the concrete cache model.

`latest_visible_thread_event_id` is related to `latest_visible_thread_event_id_by_thread` in `src/mindroom/matrix/thread_projection.py:305`.
Both return visible event IDs at a thread tail, but the projection helper groups many messages by thread, resolves duplicate visible-event candidates, sorts by thread order, and handles thread roots.
The primary helper intentionally trusts one already-resolved thread history order and only returns the last message's `visible_event_id` fallback chain.

`thread_cache_rejection_reason` has no duplicated implementation in `./src`.
The read path in `src/mindroom/matrix/client_thread_history.py:462`, SQLite snapshot path in `src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:53`, and PostgreSQL snapshot path in `src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:54` all call this helper rather than repeating its policy.
The timestamp freshness helpers in `src/mindroom/matrix/cache/sqlite_event_cache_threads.py:260` and `src/mindroom/matrix/cache/postgres_event_cache_threads.py:284` are related only: they detect whether any state timestamp changed after a fetch began, not whether an already-loaded durable snapshot is reusable.

`thread_cache_state_is_usable` has no duplicated behavior found.
It is a thin boolean wrapper over `thread_cache_rejection_reason` and appears to be exported from `src/mindroom/matrix/cache/__init__.py:28`.

## Proposed Generalization

No refactor recommended.
The current helper module is already the shared location for durable thread-cache reuse policy.
Moving the related projection or fetch-race checks into it would couple different concepts and would require parameterizing semantics that are currently clear at their call sites.

## Risk/Tests

No production changes were made.
If this module is changed later, tests should cover cache states with no state, never-validated state, thread invalidation after validation, room invalidation after validation, and valid states.
For `latest_visible_thread_event_id`, tests should preserve the empty-history result and the `visible_event_id` to `event_id` fallback behavior.
