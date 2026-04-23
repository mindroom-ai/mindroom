# ISSUE-176 architecture decision — CLAUDE

## TL;DR

Ship a slimmed Option B (per-thread coordinator) **but only after replacing the
committed validation test with one that actually models the production
bottleneck**. The current validation script is measuring the wrong thing and
its "0.4 ms difference" finding does not falsify the PR — it falsifies the
test. The coordinator does deliver real parallelism for the case ISSUE-176
was filed about; it just cannot help the synthetic SQLite-bound scenario
the test was written for. Option A (split the `_EventCache` room lock) does
not solve the residual problem either: a single shared `_db_lock` already
serializes every cache operation in the process, so per-thread room-lock
granularity buys nothing without a connection-pool refactor. That refactor
is out of scope.

## 1. Validation honesty

REV-C's reading of the *committed* test is technically correct and the
numbers are real: 202.2 ms vs 201.8 ms on this branch vs `origin/main`. But
the test is measuring SQLite-bound serialization, not the production
contention pattern, so the conclusion "no measurable user-visible
parallelism" generalises further than the evidence supports.

Two structural facts in `event_cache.py` explain why:

- `_EventCacheRuntime.acquire_db_operation` (line 327–338) does
  `async with self._db_lock, self.acquire_room_lock(room_id, ...)`. The
  `_db_lock` is a single process-wide `asyncio.Lock` (line 235) held for the
  full duration of every read and every write. The per-room lock is layered
  inside it, so the *room* lock can never actually be contended in a
  meaningful way: by the time you hold `_db_lock` you've already serialized
  with everyone else in the cache. From a concurrency-control perspective
  the room lock is more about ordering and observability than about
  parallelism.
- The injected `LOCK_HOLD_SECONDS = 0.2` sleep in
  `tests/test_issue_176_validation.py:146` patches `load_thread_events`,
  which runs *inside* `_EventCache._read_operation` and therefore *inside*
  `_db_lock`. So thread B's read on the same `_EventCache` is forced to wait
  for `_db_lock`. The coordinator runs upstream of the cache; it can let
  thread B's coro start, but the moment the coro reaches a
  `_read_operation`, it re-serializes at `_db_lock`. The test was
  guaranteed to find ≈200 ms regardless of any change to the coordinator.

The production scenario that filed ISSUE-176 was different. The 100 s+
`predecessor_wait_ms` came from `read_thread` queueing
`fetch_thread_history_from_client` through the room-scoped barrier on
`origin/main`. That fetcher hits the Matrix homeserver
(`fetch_thread_history` → /relations) and runs *outside* `_db_lock`; the
SQLite touches happen at the very end, very briefly. Two slow homeserver
fetches for sibling threads are the situation where parallelism is
actually achievable.

I ran a falsification measurement against the real coordinator
(`/tmp/measure_network_bound_thread_parallelism.py`, not committed) that
mirrors that production shape — two coros with a 200 ms async sleep
representing the network call, scheduled through the *same* coordinator
this branch ships:

```
sleep_per_fetch_ms=200
room_scoped_(serial)_ms_samples   = [409.6, 401.1, 402.0, 401.3, 401.7]
thread_scoped_(parallel)_ms_samples = [200.4, 200.4, 200.4, 200.4, 200.4]
room_median_ms   = 401.7
thread_median_ms = 200.4
```

So the coordinator delivers ~2x wall-clock speedup for two concurrent
sibling-thread reads when the slow region is the fetcher (the production
case), and 0x speedup when the slow region is inside `_db_lock` (the
committed test case). REV-C's interpretation is half right: the PR does not
help the *test*, but the test is wrong.

The PR could *also* help concurrent writes across sibling threads in the
same room (outbound notify, live append), again provided the bookkeeping
work that runs inside the coordinator is not 100 % SQLite — which is
usually true because resolver work calls back into other awaitables.

## 2. Option scoring

### Option A — extend per-thread granularity to `_EventCache`

**For:** It would be the only way to make the committed validation test
turn green. Conceptually it removes a layered lock that does no useful work
(see §1). If you ever do move to a connection pool, you'll need this anyway.

**Against:** It does not solve the user-visible problem on its own. As long
as `_db_lock` exists, splitting `acquire_room_lock` by thread changes
nothing observable: every operation still queues at `_db_lock` first. To
gain measurable parallelism here you would have to (a) drop `_db_lock`,
(b) introduce a connection pool of `aiosqlite` connections (one per active
thread or a small pool), (c) audit every `_read_operation` and
`_write_operation` for transactional invariants, and (d) confirm WAL mode
behaviour (`PRAGMA journal_mode=WAL` is set at line 48, so writes still
serialize but readers can run concurrent to a writer — only with separate
connections). That is a real cache-layer rewrite, not a small change.

