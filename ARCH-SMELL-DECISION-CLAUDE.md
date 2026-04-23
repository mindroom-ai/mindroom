# ARCH-SMELL DECISION — ISSUE-176 outbound FIFO regression (Claude)

Branch: `issue-176-thread-barrier` @ `99aa0f61f`
Author: Claude (Opus 4.7)

## TL;DR

Revert the outbound parts of `a336f8ad7` (Option A).
Keep the regression tests from `99aa0f61f` minus the ones that lock in the
broken preflight semantics.
File ISSUE-189 for outbound parallelism. Ship ISSUE-176.

## 1. Is FIFO actually a hard correctness invariant?

**Yes — for the local advisory cache, not for Matrix itself.**

- The Matrix homeserver orders events by `origin_server_ts` and is the
  authoritative source of truth. Outbound `send_message_result()` waits
  for the homeserver to acknowledge each event before returning, so the
  *server-visible* order of A and B is correct regardless of what we do
  locally.
- The bug lives one level down. `notify_outbound_*()` is fire-and-forget
  bookkeeping that maintains the local SQLite advisory cache so that
  thread-history reads do not have to round-trip the homeserver. The new
  preflight design lets B's resolver run against a SQLite snapshot where
  A has not yet been written. When B is an *edit* of A or a *redaction*
  of A and A is not yet in cache, B's resolver returns `UNKNOWN` and the
  whole room cache gets invalidated via `mark_room_threads_stale`.
- This matters in production for two concrete real workloads:
  - **Streaming responses (`streaming.py:522, 533`)**: the initial send
    carries an explicit `m.relates_to.m.thread`, so its EventInfo has
    `thread_id` and the resolver returns synchronously. But every
    subsequent edit uses `m.relates_to.m.replace`, and
    `client_delivery.py:329` *pops* `m.relates_to` from the inner
    `m.new_content`, so `EventInfo.thread_id_from_edit` is `None`. The
    edit's resolver therefore needs to look up the original event in the
    SQLite cache — and during streaming we generate dozens of
    back-to-back edits whose preflights race against each other and
    against the initial append. Every losing race wipes the cache for
    the room.
  - **Outbound redactions of just-sent messages (e.g., stop-tool, retry,
    delivery_gateway recovery)**: the redaction needs to look up the
    target's thread membership; if the target's append is still queued,
    the redaction degrades to a room-level invalidation.
- Sibling-thread "two unrelated agents typing in different threads" is
  the only workload where the lost room serialization would have
  delivered *any* parallelism win, and even there the SQLite append
  takes 1–5 ms; the upside is small.

So FIFO is a hard invariant for the *advisory cache*, and breaking it
turns streaming-edit storms into room-wide cache invalidations. That is
strictly worse than the small parallelism win the new path was meant to
deliver.

## 2. Validating the bug

I wrote `/tmp/measure_outbound_fifo.py`. It exercises
`_EventCacheWriteCoordinator` directly: two `queue_room_preflight()`
submissions in order A→B, with A's "resolver" sleeping 200 ms and B's
sleeping 50 ms; both then enqueue a same-thread update on the same
`thread_id`. A's append blocks on an event so the timeline is observable.

```
$ .venv/bin/python /tmp/measure_outbound_fifo.py
Order after 150 ms (A still blocked): ['B']
Final completion order: ['B', 'A']
FIFO BROKEN
```

This is the coordinator-level demonstration of REV-A/B/C/D/E's claim:
because the preflight does its async work *outside* any ordered queue
position and only enqueues the real `queue_thread_update` after
returning, a faster preflight wins the race regardless of submission
order. This is exactly the path now used by both
`notify_outbound_event()` (line 332) and `notify_outbound_redaction()`
(line 478) in `thread_writes.py`.

The downstream consequence depends on the resolver's data dependency on
prior appends:
- if B carries an explicit `thread_id` in its content, B's resolver
  returns immediately and the FIFO break is "only" about per-thread
  append order (mostly benign — edits are commutative on a thread);
- if B's resolver depends on a SQLite row that A's append would have
  written, B falls through to `UNKNOWN` and we invalidate the room
  (REV-A's and REV-D's exact reproduction). This is the workload that
  matches mindroom's streaming-edit pattern.

## 3. Option scoring

### Option A — revert the outbound fix entirely

**For:** Smallest, surest correct change. Keeps the proven 2× speedup
from `db5e9bf83` / `50945af51` (the read-vs-write win that ISSUE-176 was
actually filed for). Eliminates the regression now, with zero new risk.
Honest about scope: outbound parallelism was never in the original
ISSUE-176 problem statement; reviewers asked for it as scope creep and
the first attempt broke FIFO.

