# PLAN-B: Matrix Cache Simplification Review

Review of `docs/dev/2026-04-14-matrix-cache-invalidate-and-refetch-plan.md` against the current cache code in `src/mindroom/matrix/cache/` and `src/mindroom/matrix/conversation_cache.py`.

---

## Current State Analysis

The cache subsystem spans 8 files totaling ~3,784 lines:

| File | Lines | Purpose |
|------|-------|---------|
| `thread_writes.py` | 1,113 | Write-through and sync mutation policy |
| `event_cache.py` | 1,162 | SQLite schema, CRUD, repair tables |
| `thread_reads.py` | 581 | Read policy, repair adoption, version checks |
| `conversation_cache.py` | 461 | Public facade, repair re-exports |
| `thread_cache.py` | 194 | In-memory resolved cache + generation tracking |
| `write_coordinator.py` | 122 | Room-ordered background write queue |
| `thread_cache_helpers.py` | 77 | Sorting, logging, event-id extraction |
| `thread_history_result.py` | 74 | `ThreadHistoryResult` list subclass |

The repair machinery is threaded through every layer:
- **SQLite**: 2 repair-only tables (`pending_lookup_repairs`, `thread_repairs`) with 6 dedicated methods.
- **Reads**: `ThreadRepairRequiredError`, `_repair_history_is_authoritative`, `_repair_history_durably_refilled`, `adopt_room_lookup_repairs_locked`, repair-aware `_should_refresh_cached_thread_history`, repair branching in every read path.
- **Writes**: `_mark_lookup_repair_pending`, `_promote_lookup_repairs_locked`, repair promotion on every thread resolution failure.
- **Generations**: `_generations`, `_next_generation`, `bump_version`, `thread_version`, `_generation_touched_at`, `_prune_stale_generations` -- all exist to track repair-era invalidation freshness.
- **Caller coupling**: `turn_controller.py:1256` catches `ThreadRepairRequiredError` and posts a dispatch failure to the room. Tests in `test_threading_error.py` lock in repair mechanics.

---

## Plan Validation

The plan gets the core architecture right:

1. **"Matrix is source of truth, cache is advisory"** -- correct invariant.
2. **"Invalidate on ambiguity, refetch on next read"** -- correct simplification.
3. **"No durable repair obligations"** -- deleting `pending_lookup_repairs` and `thread_repairs` tables is the right call.
4. **"Reset old cache state instead of migrating"** -- already implemented and sound.
5. **"Keep the public boundary at `conversation_cache.py`"** -- correct, callers already use this facade exclusively.
6. **Explicit-thread happy paths stay fast** -- right tradeoff.
7. **File map is accurate** -- correctly identifies all production files and test files that need touching.

---

## Over-Engineering Risks

### 1. Seven tasks is five too many

The plan splits the work into 7 tasks, but the actual work decomposes into 3 natural units:

- **Delete repair machinery** (tables, exceptions, repair-aware branching) -- this touches reads, writes, event_cache, and facade simultaneously because they're all coupled.
- **Simplify callers** (turn_controller, streaming, scheduling, delivery_gateway, hooks/sender) -- remove repair-aware error handling.
- **Clean up tests and run full suite** -- delete repair-era tests, verify.

Splitting reads/writes/durable-state into separate tasks (Tasks 1-4) creates artificial boundaries.
The repair code in reads depends on repair code in writes (`adopt_room_lookup_repairs_locked`) which depends on repair code in event_cache (`matching_pending_lookup_repairs`).
Deleting one layer without the others leaves broken intermediate states that don't compile.

### 2. Write-failing-tests-first is backwards for deletion work

Tasks 1-4 each start with "write failing behavior tests for the new invariants."
When the work is *deleting* complexity, the natural order is: delete the code, then see which existing tests break, then fix or delete those tests, then add the (few) new behavior tests.
Writing new tests against the old code, watching them fail, then changing the code to make them pass is TDD for *new features*, not for simplification.

### 3. The plan preserves the generation/version system

The `ResolvedThreadCache` has a generation system (`_generations`, `_next_generation`, `bump_version`, `thread_version`, `_generation_touched_at`, `_prune_stale_generations`) that exists entirely to track repair-era invalidation freshness.
The plan mentions "simplify or delete cross-turn resolved cache generations" (Task 5 area) but never commits to deleting it.