**Cost:** ~3–5 agent-days, 3–4 review rounds, plus follow-up on cache test
parity. Material risk to durability invariants. Not justified by current
production data.

### Option B — ship the coordinator-only PR

**For:** It demonstrably reduces wall-clock latency for the
network-bound scenario that filed the bug (~2x for two concurrent sibling
thread reads, ~Nx for N). It targets the actual layer where production
`predecessor_wait_ms` was recorded. The cancellation-handling and scheduler
refactor in commit `cf3d45a81` are real bug fixes (the predecessor-graph
machinery had recurring cancellation ordering issues — see commits
`edca9a7df`, `052050920`, `412d1d2d9`, `aeb7ebb0c`, `edd43e0b0`) and would
need to ship eventually anyway. Tests in `tests/test_threading_error.py`
(+547 lines) lock down behaviours that have already regressed twice on
this branch.

**Against:** The committed validation test gives the wrong impression and
must be replaced or reframed before merge — otherwise reviewers and
future maintainers will read it as evidence that the PR does nothing. 1100
lines is a lot of new scheduler code, and a chunk of it (the timing
instrumentation, the cancelled-room-fence preservation logic) only exists
because of corner cases in this very design.

**Cost:** ~0.5 agent-day to swap the validation test for a network-bound
one and trim cruft, ~1 review round to confirm correctness, ~1 round of
production observation. Total ~1 day end to end.

### Option C — shelve the branch

**For:** Honest about the fact that the coordinator does not address every
form of cache contention; avoids shipping a 1100-line scheduler whose
correctness surface has needed five follow-up patches. Forces a clean
reframing of the original ticket as "room lock vs `_db_lock` granularity".

**Against:** Throws away a fix that *does* help the production scenario
that filed the bug. The cancellation-handling fixes and the regression
suite have to be re-derived later. Asking a future engineer to redo
the same scheduler is wasteful — the design is now well understood.
Shelving also implicitly endorses the wrong reading of the validation test.

**Cost:** ~2 agent-days to extract the cancellation fixes and regression
tests as a standalone PR, plus the future cost of re-implementing the
coordinator if the same production complaint resurfaces.

### Option D — ship Option B, file Option A (real `_EventCache` redesign) as a follow-up *only if* production traces still show contention

**For:** Matches the principle "smallest correct change that demonstrably
addresses the reported problem". Avoids speculative refactor of the cache
layer. Keeps the door open for the bigger redesign if it actually proves
necessary in production after a few weeks of observation.

**Against:** Two-PR plan; makes the next round of review work harder if
production data is ambiguous.

**Cost:** Same as Option B initially (~1 day), plus a deferred decision
gate. The follow-up only exists if metrics demand it.

## 3. Recommendation

**Option D, executed as a thin Option B today.**

Reasoning:

- The committed validation test is misleading evidence and must not ship in
  its current form. Replace it with a fetcher-side measurement (the script
  in `/tmp/measure_network_bound_thread_parallelism.py` is the basis — a
  proper version belongs under `tests/`). That measurement is what backs
  the claim "this PR delivers real wall-clock improvement on the production
  scenario".
- The branch already contains real bug fixes that are unrelated to the
  parallelism win and would otherwise need to be re-extracted: the
  scheduler rewrite resolves the recurring predecessor-graph cancellation
  bugs, and the cancelled-room-fence handling fixes a thread-history
  starvation bug discovered in QA on this branch. Shelving is wasteful.
- Option A would be the right answer *if* production traces still show
  contention after Option B ships. Until then, paying the cost of a
  connection-pool refactor is exactly the over-engineering the project
  philosophy warns against. Filing it as a follow-up gated on real metrics
  is the correct posture.
- Bas's stated goal is "robustly solve the original ISSUE-176 latency
  problem". The original problem is concurrent thread reads serialising
  through a room barrier. The coordinator solves that for the network-bound
  case. The remaining residual contention at `_db_lock` was never the
  reported complaint and is not currently observed in production.

## 5. Honest minimum-viable PR description (Option B / D)