**Against:** The previous 4 review rounds will continue to flag the
"outbound still on room barrier" smell. Bas explicitly asked us to fix
it in this PR.

**Cost:** ~1 agent-hour. 1 review round to confirm clean revert.

### Option B1 — coordinator placeholder/reservation API

**For:** The only design that delivers thread-barrier parallelism for
outbound *and* preserves FIFO. Conceptually clean: the preflight reserves
a slot up-front, then "upgrades" to the resolved scope.

**Against:** New coordinator surface (`reserve_room_slot()`,
`upgrade_to_thread()`), new states in `_RoomSchedulerState`, new
edge-case interactions with the existing `_QueuedRoomFence` /
`ignore_cancelled_room_fences` / cancellation logic. The current
coordinator is already the most subtle code in the repo (see all the
review rounds it took to get the *cancelled-fence* corner right). Adding
"upgrade in place" risks more REV-X rounds. Not justified by the
measured upside (outbound bottleneck is unproven; SQLite appends are
1–5 ms).

**Cost:** ~6–10 agent-hours, 2–3 more review rounds.

### Option B2 — keep resolution INSIDE the ordered barrier, then re-queue

**For:** Mostly correct by construction (room barrier preserves
submission order before the re-queue happens).

**Against:** The re-queue hop happens *before* the append finishes. So
if A's room task does `resolve → re-queue thread T → return`, A's room
slot completes and B's room task starts before A's append has actually
written its lookup row. B's resolver still races against A's pending
append. Same bug, slightly rearranged. Even if you keep the *whole*
A-pipeline on the room barrier, you have lost any parallelism — that is
just Option A with extra hops. Not actually a different option.

**Cost:** ~3 agent-hours, plus discovering it does not work, plus
Option A.

### Option C — same as A but with a pointed comment

**For:** Same correctness as A. The comment forecloses the "this is
wrong, please fix" review loop on the next PR.

**Against:** Same as A — reviewers will still flag it; comments are
weak protection. Marginal value over A.

**Cost:** ~1.5 agent-hours, 1 review round.

### Option D (proposed) — synchronous fast-path + room fallback

**For:** If `event_info.thread_id` (or `thread_id_from_edit`) is set
*synchronously* from the content payload, route directly to
`queue_thread_update()` (no preflight, no race). Otherwise fall back to
`queue_room_update()` (the safe FIFO path). This delivers thread-barrier
parallelism for the *easy* outbound case (any client that includes the
thread relation in the message — Element, Cinny, mindroom's own
`send_threaded_message_result`) without touching the coordinator at all.

**Against:** Mindroom's own *edits* deliberately drop
`m.relates_to` from `m.new_content` (`client_delivery.py:329`), so the
streaming workload — the one with the most outbound traffic — would
still go through the room barrier. Net win is small.
Also: this is "fix the easy half and document the hard half." If we ship
it without also fixing the edit path, the next reviewer will flag the
edit path. To avoid that, we would have to also stop dropping
`m.relates_to` from `m.new_content` in `build_edit_event_content()`.
That is a Matrix-spec change that needs separate cross-client testing.

**Cost:** ~3 agent-hours for the routing change alone, ~6–8 if the edit
content fix is bundled. 1–2 review rounds.

## 4. Recommendation: Option A (revert outbound fix)

Reasoning:

1. **Honestly scoped, ISSUE-176 is done.** The 401 → 200 ms speedup is
   from the read-vs-write parallelism on the *thread barrier*, not from
   anything outbound. That fix is in `db5e9bf83` / `50945af51` and the
   regression test (`tests/test_issue_176_real_thread_parallelism.py`)
   covers it. Reverting the outbound preflight does not regress that.

2. **The new bug is actively bad.** The streaming-edit workload would
   trigger room-wide cache invalidation on every burst of edits. That
   defeats the very thread cache the rest of this PR was speeding up.
   This is a strict regression, not "less optimal."

3. **The "real fix" (B1) is too risky for the upside.** A new
   coordinator API in code that just took five review rounds to get
   right, in exchange for a few ms saved on a SQLite append that was
   never measured to be a bottleneck. Wrong trade.

4. **Bas's two stated goals are not equally weighted.** "Robustly solve
   ISSUE-176 latency" is the contract — done. "Actually fix outbound
   routing" is a stretch goal that, in retrospect, exposed an
   architectural smell larger than this PR. Filing ISSUE-189 with the
   ARCH-SMELL artifacts and the placeholder-reservation sketch is the
   correct discharge of that signal — it preserves the work and lets
   the next PR address it deliberately, with proper benchmarks first.