With invalidate-and-refetch, you don't need monotonically increasing version tokens.
You need exactly one bit per thread: **valid or invalid**.
Storing an entry implies valid; absence implies invalid.
`ResolvedThreadCache.store()` / `invalidate()` / `lookup()` already provide this.
The entire generation system is dead weight.

### 4. `ThreadReadDeps` / `ThreadWriteDeps` indirection survives

These frozen dataclasses bundle 8-10 callable fields each, all just forwarding to `MatrixConversationCache` methods.
They're an abstraction introduced for testability of the repair policy.
Post-simplification, `ThreadReadPolicy` and `ThreadWritePolicy` are thin enough to inline into the facade, or at minimum to take direct constructor args instead of a deps bundle.

### 5. The `_SYNC_FRESHNESS_WINDOW_SECONDS` shortcut survives without justification

`thread_reads.py:99` skips incremental refresh when sync is "fresh" (within 30s).
This heuristic exists to avoid redundant server fetches when the cache was recently populated via sync.
Post-simplification, the read path is: cached snapshot valid -> return it; invalid -> refetch from server.
The sync-freshness window is an optimization that adds a code path.
Either keep it as a simple "skip refetch if TTL not expired" (already handled by `ResolvedThreadCache.lookup()` TTL), or delete it.

---

## Missing/Risky Areas

### 1. `turn_controller.py` is not in the file map

`turn_controller.py:1256` catches `ThreadRepairRequiredError` and calls `_finalize_dispatch_failure`.
When the exception type is deleted, this catch block must also be deleted.
The plan's adjacent-caller list does not mention it.

### 2. No plan for what replaces `ThreadRepairRequiredError` at dispatch boundaries

Currently, when repair-required history can't be authoritatively refilled, MindRoom posts an error message to the room.
Post-simplification, if a refetch fails (homeserver down, network error), what happens?
Options:
- Return empty/partial history and let the agent respond with whatever it has.
- Raise a different exception and let dispatch handle it.
- Retry once and then proceed with stale data.

The plan should make this decision explicit.

### 3. `redacted_events` table -- repair or legitimate?

The plan doesn't explicitly address the `redacted_events` tombstone table.
This table prevents re-caching events that were already redacted (correct behavior, not repair-specific).
The plan should explicitly state it's kept.

### 4. `event_threads` table -- is it still needed?

The `event_threads` table maps `(room_id, event_id) -> thread_id`.
It's used by `get_thread_id_for_event()` which is called by:
- Write-through paths to resolve which thread an edit/redaction affects.
- Repair promotion (being deleted).

Post-simplification, write-through still needs to know which thread an edit belongs to, so `event_threads` stays.
The plan should say this explicitly.

### 5. Dual append paths: `append_event` vs `append_thread_event`

`event_cache.py` has both `append_event` (writes lookup row + thread row) and `append_thread_event` (thread row only).
`append_thread_event` is only used by sync timeline persistence.
Post-simplification, consider merging these into one method.

### 6. `_incrementally_refresh_resolved_thread_cache` -- keep or delete?

`thread_reads.py:211-317` incrementally merges one new raw event into the resolved cache without a full refetch.
This is a sophisticated optimization (106 lines) that avoids re-resolving the entire thread when exactly one new explicit-thread event arrived.
The plan's philosophy is "invalidate and refetch on ambiguity" but doesn't clarify whether single-event incremental refresh counts as "ambiguity."
This optimization should either be explicitly kept (with a clear invariant for when it fires) or deleted in favor of always refetching.
Recommendation: **delete it**.
It's 106 lines of code for an optimization that saves one server roundtrip in the common case, but the common case is also fast (threads are typically <100 events).

### 7. `entry_lock` -- still needed?

The per-thread entry lock in `ResolvedThreadCache` prevents thundering-herd refetches when multiple concurrent reads hit an invalidated thread.
Post-simplification, two concurrent reads of an invalid thread would both fetch from the server.
The lock is still valuable but the plan should acknowledge it explicitly as a kept mechanism.

