## Current State Analysis

- `src/mindroom/matrix/cache/event_cache.py:27-35` and `216-307` currently persist seven tables, including the repair-era `pending_lookup_repairs` and `thread_repairs` tables, not just event lookups and thread snapshots.
- `src/mindroom/matrix/conversation_cache.py:222-461` is already a facade over multiple subsystems, including SQLite state, `ThreadReadPolicy`, `ThreadWritePolicy`, a second in-memory `ResolvedThreadCache`, and reply-chain caches.
- `src/mindroom/matrix/cache/thread_reads.py:90-571` still implements room-idle waiting, repair adoption, generation checks, per-thread entry locks, delta refresh, special tail reads, and `ThreadRepairRequiredError`.
- `src/mindroom/matrix/cache/thread_writes.py:343-1113` still implements pending lookup repairs, promotion to thread repairs, generation bumps, source-event-set invalidation, and separate outbound, live, and sync mutation flows.
- `src/mindroom/matrix/client.py:1327-1452` still does incremental raw-snapshot refresh, and `src/mindroom/matrix/client.py:1894-1905` still carries `authoritative_refill` and `cache_refilled` bookkeeping.
- `src/mindroom/matrix/cache/thread_cache.py:53-179` is a full secondary cache with TTL, LRU, generations, source-event sets, and locks, so most of the current complexity is the extra cache tier itself.
- `src/mindroom/matrix/reply_chain.py:430-445` and `691-729` still depend on `get_thread_snapshot(...)` and the snapshot-versus-full-history distinction, so snapshot-based planning is still real branch behavior.
- `src/mindroom/matrix/turn_controller.py:1256-1265` still treats `ThreadRepairRequiredError` as a user-visible dispatch failure path.
- `src/mindroom/matrix/cache/event_cache.py:870-893` already resets stale cache state instead of migrating it, so that part of the plan matches current code rather than introducing a new behavior.
- The branch currently has three separate ordering and coherency layers, namely the write coordinator in `src/mindroom/matrix/cache/write_coordinator.py:17-122`, the room-lock plus DB-lock layer in `src/mindroom/matrix/cache/event_cache.py:119-201`, and the per-thread entry locks in `src/mindroom/matrix/cache/thread_cache.py:161-172`.

## Plan Validation

- The plan is correct to make Matrix the source of truth and the cache advisory.
- The plan is correct to delete durable repair obligations instead of inventing a smaller repair system.
- The plan is correct to prefer invalidation plus refetch over ambiguous local patching.
- The plan is correct to keep explicit-thread happy paths fast and outbound write-through fail-open after a successful send.
- The plan is correct to delete repair-era tests instead of porting them forward.
- The plan is correct to keep schema reset instead of schema migration.

## Over-Engineering Risks

- The seven-task breakdown is too granular for one simplification pass, because Tasks 1 and 3 overlap heavily and Tasks 4 and 5 also overlap.
- `src/mindroom/matrix/cache/thread_cache.py` should default to deletion, not “simplify or delete,” because invalidate-and-refetch does not need a second cross-turn resolved cache with generations and locks.
- The plan does not explicitly delete client-side incremental refresh, namely `refresh_cache`, `_refresh_cached_thread_event_sources`, `_fetch_incremental_thread_events`, and `resolve_thread_history_delta`, which risks keeping the old model under new names.
- The plan does not explicitly delete repair-era metadata, namely `thread_version`, `authoritative_refill`, and `cache_refilled`, which risks leaving inert bookkeeping behind.
- Task 2 includes `runtime_support.py` and `orchestrator.py` even though schema reset already lives inside `_EventCache.initialize()`, so those files look like task padding unless a real interface changes.
- Task 5 hardcodes a partial caller list even though many callers hit the public boundary, which adds process complexity without simplifying architecture.
- Task 1 proposes keeping “explicit invalidation helpers” on the public facade even though no such public API exists today, which grows the surface area while claiming simplification.
- If the plan keeps the write coordinator, the event-cache room-lock LRU, and the per-thread entry locks, then most of the coordination complexity survives the repair deletion.

