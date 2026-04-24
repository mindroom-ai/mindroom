# PLAN-B — Independent Review of PR #714 (`fix: trust thread caches after sync catch-up`)

Author: Planning Agent B (Claude).
Base: `origin/main` @ `6c85df97a`.
PR head: `pr-714` @ `99e6b2d35`.
Diff inspected with `git diff origin/main...pr-714` and `git show pr-714:<file>`.

## 1. PR Intent (as I read it)

Goal: stop forcing every restart to refetch thread history from the homeserver when a durable Matrix sync token survives a restart.

Mechanism:

1. `AgentBot._restore_saved_sync_token()` now returns whether a persisted `next_batch` was actually loaded onto `self.client`.
2. `BotRuntimeState` gains three pieces of state — `restored_sync_token: bool`, `sync_catchup_applied_at: float | None`, and the derived `pre_runtime_thread_cache_trusted` property.
3. The bot calls `mark_runtime_started(restored_sync_token=...)` so the runtime view knows whether we *might* be able to trust pre-runtime cache rows.
4. The first successful `SyncResponse` after start does the catch-up handshake:
   - `cache_sync_timeline()` now returns the queued cache-write tasks; the bot `await asyncio.gather(...)`s them so persistence finishes before any decision is made.
   - If any task raises `Exception` -> log only.
   - Else if any joined room has `timeline.limited is True` -> `mark_restored_sync_token_invalid()` (fail-closed).
   - Else -> `mark_sync_catchup_applied()`.
5. `_on_sync_error` invalidates the restored-token flag when `M_UNKNOWN_POS` arrives before the first successful sync.
6. `MatrixConversationCache` and `ThreadMutationCacheOps` route their `runtime_started_at` argument through a new `_effective_thread_cache_runtime_started_at()` that returns `None` (no boundary) once `pre_runtime_thread_cache_trusted` is True.
7. `revalidate_thread_after_incremental_update_locked` accepts `runtime_started_at: float | None`; `None` skips the “must be after restart” gates.

Net effect: when (a) we restored a sync token, (b) the first sync after that restore wrote successfully, and (c) no joined room reported a limited timeline, durable thread caches written before the restart are reused. Otherwise the previous behaviour stands.

## 2. Files Touched

| File | Why |
|------|-----|
| `src/mindroom/bot.py` | New `_limited_joined_timeline_room_ids` helper; `_restore_saved_sync_token` returns bool; `_on_sync_response` awaits first-sync cache tasks and gates `mark_sync_catchup_applied` / `mark_restored_sync_token_invalid`; `_on_sync_error` handles `M_UNKNOWN_POS`. |
| `src/mindroom/bot_runtime_view.py` | New protocol property + dataclass fields + three lifecycle methods. |
| `src/mindroom/matrix/cache/event_cache.py` | Protocol + `_EventCache` accept `runtime_started_at: float \| None`. |
| `src/mindroom/matrix/cache/event_cache_threads.py` | `revalidate_thread_after_incremental_update_locked` accepts `None` and treats it as “no boundary”. |
| `src/mindroom/matrix/cache/thread_write_cache_ops.py` | Adds `_effective_thread_cache_runtime_started_at()` and uses it for the incremental revalidate call. |
| `src/mindroom/matrix/cache/thread_writes.py` | `cache_sync_timeline` now returns the per-room `asyncio.Task` list it scheduled. |
| `src/mindroom/matrix/conversation_cache.py` | Mirrors the same effective-boundary helper for the four `get_*` history paths and propagates the task-list return. |
| `tests/test_thread_history.py` | Two new cache-trust tests using a hand-built `BotRuntimeState`. |
| `tests/test_threading_error.py` | Two new bot-level tests exercising the limited / non-limited branches of `_on_sync_response`. |

## 3. Bug Findings (with evidence)

I focused on real correctness problems, not style. Severity is my own; A’s plan should re-rate.

### B-1. `CancelledError` slips past `isinstance(result, Exception)` and gets miscoded as success — **medium**

