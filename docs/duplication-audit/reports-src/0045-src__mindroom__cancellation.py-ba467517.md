Summary: One small duplication candidate exists: `DeliveryGateway._cancelled_error_failure_reason()` repeats `classify_cancel_source()` logic before calling `cancel_failure_reason()`.
No meaningful duplication found for task provenance cleanup, request cancellation recording, or cancelled-error construction.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_clear_task_cancel_source	function	lines 15-17	none-found	"_TASK_CANCEL_SOURCES pop add_done_callback clear cancel source"	none
request_task_cancel	function	lines 20-28	related-only	"request_task_cancel task.cancel msg cancel_msg add_done_callback USER_STOP_CANCEL_MSG SYNC_RESTART_CANCEL_MSG"	src/mindroom/orchestration/runtime.py:123; src/mindroom/orchestration/runtime.py:203; src/mindroom/orchestration/runtime.py:257; src/mindroom/stop.py:312; src/mindroom/history/compaction.py:1171
build_cancelled_error	function	lines 31-38	related-only	"build_cancelled_error CancelledError current_task cancelling Run cancelled _raise_agent_run_cancelled _raise_team_run_cancelled"	src/mindroom/ai.py:359; src/mindroom/teams.py:1198
_cancel_failure_reason	function	lines 41-47	duplicate-found	"cancel_failure_reason sync_restart_cancelled cancelled_by_user interrupted USER_STOP_CANCEL_MSG SYNC_RESTART_CANCEL_MSG"	src/mindroom/delivery_gateway.py:388; src/mindroom/orchestration/runtime.py:63; src/mindroom/response_runner.py:1207; src/mindroom/response_attempt.py:68
```

Findings:

1. `DeliveryGateway._cancelled_error_failure_reason()` duplicates cancellation-source classification.
   - Primary behavior: `src/mindroom/cancellation.py:41` maps a resolved `CancelSource` to canonical failure reasons: `sync_restart_cancelled`, `cancelled_by_user`, or `interrupted`.
   - Duplicated behavior: `src/mindroom/delivery_gateway.py:388` parses `asyncio.CancelledError.args` into the same `CancelSource` values, then calls `cancel_failure_reason()`.
   - Canonical classifier already exists at `src/mindroom/orchestration/runtime.py:63`, and other response paths use `cancel_failure_reason(classify_cancel_source(exc))`, for example `src/mindroom/response_runner.py:1207` and `src/mindroom/response_runner.py:2426`.
   - Difference to preserve: `DeliveryGateway` currently defaults unknown/no-arg cancellation to `interrupted`, which matches `classify_cancel_source()`.

Related-only checks:

- `request_task_cancel()` is the central helper for preserving the first explicit cancellation source while still calling `task.cancel(msg=...)`.
  Callers in `src/mindroom/orchestration/runtime.py:123`, `src/mindroom/orchestration/runtime.py:203`, `src/mindroom/orchestration/runtime.py:257`, `src/mindroom/stop.py:312`, and `src/mindroom/history/compaction.py:1171` delegate to it rather than duplicating its provenance dictionary and done-callback cleanup.
- `build_cancelled_error()` is already the canonical constructor used by the agent/team cancelled-run wrappers at `src/mindroom/ai.py:359` and `src/mindroom/teams.py:1198`.
  Those wrappers duplicate only naming/context, not the behavior of preserving in-flight cancellation provenance.
- `_clear_task_cancel_source()` has no active duplicate under `src/`.

Proposed generalization:

1. Replace `DeliveryGateway._cancelled_error_failure_reason(error)` internals with `return cancel_failure_reason(classify_cancel_source(error))`.
2. Import `classify_cancel_source` from `mindroom.orchestration.runtime` where `DeliveryGateway` already imports `cancel_failure_reason`.
3. Keep the private method if it improves local readability for delivery cleanup paths.

Risk/tests:

- Risk is low because the proposed change uses the same message constants and preserves the fallback to `interrupted`.
- Focused tests should cover `DeliveryGateway._cancelled_error_failure_reason()` for no args, `USER_STOP_CANCEL_MSG`, `SYNC_RESTART_CANCEL_MSG`, and unknown args.
- Existing cancellation-path tests around response finalization and stream cleanup should continue to assert the failure reasons `cancelled_by_user`, `sync_restart_cancelled`, and `interrupted`.
