# Duplication Audit: `src/mindroom/knowledge/refresh_scheduler.py`

## Summary

Top duplication candidates: `KnowledgeRefreshScheduler.is_refreshing` duplicates the target-resolution/status wrapper already provided by `refresh_runner.is_refresh_active_for_binding`, and the scheduler's "one active task per key plus one pending rerun" lifecycle is functionally repeated in `MCPManager._schedule_refresh_task`.
The remaining symbols are either data containers, thin public wrappers around scheduler internals, or knowledge-specific calls with only related task-management patterns elsewhere.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_ScheduledRefresh	class	lines 31-35	not-a-behavior-symbol	_ScheduledRefresh dataclass base_id config runtime_paths execution_identity	none
KnowledgeRefreshScheduler	class	lines 39-189	duplicate-found	KnowledgeRefreshScheduler per key task pending stale refresh scheduler shutdown create_task done_callback	src/mindroom/mcp/manager.py:391, src/mindroom/mcp/manager.py:415, src/mindroom/mcp/manager.py:417, src/mindroom/background_tasks.py:21, src/mindroom/orchestration/runtime.py:295
KnowledgeRefreshScheduler.schedule_refresh	method	lines 46-60	related-only	schedule_refresh refresh_scheduler.schedule_refresh background refresh knowledge base callers	src/mindroom/api/knowledge.py:147, src/mindroom/api/knowledge.py:164, src/mindroom/knowledge/watch.py:222, src/mindroom/knowledge/watch.py:265, src/mindroom/knowledge/utils.py:297
KnowledgeRefreshScheduler.is_refreshing	method	lines 62-81	duplicate-found	is_refreshing resolve_refresh_target is_refresh_active is_refresh_active_for_binding ValueError false	src/mindroom/knowledge/refresh_runner.py:177, src/mindroom/api/knowledge.py:264, src/mindroom/knowledge/utils.py:280, src/mindroom/knowledge/utils.py:306
KnowledgeRefreshScheduler.refresh_now	async_method	lines 83-108	related-only	refresh_now refresh_knowledge_binding force_reindex pending pop direct refresh API reindex	src/mindroom/api/knowledge.py:690, src/mindroom/api/knowledge.py:697, src/mindroom/api/knowledge.py:706, src/mindroom/knowledge/refresh_runner.py:442
KnowledgeRefreshScheduler.shutdown	async_method	lines 110-120	related-only	shutdown cancel tasks clear pending suppress CancelledError watcher shutdown manager shutdown	src/mindroom/knowledge/watch.py:171, src/mindroom/mcp/manager.py:118, src/mindroom/background_tasks.py:81, src/mindroom/orchestrator.py:1656
KnowledgeRefreshScheduler._schedule	method	lines 122-154	duplicate-found	_schedule key in tasks pending request stale reschedule refresh_task done	src/mindroom/mcp/manager.py:391, src/mindroom/mcp/manager.py:395, src/mindroom/mcp/manager.py:416, src/mindroom/mcp/manager.py:417
KnowledgeRefreshScheduler._start_task	method	lines 156-163	related-only	_start_task running loop create_task name add_done_callback mark active create_logged_task background_task	src/mindroom/orchestration/runtime.py:295, src/mindroom/background_tasks.py:21, src/mindroom/mcp/manager.py:419
KnowledgeRefreshScheduler._handle_done	method	lines 165-181	duplicate-found	_handle_done task.result exception pending pop start next stale finally refresh_task none schedule again	src/mindroom/mcp/manager.py:407, src/mindroom/mcp/manager.py:415, src/mindroom/mcp/manager.py:417, src/mindroom/background_tasks.py:52
KnowledgeRefreshScheduler._run_refresh	async_method	lines 183-189	related-only	refresh_knowledge_binding_in_subprocess background scheduled refresh subprocess direct refresh	src/mindroom/knowledge/refresh_runner.py:198, src/mindroom/knowledge/refresh_runner.py:442, src/mindroom/api/knowledge.py:706
_running_loop_for_schedule	function	lines 192-197	related-only	get_running_loop RuntimeError warning create_task without running loop	src/mindroom/background_tasks.py:21, src/mindroom/orchestration/runtime.py:295, src/mindroom/custom_tools/browser.py:276
```

## Findings

### 1. Refresh status resolution is duplicated

`src/mindroom/knowledge/refresh_scheduler.py:62` and `src/mindroom/knowledge/refresh_runner.py:177` perform the same behavior: resolve a `base_id` to a `KnowledgeRefreshTarget` with `create=False`, return `False` on `ValueError`, then call `is_refresh_active(key)`.
The scheduler method exists so callers do not need to import runner internals, but the implementation is a direct duplicate of the existing public helper.

Differences to preserve: `KnowledgeRefreshScheduler.is_refreshing` is the scheduler-facing API used by API and availability code, while `is_refresh_active_for_binding` is the fallback when no scheduler is available.

### 2. Keyed coalesced background refresh lifecycle is repeated

`src/mindroom/knowledge/refresh_scheduler.py:122` through `src/mindroom/knowledge/refresh_scheduler.py:181` maintains one active task per key and stores one pending request if another schedule arrives while the task is active.
`src/mindroom/mcp/manager.py:391` through `src/mindroom/mcp/manager.py:419` implements the same lifecycle for MCP catalog refreshes: skip scheduling during shutdown, do not start a second task while one is active, mark the state stale, clear the task on completion, and schedule another refresh if stale work arrived during the active run.

Differences to preserve: the knowledge scheduler keeps external activity counters via `mark_refresh_active` and `mark_refresh_inactive`, deep-copies `Config` into `_ScheduledRefresh`, resolves physical knowledge targets before scheduling, and logs task failures in a done callback.
The MCP manager stores the pending bit on `state.stale`, wraps refresh errors inside the coroutine with warning-level logging, and refreshes a mutable server state object.

### 3. Detached task cleanup/error handling is related but not a direct duplicate

`src/mindroom/knowledge/refresh_scheduler.py:156` through `src/mindroom/knowledge/refresh_scheduler.py:181`, `src/mindroom/background_tasks.py:21` through `src/mindroom/background_tasks.py:72`, and `src/mindroom/orchestration/runtime.py:295` through `src/mindroom/orchestration/runtime.py:304` all create detached tasks with strong references and completion callbacks that drain exceptions.
This is related task-management behavior, but not a refactor target for this primary file because the knowledge scheduler's callback also drives per-key active-state cleanup and pending reruns.

## Proposed Generalization

For the refresh status duplicate, change `KnowledgeRefreshScheduler.is_refreshing` to delegate to `is_refresh_active_for_binding`.
That keeps the scheduler API stable and removes the repeated `resolve_refresh_target` plus `ValueError` wrapper.

For the keyed coalesced lifecycle, no immediate production refactor is recommended from this audit alone.
If the same "one active task per key plus one pending rerun" pattern appears in a third active subsystem, introduce a small generic keyed coalescing runner, likely under `src/mindroom/background_tasks.py` or a focused runtime helper module, parameterized by key, task name, coroutine factory, shutdown flag, and completion error handling.

## Risk/tests

Delegating `is_refreshing` to `is_refresh_active_for_binding` is low risk; tests should cover invalid base IDs and active scheduled refresh visibility.
Generalizing the coalesced scheduler lifecycle would be higher risk because it must preserve knowledge refresh active counters, pending request replacement semantics, MCP stale-flag behavior, task exception logging level, shutdown cancellation, and rerun-after-completion behavior.
Relevant tests include `tests/test_knowledge_manager.py` refresh scheduler tests around independent per-binding tasks, duplicate schedule coalescing, manual reindex, shutdown, and active refresh status, plus MCP catalog refresh tests if a shared runner is introduced.
No production code was edited for this audit.
