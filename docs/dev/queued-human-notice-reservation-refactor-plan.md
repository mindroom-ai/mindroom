# Queued Human Notice Reservation Refactor Plan

## Goal

Replace the active-follow-up queued-human side table with an explicit lifecycle-owned reservation.

The current implementation pre-signals active-thread follow-ups by writing event IDs into `ResponseLifecycleCoordinator`.

Later dispatch paths try to rediscover and clear that state using `target` and `response_envelope`.

That makes cleanup depend on dispatch reaching a point where `PreparedDispatch` exists.

If normalization, preparation, command handling, routing, skipping, cancellation, or an exception exits earlier than expected, the queued-human notice can leak.

The refactor should make ownership explicit.

Every queued-human notice reservation must be consumed exactly once when a real response turn starts, or canceled exactly once when dispatch exits before that point.

## Core Invariants

- `response_lifecycle.py` owns queued-human notice state.
- `response_lifecycle.py` is the only module that mutates pending queued-human counts.
- A pre-dispatch active-follow-up notice is represented by an opaque reservation object.
- The reservation is idempotent.
- Calling `consume()` more than once is safe.
- Calling `cancel()` more than once is safe.
- Calling `cancel()` after `consume()` is safe.
- `consume()` means the follow-up has reached response lifecycle ownership.
- `cancel()` means the follow-up will not reach response lifecycle ownership.
- `coalescing.py` may carry generic opaque dispatch metadata.
- `coalescing.py` must not import, name, inspect, consume, or know the meaning of queued-human reservations.
- `coalescing.py` may close generic dispatch metadata only when claimed work cannot be handed to dispatch ownership.
- Dispatch owns cancellation for every terminal path before response lifecycle ownership.
- `run_locked_response()` owns consumption once the response lifecycle lock is acquired and the response turn is about to run.
- The final implementation should not need `_pre_signaled_human_message_ids`, `signal_waiting_human_message()`, or `clear_waiting_human_message()`.

## Reservation Shape

Add a lifecycle-owned reservation object.

```python
@dataclass
class QueuedHumanNoticeReservation:
    _state: _QueuedMessageState
    _active: bool = True

    def _release_waiting_human_message(self) -> None:
        if not self._active:
            return
        self._state.consume_waiting_human_message()
        self._active = False

    def consume(self) -> None:
        self._release_waiting_human_message()

    def cancel(self) -> None:
        self._release_waiting_human_message()
```

The object should stay small and opaque.

It should not expose thread keys, event IDs, or lifecycle internals to the queue.

Use `@dataclass(slots=True)` unless local style or tests make that awkward.

If test visibility is needed, prefer observable queued-signal state over exposing internals.

## Lifecycle API

Replace the current pre-signal API with reservation creation.

```python
def reserve_waiting_human_message(
    self,
    *,
    target: MessageTarget,
    response_envelope: MessageEnvelope | None,
) -> QueuedHumanNoticeReservation | None:
    if not self._should_signal_queued_message(response_envelope):
        return None
    if not self.has_active_response_for_target(target):
        return None

    state = self._get_or_create_queued_signal(target)
    state.add_waiting_human_message()
    return QueuedHumanNoticeReservation(state)
```

Expose this through `ResponseRunner`.

Remove the event-id side table once all callers use reservations.

Keep the existing automatic queued-message notice behavior for normal concurrent response attempts.

The reservation path is only for ingress that must notify an already-running response before the queued dispatch reaches the lifecycle.

## Coalescing Boundary

Extend `PendingEvent` with generic opaque dispatch metadata.

```python
@dataclass
class PendingDispatchMetadata:
    kind: str
    payload: object
    close: Callable[[], None]
    requires_solo_batch: bool = False


@dataclass
class PendingEvent:
    event: DispatchEvent
    room: nio.MatrixRoom
    source_kind: str
    enqueue_time: float = field(default_factory=time.time)
    dispatch_metadata: tuple[PendingDispatchMetadata, ...] = ()
```