`bot.py` (PR view, lines ~993-1014):

```python
sync_cache_results = await asyncio.gather(*sync_cache_tasks, return_exceptions=True)
sync_cache_errors = [result for result in sync_cache_results if isinstance(result, Exception)]
if sync_cache_errors:
    self.logger.warning("matrix_sync_cache_catchup_failed", ...)
elif limited_first_sync_room_ids:
    ...
    self._runtime_view.mark_restored_sync_token_invalid()
    ...
else:
    self._runtime_view.mark_sync_catchup_applied()
```

Since Python 3.8, `asyncio.CancelledError` derives from `BaseException`, not `Exception` (verified locally: `python3 -c "import asyncio; print(issubclass(asyncio.CancelledError, Exception))"` -> `False`). `asyncio.gather(..., return_exceptions=True)` *does* surface a child task’s `CancelledError` as a result entry in the returned list. The PR’s comprehension drops it, so the code falls into the `else` branch and **declares the catch-up successful even though the per-room write was cancelled** — and therefore trusts pre-runtime cache rows that may not have been hydrated by this batch.

How a child task can be cancelled here: the `_EventCacheWriteCoordinator.queue_room_update` chain handles `CancelledError` carefully, but the *inner* `update_coro_factory` coroutine (`_persist_room_sync_timeline_updates`) is not shielded; any `task.cancel()` propagated to the coordinator’s background tasks (e.g., during `prepare_for_sync_shutdown`, or from a future reuse of `coordinator.close()`) lands as `CancelledError` in the gather result.

Fix direction (B will leave to A but documenting the shape): change the filter to `isinstance(result, BaseException)`, and on `CancelledError` either fail-closed (`mark_restored_sync_token_invalid`) and re-raise if the parent task itself is being cancelled, or short-circuit before reaching the `else` branch.

### B-2. `_persist_sync_token()` runs even when first-sync cache writes errored — **medium**

Same handler, after the `if/elif/else` block:

```python
self._persist_sync_token()
self._first_sync_done = True
```

The PR docstring still claims:

```
# Cache before persisting so a crash prefers replaying one batch over
# skipping events whose timeline metadata never reached local state.
```

But for first sync the new code only `await`s the cache tasks; if any of them raised, the warning fires and execution falls through to `_persist_sync_token()`. So the new `next_batch` is durably saved while the cache rows it represents are not. On the *next* restart, `_restore_saved_sync_token()` loads that token and `pre_runtime_thread_cache_trusted` will be False (good — `sync_catchup_applied_at` was never set), but the underlying durable cache is now permanently behind the persisted token. That breaks the very invariant the comment promises and forces a homeserver scan for any room touched by the failed batch.

Fix direction: when `sync_cache_errors` is non-empty during the first sync, either skip `_persist_sync_token()` for that response or actively clear the persisted token so the next start does a cold sync.

### B-3. M_UNKNOWN_POS handler invalidates cache trust but leaves `client.next_batch` poisoned — **medium (possibly out-of-scope)**

```python
async def _on_sync_error(self, _response: nio.SyncError) -> None:
    ...
    if not self._first_sync_done and _response.status_code == "M_UNKNOWN_POS":
        self._runtime_view.mark_restored_sync_token_invalid()
        self.logger.warning("matrix_sync_token_rejected", ...)
```

Evidence:

- `client.next_batch` is set *only* by `_restore_saved_sync_token` (`bot.py:949`) and by nio’s `_handle_sync` (only on a successful `SyncResponse`). It is never cleared in MindRoom.
- Matrix-nio’s `sync_forever` loops `await self.sync()` indefinitely, with `since = self.next_batch`. There is no built-in recovery for `M_UNKNOWN_POS`.
- `_on_sync_error` updates `_last_sync_monotonic = time.monotonic()`, so the watchdog (`orchestration/runtime.py:165` and onwards) treats every error as "sync activity" and never triggers the stalled-sync restart.