### 8. `write_coordinator.py` -- plan doesn't mention it

The `_EventCacheWriteCoordinator` serializes same-room cache writes into a background task chain.
It's not repair-specific and should be kept, but the plan's file map omits it.

---

## Proposed Simplifications

### S1: Collapse 7 tasks into 3

**Task A: Delete repair machinery**
- Delete `pending_lookup_repairs` and `thread_repairs` SQLite tables and all 6 methods.
- Delete `ThreadRepairRequiredError`.
- Delete `_promote_lookup_repairs_locked`, `adopt_room_lookup_repairs_locked`, `_mark_lookup_repair_pending`.
- Delete `_repair_history_is_authoritative`, `_repair_history_durably_refilled`, `_should_store_resolved_thread_cache_entry`.
- Delete `_thread_requires_refresh`, `_clear_thread_refresh_required` (these just proxy to `thread_repair_required` / `clear_thread_repair_required`).
- Delete `_incrementally_refresh_resolved_thread_cache` (106 lines of optimization for a case that refetch handles fine).
- Delete generation system from `ResolvedThreadCache`: `_generations`, `_next_generation`, `_generation_touched_at`, `_prune_stale_generations`, `bump_version`, `version`. Replace `thread_version` with simple presence/absence in the cache.
- Simplify `_should_refresh_cached_thread_history` to just check TTL (already handled by cache lookup).
- Delete `_SYNC_FRESHNESS_WINDOW_SECONDS` and `last_sync_activity_monotonic` dependency from reads.
- Simplify read path: lookup in resolved cache -> hit = return; miss = fetch from server, store, return.
- Simplify write path: on explicit-thread mutation, invalidate resolved cache entry. On ambiguous mutation, invalidate resolved cache entry. No repair state.
- Bump `_EVENT_CACHE_SCHEMA_VERSION` to trigger automatic reset.
- Update `_EVENT_CACHE_TABLES` and `_REQUIRED_EVENT_CACHE_TABLES` to exclude deleted tables.
- Remove `ThreadRepairRequiredError` catch from `turn_controller.py:1256`.
- Decide: on refetch failure, return whatever partial/empty data is available (fail-open, consistent with "cache is advisory").
- Delete or inline `ThreadReadDeps` / `ThreadWriteDeps` if the policy classes become thin enough.
- Run tests, fix or delete broken ones.

**Task B: Simplify callers**
- Remove repair-aware branching from `streaming.py`, `scheduling.py`, `delivery_gateway.py`, `hooks/sender.py`, `thread_summary.py`, `custom_tools/matrix_api.py`.
- Verify callers only use the public facade methods.
- Run caller-scoped tests.

**Task C: Clean up and verify**
- Delete dead tests (repair promotion, repair-required exceptions, generation math).
- Run full pytest suite.
- Run pre-commit.
- Review diff size.

### S2: Delete the generation system entirely

Replace `ResolvedThreadCache`'s generation machinery with simple store/invalidate/lookup:

```python
@dataclass(slots=True)
class ResolvedThreadCacheEntry:
    history: list[ResolvedVisibleMessage]
    source_event_ids: frozenset[str]
    cached_at_monotonic: float
```

No `thread_version`.
`store()` inserts an entry.
`invalidate()` removes it.
`lookup()` returns entry if not TTL-expired, else None.
`entry_lock` stays for thundering-herd protection.

This deletes ~40 lines of generation bookkeeping and removes the `thread_version` / `bump_thread_version` dependency chain through `ThreadReadDeps`, `ThreadWriteDeps`, `MatrixConversationCache`, and `ThreadHistoryResult`.

### S3: Delete `_incrementally_refresh_resolved_thread_cache`

106 lines that optimize one specific case (exactly one new non-edit explicit-thread event since last cache store).
With invalidate-and-refetch, just invalidate and let the next read refetch.
The refetch path already caches the result, so subsequent reads are fast.

### S4: Delete `ThreadHistoryResult` diagnostics related to repair

Remove `THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC`, `THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC`, `thread_history_is_authoritative_refill()`, `thread_history_cache_refilled()`.
Keep `THREAD_HISTORY_SOURCE_DIAGNOSTIC` (cache vs homeserver) for observability.

