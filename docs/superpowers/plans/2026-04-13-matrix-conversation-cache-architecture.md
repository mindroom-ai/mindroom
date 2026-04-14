# Matrix Conversation Cache Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `mindroom.matrix.conversation_cache` the required read-through owner of Matrix conversation data, remove split cache ownership, and eliminate stale-read and duplicate-summary regressions.

**Architecture:** Persisted normalized events become the only durable conversation source of truth. `MatrixConversationCache` becomes the only public owner of cache read, write, freshness, invalidation, and repair policy. Standalone and orchestrated runtimes inject that public cache service, while private cache backend modules stay internal to `mindroom.matrix`.

**Tech Stack:** Python, asyncio, aiosqlite, matrix-nio, pytest, AsyncMock, existing Matrix cache and bot runtime abstractions.

---

## File Map

### Public boundary and policy

- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/client.py`
- Modify: `src/mindroom/matrix/thread_history_result.py`

### Runtime ownership and injection

- Modify: `src/mindroom/runtime_support.py`
- Modify: `src/mindroom/orchestrator.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/bot_runtime_view.py`
- Modify: `src/mindroom/tool_system/runtime_context.py`

### Callers that must consume the public boundary only

- Modify: `src/mindroom/api/schedules.py`
- Modify: `src/mindroom/scheduling.py`
- Modify: `src/mindroom/commands/handler.py`
- Modify: `src/mindroom/post_response_effects.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/thread_summary.py`

### Tests

- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `tests/test_scheduling.py`
- Modify: `tests/test_matrix_sync_tokens.py`
- Modify any nearby focused test modules needed for cache-init and summary-path coverage

## Task 1: Lock in required-cache runtime ownership

**Files:**
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `src/mindroom/runtime_support.py`
- Modify: `src/mindroom/orchestrator.py`
- Modify: `src/mindroom/bot.py`

- [ ] **Step 1: Write failing tests for lazy ownership and required initialization**

Add tests that prove:
- plain `AgentBot(...)` construction does not resolve cache paths eagerly
- orchestrator-managed bots receive injected shared cache support
- standalone initialization fails if required cache init fails

- [ ] **Step 2: Run the focused tests to verify they fail for the right reason**

Run: `uv run pytest tests/test_multi_agent_bot.py -x -n 0 --no-cov -v`

- [ ] **Step 3: Refactor runtime ownership**

Implement:
- `AgentBot.__init__` no longer builds standalone cache support eagerly
- standalone cache support is built only in standalone runtime initialization
- orchestrator remains the single owner for shared managed-bot cache support
- required-cache startup stays fail-fast

- [ ] **Step 4: Run the focused runtime tests to verify they pass**

Run: `uv run pytest tests/test_multi_agent_bot.py -x -n 0 --no-cov -v`

- [ ] **Step 5: Commit**

Use a focused commit after tests pass.

## Task 2: Remove private cache imports from non-matrix callers

**Files:**
- Modify: `tests/test_scheduling.py`
- Modify: `src/mindroom/api/schedules.py`
- Modify: `src/mindroom/scheduling.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`

- [ ] **Step 1: Write failing tests for API scheduling without private cache construction**

Add tests that prove the schedule update path does not instantiate `_EventCache` and does not need a concrete cache when `restart_task=False`.

- [ ] **Step 2: Run the scheduling tests to verify they fail**

Run: `uv run pytest tests/test_scheduling.py -x -n 0 --no-cov -v`

- [ ] **Step 3: Split persistence from restart behavior**

Implement:
- remove private `_EventCache` import from `api/schedules.py`
- make schedule-edit persistence use public boundary types only
- make restart logic the only place that requires a concrete/injected cache dependency

- [ ] **Step 4: Run the scheduling tests to verify they pass**

Run: `uv run pytest tests/test_scheduling.py -x -n 0 --no-cov -v`

- [ ] **Step 5: Commit**

Use a focused commit after tests pass.

## Task 3: Fix sync edit coherence and repair-required semantics

**Files:**
- Modify: `tests/test_threading_error.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/client.py`

- [ ] **Step 1: Write failing tests for sync-delivered edit invalidation**

Add tests that prove:
- sync-delivered edits that resolve thread membership through `original_event_id` bump or invalidate resolved thread state
- a second `get_thread_history()` returns edited content instead of stale content

- [ ] **Step 2: Write failing tests for sync-write failure repair**

Add tests that prove:
- after `store_events_batch()` failure on a thread-affecting update, the next `get_thread_history()` forces homeserver-backed repair
- the stale persisted thread payload is not served during the freshness window

- [ ] **Step 3: Run the focused threading tests to verify they fail**

Run: `uv run pytest tests/test_threading_error.py -x -n 0 --no-cov -v`

- [ ] **Step 4: Implement single-owner coherence rules**

Implement:
- explicit per-thread repair-required tracking inside `MatrixConversationCache`
- sync-path version bumps and invalidations only after successful persistence, or else mark thread repair-required
- sync-delivered edit handling that resolves thread membership through persisted lookup before coherence decisions
- read path that bypasses freshness suppression when repair is required

- [ ] **Step 5: Run the focused threading tests to verify they pass**

Run: `uv run pytest tests/test_threading_error.py -x -n 0 --no-cov -v`

- [ ] **Step 6: Commit**

Use a focused commit after tests pass.

## Task 4: Make freshness mean successful sync, not failed sync activity

**Files:**
- Modify: `tests/test_matrix_sync_tokens.py`
- Modify: `tests/test_threading_error.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`

- [ ] **Step 1: Write failing tests for sync-error freshness behavior**

Add tests that prove sync errors do not move the freshness clock used to suppress homeserver repair reads.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py -x -n 0 --no-cov -v`

