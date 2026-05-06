## Summary

Top duplication candidate: `create_background_task` and `create_logged_task` both create detached asyncio tasks and attach a completion callback that consumes `task.result()` and logs non-cancellation failures.
Related local task registries exist in `stop.py`, `voice_handler.py`, `knowledge/refresh_scheduler.py`, and `scheduling.py`, but they preserve domain-specific indexing, pending queues, or shutdown semantics and are not clear candidates for broad generalization.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
create_background_task	function	lines 21-72	duplicate-found	create_task add_done_callback task.result background task strong reference logging owner	src/mindroom/orchestration/runtime.py:276; src/mindroom/orchestration/runtime.py:295; src/mindroom/stop.py:119; src/mindroom/voice_handler.py:174; src/mindroom/knowledge/refresh_scheduler.py:161; src/mindroom/scheduling.py:317
create_background_task.<locals>._task_done_callback	nested_function	lines 52-69	duplicate-found	task.result CancelledError logger.exception done callback discard cleanup	src/mindroom/orchestration/runtime.py:276; src/mindroom/stop.py:114; src/mindroom/voice_handler.py:186; src/mindroom/knowledge/refresh_scheduler.py:163
_tasks_for_owner	function	lines 75-78	related-only	owner scoped task registry filter background_task_owners	src/mindroom/matrix/cache/write_coordinator.py:566; src/mindroom/matrix/cache/write_coordinator.py:878; src/mindroom/bot.py:1231; src/mindroom/knowledge/refresh_scheduler.py:113
_cancel_and_drain_background_tasks	async_function	lines 81-99	related-only	cancel gather return_exceptions drain shutdown bounded cancellation	src/mindroom/bot.py:1289; src/mindroom/bot.py:1301; src/mindroom/scheduling.py:423; src/mindroom/knowledge/refresh_scheduler.py:110; src/mindroom/orchestration/runtime.py:257
wait_for_background_tasks	async_function	lines 102-136	related-only	wait background tasks timeout pending cancel drain owner	src/mindroom/bot.py:1231; src/mindroom/matrix/cache/write_coordinator.py:878; src/mindroom/scheduling.py:423; src/mindroom/knowledge/refresh_scheduler.py:110
_get_background_task_count	function	lines 139-141	none-found	background task count len registry active tasks count	none
```

## Findings

### 1. Detached task creation and exception logging are duplicated

`src/mindroom/background_tasks.py:21` creates a detached task, optionally names it, registers a done callback, consumes `task.result()`, ignores `asyncio.CancelledError`, and logs other exceptions.
`src/mindroom/orchestration/runtime.py:295` creates a detached task with a required name and attaches `_log_detached_task_result`.
That callback at `src/mindroom/orchestration/runtime.py:276` performs the same core completion behavior: consume `task.result()`, ignore cancellation, and log failures.

Differences to preserve:

- `background_tasks.create_background_task` keeps a strong reference in a global registry and optionally associates an owner for scoped draining.
- `background_tasks.create_background_task` supports an `error_handler` callback and `log_exceptions=False`.
- `orchestration.runtime.create_logged_task` does not retain tasks globally, uses a caller-supplied failure message, and has special debug logging when the task is in `_CANCELLING_LOGGED_TASKS`.

This is real behavioral duplication in the task-result consumption and failure logging policy, but it is not a drop-in replacement because the lifecycle ownership differs.

### 2. Strong-reference registries recur, but each is domain-specific

`src/mindroom/background_tasks.py:47` stores tasks in `_background_tasks` and removes them in the done callback at `src/mindroom/background_tasks.py:53`.
`src/mindroom/stop.py:119` tracks cleanup tasks in `self.cleanup_tasks`, then removes finished tasks via `_discard_cleanup_task` at `src/mindroom/stop.py:114`.
`src/mindroom/voice_handler.py:172` caches one normalization task per voice cache key and removes/finalizes it through a key-aware callback at `src/mindroom/voice_handler.py:186`.
`src/mindroom/knowledge/refresh_scheduler.py:161` stores one refresh task per `KnowledgeRefreshTarget` and uses a key-aware done callback at `src/mindroom/knowledge/refresh_scheduler.py:163`.

These all use the same asyncio pattern of strong reference plus done callback cleanup, but their indexing semantics differ.
The background task registry tracks anonymous tasks with optional owner scope, while the voice and knowledge registries coalesce work by cache key or refresh target.
No shared registry abstraction is recommended unless another refactor already needs common keyed task lifecycle semantics.

### 3. Cancellation and drain loops are related, not identical

`src/mindroom/background_tasks.py:81` repeatedly cancels a scoped set of background tasks and drains them with `asyncio.gather(..., return_exceptions=True)`.
`src/mindroom/scheduling.py:423` cancels all scheduled tasks once, deletes them from `_running_tasks`, gathers them, and returns the cancellation count.
`src/mindroom/knowledge/refresh_scheduler.py:110` cancels refresh tasks after clearing scheduler state and suppresses task errors one by one.
`src/mindroom/bot.py:1289` and `src/mindroom/bot.py:1301` cancel single bot-owned tasks and gather them.

These flows are related shutdown idioms, but the state mutation and retry behavior are different enough that a shared cancellation helper would likely obscure domain behavior.

## Proposed Generalization

A minimal refactor could extract only the task-result logging callback shape, for example a private helper in `src/mindroom/background_tasks.py` or a small `src/mindroom/task_results.py` helper:

1. Add a helper that consumes a completed task result, ignores `asyncio.CancelledError`, and logs non-cancellation exceptions through a supplied logger/message callback.
2. Use it from `create_background_task._task_done_callback` while keeping registry cleanup, owner cleanup, `error_handler`, and `log_exceptions` in `background_tasks.py`.
3. Use it from `orchestration.runtime._log_detached_task_result` while preserving `_CANCELLING_LOGGED_TASKS` handling and the caller-supplied failure message.
4. Leave keyed registries and cancellation drains unchanged.

No broader refactor is recommended.

## Risk/tests

Risk is low if only the common `task.result()`/`CancelledError`/exception logging helper is extracted, but logging behavior is easy to regress.
Tests should cover successful task completion, cancellation, exception logging, disabled logging in `create_background_task`, `error_handler` invocation and failure logging, and owner-scoped `wait_for_background_tasks` behavior.
Existing orchestration runtime tests should also verify that cancellation-marked detached task failures still use the debug path.

## Questions or assumptions

Assumption: this audit is report-only, so no production code was edited.
