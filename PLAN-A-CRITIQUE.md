# PLAN-A Cross-Review of PLAN-B

## Summary

PLAN-B is right about several first-sync failure paths, especially cancellation, cache-write errors, and `M_UNKNOWN_POS` recovery.
I disagree with PLAN-B's conclusion that the boolean trust predicate and `runtime_started_at=None` approach are safe.
The final implementation should keep ISSUE-197 small, but it needs a durable cache-trust checkpoint rather than a process-local boolean that erases the freshness boundary for every old thread snapshot.

## PLAN-B Findings I Agree Are Real Bugs

### B-1: `CancelledError` can be mistaken for first-sync cache success

This is real.
`asyncio.gather(..., return_exceptions=True)` can return `asyncio.CancelledError`, and `CancelledError` is a `BaseException`, not an `Exception`.
The PR filters with `isinstance(result, Exception)`, so a cancelled first-sync cache task can fall through to `mark_sync_catchup_applied()`.
That violates the core invariant because cancelled cache catch-up has not proven that downtime timeline events reached durable cache state.

### B-2: First-sync cache-write errors still allow token persistence

This is real, and I think it is more serious than PLAN-B states.
The PR logs `matrix_sync_cache_catchup_failed`, then still persists the advanced `next_batch`.
A later restart can restore that advanced token, receive a clean first sync from after the skipped batch, mark catch-up trusted, and accept older thread snapshots even though the missed batch was the only replay window that could have repaired them.
This should either skip persistence until the failed batch is safely replayable or persist the token only with a conservative cache boundary that rejects older snapshots.

### B-3: `M_UNKNOWN_POS` leaves the sync loop poisoned

This is real.
Matrix-nio uses `since or self.next_batch`, and a `SyncError` does not clear `next_batch`.
The current handler only invalidates cache trust, while `_on_sync_error` also refreshes the watchdog activity clock, so the watchdog will not rescue a loop that repeatedly submits the same bad token.
The fix must clear the in-memory token, clear the persisted token file, and force a clean cold-sync path or a sync-loop restart.

### B-6: `_first_sync_done = True` on cache-error paths is a real symptom, not a separate root cause

This is real insofar as it makes the first-sync error path non-recoverable inside the same bot lifecycle.
I would not treat it as a separate implementation item.
It falls out of the token persistence and cache-boundary fix because a failed first-sync catch-up should not become a trusted first sync.

## PLAN-B Findings I Consider Theoretical Or Out Of Scope

### B-4: Empty joined-room first sync grants trust

This is mostly theoretical for ISSUE-197.
A valid restored-token sync with no joined timeline entries can mean there were no joined-room events to catch up, and rejecting that case would reduce the intended optimization.
The kicked-from-every-room scenario does not threaten normal thread-history reads because the bot should not dispatch into rooms it no longer belongs to.
I would not add a `len(joined_rooms) >= 1` requirement unless a concrete Matrix response shape proves that empty join can hide omitted joined-room timeline events.

### B-5: `isinstance(response.rooms.join, dict)` is fail-open if nio changes types

This is defensive rather than a present PR bug.
Nio currently returns a plain `dict`, and there is no evidence that this PR needs to support other `Mapping` implementations.
Using `collections.abc.Mapping` is a harmless cleanup if the helper is already being edited, but it should not drive the ISSUE-197 scope.

### B-7: Limited first-sync logging is cosmetic

This is out of scope for the correctness fix.
Logging cold-start limited timelines can help operations, but it does not protect the thread-cache trust invariant.

## Bugs PLAN-B Found That PLAN.md Missed

PLAN-B did not add a new independent blocker that PLAN.md missed.
It did add useful evidence for the `M_UNKNOWN_POS` bug by pointing out that the watchdog treats repeated sync errors as activity.
It also called out `_first_sync_done = True` on cache-error paths, which is a useful implementation detail for the first-sync failure fix.
PLAN-B's live-test note should be corrected because the PR persists restored sync tokens under `mindroom_data/sync_tokens/<agent>.token`, not `mindroom_data/matrix_state.yaml`.

## Bugs PLAN.md Found That PLAN-B Missed

### The PR removes the freshness boundary too broadly

PLAN-B treats `pre_runtime_thread_cache_trusted` as basically correct, but this is the main correctness gap.
The first restored-token sync only proves that events since the restored token were processed.
It does not prove that every durable thread snapshot in SQLite is safe, especially rows validated before an earlier cold start, before an earlier limited catch-up, or before an earlier cache-write failure.
Returning `None` for `runtime_started_at` after one successful catch-up accepts all of those rows.
The implementation needs a persisted cache-trust boundary and should pass that boundary into thread-cache freshness checks rather than passing `None`.

### Limited first sync still persists the advanced token

