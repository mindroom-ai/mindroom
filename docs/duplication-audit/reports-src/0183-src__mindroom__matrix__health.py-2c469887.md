Summary: No meaningful Matrix-health duplication found.
The primary module already centralizes Matrix `/versions` URL construction, `/versions` payload validation, and process-local Matrix sync-loop health state.
The only concrete duplicate behavior found is small UTC datetime normalization logic repeated in unrelated modules, but it is not Matrix-specific enough to justify a refactor from this file alone.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_MatrixSyncState	class	lines 20-25	related-only	"_MatrixSyncState running loop_started_time last_sync_time sync state"	src/mindroom/bot.py:911; src/mindroom/bot.py:1014; src/mindroom/orchestration/runtime.py:180
MatrixSyncHealthSnapshot	class	lines 29-39	none-found	"MatrixSyncHealthSnapshot active_entities stale_entities last_sync_time snapshot"	src/mindroom/api/main.py:466; tests/api/test_api.py:879; tests/api/test_api.py:945
MatrixSyncHealthSnapshot.is_healthy	method	lines 37-39	none-found	"is_healthy stale_entities status unhealthy sync_health"	src/mindroom/api/main.py:477; tests/api/test_api.py:887; tests/api/test_api.py:956
_normalize_sync_time	function	lines 46-50	duplicate-found	"tzinfo UTC replace astimezone datetime normalization"	src/mindroom/approval_events.py:147; src/mindroom/approval_manager.py:99; src/mindroom/attachments.py:229
matrix_versions_url	function	lines 53-55	none-found	"/_matrix/client/versions rstrip homeserver versions_url"	src/mindroom/cli/doctor.py:600; src/mindroom/cli/local_stack.py:255; src/mindroom/orchestration/runtime.py:357
response_has_matrix_versions	function	lines 58-66	none-found	"response.is_success response.json versions payload Matrix versions"	src/mindroom/cli/doctor.py:606; src/mindroom/cli/local_stack.py:261; src/mindroom/orchestration/runtime.py:387
mark_matrix_sync_loop_started	function	lines 69-79	related-only	"mark sync loop started startup grace loop_started_time first sync"	src/mindroom/bot.py:911; src/mindroom/orchestration/runtime.py:211; tests/api/test_api.py:899
mark_matrix_sync_success	function	lines 82-89	related-only	"mark sync success last_sync_time SyncResponse health watchdog"	src/mindroom/bot.py:1020; src/mindroom/orchestration/runtime.py:180; tests/api/test_api.py:879
clear_matrix_sync_state	function	lines 92-95	none-found	"clear matrix sync state pop entity shutdown stale entity"	src/mindroom/bot.py:39; tests/api/test_api.py:988
get_matrix_sync_health_snapshot	function	lines 98-152	none-found	"stale_after_seconds startup_grace_seconds stale_entities oldest last_sync_time active_states"	src/mindroom/api/main.py:466; tests/api/test_api.py:879; tests/api/test_api.py:945
reset_matrix_sync_health	function	lines 155-158	related-only	"reset clear shared health state orchestrator shutdown tests"	src/mindroom/orchestrator.py:2022; tests/api/test_api.py:869; tests/api/test_api.py:879
```

Findings:

1. Repeated UTC datetime normalization helper
   - `src/mindroom/matrix/health.py:46` normalizes an already-created `datetime` by assigning UTC to naive values and converting aware values with `astimezone(UTC)`.
   - `src/mindroom/attachments.py:229` performs the same naive/aware normalization after parsing attachment timestamps.
   - `src/mindroom/approval_events.py:147` and `src/mindroom/approval_manager.py:99` perform a partial version of the same behavior for ISO timestamps, assigning UTC to naive values but leaving aware values in their original timezone.
   - The shared behavior is "turn a datetime-like persisted timestamp into a timezone-aware value suitable for elapsed-time comparisons."
   - Differences to preserve: `attachments` converts aware timestamps to UTC, while approval modules currently preserve non-UTC aware offsets; `_normalize_sync_time` accepts a `datetime`, while approval/attachment helpers also parse strings and handle `None` or invalid values.

No duplicate Matrix `/versions` behavior was found.
The three `/versions` consumers call `matrix_versions_url()` and `response_has_matrix_versions()` directly at `src/mindroom/cli/doctor.py:600`, `src/mindroom/cli/local_stack.py:255`, and `src/mindroom/orchestration/runtime.py:357`.

No duplicate Matrix sync-health registry was found.
`src/mindroom/bot.py:911` and `src/mindroom/bot.py:1020` only write through this module, while `src/mindroom/api/main.py:466` is the sole runtime health snapshot consumer.
`src/mindroom/orchestration/runtime.py:180` has a related but intentionally different monotonic watchdog path, which tracks local loop stall timing rather than API health reporting.

Proposed generalization:

No refactor recommended for Matrix health.
If UTC normalization duplication becomes broader, add a small typed helper such as `normalize_utc_datetime(value: datetime) -> datetime` in an existing date/time utility module and migrate callers that want aware UTC output.
Do not move the approval string parsing helpers unless their timezone-preservation behavior is intentionally changed.

Risk/tests:

Changing `_normalize_sync_time()` would affect Matrix health freshness and startup grace calculations.
Relevant tests are `tests/api/test_api.py:879`, `tests/api/test_api.py:899`, `tests/api/test_api.py:918`, `tests/api/test_api.py:945`, `tests/api/test_api.py:967`, and `tests/api/test_api.py:988`.
If a shared UTC helper is introduced later, add focused tests for naive datetimes, aware non-UTC datetimes, and exact threshold comparisons around stale sync timing.
