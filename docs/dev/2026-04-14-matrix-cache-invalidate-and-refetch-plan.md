# Matrix Cache Invalidate-and-Refetch Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current repair-and-preserve Matrix thread cache with a smaller invalidate-and-refetch cache that is fast in normal explicit-thread cases and falls back to authoritative server reconstruction in ambiguous cases.

**Architecture:** Keep durable event blobs and durable snapshots for explicitly threaded conversations, but delete durable repair obligations, repair-aware generations, and ambiguous incremental patching.
On any cache ambiguity, invalidate the thread snapshot and rebuild it from Matrix on the next read.

**Tech Stack:** Python 3.13, `nio`, SQLite event cache, Matrix conversation cache, pytest, pre-commit.

## Final State

Implemented on 2026-04-14 with three commits for cache-core deletion, caller-boundary cleanup, and final verification.
The shipped model keeps durable event blobs, durable thread snapshots, and a small in-memory resolved-thread cache with entry locks and TTL only.
The read path is now `lookup -> hit return -> miss fetch, store, return`.
The write path now treats explicit threaded mutations as append-plus-invalidate and ambiguous mutations as invalidate-only.
The repair tables, repair exceptions, generation bookkeeping, incremental refresh, and sync-freshness read heuristics were deleted.
`custom_tools/matrix_api.py` now resolves threaded write detection through `ConversationCacheProtocol` during normal operation instead of reaching into the raw event cache directly.
Validation for the final implementation is the full nix-shell pytest run, pre-commit run, and diff review against `origin/main`.

---

## Goals

- Make the cache understandable again.
- Keep normal explicit-thread reads and writes fast.
- Prefer invalidation plus server refetch over local repair machinery.
- Reduce `src/` size by deleting repair-specific code paths and the tests that only exist to lock them in.
- Keep the public boundary at `src/mindroom/matrix/conversation_cache.py`.
- Explicitly reset old cache state instead of migrating it.

## Non-Goals

- Preserve backward compatibility for old cache schema or old repair tables.
- Keep the current repair-aware generation model.
- Keep snapshot-based threaded planning changes in this branch.
- Preserve every existing optimization if it depends on repair tracking to stay correct.

## Target Invariants

- Matrix is the source of truth.
- The cache is advisory.
- Only explicit, easy-to-classify thread updates mutate cached thread state incrementally.
- Any ambiguous update invalidates cached thread state.
- The next read refetches full thread history from Matrix and replaces the cached snapshot.
- No durable repair obligations survive across turns or restarts.
- Outbound cache write-through must stay fail-open after a successful Matrix send.

## File Map

### Core production files

- Modify: `src/mindroom/matrix/conversation_cache.py`
  Public facade only.
  Keep the simple read and write entrypoints.
- Modify: `src/mindroom/matrix/cache/event_cache.py`
  Remove durable repair tables and repair-specific helpers.
  Keep durable events and durable thread snapshots.
- Modify: `src/mindroom/matrix/cache/thread_reads.py`
  Remove repair-aware read policy, generations, and repair-required branching.
  Read snapshot if valid, else refetch and replace.
- Modify: `src/mindroom/matrix/cache/thread_writes.py`
  Keep only obvious explicit-thread incremental updates.
  Invalidate on ambiguity or write failure.
- Modify: `src/mindroom/matrix/cache/thread_cache.py`
  Simplify or delete cross-turn resolved cache generations and entry-lock machinery if no longer needed.
- Modify: `src/mindroom/matrix/cache/thread_cache_helpers.py`
  Remove helpers that only exist for repair promotion or generation math.
- Modify: `src/mindroom/matrix/client.py`
  Simplify thread fetch helpers to support invalidate-and-refetch semantics.
  Remove repair-aware incremental refresh behavior.
- Modify: `src/mindroom/matrix/reply_chain.py`
  Stop coupling cache mutation logic to reply-chain storage shape.
  Keep only behavior-level invalidation hooks if still needed.

### Adjacent callers

- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/scheduling.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/hooks/sender.py`
- Modify: `src/mindroom/thread_summary.py`
- Modify: `src/mindroom/custom_tools/matrix_api.py`

These callers should keep using the public conversation-cache boundary, but they should not care about repair state because it no longer exists.

### Tests

- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_thread_history.py`
- Modify: `tests/test_matrix_api_tool.py`
- Modify: `tests/test_send_file_message.py`
- Modify: `tests/test_streaming_behavior.py`
- Modify: `tests/test_thread_summary.py`
- Modify: `tests/test_scheduling.py`
- Modify or delete: any tests that only exist to lock in repair promotion, repair-required state, generation math, or internal wrapper shape

## Implementation Strategy

### Task 1: Freeze the simplification boundary

**Files:**
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_reads.py`
- Modify: `src/mindroom/matrix/cache/thread_writes.py`
- Test: `tests/test_threading_error.py`

- [ ] **Step 1: Write failing behavior tests for the new invariants**

Add or update tests to prove:
- explicit `m.thread` happy-path appends still work,
- ambiguous plain replies invalidate cached thread state,
- ambiguous edits and redactions invalidate cached thread state,
- the next read refetches full thread history from Matrix instead of attempting repair.

- [ ] **Step 2: Run only the new or changed tests and confirm the old repair-oriented behavior fails**

Run:
```bash
uv run pytest tests/test_threading_error.py -k 'invalidate or refetch or plain_reply or redaction or edit' -x -n 0 --no-cov -q
```

- [ ] **Step 3: Delete repair-oriented public policy from the facade**

Remove or collapse facade helpers that only exist for:
- repair-required state,
- pending lookup repair promotion,
- generation/version bookkeeping.

Keep only:
- `get_thread_history(...)`,
- `get_latest_thread_event_id_if_needed(...)`,
- `record_outbound_message(...)`,
- `record_outbound_redaction(...)`,
- explicit invalidation helpers that callers still need.

- [ ] **Step 4: Re-run the focused tests**

Run:
```bash
uv run pytest tests/test_threading_error.py -k 'invalidate or refetch or plain_reply or redaction or edit' -x -n 0 --no-cov -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/thread_reads.py src/mindroom/matrix/cache/thread_writes.py tests/test_threading_error.py
git commit -m "refactor: drop matrix cache repair policy"
```

### Task 2: Simplify durable cache state

**Files:**
- Modify: `src/mindroom/matrix/cache/event_cache.py`
- Modify: `src/mindroom/runtime_support.py`
- Modify: `src/mindroom/orchestrator.py`
- Test: `tests/test_event_cache.py`

- [ ] **Step 1: Write failing tests for the new durable-state contract**

Add or update tests to prove:
- durable event blobs and thread snapshots still store and load,
- old repair tables are not required,
- an old incompatible cache schema is reset and rebuilt instead of migrated.

- [ ] **Step 2: Run the focused durable-cache tests**

Run:
```bash
uv run pytest tests/test_event_cache.py -k 'schema or reset or snapshot or thread_events' -x -n 0 --no-cov -q
```

- [ ] **Step 3: Remove repair-specific durable state**

Delete:
- pending lookup repair storage,
- thread repair storage,
- repair promotion queries,
- repair cleanup helpers.

Keep:
- event blobs,
- event edits if still needed for rendering,
- explicit thread snapshots for cached thread history.

When schema changes, reset the old cache and rebuild through usage.
Document this directly in code comments near initialization.

- [ ] **Step 4: Re-run the focused durable-cache tests**

Run:
```bash
uv run pytest tests/test_event_cache.py -k 'schema or reset or snapshot or thread_events' -x -n 0 --no-cov -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/matrix/cache/event_cache.py src/mindroom/runtime_support.py src/mindroom/orchestrator.py tests/test_event_cache.py
git commit -m "refactor: simplify durable matrix cache state"
```

### Task 3: Replace repair-aware reads with invalidate-and-refetch reads

**Files:**
- Modify: `src/mindroom/matrix/cache/thread_reads.py`
- Modify: `src/mindroom/matrix/client.py`
- Modify: `src/mindroom/matrix/cache/thread_history_result.py`
- Test: `tests/test_thread_history.py`
- Test: `tests/test_threading_error.py`

- [ ] **Step 1: Write failing tests for the new read path**

Add or update tests to prove:
- cached explicit-thread snapshots are reused when valid,
- invalidated snapshots force one authoritative Matrix refetch,
- latest-thread fallback prefers authoritative fetch over clever local repair,
- no read path raises repair-specific errors anymore.

- [ ] **Step 2: Run the focused read-path tests**

Run:
```bash
uv run pytest tests/test_thread_history.py tests/test_threading_error.py -k 'snapshot or latest_thread or invalidate or refetch' -x -n 0 --no-cov -q
```

- [ ] **Step 3: Delete repair-aware read mechanics**

Remove:
- repair-required exceptions and branching,
- generation-aware refresh decisions,
- repair-adoption logic across reads.

Implement:
- valid snapshot hit,
- snapshot miss or invalidation -> full Matrix refetch,
- replace cached snapshot with the refetched history.

- [ ] **Step 4: Re-run the focused read-path tests**

Run:
```bash
uv run pytest tests/test_thread_history.py tests/test_threading_error.py -k 'snapshot or latest_thread or invalidate or refetch' -x -n 0 --no-cov -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/matrix/cache/thread_reads.py src/mindroom/matrix/client.py src/mindroom/matrix/cache/thread_history_result.py tests/test_thread_history.py tests/test_threading_error.py
git commit -m "refactor: use invalidate-and-refetch thread reads"
```

### Task 4: Simplify write-through and sync mutation policy

**Files:**
- Modify: `src/mindroom/matrix/cache/thread_writes.py`
- Modify: `src/mindroom/matrix/reply_chain.py`
- Modify: `src/mindroom/matrix/client.py`
- Test: `tests/test_threading_error.py`
- Test: `tests/test_queued_message_notify.py`

- [ ] **Step 1: Write failing tests for explicit-vs-ambiguous write behavior**

Add or update tests to prove:
- explicit threaded message sends append cheaply,
- explicit threaded edits and redactions update if easy,
- ambiguous plain replies, edits, or redactions invalidate snapshots instead of creating repair state,
- sync updates follow the same rules.

- [ ] **Step 2: Run the focused write-path tests**

Run:
```bash
uv run pytest tests/test_threading_error.py tests/test_queued_message_notify.py -k 'outbound or sync or invalidate or explicit_thread' -x -n 0 --no-cov -q
```

- [ ] **Step 3: Delete repair promotion and reply-chain-shape coupling**

Remove:
- write-time repair promotion,
- pending repair adoption,
- direct dependence on reply-chain node storage shape where behavior-level invalidation is enough.

Keep:
- explicit thread append/update,
- snapshot invalidation on ambiguity,
- fail-open post-send bookkeeping.

- [ ] **Step 4: Re-run the focused write-path tests**

Run:
```bash
uv run pytest tests/test_threading_error.py tests/test_queued_message_notify.py -k 'outbound or sync or invalidate or explicit_thread' -x -n 0 --no-cov -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/matrix/cache/thread_writes.py src/mindroom/matrix/reply_chain.py src/mindroom/matrix/client.py tests/test_threading_error.py tests/test_queued_message_notify.py
git commit -m "refactor: simplify matrix thread write-through"
```

### Task 5: Shrink adjacent caller logic

**Files:**
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/scheduling.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/hooks/sender.py`
- Modify: `src/mindroom/thread_summary.py`
- Modify: `src/mindroom/custom_tools/matrix_api.py`
- Test: `tests/test_streaming_behavior.py`
- Test: `tests/test_send_file_message.py`
- Test: `tests/test_thread_summary.py`
- Test: `tests/test_scheduling.py`
- Test: `tests/test_matrix_api_tool.py`

