# Conversation Cache Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `MatrixConversationCache` a thinner public facade by extracting thread read policy and thread write policy while finishing the remaining authoritative read and local write-through invariants.

**Architecture:** `_event_cache.py` remains the durable owner of persisted event truth and durable repair obligations.
`thread_cache.py` remains the process-local owner of resolved-thread reuse, generation tokens, and per-thread locks.
`conversation_cache.py` becomes a coordinator that delegates read policy to `thread_reads.py` and mutation policy to `thread_writes.py` instead of owning every detail directly.
The split also closes the remaining bypasses so authoritative thread reads, local threaded sends, and local redactions all go through one repair-aware cache contract.

**Tech Stack:** Python 3.13, asyncio, SQLite, matrix-nio, pytest, Ruff, pre-commit.

---

## Status

This plan intentionally replaces the earlier umbrella plan and the completed boundary-retention cleanup plan.
The remaining work is narrower than those older documents described.
The current code already has the corrected ownership model for durable repair state, process-local reuse state, outbound fallback ownership, and restart cleanup.
The `thread_reads.py` / `thread_writes.py` decomposition step has now landed.
What remains is keeping the extracted ownership lines honest:
- authoritative thread questions must go through the repair-aware read path,
- local thread-affecting sends, edits, and redactions must go through one write-through path,
- degraded or source-less results must not become reusable authoritative cache entries.

## Non-Goals

- Do not move durable repair-required state or pending lookup repairs out of `_event_cache.py`.
- Do not move process-local generation or lock state out of `thread_cache.py`.
- Do not change raw versus visible point-event semantics.
- Do not introduce new public APIs outside the current `MatrixConversationCache` facade unless a rename is strictly needed for clarity.
- Do not split files just to increase file count.
- Do not add compatibility behavior for old on-disk Matrix event cache schemas.
  If a future schema change needs it, the stale cache should be discarded and rebuilt through normal usage rather than migrated.

## Success Criteria

- `conversation_cache.py` gets materially smaller.
- `thread_reads.py` owns thread read, reuse, and repair policy.
- `thread_writes.py` owns live and sync thread mutation policy.
- Local threaded sends and local redactions use one write-through contract.
- Ordinary room-mode edits and redactions do not create durable thread-repair garbage.
- Post-send advisory cache failures do not turn successful Matrix delivery into a raised failure.
- No new mutable state owner is introduced.
- Existing tests keep the current behavior stable.
- Net `src/` LOC stays roughly flat.

## File Map

### Public facade

- Modify: `src/mindroom/matrix/conversation_cache.py`

### New internal policy modules

- Create: `src/mindroom/matrix/thread_reads.py`
- Create: `src/mindroom/matrix/thread_writes.py`

### Existing state owners that must stay put

- Modify: `src/mindroom/matrix/thread_cache.py`
- Modify: `src/mindroom/matrix/_event_cache.py`

### Focused tests

- Modify: `tests/test_thread_history.py`
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_matrix_sync_tokens.py`
- Modify: `tests/test_matrix_api_tool.py`
- Modify: `tests/test_thread_summary.py`
- Modify: `tests/test_send_file_message.py`
- Modify: `tests/test_multi_agent_bot.py`

## Task 1: Lock Read Behavior Before Extraction

**Files:**
- Modify: `tests/test_thread_history.py`
- Modify: `tests/test_threading_error.py`

- [ ] **Step 1: Add or tighten characterization tests for thread reads**

Cover the existing public behavior of:
- `get_thread_history()`
- `get_thread_snapshot()`
- `get_latest_thread_event_id_if_needed()`
- `is_thread_history_current()`

The tests should lock in:
- cached reuse versus authoritative repair behavior,
- promotion of durable lookup-repair obligations from `_event_cache.py`,
- repair clearing only after an authoritative refill,
- post-lock currentness checks that do not trust stale pre-lock state,
- latest-thread fallback reads using the same repair-aware thread-history path.

- [ ] **Step 2: Run the focused read tests**

Run: `uv run pytest tests/test_thread_history.py tests/test_threading_error.py -x -n 0 --no-cov -v`

Expected: PASS.

- [ ] **Step 3: Commit the characterization coverage**

```bash
git add tests/test_thread_history.py tests/test_threading_error.py
git commit -m "test: lock conversation cache read behavior before extraction"
```

## Task 2: Extract Thread Read Policy

**Files:**
- Create: `src/mindroom/matrix/thread_reads.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/thread_cache.py`
- Modify: `src/mindroom/matrix/_event_cache.py`
- Modify: `tests/test_thread_history.py`
- Modify: `tests/test_threading_error.py`

- [ ] **Step 1: Introduce a focused internal read-policy module**

Create `thread_reads.py` with one internal owner for:
- snapshot versus full-history selection,
- resolved-thread cache reuse,
- post-lock lookup-repair adoption,
- freshness/currentness checks,
- authoritative repair completion rules,
- latest-thread-event selection for outbound fallback.

- [ ] **Step 2: Move the read-policy helpers out of `conversation_cache.py`**

Move the read-side helpers that currently make `conversation_cache.py` a policy engine.
This includes the helpers around:
- thread refresh checks,
- lookup-repair promotion and adoption,
- cached thread source-event loading,
- resolved-thread cache population and reuse,
- full-history repair reads,
- snapshot/full-history result building,
- the shared `_read_thread()` path.

- [ ] **Step 3: Keep `MatrixConversationCache` as the public facade**

Leave the public methods on `MatrixConversationCache`.
Make them delegate to the new read-policy owner instead of open-coding the behavior inline.
Do not move durable repair state into memory.
Do not introduce a second public entrypoint.

- [ ] **Step 4: Re-run the focused read tests**

Run: `uv run pytest tests/test_thread_history.py tests/test_threading_error.py -x -n 0 --no-cov -v`

Expected: PASS.

- [ ] **Step 5: Commit the read-policy extraction**

```bash
git add src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/thread_reads.py src/mindroom/matrix/thread_cache.py src/mindroom/matrix/_event_cache.py tests/test_thread_history.py tests/test_threading_error.py
git commit -m "refactor: extract conversation cache thread read policy"
```

## Task 3: Lock Mutation Behavior Before Extraction

**Files:**
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_matrix_sync_tokens.py`
- Modify: `tests/test_matrix_api_tool.py`
- Modify: `tests/test_thread_summary.py`
- Modify: `tests/test_send_file_message.py`
- Modify: `tests/test_multi_agent_bot.py`