Net result: after this PR, on a rejected restored token the bot logs `matrix_sync_token_rejected` and then spins on M_UNKNOWN_POS errors **forever** without progressing past the first sync. The cache-trust signal is now correct (`pre_runtime_thread_cache_trusted` stays False), but no first sync ever completes, so the bot is effectively dead for inbound traffic.

Fix direction: in the same branch, also reset `self.client.next_batch = ""` (and ideally arrange `client.sync_forever(..., full_state=True)` for the next iteration) so the bot can recover by doing a cold sync. Alternatively raise into `_SyncIteration._watch` with a dedicated "sync token rejected" error so `sync_forever_with_restart` re-enters via `_SyncIteration.start` and the user-facing restart path.

### B-4. Empty-join first sync silently grants pre-runtime cache trust — **low**

`_limited_joined_timeline_room_ids(response)` returns `[]` whenever `response.rooms.join` has no rooms, and the `else` branch then calls `mark_sync_catchup_applied()`. For a freshly provisioned agent that hasn’t been added to any rooms yet — or, more realistically, for an agent that was kicked/banned from every previously joined room during downtime — the catch-up batch contains nothing, but the runtime view now declares "we caught up; trust the durable cache". In the `kicked` scenario the durable rows for a room we no longer belong to remain trusted indefinitely.

This isn’t catastrophic because production read paths only use the cache for rooms the bot currently dispatches into, but it is unnecessary trust. Either require at least one joined room to participate in the catch-up before flipping the flag, or have the catch-up be conditional on `len(joined_rooms) >= 1`.

### B-5. `isinstance(response.rooms.join, dict)` is fail-open — **low**

Both `_limited_joined_timeline_room_ids` (new helper in `bot.py`) and the existing `_group_sync_timeline_updates` skip the iteration entirely if `response.rooms.join` isn’t a `dict`. nio currently produces a plain `dict`, but if it ever changes (e.g., to a `MappingProxyType` or a `defaultdict`), the helper silently returns `[]`, no rooms are inspected, and the `else` branch trusts the cache. Prefer `isinstance(..., Mapping)` from `collections.abc`, or duck-type via `.items()`.

### B-6. `_first_sync_done = True` even on the cache-error path — **low**

After the new `if first_sync_response:` block, control falls through unconditionally to `self._first_sync_done = True`. So a future M_UNKNOWN_POS in the same iteration won’t hit the new pre-first-sync branch in `_on_sync_error`. Combined with B-2 and B-3 this drains diagnostic signal: a botched first sync never gets the second-chance treatment that the PR added.

### B-7. Cosmetic — `mark_restored_sync_token_invalid()` warning is gated on previous flag — **cosmetic**

In the `elif limited_first_sync_room_ids:` branch:

```python
restored_sync_token = self._runtime_view.restored_sync_token
self._runtime_view.mark_restored_sync_token_invalid()
if restored_sync_token:
    self.logger.warning("matrix_sync_cache_catchup_limited", ...)
```

For a *cold start* whose first sync is limited, no warning is emitted. That’s "by design" but it makes ops debugging harder — limited first syncs always disable catch-up trust, so a single info-level log would be cheap and useful.

## 4. Proposed Fix Approach (no code yet)

Order proposals roughly by impact:

1. **Tighten the gather error filter (B-1)**:
   - Change `isinstance(result, Exception)` → `isinstance(result, BaseException)`.
   - Detect `CancelledError` specifically; if `asyncio.current_task().cancelling()`, re-raise; otherwise treat as a failure (do not mark catch-up applied; consider invalidating the restored-sync-token flag for parity with the limited-timeline branch).

2. **Make persistence honour the cache invariant the comment claims (B-2)**:
   - Inside `_on_sync_response`, gate the call to `_persist_sync_token()` (and the `_first_sync_done = True` assignment) on either no-cache-errors *or* cache being unavailable.
   - On cache failure during first sync, additionally call `mark_restored_sync_token_invalid()` so the runtime view stops claiming a viable restored token.
   - For non-first syncs, decide explicitly: either keep the existing fire-and-forget (but soften the "cache before persist" comment) *or* `await asyncio.gather` for those too. Keeping fire-and-forget is fine for incremental syncs; the comment should drop the "cache before persisting" language because it isn’t honoured for them.

