# Matrix Conversation Cache Response-Path Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify Matrix reply generation so each reply uses one authoritative post-lock thread-history read through `MatrixConversationCache` while keeping the durable and cross-turn caches that are already necessary.

**Architecture:** `MatrixConversationCache` remains the one public conversation-data boundary.
Durable event truth, durable repair obligations, and sync-populated raw thread state stay in `_event_cache.py`.
Response generation stops trusting pre-lock `thread_history`, and every freshness-affecting live write must become visible behind the same room-ordered barrier that post-lock thread reads trust.

**Tech Stack:** Python 3.13, asyncio, SQLite, matrix-nio, pytest, Ruff, pre-commit.

---

## Status

This plan intentionally supersedes the extraction-first plan in `docs/superpowers/plans/2026-04-13-matrix-conversation-cache-architecture.md`.
The approved spec for this work is `docs/superpowers/specs/2026-04-13-matrix-conversation-cache-response-path-simplification-design.md`.
The priority is to simplify the reply-path invariant first.
Only after that lands should we reconsider any file-splitting refactor.

## Non-Goals

- Do not remove the durable Matrix event cache.
- Do not remove cache-aware latest-thread-event lookup for outbound MSC3440 fallback.
- Do not remove cross-turn resolved thread reuse.
- Do not delete `get_thread_snapshot()` everywhere in this plan.
- Do not do `thread_reads.py` or `thread_writes.py` extraction in this plan.

## Success Criteria

- Normal agent and team reply generation do one authoritative full-thread read after the lifecycle lock.
- `ResponseRunner` no longer relies on `is_thread_history_current()` to reuse a pre-lock `ThreadHistoryResult`.
- Normal dispatch no longer hydrates full thread history before the lock just to feed the eventual reply.
- Freshness-affecting live writes become visible after the same room-idle barrier the post-lock read trusts.
- The full test suite passes with `-n auto`.

## File Map

### Core behavior

- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/_event_cache_write_coordinator.py`

### Supporting tests

- Modify: `tests/test_queued_message_notify.py`
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_thread_mode.py`
- Modify: any other directly affected tests that currently assert pre-lock hydration or currentness-shortcut behavior

### Docs

- Modify: `docs/superpowers/plans/2026-04-13-matrix-conversation-cache-architecture.md`
- Modify: `docs/superpowers/specs/2026-04-13-matrix-conversation-cache-response-path-simplification-design.md` only if implementation details require a small correction

## Task 1: Lock The New Reply-Path Contract In Tests

**Files:**
- Modify: `tests/test_queued_message_notify.py`
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `tests/test_threading_error.py`

- [ ] **Step 1: Add a failing unit test for post-lock refresh behavior**

Add a test around `ResponseRunner._refresh_thread_history_after_lock()` that proves:
- if `thread_id` is present, the helper fetches full thread history through the resolver after lock,
- it does not short-circuit on `ThreadHistoryResult.thread_version`,
- and the returned request always carries the refreshed history.

- [ ] **Step 2: Add a failing reply-path integration test**

Add or update a `ResponseRunner` or `AgentBot` test proving that:
- request construction may carry snapshot or stale history,
- the locked reply path overwrites that history before prompt/model preparation,
- and the model sees the post-lock history rather than the stale pre-lock copy.

- [ ] **Step 3: Add a failing barrier-visibility test for live repair writes**

Add a test in `tests/test_threading_error.py` that proves:
- a live lookup-repair or live append/redaction mutation is not considered visible until the room write queue drains,
- and the post-lock read no longer trusts stale history during that window.

- [ ] **Step 4: Run the new focused tests**

Run: `uv run pytest -n auto --no-cov tests/test_queued_message_notify.py tests/test_multi_agent_bot.py tests/test_threading_error.py -q`

Expected: FAIL on the new assertions.

- [ ] **Step 5: Commit the characterization tests**

