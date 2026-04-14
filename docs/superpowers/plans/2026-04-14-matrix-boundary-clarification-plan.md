# Matrix Boundary Clarification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the remaining overloaded Matrix conversation concepts so thread identity, reply anchors, cache read modes, and outbound advisory bookkeeping each have one explicit contract.

**Architecture:** Split thread identity from reply and relation anchoring in place. Replace boolean-driven cache policy with explicit dispatch and non-dispatch read entrypoints, and make outbound cache bookkeeping genuinely detached advisory work on top of the existing room write coordinator.

**Tech Stack:** Python 3.13, Matrix nio, existing `MatrixConversationCache`, pytest, pre-commit.

---

## File Map

- Modify: `src/mindroom/matrix/event_info.py`
- Modify: `src/mindroom/message_target.py`
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_reads.py`
- Modify: `src/mindroom/matrix/client.py`
- Modify: `src/mindroom/matrix/cache/thread_writes.py`
- Modify: `src/mindroom/hooks/sender.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/custom_tools/matrix_api.py`
- Modify: `src/mindroom/custom_tools/matrix_message.py`
- Modify: `src/mindroom/custom_tools/subagents.py`
- Modify: `src/mindroom/scheduling.py`
- Modify: `src/mindroom/thread_summary.py`
- Modify: `src/mindroom/matrix/stale_stream_cleanup.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/response_runner.py`
- Test: `tests/test_event_relations.py`
- Test: `tests/test_hook_sender.py`
- Test: `tests/test_workloop_thread_scope.py`
- Test: `tests/test_dm_functionality.py`
- Test: `tests/test_streaming_behavior.py`
- Test: `tests/test_threading_error.py`
- Test: `tests/test_thread_mode.py`
- Test: `tests/test_multi_agent_bot.py`
- Test: `tests/test_matrix_api_tool.py`
- Test: `tests/test_send_file_message.py`
- Test: `tests/test_scheduling.py`
- Test: `tests/test_workflow_scheduling.py`
- Test: `tests/test_thread_summary.py`
- Test: `tests/test_queued_message_notify.py`
- Test: `tests/test_large_messages_integration.py`
- Test: `tests/test_skip_mentions.py`
- Test: `tests/test_stale_stream_cleanup.py`
- Test: `tests/test_restore_dedup.py`
- Test: `tests/test_hook_schedule.py`

### Task 1: Freeze The Boundary Bugs In Tests

**Files:**
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_thread_mode.py`
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `tests/test_queued_message_notify.py`

- [ ] **Step 1: Write failing tests for relation-anchor leakage**

Add regressions that prove:
- an edit against a plain reply does not set `resolved_thread_id`
- a reaction against a plain reply does not set `resolved_thread_id`
- a reference against a plain reply does not set `resolved_thread_id`
- those cases do not change `session_id`

- [ ] **Step 2: Write failing tests for dispatch read strictness**

Add regressions that prove:
- dispatch preview uses a strict dispatch snapshot path
- post-lock refresh uses a strict dispatch history path
- strict dispatch reads do not return stale durable cache on homeserver failure

- [ ] **Step 3: Run the targeted tests to verify failure**

Run: `uv run pytest tests/test_threading_error.py tests/test_thread_mode.py tests/test_multi_agent_bot.py tests/test_queued_message_notify.py -k 'plain reply or reaction or reference or dispatch or stale cache or post lock' -x -n 0 --no-cov -q`

Expected: FAIL on the currently overloaded semantics.

- [ ] **Step 4: Commit the failing-test checkpoint**

```bash
git add tests/test_threading_error.py tests/test_thread_mode.py tests/test_multi_agent_bot.py tests/test_queued_message_notify.py
git commit -m "test: lock matrix boundary contracts"
```

### Task 2: Split Thread Identity From Reply And Relation Anchors

**Files:**
- Modify: `src/mindroom/matrix/event_info.py`
- Modify: `src/mindroom/message_target.py`
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `tests/test_event_relations.py`
- Modify: `tests/test_hook_sender.py`
- Modify: `tests/test_workloop_thread_scope.py`
- Modify: `tests/test_dm_functionality.py`
- Modify: `tests/test_streaming_behavior.py`
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_thread_mode.py`

- [ ] **Step 1: Remove the overloaded routing field**

Delete `safe_thread_root` from `EventInfo`.
Keep relation-target metadata only as relation metadata.
If a separate explicit root-start field is needed, add it with a name that only means “start a new thread under this root event.”

- [ ] **Step 2: Make `MessageTarget.resolve()` explicit**

Change the resolver signature so it accepts:
- explicit `thread_id`
- explicit `thread_start_root_event_id`
- `reply_to_event_id`

Do not derive `resolved_thread_id` from reply anchors or relation targets.

- [ ] **Step 3: Update `ConversationResolver.build_message_target()`**

Pass explicit thread identity only from:
- inbound explicit thread metadata, or
- explicit “start a new thread under this root event” behavior for room-root events in thread mode

Do not pass relation-target metadata into thread identity.

- [ ] **Step 4: Run focused target-resolution tests**

Run: `uv run pytest tests/test_threading_error.py tests/test_thread_mode.py tests/test_event_relations.py tests/test_hook_sender.py tests/test_workloop_thread_scope.py tests/test_dm_functionality.py tests/test_streaming_behavior.py -k 'resolved_thread_id or session_id or safe_thread_root or plain reply or reaction or reference' -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 5: Clear all direct `safe_thread_root` fallout**

