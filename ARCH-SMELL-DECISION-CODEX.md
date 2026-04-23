# ARCH smell decision for ISSUE-176 outbound FIFO

I inspected the current outbound path in [thread_writes.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/cache/thread_writes.py:95), the new preflight hook in [write_coordinator.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/cache/write_coordinator.py:565), the mutation resolver in [thread_bookkeeping.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/thread_bookkeeping.py:254), the event lookup path in [conversation_cache.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/conversation_cache.py:460), the five reviewer memos, and the pre-change outbound implementation on `origin/main`.
My conclusion is that the reviewers found a real cache-correctness regression, not a test artifact.

## 1. FIFO criticality

The same-thread FIFO here is not a Matrix delivery invariant, but it is a hard local cache-coherency invariant for this subsystem.
The homeserver may already have accepted and ordered event `A` before `notify_outbound_*` runs for `B`, but this code does not resolve `B` from the homeserver’s authoritative order.
It resolves `B` from the advisory cache first, and only optionally from `room_get_event`, via [conversation_cache.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/conversation_cache.py:491) and [thread_bookkeeping.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/thread_bookkeeping.py:289).
If that lookup path cannot prove the target thread, the mutation resolves to `UNKNOWN`.
Once that happens, `_apply_thread_message_mutation()` invalidates the room and returns without appending the event at all, as shown in [thread_writes.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/cache/thread_writes.py:116).

That means earlier outbound writes are not just “nice to have” predecessors.
They can create the exact lookup rows that later outbound mutations need in order to route correctly.
If `A` has not yet appended its lookup rows when `B` resolves, `B` can degrade from “append to known thread” to “invalidate room and skip append.”
That is a real correctness difference inside the cache, even if Matrix delivery itself is already done.

The new regression comes from resolving impact off-barrier in `_schedule_fail_open_room_preflight()` and only queuing the real mutation afterward, as in [thread_writes.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/cache/thread_writes.py:266) and [write_coordinator.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/cache/write_coordinator.py:565).
`queue_room_preflight()` does not reserve an ordered slot in `state.entries`.
It only tracks a background task in `_room_preflight_tasks`, so later conflicting work can resolve and queue first.

The old code on `origin/main` avoided that by keeping the whole outbound resolve-and-apply path under the ordered room barrier unless the thread ID was already explicit in the outbound payload.
That is visible in the pre-change implementation at [origin/main thread_writes.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/cache/thread_writes.py:295) compared against the `git show origin/main` snippet I reviewed.
So the invariant is narrower than “everything outbound must be room-serialized forever,” but it is absolutely broader than “the homeserver ordered it, so local preflight order no longer matters.”

## 2. Bug validation

I wrote `/tmp/measure_outbound_fifo.py` and ran it with the repo’s `.venv` Python.
The script uses a real `_EventCache`, a real `_EventCacheWriteCoordinator`, and the real `MatrixConversationCache` outbound path.
It seeds one cached thread root, sends outbound event `A` as an explicit thread reply to that root, delays the resolver for the first outbound call, then sends outbound event `B` as a plain reply to `A`.
I forced `room_get_event(A)` to miss so the outcome depends on the cache rows written by `A`, which is the precise architectural hazard under review.

Observed output:

```text
tmp_root=/tmp/measure-outbound-fifo-n1il6y4v
scheduled_first
first_resolver_blocked
scheduled_second
2026-04-23 09:46:22 [info     ] Event cache update timing      barrier_kind=room operation=matrix_cache_notify_outbound_event outcome=ok predecessor_count=0 predecessor_wait_ms=0.1 queued_behind_predecessor=False room_id=!room:localhost total_ms=7.0 update_run_ms=6.9
released_first_resolver
2026-04-23 09:46:22 [info     ] Event cache idle wait timing   barrier_kind=room pending_task_count=2 room_id=!room:localhost wait_iterations=2 wait_ms=10.2
room_idle
background_idle
first_thread_id=$thread-root:localhost
second_thread_id=None
second_event_cached=False
thread_events=['$thread-root:localhost', '$reply-a:localhost']
thread_state=validated_at=1776962782.557488, invalidated_at=1776962782.616806, invalidation_reason=outbound_thread_mutation, room_invalidated_at=1776962782.5664127, room_invalidation_reason=outbound_thread_lookup_unavailable
```

That reproduces the substance of REV-D’s claim.
`A` is appended and indexed under `$thread-root:localhost`.
`B` is not cached, has no thread ID row, and the room is marked stale with `outbound_thread_lookup_unavailable`.
So the answer to “does `B` actually get dropped?” is yes, under the lookup-miss condition that this code explicitly supports.

The nuance is that the bug is conditional, not universal.
If `room_get_event(A)` succeeds quickly enough, `B` may still resolve and append.
But that does not make the reviewers wrong.
It means the new design changed a deterministic ordered path into a timing-dependent path whose success now depends on resolver latency and side-channel availability.

## 3. Option scoring

### Option A

For:
Option A is the fastest way back to the last known-correct outbound behavior.
It fully removes the new unordered preflight mechanism and returns the branch to the R-final-2 architecture that already preserved the proven 2x ISSUE-176 read/live improvement.
Because commit `a336f8ad7` only touched the three outbound-routing files, the rollback surface is small and easy to reason about.