- [ ] **Step 3: Implement the freshness fix**

Implement:
- only successful sync responses update `last_sync_activity_monotonic`
- error callbacks may keep separate liveness metrics if needed, but not freshness state

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py -x -n 0 --no-cov -v`

- [ ] **Step 5: Commit**

Use a focused commit after tests pass.

## Task 5: Centralize post-response summary policy

**Files:**
- Modify: `tests/test_thread_summary.py`
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/post_response_effects.py`
- Modify: `src/mindroom/thread_summary.py`
- Modify: `src/mindroom/response_runner.py`

- [ ] **Step 1: Write failing tests for duplicate team summary scheduling**

Add tests that prove a team reply queues exactly one summary task and goes through the normal `should_queue_thread_summary()` gate.

- [ ] **Step 2: Run the focused summary tests to verify they fail**

Run: `uv run pytest tests/test_thread_summary.py tests/test_multi_agent_bot.py -x -n 0 --no-cov -v`

- [ ] **Step 3: Remove duplicate policy owners**

Implement:
- delete the duplicate helper in `bot.py`
- route all summary threshold logic through `thread_summary.py`
- remove direct team-summary queueing from `bot.py`
- keep summary scheduling solely in post-response effects

- [ ] **Step 4: Run the focused summary tests to verify they pass**

Run: `uv run pytest tests/test_thread_summary.py tests/test_multi_agent_bot.py -x -n 0 --no-cov -v`

- [ ] **Step 5: Commit**

Use a focused commit after tests pass.

## Task 6: Remove duplicate handled-turn metadata policy

**Files:**
- Modify: `tests/test_multi_agent_bot.py` or the nearest existing post-response/turn-store test module
- Modify: `src/mindroom/post_response_effects.py`
- Modify: `src/mindroom/turn_store.py`

- [ ] **Step 1: Write failing or strengthening tests around handled-turn metadata ownership**

Add or tighten tests so one canonical implementation owns matrix run metadata for handled turns.

- [ ] **Step 2: Run the focused tests to verify they fail or expose duplicate ownership**

Run: `uv run pytest tests/test_multi_agent_bot.py -x -n 0 --no-cov -v`

- [ ] **Step 3: Remove dead or duplicate production code**

Implement:
- keep one canonical helper only
- remove unused duplicate helper
- update callers to the surviving owner

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `uv run pytest tests/test_multi_agent_bot.py -x -n 0 --no-cov -v`

- [ ] **Step 5: Commit**

Use a focused commit after tests pass.

## Task 7: Final focused regression sweep

**Files:**
- Modify only as needed from previous tasks

- [ ] **Step 1: Run the focused cache, scheduling, and summary regression suites**

Run: `uv run pytest tests/test_threading_error.py tests/test_multi_agent_bot.py tests/test_scheduling.py tests/test_thread_summary.py tests/test_matrix_sync_tokens.py -x -n 0 --no-cov -v`

- [ ] **Step 2: Fix any regressions with the same TDD loop**

Write one failing test per uncovered regression before changing production code.

- [ ] **Step 3: Run a broader sanity pass**

Run: `uv run pytest tests/test_send_file_message.py tests/test_thread_history.py tests/test_matrix_message_tool.py -x -n 0 --no-cov -v`

- [ ] **Step 4: Commit final integration fixes**

Use a focused commit after tests pass.