5. **The five-out-of-six concurrent reviewer signal is data, not
   noise.** Five independent reproductions of the same bug, with three
   different concrete scenarios (REV-D's reply-to-A, REV-A's
   redact-A, REV-E's two-message-race), is as strong an "abandon this
   approach" signal as we ever get. REV-F missing it is not evidence
   the others are wrong; it confirms the bug is in a path REV-F's tests
   did not exercise.

## 5. (B1 sketch — included for ISSUE-189, not for this PR)

If/when outbound parallelism is actually proven to matter, the minimum
viable coordinator API is:

```python
def reserve_room_slot(self, room_id: str) -> _SlotHandle: ...
async def upgrade_slot(
    self,
    handle: _SlotHandle,
    *,
    kind: Literal["room", "thread"],
    thread_id: str | None,
    update_coro_factory,
    name: str,
) -> asyncio.Task[object]: ...
```

`reserve_room_slot()` synchronously appends a `_QueuedReservation` entry
into `state.entries`. Reservations behave exactly like `kind="room"`
entries for the purposes of `_reevaluate_entry()` (they block both later
room and later thread work). `upgrade_slot()` swaps the reservation in
place: if `kind="thread"` and no later same-thread entry has been
queued, the reservation collapses (still blocks until upgraded);
otherwise the queued task inherits the reservation's sequence number.

This preserves FIFO (same-thread predecessors still see each other in
submission order) while letting two *different* thread reservations in
the same room run in parallel once both have been upgraded.

This is ~6–10 agent-hours plus tests. Not in scope for this PR.

## 6. What to revert (concretely)

This is **not** `git revert a336f8ad7 99aa0f61f`. It is selective:

**Revert these (the broken path):**

- `src/mindroom/matrix/cache/thread_writes.py`:
  - drop `_apply_outbound_event_notification_with_impact`,
  - drop `_resolve_and_route_outbound_event_notification`,
  - drop `_apply_outbound_redaction_notification_with_impact`,
  - drop `_resolve_and_route_outbound_redaction_notification`,
  - drop `_schedule_fail_open_room_preflight`,
  - drop `_schedule_fail_open_impact_update` and
    `_schedule_fail_open_thread_update`,
  - restore `notify_outbound_event` to the origin/main version that
    calls `_schedule_fail_open_room_update(...)` with
    `_apply_outbound_event_notification`,
  - restore `notify_outbound_redaction` to call
    `_schedule_fail_open_room_update(...)` with
    `_apply_outbound_redaction_notification`.
- `src/mindroom/matrix/cache/thread_write_cache_ops.py`:
  - drop `queue_room_cache_preflight()` (only consumer was the broken
    path).
- `src/mindroom/matrix/cache/write_coordinator.py`:
  - drop `queue_room_preflight` from the `EventCacheWriteCoordinator`
    `Protocol`,
  - drop `queue_room_preflight()` implementation,
  - drop the `_room_preflight_tasks` field,
  - drop `_room_preflight_tasks` references in
    `_prune_done_task_maps()`, `_room_is_idle()`, `_thread_is_idle()`,
    `_fallback_room_tasks()`, `_fallback_thread_tasks()`, `close()`.

**Keep these (the proven wins):**

- All of `db5e9bf83` (`Add timing-path thread barrier regression test`).
- All of `50945af51` (`Fix timed live threaded cache appends`).
- `60debf098` (REV-B same-thread ordering with cancelled fences) — this
  is the live-event path, unrelated.
- `63d82ab81` (REV-F UNKNOWN-impact live mutation routing) — also live
  path.
- `c899f176b` (lint).
- `tests/test_issue_176_real_thread_parallelism.py` from `83d6ffd65`.

**From `99aa0f61f` (test commit) — keep the parts that test outbound
behavior on the room barrier:**

- The reaction fast-path test (still valid; reactions still go through
  `_schedule_fail_open_room_update`).
- The "outbound message with explicit thread_id ends up on the right
  thread" test (still valid; same answer with the room barrier path
  because it queues a room update that internally invalidates the right
  thread).
- Drop the tests that lock in `queue_room_preflight()` semantics
  (anything asserting that two same-thread outbound preflights interleave
  with thread predecessors). They were green only because they exercised
  the broken design.

**Mechanically:** easiest path is `git revert a336f8ad7` (clean revert
of the production code), then on top of that `git restore --source=HEAD~
-- tests/test_threading_error.py` to keep all the new tests and let the
preflight-specific assertions fail; then surgically delete the failing
ones. Total diff vs `e2382775f`: production code returns to the proven
state; test suite gains the still-valid outbound coverage.

After the revert, file ISSUE-189 with this document and
`/tmp/measure_outbound_fifo.py` attached as the "what we know" baseline.