Against:
Option A is blunt and undersells the real architectural lesson.
It also does nothing to explain to reviewers why lookup-dependent outbound routing is intentionally not parallelized in this PR.
If applied without an explicit comment or follow-up issue, the same “why is outbound still on the room barrier?” discussion will repeat.

Estimate:
About 0.5 to 1.5 agent-hours, and likely 1 review round.

### Option B1

For:
B1 is the only option that actually satisfies both goals at once.
It can preserve submission order for dependent outbound mutations and still let the final apply phase land on per-thread barriers once impact is known.
Architecturally, it matches the real problem, which is not “needs a faster resolver” but “needs queue position reservation before async resolution.”

Against:
B1 is a new coordinator abstraction, not a tweak.
It needs reservation lifecycle, upgrade semantics, cancellation semantics, room-idle semantics, same-thread predecessor semantics, and tests for all of those.
Given how subtle the current coordinator already is, this is exactly the kind of change that can trade one ordering bug for another.
It is too large and risky for a PR whose original issue is already solved.

Estimate:
Roughly 10 to 18 agent-hours, and 3 to 5 review rounds.

### Option B2

For:
B2 is correct by construction.
If resolution happens inside an ordered room segment, later dependent outbound mutations cannot observe stale predecessor state.
It uses existing coordinator concepts and avoids inventing a reservation API.

Against:
B2 almost certainly gives away most of the hoped-for outbound benefit.
The variable-cost part of outbound routing is the lookup itself, including possible `room_get_event` work, and B2 keeps that serialized on the room barrier.
It also adds awkward internal requeue logic for the apply phase, so it is not actually “cheap,” just cheaper than B1.

Estimate:
About 4 to 8 agent-hours, and 2 to 3 review rounds.

### Option C

For:
Option C has the best risk-adjusted outcome for this PR.
It reverts only the unsafe lookup-dependent outbound preflight routing, keeps the pre-existing explicit-thread fast path that already routed known-thread outbound events directly to the thread barrier, and adds a comment documenting why lookup-dependent outbound mutations stay on the room barrier for now.
That preserves the real ISSUE-176 win and avoids shipping a known correctness regression for bonus scope.

Against:
Option C will not satisfy reviewers who wanted fully thread-scoped outbound routing for plain replies and redactions in this PR.
It is an honest deferral, not a performance win.
If outbound becomes a measured bottleneck later, the team will still need a B1-style reservation design.

Estimate:
About 1 to 3 agent-hours, and 1 to 2 review rounds.

## 4. Recommendation

I recommend Option C.
The original ISSUE-176 objective is already met by the read/live thread-barrier work and is backed by the new parallelism test in [tests/test_issue_176_real_thread_parallelism.py](/srv/mindroom-worktrees/issue-176-thread-barrier/tests/test_issue_176_real_thread_parallelism.py:1).
Outbound routing was bonus scope, and the attempted bonus fix created a real correctness regression in the advisory cache.
That is the wrong trade.

B1 is the only fully satisfying outbound design, but it is too much new coordinator surface for this PR.
B2 is safer than the current code but likely low-yield and still nontrivial.
C is the right architectural boundary: keep explicit thread-ID outbound mutations on the thread barrier, keep lookup-dependent outbound mutations on the room barrier, and document that safe parallelization would require reservation before async resolution.

I would not defend the current code by saying “the homeserver already ordered the sends.”
That argument is too weak for this implementation.
The local cache code plainly allows lookup failure and plainly skips append on `UNKNOWN`.
Once the code does that, ordering before resolution is a correctness property of the cache, not a reviewer preference.

## 5. Specific revert scope

Do not revert the whole branch tip.
If you want the smallest clean code rollback, revert commit `a336f8ad7` or back out its equivalent hunks in the three files it touched.
That commit added no tests, so there is no valuable test payload inside it that needs to be preserved.

The concrete rollback is:

- Remove `queue_room_preflight()` from [write_coordinator.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/cache/write_coordinator.py:565).
- Remove `_room_preflight_tasks` and the idle-wait bookkeeping that exists only to support those unordered preflights in the same file.
- Remove `queue_room_cache_preflight()` from [thread_write_cache_ops.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/cache/thread_write_cache_ops.py:50).
- In [thread_writes.py](/srv/mindroom-worktrees/issue-176-thread-barrier/src/mindroom/matrix/cache/thread_writes.py:266), remove `_resolve_and_route_outbound_event_notification()`, `_resolve_and_route_outbound_redaction_notification()`, `_schedule_fail_open_room_preflight()`, and `_schedule_fail_open_impact_update()`.
- Restore the pre-a336 outbound routing shape: explicit `thread_id` or `thread_id_from_edit` may still go directly to `queue_thread_update`, and every lookup-dependent outbound message or redaction stays under `queue_room_update`.
- Add one short explanatory comment near that room-update fallback saying that lookup-dependent outbound mutations stay on the room barrier because earlier outbound writes can create the lookup rows needed to resolve thread impact, and safe parallelization would require reservation-based routing.

There are a couple of test references to the preflight API that would need follow-up adjustment if code is reverted, notably [tests/test_threading_error.py](/srv/mindroom-worktrees/issue-176-thread-barrier/tests/test_threading_error.py:722) and the inline coordinator stub around [tests/test_threading_error.py](/srv/mindroom-worktrees/issue-176-thread-barrier/tests/test_threading_error.py:5166).
Those are coupling artifacts to the new API, not reasons to keep the API.
In the next code PR, I would also add one real regression test for the reproduced case above so the team does not revisit this debate.