Do not import the concrete lifecycle reservation into `coalescing.py`.

Add an explicit `dispatch_metadata` field to `CoalescedBatch`.

`build_coalesced_batch()` should preserve generic metadata only for valid batches.

The queued-human reservation should be wrapped by turn-controller-owned metadata and only be present on bypass active-follow-up events.

Any metadata marked `requires_solo_batch` in a multi-event batch is invalid.

The implementation should fail loudly and close the metadata rather than silently dispatching mixed ownership.

The expected path is that active-follow-up bypass events dispatch solo.

## Turn Controller Ownership

Create the reservation at active-follow-up ingress before enqueueing.

```python
reservation = self.deps.response_runner.reserve_waiting_human_message(
    target=target,
    response_envelope=envelope,
)
try:
    await self._enqueue_for_dispatch(
        prepared_event,
        room,
        source_kind=ACTIVE_THREAD_FOLLOW_UP,
        queued_notice_reservation=reservation,
        requester_user_id=prechecked_event.requester_user_id,
        coalescing_key=(room.room_id, coalescing_thread_id, prechecked_event.requester_user_id),
    )
except Exception:
    if reservation is not None:
        reservation.cancel()
    raise
```

Pass the reservation as generic dispatch metadata from `PendingEvent` through `CoalescedBatch`, then unwrap it in `handle_coalesced_batch()` before `_dispatch_text_message()`.

Make `_dispatch_text_message()` the final cancellation boundary.

```python
async def _dispatch_text_message(..., queued_notice_reservation=None):
    reservation = queued_notice_reservation
    try:
        ...
        if command:
            await self._execute_command(...)
            return

        if newer_message_skip:
            self._mark_source_events_responded(...)
            return

        await self.deps.response_runner.generate_response(
            ...,
            queued_notice_reservation=reservation,
        )
        reservation = None
    finally:
        if reservation is not None:
            reservation.cancel()
```

The same cancellation rule must apply to ignore, route, hook suppression, preparation failure, normalization failure, dispatch cancellation, and arbitrary exceptions.

The implementation should not rely on `PreparedDispatch` existing before cleanup is possible.

### Ownership After Enqueue

Before enqueue succeeds, the caller that created the reservation owns it.

If enqueue raises, that caller must cancel the reservation.

After enqueue succeeds, ownership transfers to queued dispatch metadata.

The queued dispatch item owns generic metadata that carries the reservation until the coalescing drain claims it.

After the drain claims the event, the dispatch callback owns the reservation until it either hands the reservation to response lifecycle ownership or cancels it.

This means `handle_coalesced_batch()` must also be a cleanup boundary.

If retargeting, `build_batch_dispatch_event()`, key resolution, source-event bookkeeping, or any other pre-`_dispatch_text_message()` step raises after the gate has claimed a reserved event, the reservation must be canceled.

The coalescing gate should not understand the reservation, but it may close generic metadata if claimed work cannot reach the dispatch callback.

The dispatch callback that receives a claimed `CoalescedBatch` must close any opaque dispatch metadata when it drops or fails claimed work before handing it onward.

## Response Lifecycle Consumption

Thread the reservation into `ResponseRequest` or the locked response lifecycle call.

Consume it only after the lifecycle lock is acquired and before the locked operation starts.

```python
async def run_locked_response(..., queued_notice_reservation=None):
    ...
    await lifecycle_lock.acquire()
    if queued_notice_reservation is not None:
        queued_notice_reservation.consume()

    with queued_message_signal_context(queued_signal):
        return await locked_operation(target)
```

After consumption, the dispatch owner must release its local reference so the `finally` block does not cancel it.

Idempotence makes this safe, but the code should still express ownership transfer clearly.

A supplied reservation replaces the automatic waiting-human notice for that response attempt.