Run: `rg -n "safe_thread_root=|\\.safe_thread_root\\b" src tests`

Update every remaining hit or deliberately rename it to the new explicit field before moving on.

- [ ] **Step 6: Commit the target-model checkpoint**

```bash
git add src/mindroom/matrix/event_info.py src/mindroom/message_target.py src/mindroom/conversation_resolver.py tests/test_event_relations.py tests/test_hook_sender.py tests/test_workloop_thread_scope.py tests/test_dm_functionality.py tests/test_streaming_behavior.py tests/test_threading_error.py tests/test_thread_mode.py
git commit -m "refactor: separate thread identity from reply anchors"
```

### Task 3: Replace Boolean Cache Policy With Explicit Read Entry Points

**Files:**
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_reads.py`
- Modify: `src/mindroom/matrix/client.py`
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_thread_mode.py`
- Modify: `tests/test_multi_agent_bot.py`

- [ ] **Step 1: Add explicit public read methods to `ConversationCacheProtocol`**

Expose:
- `get_thread_snapshot(...)`
- `get_thread_history(...)`
- `get_dispatch_thread_snapshot(...)`
- `get_dispatch_thread_history(...)`

Delete `allow_durable_cache` from the public cache facade and resolver call sites.

- [ ] **Step 2: Implement explicit read-mode methods in `thread_reads.py`**

Keep one small internal helper only if it removes obvious duplication.
Normal methods may use healthy durable cache and stale fallback.
Dispatch methods must not use durable cache reuse and must not return stale cache.

- [ ] **Step 3: Split client fetch paths by read mode**

Replace boolean branching in `client.py` with explicit functions or a private enum-backed helper that is not exposed at the public boundary.
Keep fail-open durable-cache read behavior for normal reads.
Keep strict no-stale behavior for dispatch reads.

- [ ] **Step 4: Update resolver and post-lock callers**

Dispatch preview and post-lock refresh must call dispatch-specific methods.
Non-dispatch callers such as tools and summaries must keep using normal advisory methods.

- [ ] **Step 5: Run focused cache-mode tests**

Run: `uv run pytest tests/test_threading_error.py tests/test_thread_mode.py tests/test_multi_agent_bot.py -k 'dispatch thread history or dispatch thread snapshot or durable cache or stale cache or post lock' -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 6: Commit the cache-mode checkpoint**

```bash
git add src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/thread_reads.py src/mindroom/matrix/client.py src/mindroom/conversation_resolver.py src/mindroom/response_runner.py tests/test_threading_error.py tests/test_thread_mode.py tests/test_multi_agent_bot.py
git commit -m "refactor: make dispatch cache reads explicit"
```

### Task 4: Make Outbound Cache Bookkeeping Truly Advisory

**Files:**
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_writes.py`
- Modify: `src/mindroom/hooks/sender.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/custom_tools/matrix_api.py`
- Modify: `src/mindroom/custom_tools/matrix_message.py`
- Modify: `src/mindroom/custom_tools/subagents.py`
- Modify: `src/mindroom/scheduling.py`
- Modify: `src/mindroom/thread_summary.py`
- Modify: `src/mindroom/matrix/stale_stream_cleanup.py`
- Modify: `src/mindroom/bot.py`
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_hook_sender.py`
- Modify: `tests/test_matrix_api_tool.py`
- Modify: `tests/test_send_file_message.py`
- Modify: `tests/test_scheduling.py`
- Modify: `tests/test_workflow_scheduling.py`
- Modify: `tests/test_thread_summary.py`
- Modify: `tests/test_large_messages_integration.py`
- Modify: `tests/test_skip_mentions.py`
- Modify: `tests/test_stale_stream_cleanup.py`
- Modify: `tests/test_restore_dedup.py`
- Modify: `tests/test_hook_schedule.py`

- [ ] **Step 1: Rename the public bookkeeping methods to notify-style names**

Change the public API to something like:
- `notify_outbound_message(...)`
- `notify_outbound_redaction(...)`

The public method should schedule work and return immediately.

- [ ] **Step 2: Detach caller paths from the room queue**

Use the existing `write_coordinator.queue_room_update(...)` scheduling behavior.
Do not await the returned task from successful send, edit, or redact paths.
Log task failures at the scheduled task boundary.

- [ ] **Step 3: Make cancellation part of fail-open behavior**

Ensure `CancelledError` from detached advisory bookkeeping is logged and swallowed just like ordinary exceptions.
Do not let caller-visible success depend on local cache bookkeeping.

- [ ] **Step 4: Rewrite every real caller in one pass**

Run: `rg -n "record_outbound_message\\(|record_outbound_redaction\\(" src tests`

Update every runtime caller and every affected test to the notify-style API before moving on.

- [ ] **Step 5: Rewrite misleading caller-side tests**

Replace tests that only assert a method was awaited.
Use mocks that actually raise `RuntimeError` and `asyncio.CancelledError`.
Assert the outward send/edit/redact result still succeeds.

- [ ] **Step 6: Run focused advisory-bookkeeping tests**

Run: `uv run pytest tests/test_threading_error.py tests/test_hook_sender.py tests/test_matrix_api_tool.py tests/test_send_file_message.py tests/test_scheduling.py tests/test_workflow_scheduling.py tests/test_thread_summary.py tests/test_large_messages_integration.py tests/test_skip_mentions.py tests/test_stale_stream_cleanup.py tests/test_restore_dedup.py tests/test_hook_schedule.py -k 'cancelled or fail open or outbound message or outbound redaction' -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 7: Commit the advisory-bookkeeping checkpoint**

