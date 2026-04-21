# ISSUE-176 Thread Barrier Architecture Consult

I read the current versions of `src/mindroom/matrix/cache/write_coordinator.py`, `src/mindroom/matrix/cache/thread_writes.py`, `src/mindroom/matrix/cache/thread_reads.py`, the branch-added tests in `tests/test_threading_error.py`, and the branch commit history.

## 1. Is this the right abstraction?

The room/thread conflict model is the right shape, but the current abstraction is not.

Allowing unrelated thread updates to run concurrently while still forcing room-wide updates to fence the whole room matches the real concurrency requirements.

What does not fit is encoding that model as background tasks plus mutable tail pointers plus cancellation-aware predecessor graphs.

The moment room updates started waiting on `ALL` thread tails, this stopped being a linear barrier chain and became a DAG with join nodes.

`write_coordinator.py` still stores and repairs state as if each namespace had one meaningful predecessor tail, and that mismatch is exactly where the recurring bugs are coming from.

So my answer is: keep the room-vs-thread conflict model, but do not keep the current “task graph encoded in task objects and tail rollback maps” implementation strategy.

## 2. What is the root cause of the recurring cancellation bug class?

The recurring bug class is mostly caused by this implementation strategy, not by per-thread barriers in general.

The current code mixes two different models that do not compose cleanly under cancellation.

At queue time it computes a predecessor set, which is DAG state.

At cleanup time it restores a single tail predecessor, which is linear-chain state.

At idle-wait time it mutates coordinator state again from observer code, which duplicates cleanup logic in a second code path.

That is why round 2 fixed the forward cancellation case, but round 4 found the backward case and the `wait_for_thread_idle()` case.

Those are not unrelated bugs.

They are the same invariant leaking through different repair paths.

The strongest signal is the current room-tail cancellation bug.

A cancelled room task can represent “waited on A and B,” but `_clear_room_tail()` can only restore one room-tail predecessor or `None`.

That is not enough information to preserve the intended ordering.

The outbound routing regression has a related but separate root cause.

`thread_writes.py` is choosing the barrier from a local fast-path classification before `_apply_outbound_event_notification()` runs the canonical resolver.

That duplicates scheduling logic at the call site, which is why the R2 guard drifted away from `thread_id_from_edit` and over-serialized threaded edits.

## 3. Three options

### (a) Continue patching round-by-round with the current design

Case for: this is the smallest short-term delta, the public API is already in place, and the latest tests are finally exercising the reported failures instead of passing incidentally.

Case against: I would expect roughly 2 to 4 more review rounds before I would trust convergence, because every path that observes or cleans up cancelled tasks now has to reconstruct DAG semantics from partial tail state, and the outbound routing logic is still a separate leaky classification layer.

### (b) Refactor cancellation handling within the current per-thread/per-room barrier model

Case for: this keeps the intended semantics and public surface area (`queue_room_update`, `queue_thread_update`, `run_*`, `wait_for_*`) while removing the specific mechanism that keeps failing.

The refactor I would do is to replace predecessor maps and tail rollback with explicit per-room scheduler state that knows which room op is active, which thread ops are active, which ops are queued in arrival order, and which queued ops become runnable after enqueue, completion, or cancellation.

Case against: this is a meaningful rewrite of `write_coordinator.py`, not a targeted patch, so it is more work than fixing the two currently reported bugs and it will require carefully preserving the existing ordering semantics that the new tests now encode.

### (c) Replace the barrier model entirely

Case for: a different primitive such as a per-room scheduler or a writer-preferring room gate plus per-thread locks can be much simpler than task-predecessor graphs, and cancellation becomes ordinary queue removal or lock release instead of graph repair.

Case against: this risks changing semantics rather than just fixing implementation, especially around what should happen to a thread update that was queued behind a room-wide op that later gets cancelled, so it is the highest-risk option unless you are explicitly willing to re-spec those ordering rules.

## 4. Recommendation

I recommend option (b).

The design goal is correct.

The implementation technique is not.

This does not look like a fundamentally bad product abstraction that needs a greenfield replacement.

It looks like a good room/thread conflict model expressed through the wrong low-level primitive.

I would not spend more rounds polishing the current predecessor-map approach.

I also would not jump straight to a new semantic model unless you want to reopen the ordering contract itself.

A scheduler-style refactor inside the current API boundary is the smallest change that attacks the actual source of the repeated failures.

## 5. Minimum-viable refactor scope

Keep the public API additions from this branch.

Keep `thread_reads.py` on the same-thread path.

Keep the live-write and read-side idea that unrelated threads should not block each other.

Throw away the cancellation-specific graph repair machinery in `write_coordinator.py`.

That means `_room_update_predecessors`, `_room_tail_predecessors`, `_thread_update_predecessors`, `_thread_tail_predecessors`, `_pending_predecessors()`, `_await_predecessors()`, `_clear_room_thread_tail_if_current()`, and the whole idea of restoring ordering by re-pointing a single tail task after cancellation.

Replace that with one explicit per-room state object.

That state object should track queued operations in order, the active room operation if any, the active thread operations by thread id, and one wakeup path that re-runs scheduling after enqueue, completion, or cancellation.

`wait_for_room_idle()` and `wait_for_thread_idle()` should observe that scheduler state, not mutate barrier tails as a side effect.

In `thread_writes.py`, keep only one direct-thread fast path for outbound events and make it match the resolver’s explicit-thread rule by using `event_info.thread_id or event_info.thread_id_from_edit`.

I would not throw away the per-thread feature itself.

I would throw away the predecessor-graph-on-top-of-task-tails implementation.

If you want the smallest possible scope, most of the refactor can stay inside `src/mindroom/matrix/cache/write_coordinator.py`, plus a small outbound routing cleanup in `src/mindroom/matrix/cache/thread_writes.py`.