> **Allow concurrent thread reads and writes inside the same room**
>
> The Matrix event-cache write coordinator previously serialized every
> queued operation in a room behind every other queued operation in that
> same room, even when the operations targeted independent threads. In
> production this surfaced as `predecessor_wait_ms` exceeding 100 s for
> thread-history refreshes when an unrelated room-scoped fetch was already
> in flight.
>
> This change introduces an explicit per-room scheduler that distinguishes
> room-scoped entries (which still serialize the whole room) from
> thread-scoped entries (which only serialize against same-thread
> predecessors and any room-scoped predecessor). The scheduler also
> replaces the previous predecessor-graph bookkeeping, which had needed
> repeated patches for cancellation ordering bugs.
>
> Measured impact: for two concurrent sibling thread reads with a 200 ms
> network fetch, wall-clock latency drops from ~400 ms (serialized) to
> ~200 ms (parallel). See `tests/test_issue_176_real_thread_parallelism.py`
> for the reproducer.
>
> **Known limitation:** the underlying `_EventCache` still serializes
> every SQLite operation through a single process-wide `_db_lock`. This
> PR does not change that. If a single SQLite write becomes slow, sibling
> thread operations will still queue at the cache layer. Production
> traces today indicate the contention was at the coordinator (network
> fetches), not the cache (SQLite I/O), so this PR addresses the reported
> symptom; a follow-up to redesign `_EventCache` for parallel SQLite
> access is gated on whether new traces show residual contention there.

### What to cut from the current 1100 lines

Most of it earns its keep, but a focused cleanup pass is reasonable:

- `_emit_idle_wait_timing` and the dual `_wait_for_room_idle_with_timing`/
  `_wait_for_room_idle_without_timing` paths add ~80 lines of
  instrumentation. Keep one path; the timing variant only matters when
  `timing_enabled()`. If timing is now off by default, fold the timing
  into the same path with a cheap branch.
- `_QueuedRoomFence` machinery (sequence preservation across cancelled
  room barriers) is load-bearing for the cancellation-fix commits —
  keep it, but a single comment explaining *why* it exists would cut
  the cognitive load of the next reviewer significantly.
- The duplicate timing decorators around outbound writes in
  `thread_writes.py` (`_append_live_threaded_event_with_timing` vs
  `_append_live_event_without_timing`) follow the same dual-path pattern
  and should also collapse.
- The misleading committed `tests/test_issue_176_validation.py` should
  be **replaced**, not kept alongside a corrected one. Its current form
  bakes in the wrong mental model.

Net: the trim probably removes 100–200 lines, replaces ~200 lines of
test, and leaves the scheduler intact. ~0.5 agent-day, 1 review round.

## 4. (Reserved — Option A sketch, in case D's follow-up is needed)

If a future production trace shows `_db_lock` is the hot path, the
minimum-viable change is *not* "make `acquire_room_lock` thread-aware".
That alone changes nothing because `_db_lock` is acquired *outside* the
room lock. The minimum change is:

1. Replace the single `aiosqlite.Connection` with a small pool (e.g. one
   reader connection per room, or a 2–4 reader pool plus one writer).
   `aiosqlite.connect` is per-connection; SQLite WAL mode (already enabled
   at `event_cache.py:48`) supports concurrent readers and one writer.
2. Replace `_db_lock` with: writers acquire an exclusive write semaphore
   (size 1); readers acquire a shared read permit (size N). This is a
   straightforward `asyncio.Lock` + `asyncio.Semaphore` pair.
3. Audit `_write_operation` callers for transactional safety. Most do a
   single `INSERT … ON CONFLICT` or a small batch inside `db.commit()` /
   `db.rollback()`; these are already self-contained transactions and
   safe under WAL.
4. The room lock can then be removed entirely, or kept only for higher-
   level invariants (e.g. ordering between sync timeline and outbound
   notify) that aren't already enforced by the coordinator.

Operations that are clearly safe to parallelize as readers: `get_thread_events`,
`get_recent_room_thread_ids`, `get_thread_cache_state`, `get_event`,
`get_latest_edit`, `get_latest_agent_message_snapshot`, `get_mxc_text`,
`get_thread_id_for_event`. That's all the `_read_operation` callers.

Operations that need writer coordination: the rest, all of which already
go through `_write_operation`.

WAL mode: yes, already in use (`event_cache.py:48`,
`PRAGMA journal_mode=WAL` plus `PRAGMA busy_timeout=5000`). The schema
audit is small (8 tables, no FOREIGN KEY constraints, primary keys
include `room_id`). The biggest risk is `replace_thread_locked` — it
deletes existing thread rows and inserts replacements; under concurrent
readers a partial state could leak. A `BEGIN IMMEDIATE` or explicit
write transaction handles that under WAL.

Estimated cost for the eventual Option A follow-up: 3–5 days, 3–4 review
rounds. **Do not start it speculatively.** Wait for evidence.

---

The branch should not be shelved. The branch should not grow into a
cache-layer rewrite. It should ship as a focused per-thread coordinator
fix with a *correct* validation test, the cleanups noted above, and a
follow-up ticket gated on production data. That keeps the scope tight,
honours the "smallest correct change" principle, and actually addresses
the production complaint that opened ISSUE-176.
