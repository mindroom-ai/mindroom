# ISSUE-176 Architecture Decision

## 1. Validation Honesty

REV-C is correct about the specific scenario measured by `tests/test_issue_176_validation.py`.
That test is honest, targeted, and materially better than the earlier mock-cache evidence because it uses a real `_EventCache`, a real SQLite database, and a delay injected inside the actual locked cache read path.
The critical mechanism is visible in `tests/test_issue_176_validation.py:130-166`, where `load_thread_events()` is patched to sleep while thread A is already inside the real cache read, and thread B then measures wall-clock delay for its own `get_thread_history()` call.
The branch and `origin/main` medians in `ARCH-VALIDATION.md` are effectively identical at `202.2 ms` versus `201.8 ms`, and I re-ran the validation test in this worktree and it passed.

The code explains why the result is flat.
Every `_EventCache` read and write goes through `_read_operation()` or `_write_operation()` in `src/mindroom/matrix/cache/event_cache.py:498-520`.
Those helpers call `acquire_db_operation()` in `src/mindroom/matrix/cache/event_cache.py:328-338`.
`acquire_db_operation()` holds both the global `_db_lock` and the per-room lock for the entire database operation.
So even after a sibling thread escapes the coordinator queue, it still re-serializes when it reaches the cache layer.

The branch therefore does not deliver meaningful parallelism for DB-bound sibling thread reads in the same room.
It also does not deliver meaningful parallelism for DB-bound sibling thread writes or read-write pairs in the same room, because they all funnel through the same `_EventCache` critical section.
In fact, the cache is even more serialized than the issue summary suggests.
The per-room lock is not the only limiter.
The global `_db_lock` is acquired for every operation first, and the cache uses a single `aiosqlite` connection.
In `.venv/lib/python3.12/site-packages/aiosqlite/core.py:96-124`, `aiosqlite.Connection.run()` drains a single queue on one worker thread, so one connection executes one DB function at a time even before SQLite file-level contention enters the picture.

That means Option A as currently phrased is underspecified.
Replacing `acquire_room_lock(room_id)` with a thread-aware lock is not enough by itself.
If `_db_lock` stays wrapped around every operation and the implementation keeps one `aiosqlite` connection, thread-aware room locks still do not create true concurrent cache operations.
My extra measurement confirmed that point from the outside.
I ran a read-only benchmark that delayed one locked cache read in room A and then measured a read in room B.
The median cross-room delay was `200.7 ms`, which shows that even unrelated rooms are serialized underneath the coordinator.

That said, the synthetic validation is not exhaustive.
It measures only a DB-bound cache-hit path.
It does not measure a stale-thread refresh where the expensive phase happens before the eventual cache write.
That gap matters because `ThreadReadPolicy` now waits only for same-thread work and then wraps the whole refresh in `run_thread_update()` at `src/mindroom/matrix/cache/thread_reads.py:54-110`.
If the slow part is the homeserver fetch or another pre-cache phase, sibling thread refreshes can overlap in the branch even though they later serialize at the cache write.

I tested that case too.
Using the real coordinator and the real `_EventCache`, I measured two sibling thread refreshes with an injected `200 ms` sleep before the cache write.
With thread-scoped scheduling, the second refresh completed in a median `219.9 ms`.
With room-scoped scheduling, the second refresh completed in a median `413.0 ms`.
So the branch is not worthless in every scenario.
It does provide real overlap when the slow part is upstream of `_EventCache`.

My bottom line on validation honesty is therefore this.
REV-C is right that the current branch does not solve cache-layer contention and that the validation test proves that cleanly.
REV-C is too absolute only if “zero measurable user-visible parallelism” is taken as a universal statement.
The branch can still help network-bound or otherwise pre-cache-bound thread refreshes, but the current validation does not measure that case and therefore neither proves nor disproves that narrower benefit.

## 2. Option Scoring

### Option A

Case for.
Option A is the only option that aims at the actual cache-layer serialization point rather than the upstream symptom.
If the original production stalls were dominated by long `_EventCache` operations, then only cache-layer granularity changes can create real parallelism for sibling thread refreshes.
The cache is already in WAL mode via `PRAGMA journal_mode=WAL` in `src/mindroom/matrix/cache/event_cache.py:48`, so the storage engine is at least configured in the direction of more concurrency.
A successful A would address not only sibling-thread reads but also sibling-thread writes and read-write pairs, which is the robust solution the issue statement asks for.

