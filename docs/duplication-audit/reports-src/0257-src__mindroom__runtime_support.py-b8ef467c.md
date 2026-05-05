# Duplication Audit: `src/mindroom/runtime_support.py`

## Summary

One small duplication candidate exists in this primary file: `build_owned_runtime_support` and `sync_owned_runtime_support` both normalize the legacy `db_path` inputs into `CacheConfig` and fallback `RuntimePaths`.
No broader event-cache construction, lifecycle initialization, shutdown, identity, or startup prewarm claim logic appears duplicated elsewhere in `./src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
StartupThreadPrewarmRegistry	class	lines 27-45	related-only	StartupThreadPrewarmRegistry prewarm claim registry asyncio.Lock try_claim release	src/mindroom/bot.py:566, src/mindroom/bot.py:675, src/mindroom/bot.py:688, src/mindroom/knowledge/refresh_runner.py:100, src/mindroom/matrix/cache/sqlite_event_cache.py:253, src/mindroom/matrix/cache/postgres_event_cache.py:338
StartupThreadPrewarmRegistry.__init__	method	lines 30-32	related-only	asyncio.Lock claimed_room_ids room claim set	src/mindroom/knowledge/refresh_runner.py:100, src/mindroom/matrix/cache/sqlite_event_cache.py:253, src/mindroom/matrix/cache/postgres_event_cache.py:338
StartupThreadPrewarmRegistry.try_claim	async_method	lines 34-40	related-only	try_claim claimed_room_ids room_id lock acquire add return bool	src/mindroom/bot.py:695, src/mindroom/knowledge/refresh_runner.py:151, src/mindroom/matrix/cache/sqlite_event_cache.py:331, src/mindroom/matrix/cache/postgres_event_cache.py:596
StartupThreadPrewarmRegistry.release	async_method	lines 42-45	related-only	release claimed_room_ids discard startup prewarm	src/mindroom/bot.py:683, src/mindroom/bot.py:686, src/mindroom/knowledge/refresh_runner.py:151, src/mindroom/matrix/cache/sqlite_event_cache.py:351, src/mindroom/matrix/cache/postgres_event_cache.py:611
EventCacheRuntimeIdentity	class	lines 49-61	none-found	EventCacheRuntimeIdentity backend location namespace comparable runtime identity redacted_location	src/mindroom/orchestrator.py:362, src/mindroom/config/matrix.py:255, src/mindroom/config/matrix.py:261, src/mindroom/config/matrix.py:276
EventCacheRuntimeIdentity.redacted_location	method	lines 57-61	related-only	redacted_location redact_postgres_connection_info database_url redacted_database_url	src/mindroom/matrix/cache/postgres_event_cache.py:373, src/mindroom/matrix/cache/postgres_redaction.py:15, src/mindroom/knowledge/redaction.py:65
OwnedRuntimeSupport	class	lines 65-71	none-found	OwnedRuntimeSupport event_cache write_coordinator startup_thread_prewarm_registry event_cache_identity	src/mindroom/orchestrator.py:257, src/mindroom/bot_runtime_view.py:44, src/mindroom/bot_runtime_view.py:47, src/mindroom/bot_runtime_view.py:50
event_cache_runtime_identity	function	lines 74-88	none-found	cache_config backend resolve_db_path resolve_postgres_database_url resolve_namespace EventCacheRuntimeIdentity	src/mindroom/config/matrix.py:255, src/mindroom/config/matrix.py:261, src/mindroom/config/matrix.py:276, src/mindroom/orchestrator.py:362
_load_postgres_event_cache_class	function	lines 91-95	none-found	ensure_optional_deps psycopg import_module postgres_event_cache PostgresEventCache	src/mindroom/tool_system/dependencies.py:1, src/mindroom/matrix/cache/postgres_event_cache.py:135, src/mindroom/config/matrix.py:261
build_event_cache	function	lines 98-109	none-found	build event cache backend sqlite postgres SqliteEventCache PostgresEventCache	src/mindroom/matrix/cache/sqlite_event_cache.py:430, src/mindroom/matrix/cache/postgres_event_cache.py:688, src/mindroom/orchestrator.py:271
build_owned_runtime_support	function	lines 112-145	duplicate-found	build_owned_runtime_support db_path CacheConfig runtime_paths RuntimePaths event_cache_write_coordinator	src/mindroom/runtime_support.py:178, src/mindroom/orchestrator.py:271, src/mindroom/orchestrator.py:362
initialize_event_cache_best_effort	async_function	lines 148-175	related-only	initialize event_cache best effort EventCacheBackendUnavailableError disable warning transient unavailable	src/mindroom/matrix/cache/postgres_event_cache.py:438, src/mindroom/matrix/cache/sqlite_event_cache.py:303, src/mindroom/matrix/cache/postgres_event_cache.py:470
sync_owned_runtime_support	async_function	lines 178-238	duplicate-found	sync_owned_runtime_support db_path CacheConfig runtime_paths RuntimePaths target_identity rebind initialize	src/mindroom/runtime_support.py:112, src/mindroom/orchestrator.py:362, src/mindroom/orchestrator.py:351
close_owned_runtime_support	async_function	lines 241-255	related-only	close_owned_runtime_support close write coordinator close event cache warning exceptions	src/mindroom/orchestrator.py:379, src/mindroom/matrix/cache/sqlite_event_cache.py:310, src/mindroom/matrix/cache/postgres_event_cache.py:462, src/mindroom/matrix/client_session.py:113
```