```bash
git add tests/test_queued_message_notify.py tests/test_multi_agent_bot.py tests/test_threading_error.py
git commit -m "test: lock simplified conversation cache reply path"
```

## Task 2: Remove The Post-Lock Currentness Shortcut

**Files:**
- Modify: `src/mindroom/response_runner.py`
- Modify: `tests/test_queued_message_notify.py`

- [ ] **Step 1: Replace the skip logic in `_refresh_thread_history_after_lock()`**

Change `_refresh_thread_history_after_lock()` so it:
- returns early only when `thread_id` is `None`,
- otherwise always calls `resolver.fetch_thread_history(...)`,
- and replaces `request.thread_history` with that authoritative post-lock result.

Do not call `conversation_cache.is_thread_history_current()` from this helper anymore.

- [ ] **Step 2: Update locked reply paths to rely on the refreshed request**

Keep `generate_response_locked()` and `generate_team_response_helper_locked()` using the refreshed request.
Do not add a second refresh elsewhere in those paths.

- [ ] **Step 3: Remove or rewrite tests that assert the old shortcut**

Tests that currently expect:
- "skip when thread version is unchanged",
- "reuse stale request history if currentness passes",

must be rewritten to assert the new single authoritative post-lock fetch instead.

- [ ] **Step 4: Run the focused reply-runner tests**

Run: `uv run pytest -n auto --no-cov tests/test_queued_message_notify.py tests/test_multi_agent_bot.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the shortcut removal**

```bash
git add src/mindroom/response_runner.py tests/test_queued_message_notify.py tests/test_multi_agent_bot.py
git commit -m "refactor: always refresh thread history after lifecycle lock"
```

## Task 3: Stop Pre-Lock Full-History Hydration For Normal Replies

**Files:**
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `tests/test_thread_mode.py`
- Modify: `tests/test_multi_agent_bot.py`

- [ ] **Step 1: Identify the pre-lock hydration calls that only serve reply generation**

In `TurnController`, the `hydrate_dispatch_context(...)` calls before payload building and plan execution are the main targets.
Keep pre-lock context extraction for routing, replay guards, command handling, and targeting.
Do not keep pre-lock full-history hydration merely to prepare the eventual response request.

- [ ] **Step 2: Narrow `MessageContext` usage for normal dispatch**

Adjust `ConversationResolver` and `TurnController` so normal dispatch can carry lightweight thread context before lock.
A pre-lock snapshot or lightweight thread context is acceptable for routing and replay-guard purposes.
A full authoritative thread history should not be required before entering the locked response path.

- [ ] **Step 3: Preserve non-reply callers that still genuinely need full history**

Do not break interactive selection, explicit `fetch_thread_history()` callers, or command flows that intentionally ask for full history outside the normal reply path.
Only remove pre-lock full-history hydration where it exists solely to feed the later reply.

- [ ] **Step 4: Update tests for the new dispatch contract**

Rewrite tests that currently assert:
- extract-dispatch-context eagerly upgrades to full history before reply generation,
- `hydrate_dispatch_context()` is called as part of normal locked reply preparation,

so they instead assert that:
- pre-lock dispatch stays lightweight,
- and the authoritative history is loaded after the lock.

- [ ] **Step 5: Run the focused dispatch tests**

Run: `uv run pytest -n auto --no-cov tests/test_thread_mode.py tests/test_multi_agent_bot.py tests/test_queued_message_notify.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the pre-lock simplification**

```bash
git add src/mindroom/conversation_resolver.py src/mindroom/turn_controller.py tests/test_thread_mode.py tests/test_multi_agent_bot.py tests/test_queued_message_notify.py
git commit -m "refactor: keep reply dispatch context lightweight before lock"
```

## Task 4: Put Live Freshness-Affecting Writes Behind One Room Barrier

**Files:**
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/_event_cache_write_coordinator.py`
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_bot_ready_hook.py`

- [ ] **Step 1: Fix `wait_for_room_idle()` so it cannot livelock on a completed tail task**

