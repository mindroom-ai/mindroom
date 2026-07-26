# Thread Cache Repair Simplification Design

## Context

PR #1656 repairs stale Matrix thread-cache snapshots under concurrent writes.
Independent exact-head reviews of `07cf66abeed57d268912c17b2bda00c50474df7a` found that the repair ownership boundary is too broad and mixes caller policy with shared work.
Current `main` has advanced, so implementation begins by merging `origin/main` without rebasing or rewriting review history.

## Goals

Cache hits must return without entering the repair write lane.
Only authoritative refill work may join single-flight ownership.
Callers with different freshness or hydration contracts must not share incompatible results or exceptions.
Every caller must emit one correctly attributed completion event.
Persistent background failures must back off without caching reconstructed history or creating a permanent one-scan-per-second loop.
Membership departure must discard retained deltas from the departed epoch.
The implementation must preserve fail-open homeserver reads, fail-closed cache writes, membership-epoch guards, cancellation, and precise retained-delta retirement.
The final production diff must be simpler in control flow and smaller in code, but no safety invariant may be deleted to meet a line target.

## Non-Goals

This change does not remove the bounded second reconstruction attempt after a guarded replacement conflict.
This change does not make stale history acceptable to dispatch or strict reads.
This change does not cache transient homeserver results outside the durable cache.
This change does not add terminal repair suppression that can prevent recovery after a long outage.
This change does not promote an unproven thread candidate without a successful strict proof.

## Read and Refill Boundary

`client_thread_history.py` remains the owner of cache-read policy and authoritative Matrix reconstruction.
Each public thread fetch first probes the durable cache outside repair ownership.
A cache hit returns immediately and emits no coordinator repair entry.
A rejected or absent cache result calls an injected refill function supplied by `ConversationCache`.
Without a write coordinator, the refill function directly calls `refresh_thread_history_from_source`.
With a write coordinator, the refill function schedules only the authoritative reconstruction and guarded store.

Pending retained deltas are checked before a cache snapshot may be accepted.
When retained event IDs are absent from the durable snapshot, the thread is invalidated before the cache probe can return it.
The retained event-source provider remains late-bound so events certified while a homeserver scan is running can still enter the reconstructed snapshot.

## Repair Contracts and Ownership

Repair-flight identity includes principal, room, thread, sidecar hydration, and stale-fallback policy.
Retained-delta identity remains principal, room, and thread because deltas belong to the durable thread rather than one caller contract.
This separation prevents a strict owner exception from escaping into an advisory caller and prevents a lightweight snapshot from satisfying a full-history caller.
Contract-specific ownership removes the joined-result contract-recheck loop.
At most the existing advisory-full, strict-full, and strict-snapshot contracts may own distinct flights.

## Caller Telemetry

Completion telemetry moves to one caller-level return point.
Each cache hit, repair owner, and repair joiner emits exactly one `matrix_cache_thread_history_refreshed` event.
The event uses the current caller's label and coordinator wait measurement.
Caller-level code derives `cache_hit`, `cache_hit_after_repair_conflict`, or `full_scan` from existing result diagnostics without adding a second telemetry-only result type.
Owner-side logging that would duplicate the caller-level event is removed.
Direct source callers outside shared repair ownership report at the source boundary when they provide a caller label.

## Failure and Backoff Policy

A usable cache result clears the repair contract's failure count and retry deadline.
An exception or `HARD_FAILURE` increments a capped exponential backoff.
The first delay remains one second and later delays double to a 30-second default cap.
The backoff stores no thread history.
`WRITES_UNAVAILABLE` after a successful homeserver fetch is a normal uncached completion and does not arm failure backoff.
Background repair is not scheduled while durable cache writes are unavailable.
Contract-specific keys keep background snapshot failures from inheriting incompatible foreground caller policy.
Dispatch reads continue to degrade immediately when their contract is inside backoff.
Untimed reads continue to wait for their own contract's bounded retry deadline and then perform a fresh authoritative read.
Successful repair resets the exponential sequence.

## Membership Departure

`ThreadRepairRegistry` gains a principal-and-room repair-state clear operation.
`ConversationCache.purge_rooms` detaches active repair ownership and clears retained deltas and failure state synchronously when it marks each room departed.
The subsequent durable purge remains queued behind the room fence.
No pre-departure retained event may be merged into a post-rejoin snapshot.
Late completion of detached work cannot mutate the post-departure failure state.

## Existing-Winner Fail-Open Behavior

`EXISTING_USABLE` continues to prefer the concurrent winning snapshot when it can be read.
If that winner read fails, the already completed homeserver result returns immediately with a warning and an unusable-cache diagnostic.
The failure does not trigger a second full room scan.
Cancellation continues to propagate.

## Safe Simplification

The guarded-store missing-root branch is removed because every per-thread reconstruction raises before reaching guarded storage when its root is absent.
The shared missing-root classifier remains because existing-cache validation and bulk reconstruction still use it.
The two testing scripts require `ThreadCacheReplaceOutcome.STORED` explicitly.
The tuple wrapper, refill-attempt constructor wrapper, redundant retained-delta sort key, broad `BaseException` catch, redundant cancellation branches, and caller-supplied cache-inspection log strings are removed where the final structure still makes them obsolete.
Precise retained-delta acknowledgment remains unless a smaller implementation proves the same stored, redacted, and concurrent-append behavior.

## Testing

Each behavior change follows red-green-refactor with a focused regression before production edits.
A strict repair owner plus advisory joiner must return advisory stale fallback on a homeserver failure without sharing the strict flight.
A healthy cache hit must not enqueue a repair lane entry.
Concurrent callers must each emit one completion event with their own label.
`WRITES_UNAVAILABLE` must return homeserver history without backoff and must not start background repair while writes are disabled.
Repeated permanent background failures must show capped exponential delay and reset after success.
Departure followed by rejoin must not expose pre-departure retained deltas.
Departure must detach active repair ownership so a post-rejoin caller cannot join pre-departure work.
An `EXISTING_USABLE` winner-read failure must perform one homeserver scan and return that result.
An unproven dispatch candidate degraded by repair backoff must retry strict proof before demotion.
The unreachable guarded-store missing-root test is deleted while reachable existing-cache and bulk missing-root tests remain.
Owning SQLite suites, Ruff, format, type checks, Tach, focused pre-commit hooks, and `git diff --check` run locally.
Full pytest, PostgreSQL, Docker, and live Matrix validation wait for the shared resource gate.
Exact-head GitHub CI and fresh independent review run after the follow-up commits are pushed.

## Completion Criteria

The branch contains a normal merge of current `origin/main`.
Local and remote branch heads match.
All focused regressions and lightweight static gates pass.
GitHub exact-head CI is green.
No unresolved review thread remains.
Fresh exact-head review reports no reproducible blocker.
The pull request is merge-ready but remains unmerged until the user chooses to merge it.