- [ ] **Step 1: Write or update focused caller tests**

Cover:
- successful sends remain successful when cache write-through fails,
- oversized messages cache the actual delivered payload,
- threaded tool sends still write through on the explicit happy path,
- ambiguous cases only invalidate snapshots.

- [ ] **Step 2: Run the focused caller tests**

Run:
```bash
uv run pytest tests/test_streaming_behavior.py tests/test_send_file_message.py tests/test_thread_summary.py tests/test_scheduling.py tests/test_matrix_api_tool.py -x -n 0 --no-cov -q
```

- [ ] **Step 3: Delete repair-specific caller branching**

Callers should not know about repair state.
They should only:
- record obvious explicit-thread updates,
- or invalidate snapshots through the public facade,
- while staying fail-open after successful Matrix delivery.

- [ ] **Step 4: Re-run the focused caller tests**

Run:
```bash
uv run pytest tests/test_streaming_behavior.py tests/test_send_file_message.py tests/test_thread_summary.py tests/test_scheduling.py tests/test_matrix_api_tool.py -x -n 0 --no-cov -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/streaming.py src/mindroom/scheduling.py src/mindroom/delivery_gateway.py src/mindroom/hooks/sender.py src/mindroom/thread_summary.py src/mindroom/custom_tools/matrix_api.py tests/test_streaming_behavior.py tests/test_send_file_message.py tests/test_thread_summary.py tests/test_scheduling.py tests/test_matrix_api_tool.py
git commit -m "cleanup: shrink matrix cache caller logic"
```