3. **Recover from M_UNKNOWN_POS instead of just marking trust dead (B-3)**:
   - In `_on_sync_error`, when status is `M_UNKNOWN_POS` and `not self._first_sync_done`, also clear `self.client.next_batch` (set to `""`) and reset `loaded_sync_token`, then either let nio’s next iteration full-sync naturally or raise a dedicated exception (`MatrixSyncTokenRejectedError`) so `sync_forever_with_restart` performs a clean restart through `_SyncIteration.start`.
   - Add an `is_alive()`/health probe assertion or a watchdog branch that flags "sync errors only" as not-progressing so an operator can see it.

4. **Defensive joined-room introspection (B-4 / B-5)**:
   - Use `Mapping` instead of `dict` for the `isinstance` check.
   - Require `len(joined_rooms) >= 1` before flipping `mark_sync_catchup_applied()`. If the first sync genuinely has no joined rooms, leave `sync_catchup_applied_at` at None and the next non-empty sync will perform the gating.

5. **Diagnostics (B-6 / B-7)**:
   - Always log `matrix_sync_cache_catchup_limited` (downgrade to `info` for the cold-start case) so the limited path is visible.
   - When B-2 is fixed, surface the cache-error case in metrics or an additional log so we know first-sync persistence failed.

## 5. Test Strategy

New unit coverage I would add:

| Test | Target | How |
|------|--------|-----|
| `test_first_sync_cache_task_cancelled_does_not_trust_cache` | B-1 | Wire a fake `cache_sync_timeline` that returns one task pre-cancelled; assert `pre_runtime_thread_cache_trusted` stays False and `mark_sync_catchup_applied` was not called. |
| `test_first_sync_cache_error_does_not_persist_token` | B-2 | Patch `_conversation_cache.cache_sync_timeline` to return a task that raises; assert `_persist_sync_token` is not called (or that the restored-token flag is invalidated). |
| `test_unknown_pos_first_sync_resets_next_batch` | B-3 | Build a bot with `client.next_batch = "bad"`, fire an `_on_sync_error` with status `M_UNKNOWN_POS`; assert `client.next_batch == ""` and (optionally) that a restart-worthy exception was queued. |
| `test_first_sync_with_no_joined_rooms_does_not_trust_cache` | B-4 | Use the existing PR test pattern but with `rooms.join = {}`; assert `pre_runtime_thread_cache_trusted` is still False. |
| `test_limited_first_sync_logs_for_cold_start` | B-7 | Cold-start variant of `test_limited_first_sync_rejects_restored_thread_cache_trust`; assert the `matrix_sync_cache_catchup_limited` log fires. |

Existing tests to keep green:

- `tests/test_thread_history.py::TestThreadHistoryCache::test_restored_token_post_sync_reuses_pre_runtime_thread_cache`
- `tests/test_thread_history.py::TestThreadHistoryCache::test_untrusted_restart_rejects_pre_runtime_thread_cache`
- `tests/test_threading_error.py::TestThreadingBehavior::test_limited_first_sync_rejects_restored_thread_cache_trust`
- `tests/test_threading_error.py::TestThreadingBehavior::test_complete_first_sync_trusts_restored_thread_cache`
- The pre-existing `test_thread_history.py` cache state tests (validated_at boundary checks).

Run command (NixOS):

```
export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos
nix-shell --run 'uv run pytest tests/test_thread_history.py tests/test_threading_error.py -x -n 0 --no-cov -v'
```

Followed by the affected cache modules:

```
nix-shell --run 'uv run pytest tests/ -x -n 0 --no-cov -v -k "thread or sync or cache"'
```

## 6. Live-Test Strategy

Use the local-stack flow from `CLAUDE.md` so we get a real Synapse + restored sync token flow.

