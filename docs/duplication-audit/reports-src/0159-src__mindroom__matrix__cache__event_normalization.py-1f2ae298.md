Summary: No meaningful duplication found.

The primary module is already the shared normalization point for persistent Matrix event cache writes.
Nearby code has related event-source extraction and dict-copying behavior, but those call sites either delegate to this module for cache storage or serve different visible-message/content-resolution purposes.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
normalize_event_source_for_cache	function	lines 15-34	related-only	normalize_event_source_for_cache; for_cache; dispatch_pipeline_timing; source["event_id"]; source["origin_server_ts"]; dict event_source string-key copies	src/mindroom/matrix/cache/sqlite_event_cache.py:610,749; src/mindroom/matrix/cache/postgres_event_cache.py:987,1155; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:171; src/mindroom/matrix/cache/postgres_event_cache_threads.py:191; src/mindroom/matrix/cache/thread_writes.py:473; src/mindroom/matrix/conversation_cache.py:268-273; src/mindroom/matrix/client_visible_messages.py:206
normalize_nio_event_for_cache	function	lines 37-52	related-only	normalize_nio_event_for_cache; event.source if isinstance(event.source, dict); server_timestamp origin_server_ts; event.event_id sender	src/mindroom/matrix/client_thread_history.py:213-215,1039,1147; src/mindroom/matrix/conversation_cache.py:340-343; src/mindroom/matrix/cache/thread_writes.py:75,89,723,756; src/mindroom/approval_transport.py:349; src/mindroom/matrix/client_visible_messages.py:287,309; src/mindroom/voice_handler.py:231
```

Findings:

No real duplicated cache-normalization implementation was found.

`normalize_event_source_for_cache` removes runtime-only dispatch timing metadata and backfills missing `event_id`, `sender`, and integer `origin_server_ts` fields for durable cache storage.
The SQLite and Postgres cache implementations already call it before lookup persistence and thread append operations at `src/mindroom/matrix/cache/sqlite_event_cache.py:610`, `src/mindroom/matrix/cache/sqlite_event_cache.py:749`, `src/mindroom/matrix/cache/postgres_event_cache.py:987`, and `src/mindroom/matrix/cache/postgres_event_cache.py:1155`.
Thread cache batch helpers also call it at `src/mindroom/matrix/cache/sqlite_event_cache_threads.py:171` and `src/mindroom/matrix/cache/postgres_event_cache_threads.py:191`.
`src/mindroom/matrix/conversation_cache.py:268-273` copies string-keyed event data and updates `origin_server_ts`, but this projects a cached original event into its latest visible edited state, not persistent cache ingress.
`src/mindroom/matrix/client_visible_messages.py:206` similarly normalizes mapping keys before sidecar/content resolution, which is a display-path concern and intentionally does not strip runtime-only cache metadata or backfill Matrix storage fields.

`normalize_nio_event_for_cache` is already used by the cache-ingress paths that accept nio events.
`src/mindroom/matrix/client_thread_history.py:213-215` is only a local wrapper over the primary helper, and its callers at `src/mindroom/matrix/client_thread_history.py:1039` and `src/mindroom/matrix/client_thread_history.py:1147` use it for scanned room-history cache sources.
`src/mindroom/matrix/conversation_cache.py:340-343`, `src/mindroom/matrix/cache/thread_writes.py:75`, `src/mindroom/matrix/cache/thread_writes.py:89`, `src/mindroom/matrix/cache/thread_writes.py:723`, `src/mindroom/matrix/cache/thread_writes.py:756`, and `src/mindroom/approval_transport.py:349` all delegate to the helper rather than duplicating its event-id/sender/timestamp backfill behavior.
Other `event.source if isinstance(event.source, dict) else {}` occurrences, such as `src/mindroom/matrix/client_visible_messages.py:287`, `src/mindroom/matrix/client_visible_messages.py:309`, and `src/mindroom/voice_handler.py:231`, are related only because they defensively extract nio sources for presentation or message rewriting, not durable cache normalization.

Proposed generalization: No refactor recommended.

The shared helper already exists at the right boundary under `src/mindroom/matrix/cache/`.
Pulling unrelated visible-message and voice-message source extraction into this module would mix display/transcription behavior with cache storage semantics.

Risk/tests:

No production change is recommended, so no tests are required for this report.
If this module is changed later, focused coverage should verify that runtime-only dispatch timing is stripped, explicit fallback `event_id`/`sender`/`origin_server_ts` values are only inserted when missing and type-valid, bool timestamps are rejected, and nio events with non-dict `.source` still produce a cache-safe dict.
