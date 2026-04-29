# Coalescing Gate Rewrite Plan

## Goal

Replace the old side-queue coalescing logic with a simpler ordered per-key queue.

The replaced design spread ordering across `pending`, `immediate`, `deferred_pending`, `force_flush_pending`, `phase`, and deadline state.

That makes command, grace, failure, cancellation, and in-flight behavior hard to reason about.

The new design makes one invariant central: per-key queue order is the source of truth.

## Core Invariants

- `enqueue()` only classifies, appends, wakes, logs, and returns.
- The drain task is the only code that decides dispatch policy.
- The drain task only processes from the front of the queue.
- Commands are FIFO barriers.
- Bypass events are FIFO barriers and dispatch solo.
- Bypass does not jump ahead of already queued user work.
- Claimed events leave the queue only when they are about to be dispatched.
- Unclaimed events never leave the queue.
- A failed claimed batch may be lost and logged, but unclaimed backlog remains queued.
- After a claimed batch failure, the drain continues processing unclaimed backlog unless the gate is shutting down.
- Shutdown drains queued work without debounce or upload-grace waits.
- Retargeting preserves drain ownership for already-claimed work.
- If the final implementation still needs `immediate`, `deferred_pending`, `force_flush_pending`, or a new ordering flag, the rewrite missed the point.

## State Model

Replace side queues with one ordered queue.

```python
class QueueKind(enum.Enum):
    NORMAL = "normal"
    COMMAND = "command"
    BYPASS = "bypass"


@dataclass
class QueuedEvent:
    kind: QueueKind
    pending_event: PendingEvent


@dataclass
class _GateEntry:
    queue: deque[QueuedEvent] = field(default_factory=deque)
    drain_task: asyncio.Task[None] | None = None
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    wake_generation: int = 0
    phase: GatePhase = GatePhase.DEBOUNCE
    deadline: float | None = None
```

Delete these fields from `_GateEntry`.

```python
pending
immediate
deferred_pending
force_flush_pending
```

## Enqueue Behavior

`enqueue()` should get or create the gate.

It should classify the event as `NORMAL`, `COMMAND`, or `BYPASS`.

It should append to `gate.queue`.

It should ensure the drain task exists.

It should wake the drain.

It should emit enqueue logs and timing.

It should return without dispatching.

It should not inspect in-flight state to decide ordering.

## Drain Algorithm

### Front Is Bypass

Pop one bypass event.

Dispatch it solo.

Continue draining.

Bypass events preserve FIFO order.

They do not coalesce with neighboring events.

### Front Is Command

Pop one command event.

Dispatch it solo.

Continue draining.

Commands preserve FIFO order.

They do not coalesce with neighboring events.

### Front Is Normal

Wait debounce unless woken by a barrier or shutdown.

Identify the front contiguous normal run as the candidate batch.

If the candidate batch is text-only and upload grace is enabled, wait grace while waking early on the next normal, command, or bypass event.

Extend the candidate with contiguous media that arrived before the next normal, command, or bypass barrier.

Pop and claim exactly the candidate batch from the queue.

Dispatch the claimed batch.

Continue draining.

Do not pop normal events before upload grace.

They remain in the queue until dispatch is about to start.

## Upload Grace Semantics

Normal debounce may coalesce adjacent normal text events.

Upload grace is narrower.

It exists to let media attached immediately after text join the same turn.

Only media may join an already-open text batch during grace.

A later normal text stays queued for the next turn.

A command or bypass event stays queued and acts as a barrier.

A later normal, command, or bypass event wakes the drain so the current candidate can flush promptly.

## Failure And Cancellation Semantics

When a claimed dispatch fails, log the failure.

Do not restore the claimed batch unless existing semantics require replay.

Keep unclaimed queued events untouched.

Continue draining unclaimed backlog unless shutting down.

When the drain task is cancelled, the claimed in-flight work is cancelled.

Unclaimed queue entries remain in order.

If this is not shutdown and work remains, restart or continue the drain.

## Retarget Lifecycle

One gate owns one drain task.

Claimed work belongs to the drain that popped it until that dispatch finishes, fails, or is cancelled by shutdown.

Retarget may merge unclaimed queue entries.

Retarget must not cancel a drain that has already claimed work.

If the source and destination gates both exist, prefer the in-flight owner when exactly one gate is in flight.

If the destination gate is in flight, keep the destination gate as owner because it may already have claimed canonical-thread work.

If neither gate is in flight, keep the destination gate as owner and merge the source queue into it.

After a merge, exactly one gate remains mapped to the canonical key and that gate is woken.

Retarget may cancel only a retired drain that has not claimed work.

Retired in-flight drains remain tracked until they finish so `is_idle()` and `drain_all()` include their claimed work.

## Tests

Add or update focused tests for these cases.

- Two rapid normal text messages coalesce.
- Normal followed by command during debounce dispatches normal first, then command.
- In-flight normal followed by command followed by normal preserves order.
- Text during upload grace starts the next turn.
- Media during upload grace joins the current turn.
- Command during debounce wakes and flushes earlier normal work before command.
- Command during upload grace wakes and flushes earlier normal work before command.
- Bypass dispatches solo in FIFO order.
- Bypass does not jump ahead of already queued normal work.
- Failed dispatch with queued backlog still drains backlog.
- Cancelled dispatch with queued backlog still drains backlog.
- Shutdown drains queued work without waits.
- Retargeting preserves queued work and drain ownership.
- Retargeting into an in-flight destination gate does not cancel the claimed destination batch.
- Retargeting two in-flight gates does not cancel either already-claimed batch.
- Retargeting two in-flight gates keeps retired in-flight drains visible to shutdown and idle checks.

## Commit Strategy

Write the ordering tests locally first and confirm they fail against the current side-queue implementation.

Do not commit a failing test commit.

Commit the queue-based implementation plus passing ordering tests first.

Use a second cleanup commit if the implementation diff gets large.

The cleanup commit should remove obsolete helpers, old timing assumptions, and tests tied to side-queue behavior.

## Definition Of Done

The rewrite is not complete until `pending`, `immediate`, `deferred_pending`, and `force_flush_pending` are gone.

The rewrite is not complete until `enqueue()` is append-and-wake only.

The rewrite is not complete until queue order explains every dispatch ordering outcome.

The rewrite is not complete until FIFO bypass is implemented and tested.

The rewrite is not complete until failure and cancellation backlog behavior is tested.

The rewrite is not complete until `pytest` passes.

The rewrite is not complete until `pre-commit` passes.

The rewrite is not complete until a zero-tolerance review finds no blocker.