Case against.
As written, A is too small to work.
Thread-aware room locks alone do not defeat the global `_db_lock` or the single queued `aiosqlite` connection.
So the real A is a larger architectural change that likely requires splitting lifecycle locking from operation locking, auditing which invariants actually need cross-thread serialization, and probably moving from one connection to either separate read and write connections or a small connection strategy per cache runtime.
That is a much riskier change than the option text implies, because the current room lock is clearly load-bearing for cache state, invalidation ordering, and atomic multi-table writes.

Estimated cost.
I would budget roughly `30-50 agent-hours` and `4-6 review rounds`.
Most of the cost is not code typing but invariant audit, failure-mode review, and building benchmarks that distinguish SQLite limits from application locks.

### Option B

Case for.
Option B is defensible only if the PR is reframed very narrowly.
The branch does create overlap for sibling thread refreshes when the slow phase is before the cache write, and my benchmark showed a real difference of about `220 ms` versus `413 ms` under that pattern.
The production signal was `predecessor_wait_ms` at the coordinator, and this branch does reduce that kind of coordinator wait for unrelated thread refreshes.
If the dominant real-world pain is “one slow refresh blocks other refreshes before they even start,” then B can still produce user-visible wins without touching SQLite.

Case against.
The current branch is far too large and semantically ambitious for that modest claim.
It adds a new scheduler model, thread-scoped queue APIs, cancelled-room-fence rules, thread-scoped write routing, and hundreds of tests, while the central advertised benefit is absent on the real cache-hit contention path.
Shipping this as a latency fix would be misleading unless the PR description explicitly says it does not reduce `_EventCache` contention and does not improve DB-bound sibling thread reads.
Even with that honesty, `1484` inserted lines for a benefit that is narrow, scenario-dependent, and not tied back to production traces is hard to justify.

Estimated cost.
If the team insists on B, I would budget `10-18 agent-hours` and `2-4 review rounds` to cut scope, rewrite the PR story, delete non-essential thread-write changes, and keep only the pieces that actually earn their complexity.
Shipping the branch as-is would likely cost less coding time but more review time and more future maintenance debt.

### Option C

Case for.
Option C is the cleanest truthful response to the evidence now in hand.
The branch does not robustly solve the issue as framed, because the cache layer still serializes everything important.
Shelving avoids landing a complex concurrency model whose headline claim is false on the measured root-cause path.
It also resets the architectural discussion around the real constraints, namely the global `_db_lock`, the room lock, and the single `aiosqlite` connection, instead of arguing endlessly about scheduler behavior above a still-serialized cache.

Case against.
C throws away real work.
The branch did uncover genuine cancellation edge cases, it did improve the coordinator’s explicitness, and it does appear to help one meaningful scenario, namely sibling refreshes whose long phase is upstream of SQLite.
Discarding all of that will feel costly after multiple review rounds, and there is a non-zero chance that a narrower version of this work could have relieved some production pain sooner than a full cache redesign.

Estimated cost.
I would budget `4-8 agent-hours` and `1-2 review rounds`.
Most of that time is for harvesting the useful evidence, deciding which tests or notes survive as follow-up artifacts, and opening a clean replacement ticket with the right problem statement.

## 3. Recommendation

I recommend Option C.
I do not recommend it because the branch is worthless.
I recommend it because the branch is not a robust fix for the stated problem, and the repository guidance here is explicit about avoiding scope creep and not shipping complexity without measured payoff.

The strongest argument against C is the slow-fetch overlap result.
I think that result matters.
It proves the branch is not merely theatrical.
But it is still not enough to justify landing this exact architecture.
The original complaint was thread-history refreshes stalling for `100s+` and blocking unrelated sibling refreshes.
The current evidence says two different mechanisms can produce that symptom.
One mechanism is long work inside the coordinator before SQLite is touched, and this branch helps there.
The other mechanism is long work once `_EventCache` is entered, and this branch does not help there at all.