PLAN-B correctly notes the limited branch fails closed in memory, but it does not call out the durable follow-up bug.
The PR still saves the advanced token after `mark_restored_sync_token_invalid()`.
On a later restart, that advanced token can produce a non-limited first sync and cause old cache rows to become trusted even though the limited replay window was skipped.

### Non-first sync token persistence can outrun cache writes

PLAN-B explicitly suggests that keeping fire-and-forget persistence for incremental syncs may be acceptable.
I disagree because ISSUE-197 turns this old crash window into a durable correctness problem.
If a normal sync persists `next_batch` before its `matrix_cache_sync_timeline` task commits, a crash can leave the saved token ahead of SQLite.
The next restored-token startup will never replay the skipped event and can trust a stale thread cache unless token persistence is ordered after cache persistence or paired with a conservative boundary bump.

### The 300-second cache age guard may still undercut ISSUE-197

This is not a correctness blocker, but PLAN-B omits it.
Even with safe restored-token catch-up, `THREAD_CACHE_MAX_AGE_SECONDS` can force homeserver refetches for checkpoint-covered thread caches.
I would keep this out of the first correctness fix unless ISSUE-197's acceptance criteria require long-idle restart reuse.

## Recommended Final Implementation Scope

Implement a small durable trust model instead of a boolean `None` boundary.
Store sync-token metadata as a structured record with the Matrix token and a global `thread_cache_valid_after` timestamp.
Treat legacy plaintext token files as usable for Matrix continuity but not as proof that old thread caches are reusable.
On startup, restore the token if present, run the first sync, and only then use the persisted `thread_cache_valid_after` as the effective thread-cache boundary.
Do not pass `runtime_started_at=None` to thread-history freshness checks.

Maintain this invariant before saving any new token: the saved token must not advance past timeline events that are neither durably cached nor covered by a conservative `thread_cache_valid_after` boundary.
The simplest correct implementation is to await the returned `cache_sync_timeline()` tasks for every sync response before saving that response's token.
If any cache task returns any `BaseException`, do not mark catch-up trusted.
For cancellation, re-raise process-level cancellation, and otherwise fail closed.
For cache failures or limited first syncs, either skip token persistence so the next process can replay the batch or persist the new token with `thread_cache_valid_after` bumped to the failure time.
The global boundary approach is conservative, but it is much smaller and safer than room-scoped checkpoints.

Handle rejected restored tokens in the same PR.
Add a `clear_sync_token(storage_path, agent_name)` helper.
When first-sync `M_UNKNOWN_POS` is seen, clear `client.next_batch`, clear `client.loaded_sync_token` if present, delete the saved token, keep cache trust at the current runtime boundary, and force the sync loop onto a cold-sync path.

Leave B-4, B-5, and B-7 out unless the edited code naturally touches them.
Leave the 300-second age policy unchanged for the first correctness pass unless a focused performance test proves ISSUE-197 still fails its intended restart-speed target.

## Concise Test Strategy

Add a checkpoint test where a cache row predates the persisted `thread_cache_valid_after`, a restored token catches up cleanly, and the row is rejected.
Add a positive checkpoint test where a cache row validated after the persisted boundary is reused after successful restored-token catch-up.
Add a limited-first-sync test that proves the advanced token cannot later make pre-limited cache rows trusted.
Add cache-error and `CancelledError` tests that prove catch-up is not marked trusted and unsafe token metadata is not saved.
Add a non-first-sync ordering test where a delayed cache task prevents token persistence until the write completes.
Add an `M_UNKNOWN_POS` test that asserts `client.next_batch` and the persisted token file are cleared.
Update the existing PR tests to expect an effective checkpoint boundary rather than `None`.

Run the focused backend suite first with the Nix wrapper:

```bash
export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos
nix-shell --run 'uv run pytest tests/test_thread_history.py tests/test_threading_error.py tests/test_matrix_sync_tokens.py -x -n 0 --no-cov -v'
```

Then run the broader cache and sync slice:

```bash
nix-shell --run 'uv run pytest tests/ -x -n 0 --no-cov -v -k "thread or sync or cache"'
```

## Concise Live-Test Strategy

Use the local Matrix stack because this depends on real `/sync` token behavior.
Create a thread, let the bot cache it, stop the backend, restart with a valid saved token, and verify thread history uses the cache only when the checkpoint allows it.
Force a limited catch-up by sending enough downtime events to exceed the timeline limit, then verify old cache rows are rejected after restart.
Mangle `mindroom_data/sync_tokens/<agent>.token`, restart, and verify the bot logs token rejection, clears the token, and completes a cold sync instead of looping.
Use Matty only for the final behavioral smoke check that the agent still replies in the expected thread after the restart scenarios.