### S5: Consider deleting `ThreadReadPolicy` / `ThreadWritePolicy` classes

Post-simplification, `ThreadReadPolicy` is roughly:
1. Lookup resolved cache.
2. If hit, return.
3. Fetch from server.
4. Store in resolved cache.
5. Return.

`ThreadWritePolicy` is roughly:
1. Resolve thread ID.
2. Append to durable cache.
3. Invalidate resolved cache.

These are simple enough to be methods on `MatrixConversationCache` directly.
The separate policy classes and deps bundles were justified when the policies were complex.
Post-simplification, they add indirection without buying testability that can't be achieved by mocking the event cache and client.

At minimum, delete `ThreadReadDeps` and `ThreadWriteDeps` and pass dependencies directly.

### S6: Merge `append_event` and `append_thread_event`

Post-simplification, sync timeline can use the same append path as outbound writes.
One method, one code path.

---

## Recommended Task Order

The plan's 7-task ordering is:

1. Freeze simplification boundary (tests first)
2. Simplify durable cache state
3. Replace repair-aware reads
4. Simplify write-through
5. Shrink adjacent callers
6. Delete dead tests and docs
7. Final verification

**Recommended 3-task ordering:**

### Task A: Delete repair machinery and simplify cache core
*Estimated file touches: event_cache.py, thread_reads.py, thread_writes.py, thread_cache.py, thread_history_result.py, conversation_cache.py, turn_controller.py*

Do it all at once because the repair code is cross-cutting.
Delete tables, delete exceptions, delete generation system, simplify read/write paths.
Fix broken tests immediately after each deletion.
One commit per logical deletion (e.g., "delete repair tables", "delete generation system", "simplify read path").

### Task B: Simplify callers
*Estimated file touches: streaming.py, scheduling.py, delivery_gateway.py, hooks/sender.py, thread_summary.py, custom_tools/matrix_api.py + their tests*

Quick pass: remove any repair-aware imports, error handling, or branching.
These callers already use the facade, so changes should be small.

### Task C: Final cleanup and verification
*Full test suite, pre-commit, diff review, dead test deletion.*

---

## Summary of Repair-Era Abstractions the Plan Keeps That Should Die

| Abstraction | Location | Why delete |
|-------------|----------|------------|
| `ThreadRepairRequiredError` | thread_reads.py, conversation_cache.py, turn_controller.py | No repair = no repair exceptions |
| `_generations` / `_next_generation` / `bump_version` / `version` | thread_cache.py | Replace with store/invalidate/lookup |
| `_generation_touched_at` / `_prune_stale_generations` | thread_cache.py | Generation bookkeeping |
| `ThreadReadDeps` / `ThreadWriteDeps` | thread_reads.py, thread_writes.py | Over-abstracted deps bundles |
| `_incrementally_refresh_resolved_thread_cache` | thread_reads.py | 106-line optimization; refetch handles it |
| `_SYNC_FRESHNESS_WINDOW_SECONDS` | thread_reads.py | Repair-era heuristic; TTL handles it |
| `thread_history_is_authoritative_refill` | thread_history_result.py | Repair concept |
| `thread_history_cache_refilled` | thread_history_result.py | Repair concept |
| `_should_store_resolved_thread_cache_entry` | thread_reads.py | Repair-specific decision |
| `_repair_history_is_authoritative` / `_repair_history_durably_refilled` | thread_reads.py | Obviously repair-specific |
| `adopt_room_lookup_repairs_locked` | thread_writes.py, thread_reads.py | Repair promotion |
| `_mark_lookup_repair_pending` / `_mark_lookup_repair_pending_locked` | thread_writes.py | Pending repair tracking |
| `_promote_lookup_repairs_locked` | thread_writes.py | Repair promotion |
| `pending_lookup_repairs` table | event_cache.py | Repair obligation storage |
| `thread_repairs` table | event_cache.py | Repair obligation storage |
| `mark_pending_lookup_repair` / `matching_pending_lookup_repairs` / `consume_pending_lookup_repairs` | event_cache.py | Repair table CRUD |
| `thread_repair_required` / `mark_thread_repair_required` / `clear_thread_repair_required` | event_cache.py | Repair table CRUD |