Because the validation branch was opened to solve the issue robustly, the burden of proof is higher than “it helps in one synthetic case.”
Right now the cache layer remains globally serialized below the new scheduler.
That makes the current PR too expensive for what it guarantees.
If the team lands it, they will own the maintenance burden of a thread-aware scheduler while still needing a second architectural pass to get real cache concurrency.
That is exactly the kind of double-spend the project guidance warns against.

The honest next step is a clean follow-up issue that states the root problem precisely.
That issue should say that `_EventCache` currently serializes every operation behind a global `_db_lock`, a room lock, and one `aiosqlite` connection, so thread-aware coordination above it cannot create DB-level parallelism.
If the team later wants a smaller latency PR for slow pre-cache refresh overlap, it should be proposed as that narrower thing, not as the fix for ISSUE-176.

## 4. Why I Am Not Recommending A

I am not recommending A because the minimum viable version is larger than it first appears.
A real A is not “swap room locks for thread locks.”
A real A is “change the `_EventCache` concurrency model.”
That likely means a distinct lifecycle lock for initialize and close, a separate operation lock strategy, and a decision about whether reads and writes keep sharing one `aiosqlite` connection.
Without those changes, A does not buy the promised parallelism.

I would only start A after adding focused benchmarks for three cases.
The first case is same-room sibling thread read-read.
The second case is same-room sibling thread write-write.
The third case is different-room read-read, because the current architecture appears globally serialized even across rooms.
Until those are benchmarked on a prototype, A is too large a leap for this repository’s “smallest correct change” rule.

## 5. If The Team Forces B Anyway

The minimum honest PR description for B would be this.
This change narrows coordinator scope for thread refreshes and some thread-specific mutations so unrelated threads in the same room can overlap during non-SQLite phases.
It does not make `_EventCache` concurrent.
Real-cache validation shows no improvement for DB-bound sibling thread cache reads because `_EventCache` still serializes operations under `_db_lock`, room locks, and a single `aiosqlite` connection.
This PR is therefore a latency-shaping change for slow refreshes, not a fix for cache contention.

If B is forced through, I would cut everything that does not directly support that narrow story.
I would drop thread-specific routing changes in `thread_writes.py` and `thread_write_cache_ops.py` unless the team has separate evidence that write-side overlap matters in production.
I would also drop tests that exist only to encode broad thread-write concurrency semantics rather than read-path behavior.
What should remain is only the minimal coordinator surface and read-path behavior needed to allow sibling refresh overlap and to keep same-thread refresh ordering understandable.

## 6. What To Preserve And What To Discard Under C

Preserve the validation evidence.
`ARCH-VALIDATION.md` should survive at least as issue context, and `tests/test_issue_176_validation.py` is worth keeping or adapting because it prevents future claims that the real cache already benefits from thread-aware coordination.
Preserve the new benchmark findings in the issue notes as well, specifically that DB-bound sibling reads remain flat while slow pre-cache refreshes do overlap.

Preserve only room-only cancellation regressions if they can be shown independently on `origin/main`.
The best candidates are the tests around “cancelled queued room work should not start” and “cancelled queued room work should not let later room work skip a running predecessor.”
Those are valuable even without thread barriers, but they should be spun into a separate small PR only if reproduced cleanly against the room-only coordinator.

Discard the thread-aware coordinator API and its semantic surface.
That means `queue_thread_update()`, `run_thread_update()`, `wait_for_thread_idle()`, the explicit per-room scheduler state in `write_coordinator.py`, the thread-aware read routing in `thread_reads.py`, and the thread-aware write routing in `thread_writes.py` and `thread_write_cache_ops.py`.
Discard the tests that assert unrelated same-room threads may run concurrently, that cancelled room fences must be bypassed by unrelated thread reads, or that outbound threaded edits must use a claimed thread barrier, because those tests encode the shelved architecture rather than a bug that still exists on `main`.

My recommendation in one sentence is this.
Do not ship a large thread-aware coordinator above a still-globally-serialized cache.
Preserve the evidence, open a clean cache-concurrency ticket, and only resurrect a smaller latency PR later if production data shows that pre-cache fetch overlap is the dominant real-world stall.
