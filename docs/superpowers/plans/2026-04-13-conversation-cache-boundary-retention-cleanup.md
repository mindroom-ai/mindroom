# Conversation Cache Boundary And Retention Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `MatrixConversationCache` the only outbound thread-fallback owner, reset in-memory thread runtime state across all restart paths, and add explicit retention policy for process-local generations and durable unmatched lookup repairs.

**Architecture:** Keep `_event_cache.py` as the durable store and `thread_cache.py` as the process-local reuse layer, but stop letting lower-level client helpers compute outbound thread fallback on their own. Tighten lifecycle so process-local state is always cleared on bot stop, and bound long-lived correctness metadata with explicit pruning rules instead of accidental forever-retention.

**Tech Stack:** Python 3.13, asyncio, matrix-nio, SQLite, pytest, Ruff, pre-commit.

---

### Task 1: Plan The Boundary Enforcement

**Files:**
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/custom_tools/matrix_message.py`
- Modify: `src/mindroom/matrix/client.py`
- Test: `tests/test_send_file_message.py`
- Test: `tests/test_thread_history.py`

- [ ] **Step 1: Write the failing tests**

Add or update tests so threaded edit and first-attachment send paths require a precomputed `latest_thread_event_id` from `MatrixConversationCache` rather than resolving it inside low-level client helpers.

- [ ] **Step 2: Run the targeted tests to verify the old boundary still leaks through**

Run: `uv run pytest -n auto --no-cov tests/test_send_file_message.py tests/test_thread_history.py -q`

Expected: Existing tests fail or need updating because `send_file_message` / `build_threaded_edit_content` still resolve fallback internally.

- [ ] **Step 3: Implement the boundary cleanup**

Change `send_file_message` and `build_threaded_edit_content` so they no longer fetch latest thread fallback via low-level client helpers when a thread is present.
Make callers precompute fallback through `ConversationCacheProtocol`.
Keep room-level and non-threaded behavior unchanged.

- [ ] **Step 4: Re-run the targeted tests**

Run: `uv run pytest -n auto --no-cov tests/test_send_file_message.py tests/test_thread_history.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/matrix/conversation_cache.py src/mindroom/delivery_gateway.py src/mindroom/custom_tools/matrix_message.py src/mindroom/matrix/client.py tests/test_send_file_message.py tests/test_thread_history.py
git commit -m "refactor: route outbound thread fallback through conversation cache"
```

### Task 2: Reset Runtime State On Every Bot Stop

**Files:**
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/orchestrator.py`
- Test: `tests/test_threading_error.py`
- Test: `tests/test_multi_agent_bot.py`

- [ ] **Step 1: Write the failing tests**

Add coverage proving that injected/orchestrator-managed bots clear `MatrixConversationCache` process-local state on stop, not just standalone-owned runtime support.
Prefer one direct bot test and one orchestrator-restart-oriented test if needed.

- [ ] **Step 2: Run the targeted tests to verify the stale restart seam**

Run: `uv run pytest -n auto --no-cov tests/test_threading_error.py tests/test_multi_agent_bot.py -q`

Expected: New restart-state assertions fail before the lifecycle fix.

- [ ] **Step 3: Implement the lifecycle reset**

Move or duplicate `reset_runtime_state()` so every bot stop path clears in-memory conversation-cache state regardless of whether runtime support is standalone-owned or injected.
Do not close injected shared services from the bot.
Only clear process-local reuse state.

- [ ] **Step 4: Re-run the targeted tests**

Run: `uv run pytest -n auto --no-cov tests/test_threading_error.py tests/test_multi_agent_bot.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/bot.py src/mindroom/orchestrator.py tests/test_threading_error.py tests/test_multi_agent_bot.py
git commit -m "fix: clear conversation cache runtime state on bot shutdown"
```

### Task 3: Add Explicit Retention Policy

**Files:**
- Modify: `src/mindroom/matrix/thread_cache.py`
- Modify: `src/mindroom/matrix/_event_cache.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Test: `tests/test_threading_error.py`
- Test: `tests/test_event_cache.py`

- [ ] **Step 1: Write the failing tests**

Replace tests that currently lock in forever-retained generation state.
Add tests covering:
- generation pruning once a thread has no cached entry and no active lock
- stale unmatched `pending_lookup_repairs` aging out
- matched repairs still promoting/clearing correctly

- [ ] **Step 2: Run the targeted tests to verify the current retention bugs**

Run: `uv run pytest -n auto --no-cov tests/test_threading_error.py tests/test_event_cache.py -q`

Expected: New retention assertions fail before implementation.

- [ ] **Step 3: Implement the retention policy**

Prune per-thread generation state when a thread has no cached resolved entry and no active lock.
Keep generation monotonic globally so reused threads still get fresh tokens.
Add timestamp-based pruning for unmatched `pending_lookup_repairs` in `_event_cache.py`.
Trigger pruning from mark/query paths so the table cannot grow forever from unrecoverable misses.

- [ ] **Step 4: Re-run the targeted tests**

Run: `uv run pytest -n auto --no-cov tests/test_threading_error.py tests/test_event_cache.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/matrix/thread_cache.py src/mindroom/matrix/_event_cache.py src/mindroom/matrix/conversation_cache.py tests/test_threading_error.py tests/test_event_cache.py
git commit -m "fix: bound thread freshness and lookup repair retention"
```

### Task 4: Final Verification

**Files:**
- Modify: any touched files from Tasks 1-3

- [ ] **Step 1: Run Ruff and hooks**

Run: `uv run pre-commit run --all-files`

Expected: PASS.

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest -n auto --no-cov -q`

Expected: PASS.

- [ ] **Step 3: Create the final commit**

```bash
git add <touched files explicitly>
git commit -m "refactor: enforce conversation cache threading boundaries"
```