`run_locked_response()` should still mark that a response turn is active, but it must not add another queued-human notice for a request that already carries a reservation.

This avoids double-counting when a reserved follow-up reaches the lifecycle while the original response is still active.

## Source-Kind Cleanup

Do not make `turn_policy.py` depend on coalescing-specific constants long term.

Replace `COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP` checks outside `coalescing.py` with a neutral dispatch or envelope hint.

Possible names include `active_follow_up`, `preserve_active_follow_up_policy`, or `force_active_follow_up_response_policy`.

The exact name should match existing project vocabulary after reading nearby dispatch and envelope code.

This is a cleanup step after the reservation boundary is in place.

Do not mix it with the reservation conversion if it makes the diff hard to review.

## Source-Kind Trust Boundary

The reservation refactor does not by itself fix source-kind spoofing.

Treat this as a separate security boundary that should be fixed before or alongside the reservation work.

Matrix event content is untrusted unless the sender and creation path are explicitly trusted.

User-provided `content["com.mindroom.source_kind"]` must not influence coalescing classification, bypass behavior, command handling, response policy, or `PreparedTextEvent.source_kind_override`.

For coalescing, `PendingEvent.source_kind` should be the canonical policy input because it is assigned by internal ingress code.

`_effective_source_kind()` should not prefer Matrix content metadata over the internal fallback for raw Matrix events.

If source-kind metadata from event content is still needed for trusted internal events, the trust check must happen before that metadata is promoted into policy state.

`build_batch_dispatch_event()` may promote only internally assigned or trusted `PendingEvent.source_kind` values into `PreparedTextEvent.source_kind_override`.

Do not promote a batch source kind that was derived from untrusted Matrix content.

The spoofing regression to cover is a normal user message whose Matrix content claims `active_thread_follow_up`, `hook_dispatch`, `hook`, or `voice`.

That event must still be treated as normal user ingress unless the internal enqueue path assigned a trusted non-message source kind.

Trusted internal relay, hook, scheduled, voice-normalized, and media-normalized paths should keep their current behavior through explicit internal source-kind assignment.

## Test Plan

Add focused tests for the reservation contract.

- Active follow-up reaches normal response and consumes the reservation.
- Active follow-up is a command and cancels the reservation.
- Active follow-up is skipped by newer-message logic and cancels the reservation.
- Active follow-up is ignored before response planning and cancels the reservation.
- Active follow-up routes before response generation and cancels the reservation.
- Hook suppression after reservation cancels the reservation.
- Coalescing claims a reserved event and `handle_coalesced_batch()` fails before `_dispatch_text_message()` starts, then cancels the reservation.
- Text re-normalization failure after dequeue cancels the reservation.
- `_prepare_dispatch()` failure after dequeue cancels the reservation.
- Dispatch cancellation before response lifecycle ownership cancels the reservation.
- Dispatch exception before response lifecycle ownership cancels the reservation.
- A reserved follow-up waiting on the lifecycle lock does not increment the pending count twice.
- Constructing or dispatching a multi-event batch with a reservation fails loudly and clears the reservation.
- Duplicate `consume()` and duplicate `cancel()` are idempotent.
- `cancel()` after `consume()` is idempotent.
- `coalescing.py` transports generic opaque dispatch metadata and never calls reservation methods directly.
- A full coalescing-path spoofing regression where untrusted Matrix content sets `com.mindroom.source_kind` to `active_thread_follow_up`, `hook_dispatch`, `hook`, or `voice`, and the event still dispatches as normal user ingress.
- A trusted internal-source regression proving internally assigned non-message source kinds still dispatch with the intended policy.

Keep the existing active-follow-up tests that prove the active model sees the queued-human notice while it is still running.

Keep the tests that prove gate-owned active follow-ups still receive active-follow-up policy after the original response completes.

## Commit Strategy

First commit the reservation implementation and tests.

That commit should remove `_pre_signaled_human_message_ids`, `signal_waiting_human_message()`, and `clear_waiting_human_message()`.

