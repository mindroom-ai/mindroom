# FINAL PLAN: Matrix Cache Invalidate-and-Refetch Simplification

## Consensus Summary (Codex + Claude independently agreed)

Both planning agents converge on collapsing 7 tasks to 3, deleting the generation system entirely, deleting incremental refresh, deleting `ThreadReadDeps`/`ThreadWriteDeps`, and adding `turn_controller.py` to the scope.

## Architecture Invariants

- Matrix = source of truth; cache = advisory
- One durable model: SQLite event blobs + thread snapshots
- One read rule: lookup resolved cache → hit = return; miss = fetch from Matrix, store, return
- One write rule: explicit thread mutation = append + invalidate resolved cache; ambiguous mutation = invalidate only
- Fail-open everywhere: cache errors → refetch; refetch errors → proceed with partial/empty data
- No durable repair obligations survive
- Entry lock stays for thundering-herd protection on concurrent reads of invalidated thread
- `redacted_events` table stays (legitimate, not repair-specific)
- `event_threads` table stays (needed for edit/redaction thread resolution)
- Write coordinator stays (not repair-specific)

## ResolvedThreadCache: Delete generations, keep simple

```python
@dataclass(slots=True)
class ResolvedThreadCacheEntry:
    history: list[ResolvedVisibleMessage]
    source_event_ids: frozenset[str]
    cached_at_monotonic: float
```

- `store()` inserts entry
- `invalidate()` removes entry
- `lookup()` returns entry if not TTL-expired, else None
- `entry_lock` stays for thundering-herd
- DELETE: `_generations`, `_next_generation`, `bump_version`, `version`, `_generation_touched_at`, `_prune_stale_generations`, `thread_version`

## 3 Tasks

### Task A: Delete repair machinery and simplify cache core
**Files:** event_cache.py, thread_reads.py, thread_writes.py, thread_cache.py, thread_cache_helpers.py, thread_history_result.py, conversation_cache.py, reply_chain.py, client.py, turn_controller.py

**Delete (17 repair-era abstractions):**
- SQLite tables: `pending_lookup_repairs`, `thread_repairs` + all 6 CRUD methods
- Exceptions: `ThreadRepairRequiredError`
- Repair reads: `_repair_history_is_authoritative`, `_repair_history_durably_refilled`, `_should_store_resolved_thread_cache_entry`, `adopt_room_lookup_repairs_locked`
- Repair writes: `_mark_lookup_repair_pending`, `_promote_lookup_repairs_locked`
- Generation system: `_generations`, `_next_generation`, `bump_version`, `version`, `_generation_touched_at`, `_prune_stale_generations`, `thread_version`
- Incremental refresh: `_incrementally_refresh_resolved_thread_cache` (106 lines)
- Sync freshness: `_SYNC_FRESHNESS_WINDOW_SECONDS`, `last_sync_activity_monotonic` dependency
- Deps bundles: `ThreadReadDeps`, `ThreadWriteDeps` (inline into facade or pass directly)
- History diagnostics: `THREAD_HISTORY_AUTHORITATIVE_REFILL_DIAGNOSTIC`, `THREAD_HISTORY_CACHE_REFILLED_DIAGNOSTIC`, related methods
- Turn controller: remove `ThreadRepairRequiredError` catch at line ~1256

**Simplify:**
- Read path: lookup → hit return / miss fetch-store-return
- Write path: explicit = append + invalidate resolved; ambiguous = invalidate only
- Merge `append_event` and `append_thread_event` into single path
- Bump `_EVENT_CACHE_SCHEMA_VERSION` to trigger reset

**Approach:** Delete code first, then fix broken tests. NOT TDD-first (this is deletion, not new feature).

### Task B: Simplify callers
**Files:** streaming.py, scheduling.py, delivery_gateway.py, hooks/sender.py, thread_summary.py, custom_tools/matrix_api.py + their tests

- Remove repair-aware imports, error handling, branching
- Verify all callers use public facade only
- Run caller-scoped tests

### Task C: Final cleanup and verification
- Delete dead repair-era tests (repair promotion, repair-required exceptions, generation math, internal helper shapes)
- Run `uv run pytest -n auto --no-cov -q` (full suite)
- Run `uv run pre-commit run --all-files`
- `git diff --stat origin/main...HEAD` — verify net reduction
- Update `docs/dev/2026-04-14-matrix-cache-invalidate-and-refetch-plan.md` with final state

## Success Criteria
- Net line count reduction from current PR state
- All tests pass
- Pre-commit clean
- No repair-era abstractions survive in any file
- Read/write paths are demonstrably simpler than current
