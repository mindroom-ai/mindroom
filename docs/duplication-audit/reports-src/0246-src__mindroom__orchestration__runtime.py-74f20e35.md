Summary: Top duplication candidates are cancellation provenance normalization, capped exponential backoff, Matrix `/versions` readiness polling, lightweight detached-task cleanup, and entity display-name lookup.
Most runtime lifecycle helpers are orchestration-specific and only have related patterns elsewhere, not direct duplication.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
classify_cancel_source	function	lines 63-71	duplicate-found	"classify_cancel_source CancelledError USER_STOP_CANCEL_MSG SYNC_RESTART_CANCEL_MSG cancel_failure_reason"	src/mindroom/delivery_gateway.py:388; src/mindroom/response_attempt.py:68; src/mindroom/response_runner.py:1194; src/mindroom/streaming.py:1123
is_sync_restart_cancel	function	lines 74-76	none-found	"is_sync_restart_cancel sync_restart_cancel sync_restart classify_cancel_source"	none
matrix_sync_startup_timeout_seconds	function	lines 79-88	related-only	"MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS env_value positive timeout startup_grace_seconds"	src/mindroom/api/sandbox_exec.py:77; src/mindroom/api/main.py:467
_matrix_homeserver_startup_timeout_seconds_from_env	function	lines 91-104	related-only	"MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS must be 0 or a positive integer env_value timeout"	src/mindroom/api/sandbox_exec.py:77; src/mindroom/cli/local_stack.py:324
retry_delay_seconds	function	lines 107-115	duplicate-found	"retry_delay_seconds exponential backoff 2 ** max retry cooldown"	src/mindroom/memory/auto_flush.py:546; src/mindroom/orchestrator.py:508; src/mindroom/orchestrator.py:1787
is_permanent_startup_error	function	lines 118-120	related-only	"PermanentMatrixStartupError isinstance retry_if_not_exception_type startup permanent"	src/mindroom/bot.py:1182; src/mindroom/bot.py:1195; src/mindroom/orchestrator.py:471
cancel_task	async_function	lines 123-134	duplicate-found	"task.cancel await task suppress CancelledError gather return_exceptions cancel background task"	src/mindroom/bot.py:1289; src/mindroom/bot.py:1301; src/mindroom/knowledge/watch.py:179; src/mindroom/knowledge/refresh_scheduler.py:117; src/mindroom/background_tasks.py:81
cancel_logged_task	async_function	lines 137-147	related-only	"cancel_logged_task cancelling logged tasks suppress cancellation failure detached task"	src/mindroom/orchestrator.py:425; src/mindroom/background_tasks.py:81
MatrixSyncStalledError	class	lines 150-151	not-a-behavior-symbol	"MatrixSyncStalledError stalled sync error watchdog"	src/mindroom/orchestration/runtime.py:208
_SyncIteration	class	lines 155-273	related-only	"sync task watchdog FIRST_COMPLETED cancel restart Matrix sync loop"	src/mindroom/streaming_delivery.py:513; src/mindroom/streaming_delivery.py:594; src/mindroom/approval_manager.py:911; src/mindroom/background_tasks.py:102
_SyncIteration._watch	async_method	lines 164-208	none-found	"seconds_since_last_sync_activity MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS first sync never completed watchdog"	none
_SyncIteration.start	method	lines 211-235	related-only	"asyncio.create_task named task watchdog start close coro on create_task failure"	src/mindroom/orchestrator.py:442; src/mindroom/bot.py:1256; src/mindroom/knowledge/watch.py:149
_SyncIteration.wait	async_method	lines 237-255	related-only	"asyncio.wait FIRST_COMPLETED surface real failure watchdog task sync task"	src/mindroom/streaming_delivery.py:594; src/mindroom/approval_manager.py:911; src/mindroom/background_tasks.py:131
_SyncIteration.cancel	async_method	lines 257-273	related-only	"cancel child tasks suppress CancelledError cleanup warning MatrixSyncStalledError"	src/mindroom/background_tasks.py:81; src/mindroom/mcp/manager.py:125; src/mindroom/bot.py:1289
_log_detached_task_result	function	lines 276-292	duplicate-found	"task.result add_done_callback logger.exception CancelledError background task failed"	src/mindroom/background_tasks.py:52
create_logged_task	function	lines 295-304	duplicate-found	"asyncio.create_task add_done_callback log failures detached task name"	src/mindroom/background_tasks.py:21; src/mindroom/orchestrator.py:527; src/mindroom/orchestrator.py:547
run_with_retry	async_function	lines 307-344	related-only	"startup_step_retrying run operation until succeeds permanent error retry sleep"	src/mindroom/orchestrator.py:478; src/mindroom/bot.py:1172; src/mindroom/orchestrator.py:1758
wait_for_matrix_homeserver	async_function	lines 347-402	duplicate-found	"matrix_versions_url response_has_matrix_versions wait homeserver httpx timeout retry"	src/mindroom/cli/local_stack.py:253; src/mindroom/cli/local_stack.py:324
EntityStartResults	class	lines 406-411	not-a-behavior-symbol	"EntityStartResults started_bots retryable_entities permanently_failed_entities dataclass"	none
create_temp_user	function	lines 414-430	duplicate-found	"configured display name agent team router RouterAgent AgentMatrixUser user_id empty password empty"	src/mindroom/matrix/stale_stream_cleanup.py:1421; src/mindroom/matrix/users.py:790; src/mindroom/api/schedules.py:238
cancel_sync_task	async_function	lines 433-441	related-only	"cancel_sync_task sync_tasks.pop cancel_task entity_name"	src/mindroom/orchestrator.py:1204; src/mindroom/orchestrator.py:1662
stop_entities	async_function	lines 444-469	related-only	"prepare_for_sync_shutdown cancel sync tasks stop reason restart pop agent_bots"	src/mindroom/orchestrator.py:1214; src/mindroom/orchestrator.py:1235
sync_forever_with_restart	async_function	lines 472-507	related-only	"sync_forever restart retry_delay_seconds auxiliary task crashed restarting max_retries"	src/mindroom/orchestrator.py:1758; src/mindroom/bot.py:1325
```

Findings:

1. Cancellation provenance normalization is duplicated.
`src/mindroom/orchestration/runtime.py:63` maps `CancelledError.args[0]` values to `CancelSource`.
`src/mindroom/delivery_gateway.py:388` repeats the same `USER_STOP_CANCEL_MSG` and `SYNC_RESTART_CANCEL_MSG` checks before calling `cancel_failure_reason`.
Response and streaming paths already call the runtime helper, so this gateway method is the outlier.
The behavior is functionally the same, with only the final return type differing because the gateway wants the failure reason string.

2. Capped exponential backoff is duplicated.
`src/mindroom/orchestration/runtime.py:107` computes `min(max_delay, initial_delay * 2 ** max(0, attempt - 1))`.
`src/mindroom/memory/auto_flush.py:546` computes the same capped doubling pattern for failed memory extraction cooldowns.
`src/mindroom/orchestrator.py:508` and `src/mindroom/orchestrator.py:1787` already reuse the runtime helper.
The memory helper uses config field names and integer return values, so preserving those types matters.

3. Matrix `/versions` readiness polling is duplicated between async runtime startup and sync CLI local-stack setup.
`src/mindroom/orchestration/runtime.py:347` builds `matrix_versions_url`, repeatedly GETs it, checks `response_has_matrix_versions`, sleeps, and times out.
`src/mindroom/cli/local_stack.py:253` does the same Matrix-specific wait through the generic sync helper at `src/mindroom/cli/local_stack.py:324`.
The differences are intentional surface differences: runtime uses `RuntimePaths`, async `httpx.AsyncClient`, structured logs, runtime state updates, and optional unbounded waiting; CLI uses synchronous `httpx.get`, Rich console messages, fixed 60 second timeout, and exits via Typer.

4. Detached task completion logging overlaps with generic background task management.
`src/mindroom/orchestration/runtime.py:276` and `src/mindroom/orchestration/runtime.py:295` create named detached tasks and log completion failures via a done callback.
`src/mindroom/background_tasks.py:21` has the broader version: it creates a task, optionally names it, retains a strong reference, removes it on completion, suppresses cancellation, logs exceptions, and optionally calls an error handler.
Runtime's cancellation-time downgrade via `_CANCELLING_LOGGED_TASKS` is specialized and should be preserved if generalized.

5. Entity display-name selection is repeated.
`src/mindroom/orchestration/runtime.py:414` selects `"RouterAgent"`, configured agent display name, configured team display name, or the raw entity name before constructing a placeholder `AgentMatrixUser`.
`src/mindroom/matrix/stale_stream_cleanup.py:1421` selects configured agent/team display name or raw name for resume mentions.
`src/mindroom/matrix/users.py:790` and `src/mindroom/api/schedules.py:238` hard-code the router display name when creating/logging in the router account.
The duplicated behavior is mostly the display-name decision, not the temporary user construction.

Proposed generalization:

1. Move `classify_cancel_source` beside `request_task_cancel` in `src/mindroom/cancellation.py`, export it from there, and update `delivery_gateway` to call `cancel_failure_reason(classify_cancel_source(error))`.
2. Consider a tiny shared capped backoff helper only if memory retry behavior is being touched; it can either reuse `retry_delay_seconds` with config values or move the pure formula to a neutral utility.
3. Leave Matrix readiness waits separate for now, or extract only a sync/async-neutral response predicate plus URL construction if the CLI/runtime waits drift again.
4. Do not merge `create_logged_task` into `create_background_task` unless runtime wants owner tracking and strong-reference semantics; the current helpers solve adjacent but different lifecycle problems.
5. Add a small `entity_display_name(entity_name, config, *, router_display_name="RouterAgent")` helper if another call site starts needing exactly the same router-aware display-name lookup.

Risk/tests:

Cancellation provenance has user-visible failure reasons, so cover `CancelledError()` with no args, user-stop args, sync-restart args, and unknown args.
Backoff changes should test attempt 0, attempt 1, capped attempts, and memory cooldown integer expectations.
Matrix readiness refactors need async runtime tests for successful `/versions`, transport errors, non-Matrix responses, timeout, SSL verify resolution, and CLI tests for fixed-timeout failure behavior.
Detached task helper changes risk hiding shutdown failures or introducing retained task references, so test cancellation-time failures and normal completion cleanup.
Entity display-name helper changes should cover router, agent, team, and unknown entity names.
