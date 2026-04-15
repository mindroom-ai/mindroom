# Plain Reply Thread Inheritance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep bridged or degraded plain replies inside an already explicit thread through single-hop inheritance without restoring deep reply-chain inference.

**Architecture:** Add one-hop thread inheritance in `ConversationResolver`, persist promoted event-to-thread membership in the cache, and reuse the same rule in thread normalization helpers. Do not recurse past the direct reply target, and fail open to room-level behavior when the target cannot be classified.

**Tech Stack:** Python 3.13, Matrix nio, existing `MatrixConversationCache`, SQLite event cache, pytest, pre-commit.

---

## File Map

- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/cache/event_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_writes.py`
- Modify: `src/mindroom/thread_tags.py`
- Modify: `src/mindroom/custom_tools/thread_tags.py`
- Modify: `src/mindroom/custom_tools/thread_summary.py`
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_thread_mode.py`
- Modify: `tests/test_thread_tags_tool.py`
- Modify: `tests/test_thread_summary_tool.py`

### Task 1: Freeze The New Compatibility Rule In Tests

**Files:**
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_thread_mode.py`
- Modify: `tests/test_thread_tags_tool.py`
- Modify: `tests/test_thread_summary_tool.py`

- [ ] **Step 1: Write a failing resolver test for single-hop inheritance**

Add a test proving that a plain `m.in_reply_to` reply to an event already in explicit thread `T` resolves as threaded with `thread_id == T`.

- [ ] **Step 2: Write a failing resolver test for room-level fallback**

Add a test proving that a plain reply to a normal room message stays room-level.

- [ ] **Step 3: Write a failing history test for promoted replies**

Add a test proving that after MindRoom promotes a plain reply into thread `T`, the next thread read can still see that promoted reply.

- [ ] **Step 4: Write failing tool tests for tags and summaries**

Add regressions proving that `thread_tags` and `thread_summary` can operate on a plain reply whose direct target already belongs to explicit thread `T`.

- [ ] **Step 5: Run the focused tests to verify failure**

Run: `.venv/bin/pytest tests/test_threading_error.py tests/test_thread_mode.py tests/test_thread_tags_tool.py tests/test_thread_summary_tool.py -k 'plain reply or single hop or thread tags or thread summary' -x -n 0 --no-cov -q`

Expected: FAIL on the current explicit-thread-only behavior.

- [ ] **Step 6: Commit the failing-test checkpoint**

```bash
git add tests/test_threading_error.py tests/test_thread_mode.py tests/test_thread_tags_tool.py tests/test_thread_summary_tool.py
git commit -m "test: lock plain reply thread inheritance"
```

### Task 2: Add Single-Hop Inheritance To Conversation Resolution

**Files:**
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_thread_mode.py`

- [ ] **Step 1: Extend explicit thread resolution**

Teach `_explicit_thread_id_for_event(...)` to inspect `reply_to_event_id` when there is no explicit thread on the current event.
Fetch only the direct reply target.
If that target already belongs to thread `T`, return `T`.

- [ ] **Step 2: Keep the rule single-hop and fail-open**

Do not recurse past the direct reply target.
If the target cannot be fetched or its thread membership cannot be determined, return `None`.

- [ ] **Step 3: Preserve room-mode behavior**

Room mode should still stay room-level.
The new inheritance rule should only apply when the resolver is actually allowed to use thread context.

- [ ] **Step 4: Run the focused resolver tests**

Run: `.venv/bin/pytest tests/test_threading_error.py tests/test_thread_mode.py -k 'plain reply or single hop or room mode' -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 5: Commit the resolver checkpoint**

```bash
git add src/mindroom/conversation_resolver.py src/mindroom/matrix/conversation_cache.py tests/test_threading_error.py tests/test_thread_mode.py
git commit -m "fix: inherit explicit thread for plain replies"
```

### Task 3: Persist Promoted Membership For Later Thread Reads

**Files:**
- Modify: `src/mindroom/matrix/cache/event_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_writes.py`
- Modify: `tests/test_threading_error.py`

- [ ] **Step 1: Define the minimal cache invariant**

When MindRoom knows an event belongs to thread `T`, persist the event-to-thread mapping even if the event itself was only a plain reply.

- [ ] **Step 2: Persist promoted membership on live and sync writes**

Update the live and sync thread write paths so a plain reply to an already-threaded event records `event_id -> T` instead of staying room-level.
Do not mark unrelated room threads stale when the reply target is known to be non-threaded.

- [ ] **Step 3: Reuse the invariant on later reads**

Ensure later thread snapshots and full history reads can see promoted replies through the existing event-thread lookup paths.

- [ ] **Step 4: Run focused cache and history tests**

Run: `.venv/bin/pytest tests/test_threading_error.py -k 'promoted reply or thread history or live event or sync timeline' -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 5: Commit the cache checkpoint**

```bash
git add src/mindroom/matrix/cache/event_cache.py src/mindroom/matrix/cache/thread_writes.py tests/test_threading_error.py
git commit -m "fix: persist promoted plain replies in thread cache"
```

### Task 4: Align Thread Tags And Summaries

**Files:**
- Modify: `src/mindroom/thread_tags.py`
- Modify: `src/mindroom/custom_tools/thread_tags.py`
- Modify: `src/mindroom/custom_tools/thread_summary.py`
- Modify: `tests/test_thread_tags_tool.py`
- Modify: `tests/test_thread_summary_tool.py`

- [ ] **Step 1: Reuse single-hop normalization for tool targets**

Update thread-root normalization so a plain reply to an event already in thread `T` resolves to `T`.
Do not recurse further than the direct reply target.

- [ ] **Step 2: Keep plain room replies room-level**

If the direct target is not in a thread, thread tags and summaries should still reject the request as non-threaded.

- [ ] **Step 3: Run focused tool tests**

Run: `.venv/bin/pytest tests/test_thread_tags_tool.py tests/test_thread_summary_tool.py -k 'plain reply or single hop or thread root' -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 4: Commit the tool checkpoint**

```bash
git add src/mindroom/thread_tags.py src/mindroom/custom_tools/thread_tags.py src/mindroom/custom_tools/thread_summary.py tests/test_thread_tags_tool.py tests/test_thread_summary_tool.py
git commit -m "fix: align thread tools with plain reply inheritance"
```

### Task 5: Full Verification And Final Cleanup

**Files:**
- Modify: exact files touched above only

- [ ] **Step 1: Run the combined targeted suite**

Run: `.venv/bin/pytest tests/test_threading_error.py tests/test_thread_mode.py tests/test_thread_tags_tool.py tests/test_thread_summary_tool.py -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 2: Run targeted pre-commit**

Run: `.venv/bin/pre-commit run --files src/mindroom/conversation_resolver.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/event_cache.py src/mindroom/matrix/cache/thread_writes.py src/mindroom/thread_tags.py src/mindroom/custom_tools/thread_tags.py src/mindroom/custom_tools/thread_summary.py tests/test_threading_error.py tests/test_thread_mode.py tests/test_thread_tags_tool.py tests/test_thread_summary_tool.py`

Expected: PASS

- [ ] **Step 3: Commit the final checkpoint**

```bash
git add src/mindroom/conversation_resolver.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/event_cache.py src/mindroom/matrix/cache/thread_writes.py src/mindroom/thread_tags.py src/mindroom/custom_tools/thread_tags.py src/mindroom/custom_tools/thread_summary.py tests/test_threading_error.py tests/test_thread_mode.py tests/test_thread_tags_tool.py tests/test_thread_summary_tool.py
git commit -m "fix: inherit explicit threads across plain replies"
```