After awaiting the current tail task, explicitly re-check whether that same task is still the room tail and clear or advance it if necessary.
Do not rely solely on the done callback to make progress.

- [ ] **Step 2: Route durable lookup-repair writes through `_queue_room_cache_update()`**

Change `_mark_lookup_repair_pending()` so the durable `_event_cache` write is enqueued through the same room coordinator used by other freshness-affecting writes.
Do not leave direct `event_cache.mark_pending_lookup_repair(...)` calls in live failure paths.

- [ ] **Step 3: Fold live append/redaction finalization into the queued room update**

The room queue must order:
- raw event-cache mutation,
- durable repair-write visibility,
- and in-memory resolved-thread invalidation or version bump.

Implement the live append and live redaction paths so the mutation outcome and thread-cache finalization become visible as one ordered room update rather than two separately observable steps.

- [ ] **Step 4: Add or update tests for barrier visibility**

Prove that:
- `wait_for_room_idle()` returns reliably,
- stale history is not accepted during the delayed-finalization window,
- and live lookup-repair writes are visible after the room barrier the reply-path read trusts.

- [ ] **Step 5: Run the focused cache-coherence tests**

Run: `uv run pytest -n auto --no-cov tests/test_threading_error.py tests/test_bot_ready_hook.py tests/test_queued_message_notify.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the room-barrier fix**

```bash
git add src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/_event_cache_write_coordinator.py tests/test_threading_error.py tests/test_bot_ready_hook.py tests/test_queued_message_notify.py
git commit -m "fix: serialize live cache freshness through room barriers"
```

## Task 5: Clean Up Plans And Docs Around The Simplified Contract

**Files:**
- Modify: `docs/superpowers/plans/2026-04-13-matrix-conversation-cache-architecture.md`
- Modify: `docs/superpowers/specs/2026-04-13-matrix-conversation-cache-response-path-simplification-design.md` only if needed

- [ ] **Step 1: Mark the extraction-first plan as superseded**

Update the older architecture plan so it clearly says the reply-path simplification plan superseded it as the next implementation step.
Do not leave two active plans claiming different priorities.

- [ ] **Step 2: Verify the spec still matches the implemented behavior**

If implementation differs from the approved spec in any small but justified way, update the spec now.
Do not leave drift between the plan, the spec, and the code.

- [ ] **Step 3: Commit the docs cleanup**

```bash
git add docs/superpowers/plans/2026-04-13-matrix-conversation-cache-architecture.md docs/superpowers/specs/2026-04-13-matrix-conversation-cache-response-path-simplification-design.md
git commit -m "docs: align conversation cache plans with reply-path simplification"
```

## Task 6: Run The Full Regression Sweep

**Files:**
- Modify: any touched files from Tasks 1-5

- [ ] **Step 1: Run Ruff and formatting checks**

Run: `uv run pre-commit run --all-files`

Expected: PASS.

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest -n auto --no-cov -q`

Expected: PASS.

- [ ] **Step 3: Inspect git diff for accidental scope creep**

Run: `git diff --stat HEAD~1..HEAD`
Run: `git status --short`

Expected:
- only the planned files are changed,
- no unrelated fixture hacks or fallback branches were added,
- worktree is clean after the final commit.

- [ ] **Step 4: Make the final implementation commit**

```bash
git add src/mindroom/response_runner.py src/mindroom/conversation_resolver.py src/mindroom/turn_controller.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/_event_cache_write_coordinator.py tests/test_queued_message_notify.py tests/test_multi_agent_bot.py tests/test_threading_error.py tests/test_thread_mode.py tests/test_bot_ready_hook.py docs/superpowers/plans/2026-04-13-matrix-conversation-cache-architecture.md docs/superpowers/specs/2026-04-13-matrix-conversation-cache-response-path-simplification-design.md docs/superpowers/plans/2026-04-13-matrix-conversation-cache-response-path-simplification.md
git commit -m "refactor: simplify matrix conversation cache reply path"
```