1. `just local-matrix-up` and confirm `/_matrix/client/versions`.
2. `rm -f mindroom_data/matrix_state.yaml` to ensure a clean baseline.
3. Run MindRoom once with a tame model:
   ```
   MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false UV_PYTHON=3.13 \
     uv run mindroom run
   ```
   Have an agent join one room, send a thread reply via `matty`, wait for the cache to populate (`mindroom_data/sessions/<agent>.db`).
4. Stop MindRoom (Ctrl-C). Confirm `mindroom_data/matrix_state.yaml` exists with a `next_batch`.
5. **Happy path** — restart MindRoom; expect logs `matrix_sync_token_restored` and a successful first sync; matty `thread "Lobby" t1` should still resolve from cache (no homeserver page in the thread-history diagnostics).
6. **Limited-timeline path** — before restart, send >50 messages from another matty session in the same thread to push the server timeline past the joined-room limit; restart; expect `matrix_sync_cache_catchup_limited` and a homeserver thread fetch on next dispatch.
7. **M_UNKNOWN_POS path (B-3)** — manually mangle `mindroom_data/matrix_state.yaml` to a stale token (`s9999_99_…`); restart; expect `matrix_sync_token_rejected`. Today the bot will *not* recover; after the proposed fix it should re-enter sync with `full_state=True` and complete first sync.
8. Capture `mindroom_data/logs/*.log` from each scenario for the PR description.

## 7. Risks

- **Touching `_persist_sync_token` ordering** can regress the existing "cache before persist" behaviour for the non-first-sync path. Limit changes to the first-sync gate unless we’re willing to await the per-batch tasks for every sync (which adds latency).
- **Resetting `client.next_batch` on M_UNKNOWN_POS** triggers a `full_state=True` re-sync, which is expensive and can flood callbacks. Mitigation: only reset once per restart cycle and rely on `sync_forever_with_restart`’s backoff.
- **Filter change to `BaseException`** could cause us to swallow `KeyboardInterrupt`/`SystemExit` if we’re not careful. Re-raise those explicitly.
- **Defensive `Mapping` checks** must not miss the routing `_group_sync_timeline_updates` call site that still uses the strict `dict` check, otherwise behaviours diverge.
- The PR introduces a new public-ish protocol member `pre_runtime_thread_cache_trusted` — any TeamBot/router subclass that builds its own `BotRuntimeView` mock in a future test will fail with `AttributeError`. Already covered for the protocol because PR adds it, but Tach boundaries (`tach.toml`) may need a refresh if any consumer outside `mindroom.matrix` reaches into it.

## 8. What Not to Change

- The `pre_runtime_thread_cache_trusted` predicate logic itself looks correct (restored-token + sync-catchup ≥ runtime-start). Don’t loosen it.
- The decision to gate everything on the *first* sync only is reasonable; subsequent limited timelines do not invalidate already-applied catch-up. Don’t expand the gating to subsequent syncs.
- The `revalidate_thread_after_incremental_update_locked` rewrite is a clean equivalence (the boolean condition was refactored without changing the inclusive/exclusive boundaries — `<` became `>=` after the negation flip; both reject when `validated_at < runtime_started_at`). Don’t re-tune its boundaries; that path has heavy test coverage already.
- The new fields on `BotRuntimeState` and the protocol additions look minimal and necessary; resist adding more state for diagnostics — emit logs/metrics instead.
- The conversation-cache call sites that swap to `_effective_thread_cache_runtime_started_at()` should stay symmetric across all four `get_*` methods. Don’t change one without the others.

## 9. Quick Open Questions for A

1. Is the M_UNKNOWN_POS recovery (B-3) intentionally deferred to a follow-up PR? If so, should the handler at least raise `MatrixSyncStalledError` so `sync_forever_with_restart` reschedules instead of looping?
2. For B-1, do we want the cancellation to fail closed *and* re-raise, or just fail closed? Current PR conflates "all cache writes succeeded" with "no exception was returned".
3. For B-4, should an empty first-sync `rooms.join` be treated as "indeterminate" (don’t flip the flag) or "confirmed" (flip it)? My read is that "indeterminate" is safer.
