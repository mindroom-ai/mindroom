Summary: No meaningful duplication found.
`sync_bridge_state.py` is the shared source of truth for marking the runtime event loop while a synchronous Agno tool-hook bridge blocks it.
The only active writer is the sync bridge in `tool_system/tool_hooks.py`, and the only production reader is the Matrix approval transport deadlock guard.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
sync_tool_bridge_blocked_loop	function	lines 19-27	related-only	sync_tool_bridge_blocked_loop, blocked_loop, WeakSet, threading.Lock, mindroom-tool-hook-sync-bridge	src/mindroom/tool_system/tool_hooks.py:322; src/mindroom/api/config_lifecycle.py:39; src/mindroom/tool_system/skills.py:103; tests/test_tool_hooks.py:1732
is_loop_blocked_by_sync_tool_bridge	function	lines 30-33	related-only	is_loop_blocked_by_sync_tool_bridge, blocked loop check, run_coroutine_threadsafe, ToolApprovalTransportError	src/mindroom/approval_transport.py:116; src/mindroom/tool_system/skills.py:152; src/mindroom/approval_manager.py:526; tests/test_tool_hooks.py:1749
```

Findings:

No real duplicated behavior was found.

1. Sync tool bridge loop marking is centralized.

- `src/mindroom/sync_bridge_state.py:19` marks an `asyncio.AbstractEventLoop` in a lock-protected `WeakSet` for the duration of a context manager and always discards it in `finally`.
- `src/mindroom/tool_system/tool_hooks.py:322` is the only production caller that writes this marker while joining the `mindroom-tool-hook-sync-bridge` worker thread.
- `tests/test_tool_hooks.py:1732` verifies the marker is active before the worker can proceed.
- Related but different patterns exist in `src/mindroom/api/config_lifecycle.py:39`, which tracks registered FastAPI apps in a lock-protected `WeakSet`, and in `src/mindroom/tool_system/skills.py:103`, which tracks skill names whose script execution is blocked.
- These are not functional duplicates because they track different object lifetimes and enforce different policies.

2. Sync tool bridge loop reads are centralized.

- `src/mindroom/sync_bridge_state.py:30` is the only helper that checks whether a loop is currently marked as blocked by the sync tool bridge.
- `src/mindroom/approval_transport.py:116` is the only production reader; it prevents `asyncio.run_coroutine_threadsafe` from submitting Matrix approval work to a runtime loop that is currently blocked by synchronous `FunctionCall.execute()`.
- `tests/test_tool_hooks.py:1749` observes the same helper from the worker awaitable to cover the bridge marker timing.
- `src/mindroom/tool_system/skills.py:152` has a superficially similar `in set` blocked-state check, but it checks skill names, not event loops or sync-bridge deadlock state.
- `src/mindroom/approval_manager.py:526` also uses `asyncio.run_coroutine_threadsafe`, but it schedules cleanup on an owner loop and does not implement the blocked runtime-loop guard.

Proposed generalization:

No refactor recommended.
The module already provides the minimal shared abstraction for this behavior, with one writer and one reader.

Risk/tests:

- The behavior is deadlock-sensitive, so any future changes should keep `tests/test_tool_hooks.py:1732` or equivalent coverage proving the marker is set before the worker thread starts executing the deferred awaitable.
- If more code begins submitting work to the Matrix runtime loop from other threads, tests should verify those paths also consult `is_loop_blocked_by_sync_tool_bridge` when they can run during synchronous tool execution.