Second commit any source-kind naming cleanup if needed.

Do not commit a broad naming cleanup together with lifecycle ownership changes unless the final diff is still small and obvious.

## Definition Of Done

The refactor is complete when no cleanup path depends on finding a pre-signaled event by event ID.

The refactor is complete when every reservation has exactly one owner at each step.

The refactor is complete when dispatch can fail before `PreparedDispatch` exists without leaking a queued-human notice.

The refactor is complete when `coalescing.py` carries only generic opaque dispatch metadata and never imports, names, or interprets lifecycle state.

The refactor is complete when active-follow-up behavior is covered for response, command, skip, ignore, route, exception, and cancellation paths.

## Accountability Checks

Use this section as the merge checklist.

Do not treat the refactor as complete until every item is checked directly against the branch.

### Code Search Gates

Run these searches before review.

```bash
rg "_pre_signaled_human_message_ids|signal_waiting_human_message|clear_waiting_human_message" src tests
rg "COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP" src/mindroom/turn_policy.py
rg "queued_notice_reservation" src/mindroom tests
rg "QueuedHumanNoticeReservation|queued_notice_reservation" src/mindroom/coalescing.py
```

The first command should return nothing.

The second command should return nothing after the source-kind cleanup commit.

The third command should show a small, explainable path from ingress reservation creation, through queue metadata, through dispatch ownership, into lifecycle consumption.

If `queued_notice_reservation` appears in unrelated modules, stop and reassess the design before patching around it.

The fourth command should return nothing.

### Ownership Walkthrough

Before marking the PR ready, write a short ownership walkthrough in the PR description.

The walkthrough should name the owner at each step.

- Active follow-up ingress creates the reservation.
- Failed enqueue cancels it.
- Successful enqueue transfers it to queued dispatch metadata.
- Claimed coalesced batch transfers it to `handle_coalesced_batch()`.
- Successful text dispatch handoff transfers it to `_dispatch_text_message()`.
- Response start transfers it to `run_locked_response()`, which consumes it.
- Any terminal path before response start cancels it.

If any step cannot identify a single owner, the refactor is incomplete.

### Test Coverage Gates

Run the focused reservation tests by name.

The test set must include success, cancellation, exception, and invalid-batch cases.

Do not rely only on a broad file-level pytest run.

The PR description should list the exact focused test command.

The required behavioral cases are:

- Consumed by a normal active-follow-up response.
- Canceled by command dispatch.
- Canceled by newer-message skip.
- Canceled by ignore before response.
- Canceled by route before response.
- Canceled by hook suppression.
- Canceled by normalization failure.
- Canceled by preparation failure.
- Canceled by `handle_coalesced_batch()` failure before `_dispatch_text_message()`.
- Canceled by dispatch cancellation before lifecycle ownership.
- Canceled by dispatch exception before lifecycle ownership.
- Not double-counted while waiting for the lifecycle lock.
- Invalid in any multi-event batch.
- Idempotent for repeated `consume()` and `cancel()` calls.

### Review Gates

Ask reviewers to check these specific claims.

- The side table is gone, not renamed.
- Cleanup is tied to reservation ownership, not event-id lookup.
- `coalescing.py` does not inspect lifecycle semantics and only closes generic opaque metadata when claimed work cannot be handed to dispatch.
- `turn_policy.py` does not import coalescing-specific constants after the cleanup commit.
- Reserved requests bypass normal queued-notice creation and therefore cannot double-count.
- The implementation has one cancellation boundary for each ownership stage.

### Failure Discipline

If review finds another queued-human leak after this refactor, do not add another flag or side table.

First update the ownership walkthrough to show where the owner was ambiguous or missing.

Then fix that ownership boundary and add the missing regression test.

If two more review rounds still find new lifecycle leak classes, stop and reconsider whether the mid-turn queued-human notice feature is worth its complexity.