### Task 6: Delete dead tests and update docs to the smaller model

**Files:**
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_thread_history.py`
- Modify: `docs/dev/2026-04-14-matrix-cache-invalidate-and-refetch-plan.md`
- Modify or delete: any touched docs that still describe repair state or generation-aware cache behavior

- [ ] **Step 1: Identify dead or redundant tests**

Delete tests that only lock in:
- repair promotion,
- repair-required exceptions,
- generation math,
- internal helper shapes,
- wrapper names that no longer matter to behavior.

- [ ] **Step 2: Run the seam-area tests after deletion**

Run:
```bash
uv run pytest tests/test_threading_error.py tests/test_thread_history.py tests/test_queued_message_notify.py tests/test_scheduling.py tests/test_streaming_behavior.py tests/test_send_file_message.py tests/test_thread_summary.py tests/test_matrix_api_tool.py -x -n 0 --no-cov -q
```

- [ ] **Step 3: Update docs to the shipped model**

Document:
- invalidate-and-refetch semantics,
- explicit-thread happy-path caching,
- old cache reset instead of migration,
- no durable repair obligations.

- [ ] **Step 4: Re-run docs and focused checks**

Run:
```bash
uv run pre-commit run --files docs/dev/2026-04-14-matrix-cache-invalidate-and-refetch-plan.md
```

- [ ] **Step 5: Commit**

```bash
git add docs/dev/2026-04-14-matrix-cache-invalidate-and-refetch-plan.md tests/test_threading_error.py tests/test_thread_history.py tests/test_queued_message_notify.py tests/test_scheduling.py tests/test_streaming_behavior.py tests/test_send_file_message.py tests/test_thread_summary.py tests/test_matrix_api_tool.py
git commit -m "cleanup: remove matrix cache repair-era tests and docs"
```

### Task 7: Final verification and branch cleanup

**Files:**
- Modify: any remaining touched files from earlier tasks

- [ ] **Step 1: Run the full backend test suite**

Run:
```bash
uv run pytest -n auto --no-cov -q
```

- [ ] **Step 2: Run pre-commit on all files**

Run:
```bash
uv run pre-commit run --all-files
```

- [ ] **Step 3: Review the diff size against `origin/main`**

Run:
```bash
git diff --stat origin/main...HEAD
```

Expected:
- fewer Matrix cache files carrying repair machinery,
- smaller `src/` footprint,
- fewer branch-history tests.

- [ ] **Step 4: Make one final cleanup commit if needed**

```bash
git add <exact files>
git commit -m "cleanup: finalize matrix cache simplify branch"
```

- [ ] **Step 5: Prepare for final review**

Summarize:
- what repair machinery was deleted,
- which fast paths were kept,
- which ambiguous cases now invalidate and refetch,
- what cache schema reset behavior now exists.