## Findings

### 1. Legacy cache input normalization is repeated inside runtime support

`build_owned_runtime_support` normalizes optional `db_path`, `cache_config`, and `runtime_paths` inputs at `src/mindroom/runtime_support.py:121`.
`sync_owned_runtime_support` repeats the same behavior at `src/mindroom/runtime_support.py:190`.
Both functions raise equivalent validation errors, construct `CacheConfig(db_path=str(db_path))` when only `db_path` is supplied, and synthesize a `RuntimePaths` rooted at `db_path.parent`.

This is functionally the same compatibility path before building or syncing the owned runtime support bundle.
The only differences are the function names embedded in the `ValueError` strings.
Those names should either be parameterized or the helper should raise a neutral message if this is ever refactored.

No matching production duplicate was found outside this file.
`MultiAgentOrchestrator.__post_init__` calls `build_owned_runtime_support` with `db_path` at `src/mindroom/orchestrator.py:271`, while `_sync_event_cache_service` calls `sync_owned_runtime_support` with fully resolved config/runtime paths at `src/mindroom/orchestrator.py:362`.

## Proposed generalization

Extract a private helper in `src/mindroom/runtime_support.py`, for example `_resolve_cache_runtime_inputs(*, caller: str, db_path: Path | None, cache_config: CacheConfig | None, runtime_paths: RuntimePaths | None) -> tuple[CacheConfig, RuntimePaths]`.
Use it from both `build_owned_runtime_support` and `sync_owned_runtime_support`.
Keep the current caller-specific error text by passing the caller name, or switch to a neutral private-helper error only if tests do not assert exact messages.

No broader refactor recommended.
`StartupThreadPrewarmRegistry`, `EventCacheRuntimeIdentity`, event-cache backend construction, best-effort initialization, and ordered shutdown are already centralized in this module and are only consumed by orchestrator/bot wiring.

## Risk/tests

Risk is low but not zero because the duplicated normalization is compatibility glue for callers that pass only `db_path`.
Tests should cover `build_owned_runtime_support(db_path=...)` and `sync_owned_runtime_support(..., db_path=...)`, including the two missing-input `ValueError` paths if exact messages matter.
PostgreSQL cache tests should also exercise identity comparison with redacted logging if the helper changes how `CacheConfig` or `RuntimePaths` are resolved.