```bash
git add src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/thread_writes.py src/mindroom/hooks/sender.py src/mindroom/delivery_gateway.py src/mindroom/streaming.py src/mindroom/custom_tools/matrix_api.py src/mindroom/custom_tools/matrix_message.py src/mindroom/custom_tools/subagents.py src/mindroom/scheduling.py src/mindroom/thread_summary.py src/mindroom/matrix/stale_stream_cleanup.py src/mindroom/bot.py tests/test_threading_error.py tests/test_hook_sender.py tests/test_matrix_api_tool.py tests/test_send_file_message.py tests/test_scheduling.py tests/test_workflow_scheduling.py tests/test_thread_summary.py tests/test_large_messages_integration.py tests/test_skip_mentions.py tests/test_stale_stream_cleanup.py tests/test_restore_dedup.py tests/test_hook_schedule.py
git commit -m "refactor: detach advisory matrix cache bookkeeping"
```

### Task 5: Remove Dead Names, Flags, And Branches

**Files:**
- Modify: all files touched above

- [ ] **Step 1: Delete dead names and compatibility leftovers**

Remove:
- `safe_thread_root`
- public `allow_durable_cache`
- any helper whose only job was translating between the old overloaded names and the new contracts

- [ ] **Step 2: Search for stale call sites and comments**

Run: `rg -n "safe_thread_root|allow_durable_cache|record_outbound_message|record_outbound_redaction" src tests`

Update any stale names, comments, and docstrings.

- [ ] **Step 3: Run the full targeted matrix conversation suite**

Run: `uv run pytest tests/test_threading_error.py tests/test_thread_mode.py tests/test_multi_agent_bot.py tests/test_matrix_api_tool.py tests/test_send_file_message.py tests/test_scheduling.py tests/test_workflow_scheduling.py tests/test_thread_summary.py tests/test_queued_message_notify.py -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 4: Commit the cleanup checkpoint**

```bash
git add src/mindroom/matrix/event_info.py src/mindroom/message_target.py src/mindroom/conversation_resolver.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/thread_reads.py src/mindroom/matrix/client.py src/mindroom/matrix/cache/thread_writes.py src/mindroom/delivery_gateway.py src/mindroom/streaming.py src/mindroom/custom_tools/matrix_api.py src/mindroom/scheduling.py src/mindroom/thread_summary.py src/mindroom/response_runner.py tests/test_threading_error.py tests/test_thread_mode.py tests/test_multi_agent_bot.py tests/test_matrix_api_tool.py tests/test_send_file_message.py tests/test_scheduling.py tests/test_workflow_scheduling.py tests/test_thread_summary.py tests/test_queued_message_notify.py
git commit -m "cleanup: remove overloaded matrix conversation contracts"
```

### Task 6: Full Verification

**Files:**
- Modify: none unless failures require targeted follow-up fixes

- [ ] **Step 1: Run the full backend suite**

Run: `uv run pytest -n auto --no-cov -q`

Expected: PASS

- [ ] **Step 2: Run pre-commit on all files**

Run: `uv run pre-commit run --all-files`

Expected: PASS

- [ ] **Step 3: Commit any verification-driven follow-up**

```bash
git add <exact files>
git commit -m "test: fix matrix boundary verification fallout"
```