- [ ] **Step 1: Add or tighten characterization tests for thread-affecting mutations**

Cover the existing public behavior of:
- live append handling,
- live redaction handling,
- sync-thread persistence,
- sync-thread failure repair promotion.

The tests should lock in:
- successful writes bump or invalidate state coherently,
- degraded or failed writes mark the thread repair-required,
- successful repair clears the durable repair flag,
- sync errors do not mark cached data fresher,
- local redactions invalidate cached thread state through the conversation cache,
- tool-authored room-mode edits are allowed without a conversation cache,
- local summary sends and threaded tool sends write through to the cache,
- advisory cache failures after a successful Matrix send are fail-open.

- [ ] **Step 2: Run the focused mutation tests**

Run: `uv run pytest tests/test_threading_error.py tests/test_matrix_sync_tokens.py -x -n 0 --no-cov -v`

Expected: PASS.

- [ ] **Step 3: Commit the characterization coverage**

```bash
git add tests/test_threading_error.py tests/test_matrix_sync_tokens.py
git commit -m "test: lock conversation cache mutation behavior before extraction"
```

## Task 4: Extract Thread Write Policy

**Files:**
- Create: `src/mindroom/matrix/thread_writes.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_matrix_sync_tokens.py`

- [ ] **Step 1: Introduce a focused internal write-policy module**

Create `thread_writes.py` with one internal owner for:
- live append handling,
- live redaction handling,
- local outbound send and redaction write-through handling,
- sync thread-event persistence,
- sync redaction handling,
- mutation outcome classification,
- repair-required promotion after degraded or failed persistence.

- [ ] **Step 2: Move the write-policy helpers out of `conversation_cache.py`**

Move the helpers around:
- reply-chain invalidation inputs,
- thread change recording,
- repair-required marking,
- shared mutation finalization,
- fail-open post-send advisory cache handling,
- sync thread resolution,
- room timeline persistence for thread-affecting updates.

Keep reply-chain invalidation inside the write-policy module for now.
Do not create a separate side-effect abstraction unless the extraction clearly needs one.

- [ ] **Step 3: Keep sync grouping in place unless extraction proves it belongs elsewhere**

Leave `_group_sync_timeline_updates()` and `cache_sync_timeline()` in `conversation_cache.py` unless the write extraction makes a smaller pure helper obviously worthwhile.
Do not create `sync_timeline.py` just because earlier docs mentioned it.

- [ ] **Step 3a: Fold the remaining local write-through fixes into the extraction**

While moving the write policy:
- make local redactions use the same cache mutation contract as live sync redactions,
- stop classifying ordinary room-mode edits as requiring a conversation cache,
- keep threaded send/edit write-through under the public facade,
- make post-send advisory cache failures log and continue instead of turning delivered messages into raised failures.

- [ ] **Step 4: Re-run the focused mutation tests**

Run: `uv run pytest tests/test_threading_error.py tests/test_matrix_sync_tokens.py tests/test_matrix_api_tool.py tests/test_thread_summary.py tests/test_send_file_message.py tests/test_multi_agent_bot.py -x -n 0 --no-cov -v`

Expected: PASS.

- [ ] **Step 5: Commit the write-policy extraction**

```bash
git add src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/thread_writes.py tests/test_threading_error.py tests/test_matrix_sync_tokens.py
git commit -m "refactor: extract conversation cache thread write policy"
```

## Task 5: Thin The Facade And Verify The Outcome

**Files:**
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: any touched files from Tasks 1-4

- [ ] **Step 1: Remove leftover policy leakage from `conversation_cache.py`**

After the extractions, `conversation_cache.py` should mainly own:
- the public protocol and public facade methods,
- runtime-state reset and room-update queue plumbing,
- composition of `_event_cache.py`, `thread_cache.py`, `thread_reads.py`, and `thread_writes.py`,
- only the minimal helpers that truly belong to the facade.

- [ ] **Step 2: Check the change budget**

Verify that the refactor did not add a large amount of new code.
The expected result is a materially smaller `conversation_cache.py` with roughly flat net `src/` LOC.
If the split added substantial wrappers or coordination scaffolding, simplify before finalizing.

- [ ] **Step 3: Run the focused regression sweep**

Run: `uv run pytest tests/test_thread_history.py tests/test_threading_error.py tests/test_matrix_sync_tokens.py -x -n 0 --no-cov -v`

Expected: PASS.

- [ ] **Step 4: Run Ruff and the full suite**

Run: `uv run pre-commit run --all-files`
Run: `uv run pytest -n auto --no-cov -q`

Expected: PASS.

- [ ] **Step 5: Commit the final integration pass**

```bash
git add src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/thread_reads.py src/mindroom/matrix/thread_writes.py src/mindroom/matrix/thread_cache.py src/mindroom/matrix/_event_cache.py tests/test_thread_history.py tests/test_threading_error.py tests/test_matrix_sync_tokens.py
git commit -m "refactor: thin conversation cache facade"
```
