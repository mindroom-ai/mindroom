# Thread Conversation Boundary Untangling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move cleanup-facing thread policy to its real owners so stale-stream cleanup consumes one clear conversation or thread seam instead of reconstructing policy from client internals.

**Architecture:** `thread_membership.py` becomes the owner of scanned-event thread ordering and classification helpers that encode canonical thread semantics. `conversation_cache.py` exposes one explicit cleanup-facing seam over that logic. `stale_stream_cleanup.py` consumes that seam and drops direct imports of `matrix/client.py` thread-policy internals.
**Tech Stack:** Python, asyncio, matrix-nio, pytest

---

## File Map

- `src/mindroom/matrix/thread_membership.py`: canonical thread identity plus scanned-event ordering or grouping helpers.
- `src/mindroom/matrix/conversation_cache.py`: conversation-facing seam used by cleanup and resolver callers.
- `src/mindroom/matrix/client.py`: Matrix fetch and visible-message reconstruction only.
- `src/mindroom/matrix/stale_stream_cleanup.py`: cleanup flow that consumes the new seam.
- `tests/test_stale_stream_cleanup.py`: regression coverage for the cleanup-facing seam.
- `tests/test_thread_history.py` or `tests/test_threading_error.py`: regression coverage for any moved ordering helper.

### Task 1: Add a Failing Cleanup-Seam Test

**Files:**
- Modify: `tests/test_stale_stream_cleanup.py`

- [ ] Write one failing test for the cleanup-facing thread seam.
- [ ] Run only that test and verify it fails for the expected missing seam or wrong owner.
- [ ] Keep the test focused on one behavior, such as latest-thread-tail selection from scanned visible messages.

### Task 2: Move Thread Policy to `thread_membership.py`

**Files:**
- Modify: `src/mindroom/matrix/thread_membership.py`
- Modify: `src/mindroom/matrix/client.py`
- Test: `tests/test_thread_history.py`
- Test: `tests/test_threading_error.py`

- [ ] Add the minimal production helper needed to make the new test pass.
- [ ] Move same-thread ordering and scanned-event classification helpers that encode thread ancestry from `matrix/client.py` into `thread_membership.py`.
- [ ] Delete the old `matrix/client.py` helpers once callers stop depending on them.
- [ ] Run the focused thread-ordering tests and verify they pass.

### Task 3: Add the Conversation-Facing Seam

**Files:**
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/conversation_resolver.py` only if needed for a calmer shared seam
- Test: `tests/test_stale_stream_cleanup.py`

- [ ] Add one explicit conversation or cleanup seam that returns canonical thread grouping or latest-thread-tail results for scanned messages.
- [ ] Keep the seam concrete and local.
- [ ] Do not add a thin service layer or duplicate wrapper surface.
- [ ] Run the seam-focused stale cleanup tests and verify they pass.

### Task 4: Refactor Stale Cleanup to Consume the Seam

**Files:**
- Modify: `src/mindroom/matrix/stale_stream_cleanup.py`
- Test: `tests/test_stale_stream_cleanup.py`

- [ ] Replace direct imports of cleanup-only `matrix/client.py` thread helpers with the new owned seam.
- [ ] Remove duplicated thread grouping and latest-thread-tail logic from `stale_stream_cleanup.py`.
- [ ] Keep requester resolution and visible-message reconstruction behavior unchanged.
- [ ] Run the stale cleanup tests and verify they pass.

### Task 5: Run Final Verification

**Files:**
- Modify: any touched files from earlier tasks
- Test: `tests/test_threading_error.py`
- Test: `tests/test_thread_history.py`
- Test: `tests/test_stale_stream_cleanup.py`

- [ ] Run `uv run pytest tests/test_stale_stream_cleanup.py -x -n 0 --no-cov -v`.
- [ ] Run `uv run pytest tests/test_thread_history.py tests/test_threading_error.py -x -n 0 --no-cov -v`.
- [ ] Run `uv run pytest tests/test_threading_error.py tests/test_thread_history.py tests/test_stale_stream_cleanup.py -x -n 0 --no-cov -v`.
- [ ] Summarize the final ownership map for thread membership, bookkeeping impact, and stale-stream cleanup integration.