## Missing/Risky Areas

- Task 1 is internally inconsistent at `docs/dev/2026-04-14-matrix-cache-invalidate-and-refetch-plan.md:115-127`, because it says the facade should keep only `get_thread_history(...)`, `get_latest_thread_event_id_if_needed(...)`, and outbound write methods, but `reply_chain.py` still requires `get_event(...)` and `get_thread_snapshot(...)`.
- Removing repair-era read failures also requires touching `src/mindroom/turn_controller.py`, `tests/test_multi_agent_bot.py`, and `tests/test_threading_error.py`, because `ThreadRepairRequiredError` is currently public and handled.
- Removing incremental refresh should also remove `last_sync_activity_monotonic` from `src/mindroom/bot_runtime_view.py:16-51` and `src/mindroom/bot.py:916-921` and `1158-1162`, because that field only exists to support refresh-skip logic in `thread_reads.py`.
- The plan does not define what happens when a refetch returns usable history but not a safe durable refill, such as a room-scan fallback that does not recover the root event.
- The plan does not define what to do when an ambiguous edit or redaction has no `event_threads` mapping, which is exactly the case that currently creates pending repair state.
- The plan does not re-audit snapshot/full-history tests in `tests/test_thread_mode.py` and `tests/test_multi_agent_bot.py`, which will move if the cache tiers collapse.
- The plan does not decide whether reads should still block on `wait_for_room_idle(...)`, which matters if background cache writes remain.
- The plan does not re-evaluate helper APIs that look branch-local now, such as `append_thread_event(...)`, `get_latest_ts(...)`, and the event-cache room-lock cache.

## Proposed Simplifications

- Collapse the work to four tasks, namely delete the repair contract, delete the second cache tier and incremental refresh, simplify mutation paths, and then sweep callers, tests, and docs.
- Delete `ResolvedThreadCache` outright unless a benchmark proves it is still needed after repair removal.
- Delete `thread_version`, `ThreadRepairRequiredError`, `authoritative_refill`, `cache_refilled`, and `resolve_thread_history_delta` in the same pass as the second cache tier.
- Keep one durable model only, namely SQLite thread snapshots plus point lookups.
- Make read behavior one rule, namely use a durable snapshot if it is present and valid, otherwise fetch from Matrix, return the result, and only replace the durable snapshot when the fetch is authoritative enough to store.
- Collapse `get_latest_thread_event_id_if_needed(...)` onto the normal full-history read path instead of keeping a bespoke tail-resolution path.
- Unify outbound, live, and sync mutation code behind one helper that does only two things, namely append explicit thread events or invalidate one known thread snapshot.
- Default reply-chain invalidation to a coarse advisory strategy, such as room-level or thread-level cache clearing on edits and redactions, instead of reconstructing invalidation sets from cached event shape.
- Keep at most one ordering mechanism for cache writes.
- If the same-room background write queue stays, delete `_EventCache` room locks and all per-thread entry locks.
- Drop `runtime_support.py` and `orchestrator.py` from the implementation plan unless a constructor or type really changes.
- Do not add new public invalidation helpers unless a real caller already needs them.
- Delete `get_latest_ts(...)` and collapse `append_thread_event(...)` into the single append path unless a current production caller still needs the distinction.

## Recommended Task Order

1. Delete the repair contract first by removing repair tables and repair APIs from `event_cache.py`, removing `ThreadRepairRequiredError`, and updating `turn_controller.py` and tests so no caller expects repair-mode behavior.
2. Delete the second cache tier and all incremental refresh machinery next by removing `thread_cache.py`, `thread_version`, `resolve_thread_history_delta`, `refresh_cache`, `last_sync_activity_monotonic`, and the repair-era diagnostics together.
3. Simplify mutation handling after the read path has one source of truth by unifying outbound, live, and sync updates around append-or-invalidate and replacing fine-grained reply-chain invalidation with a coarse advisory strategy.
4. Sweep remaining callers, tests, and docs last, because at that point the work should mostly be deleting stale coverage and updating the few snapshot/full-history call sites that still matter.
