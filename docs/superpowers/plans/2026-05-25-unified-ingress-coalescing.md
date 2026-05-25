# Unified Ingress Coalescing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make live coalescing use one receive-order invariant for text, voice, and media so batching depends on Matrix receive order, not on which ingress path finishes async preparation first.

**Architecture:** `CoalescingGate` owns receive order for every prompt-like Matrix ingress item. `TurnController` creates one receive-order reservation before async thread lookup, cache append, text normalization, media preparation, fallback preparation, or STT work, then either transfers that reservation to the gate exactly once or releases it exactly once. After admission, `CoalescingKey(room_id, thread_id, requester_user_id)` is the only dispatch scope; there are no aliases, provisional dispatch keys, or gate retargeting.

**Tech Stack:** Python 3.13, asyncio, matrix-nio event objects, existing MindRoom `CoalescingGate`, `TurnController`, `dispatch_handoff`, pytest with `uv run pytest -n auto --no-cov`.

---

## Problem Statement

The current branch fixed one voice-first race by reserving receive order for raw voice before STT and target resolution.
That repair exposed the underlying design problem: receive order is not owned by one protocol.

Current behavior:

- Voice reserves early, but text still awaits `coalescing_thread_id()` before the gate can see it.
- Non-audio media still awaits thread lookup and live-cache append before the gate can see it.
- The gate has to reason about unresolved reservations, queued admissions, claimed admissions, and in-flight dispatches, but different branches observe different subsets of that state.
- The plan previously used Matrix `origin_server_ts` as receive time, which is not a local debounce clock.
- Claimed work cleanup, upload-grace requeue, command/bypass claims, and shutdown cancellation were under-specified.
- The hygiene scans banned legitimate `PendingDispatchMetadata.target_key` metadata, which is not a coalescing alias.

The violated invariant is:

> Every prompt-like Matrix ingress item must get a local receive order before its first meaningful await, and the gate must never dispatch, coalesce, drop, or shut down work in a way that contradicts that receive order.

## Design Decisions

## Non-Negotiable Simple Model

1. Prompt-like Matrix ingress has one entry rule: reserve local receive order before meaningful async work.
2. Unresolved work carries only `room_id`, `requester_user_id`, `received_order`, and `receipt_time`.
   It does not carry a guessed thread key.
3. Admission requires one resolved canonical `CoalescingKey(room_id, thread_id, requester_user_id)`.
   That key is never retargeted.
4. Prompt-like Matrix ingress must not call `CoalescingGate.enqueue()` or an ownerless `_enqueue_for_dispatch()` path.
   Ownerless enqueue is allowed only for explicitly internal, non-Matrix dispatch entry points that already have a canonical key.
5. Room-level batching policy may decide whether several ready room-level events with the same canonical room key form one dispatch segment.
   It must never merge different canonical keys, and it must not merge surviving room-level text roots after the media-like event that justified wider room batching resolved to `None`.
6. Drain, metadata, cancellation, and shutdown code is lifecycle ownership only.
   It must not choose, rewrite, alias, or repair dispatch keys.

1. **Local receipt time is the debounce clock.**
   `event.server_timestamp` remains event metadata only.
   `CoalescingGate.reserve_order()` captures `time.monotonic()` as `receipt_time`.

2. **Reservation is not admission.**
   A reservation means “this room/requester has work that arrived here.”
   Admission happens later, after the canonical `CoalescingKey` has been resolved.

3. **No aliases, provisional dispatch keys, or retargeting.**
   While an item is unresolved, the gate only knows room/requester/order.
   Once admitted, the item has exactly one canonical `CoalescingKey`.

4. **Unresolved same-room/requester work may briefly hold same-owner gates.**
   This is intentional.
   It avoids guessing at thread identity before lookup completes.
   After admission, different canonical thread keys are independent and must not globally serialize unrelated threads for the same requester.
   Same-key ordering remains strict.

5. **Debounce uses trailing quiet-period semantics.**
   `_wait_for_debounce()` continues extending the deadline on wakeups.
   After that quiet deadline, the gate waits only for unresolved same-room/requester reservations whose `receipt_time` is inside that effective debounce window.

6. **Claimed work has one lifecycle.**
   The same helper must own command, bypass, and normal claims.
   It must preserve requeue-on-cancellation while resolving, clear claimed state and wake waiters for terminal no-ready results, close metadata for resolved-but-undispatched events, and keep claimed work visible through every dispatch segment.

7. **Shutdown cannot silently drop a Matrix event and then persist the sync token.**
   Bounded shutdown may release unresolved reservations or cancel unresolved ready work.
   Dispatch failures or cancellations during shutdown are also unsafe.
   Any unsafe drain outcome means `AgentBot.prepare_for_sync_shutdown()` must not save the certified sync checkpoint for that sync generation.

8. **Queued-notice `target_key` is allowed.**
   It is response-lifecycle metadata, not a coalescing dispatch key.
   Stale scans must target aliases, retargeting, provisional coalescing keys, and second-gate concepts only.

9. **Voice-specific product behavior is allowed; voice-specific ordering rules are not.**
   Allowed: STT, raw-audio fallback, router visible echo suppression, voice transcript command policy.
   Forbidden: voice-only receive-order reservations, voice-root aliases, gate retargeting, and dispatch-key overrides.

## Lifecycle Contract

| State | Owner | Meaning | Exit |
| --- | --- | --- | --- |
| `reserved` | `TurnController` reservation owner | Event passed cheap prechecks and has a receive order, but canonical key is not known. | `admit`, `release`, or shutdown cancellation |
| `queued` | `CoalescingGate` | Canonical key is known; ready result may be immediate or task-backed. | `claimed_resolving` |
| `claimed_resolving` | `CoalescingGate` | Queue items are removed from queue and visible as claimed older work. | `claimed_dispatching`, `requeued`, or `finished_no_ready` |
| `claimed_dispatching` | `CoalescingGate` | Ready events are being dispatched through `_dispatch_batch`. | `finished` |
| `requeued` | `CoalescingGate` | Upload grace put claimed items back into queue and cleared claimed state. | `queued` |
| `finished_no_ready` | `CoalescingGate` | Ready tasks failed, returned `None`, or were cancelled before producing dispatchable events. | claimed cleared, metadata closed where present, waiters woken |
| `shutdown_released` | `CoalescingGate` and `AgentBot` | Shutdown released unresolved pre-admission reservations. | late admits rejected, no handled marking, no sync checkpoint save |
| `shutdown_cancelled` | `CoalescingGate` and `AgentBot` | Shutdown cancelled unresolved ready work. | no handled marking, no sync checkpoint save |

Every transition must close owned metadata or transfer ownership exactly once.

## Drain Context Contract

Bounded shutdown uses one gate-level `_DrainContext` object for the entire `drain_all()` call.
Every long-running gate wait must read that context dynamically through `_current_drain_context(gate)` immediately before awaiting.
Do not capture timeout or drain-result values before a wait and assume they remain valid after shutdown begins.
If `drain_all()` cancels an already-running non-`IN_FLIGHT` drain, it must await that cancelled drain task until its cleanup has requeued, cleared, or closed claimed work before scheduling the replacement drain.
If a drain is already `IN_FLIGHT`, `drain_all()` must include that task in the awaited task set and must not freeze or clear the active context until the in-flight task finishes.
Any resolved ready work closed during shutdown before dispatch increments an unsafe drain counter so the certified sync checkpoint is not saved.

## File Structure

- Modify `src/mindroom/coalescing.py`.
  Owns receive-order reservations, canonical admitted work, debounce, claim lifecycle, ready-task cancellation, and drain outcome reporting.

- Modify `src/mindroom/turn_controller.py`.
  Owns cheap precheck, reservation ownership, async conversion from Matrix event to `ReadyPendingEvent`, and handoff to `CoalescingGate`.

- Modify `src/mindroom/dispatch_handoff.py`.
  Owns synthetic batch source metadata.
  It must normalize `m.relates_to` from `CoalescedBatch.coalescing_key`, not from the primary event.

- Modify `src/mindroom/bot.py`.
  Uses bounded coalescing shutdown result to decide whether the sync checkpoint is safe to persist.

- Modify `tests/test_coalescing.py`.
  Holds direct gate invariants and scheduling reproductions.

- Modify `tests/test_live_message_coalescing.py`.
  Holds integration-level gate and handoff behavior.

- Modify `tests/test_multi_agent_bot.py`.
  Holds bot-level ordering tests where `TurnController` async lookups can race.

- Modify `tests/test_voice_bot_threading.py`.
  Holds voice-specific normalization, fallback, router echo, and active-follow-up behavior.

- Modify `tests/test_matrix_sync_tokens.py`.
  Holds shutdown drain and sync checkpoint behavior.

Do not create a second coalescing gate.
Create a small helper dataclass in `turn_controller.py` if needed to avoid manual reservation booleans.

## Reservation Ownership Table

| Path | Reserve point | First await after reserve | Terminal state | Cancellation cleanup |
| --- | --- | --- | --- | --- |
| Text prompt | After body/status/precheck/edit cheap checks | `coalescing_thread_id()` | `ADMITTED`, `CONSUMED`, or `IGNORED` | `finally: await owner.release()` unless ownership transferred |
| Async router skip | After reservation if thread snapshot is needed | `get_dispatch_thread_snapshot()` | `IGNORED` | release owner and do not mark handled |
| Trusted router visible echo | Text path reservation may exist | trusted sender check and handled marker | `CONSUMED` | mark handled, then release owner |
| Deep synthetic relay skip | Text path reservation may exist | synthetic relay check | `CONSUMED` | mark handled if current behavior does, then release owner |
| Interactive selection | Same text prompt reservation created before async ingress work; selection is detected after prepared text resolution | `coalescing_thread_id()`, then `handle_interactive_selection()` | `CONSUMED` | release owner; keep in scope for reservation cleanup but out of coalesced dispatch |
| Message edit | Before reservation | edit live-cache append and edit regenerator | out of scope; no reservation | existing edit behavior unchanged |
| File sidecar text preview | Media handler reserves before sidecar dispatch | sidecar normalization | `ADMITTED` or `IGNORED` | release owner for non-preview or failed preview |
| Non-audio media | After precheck | `coalescing_thread_id()` and live-cache append | `ADMITTED` or `IGNORED` | release owner for non-dispatch media |
| Managed-agent audio | Before reservation if sender check is available | none | mark handled as today | no reservation |
| Voice normal | Media handler reserves before target lookup | voice target lookup | admit ready voice task or release | cancel owner-owned ready task if admit fails |
| Voice fallback | Same reservation as voice normal | fallback raw-audio prep inside ready task | admit fallback ready task or release | cancel owner-owned fallback task if admit fails |
| Command/bypass | Same prompt reservation path if Matrix ingress | command classification after ready result | admit, then barrier dispatch | gate claim helper owns cleanup |

## Task 1: Correct the Gate Clock and Reservation Shape

**Files:**
- Modify: `src/mindroom/coalescing.py`
- Test: `tests/test_coalescing.py`

- [ ] **Step 1: Add local-receipt-time tests**

Add to `tests/test_coalescing.py`:

```python
class FakeMonotonicClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_reserve_order_uses_local_monotonic_receipt_time(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clock = FakeMonotonicClock(10.0)
    monkeypatch.setattr(time, "monotonic", fake_clock)
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.3,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    first = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")
    fake_clock.advance(0.5)
    second = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")

    assert first.receipt_time == 10.0
    assert second.receipt_time == 10.5
    assert first.received_order < second.received_order
```

- [ ] **Step 2: Run the test and verify it fails**

```bash
uv run pytest tests/test_coalescing.py::test_reserve_order_uses_local_monotonic_receipt_time -n auto --no-cov -q
```

Expected before implementation: FAIL because `IngressOrderReservation` has `received_at`, not `receipt_time`.

- [ ] **Step 3: Rename gate-local time fields**

In `src/mindroom/coalescing.py`, change reservation and queued-admission timing to local receipt time.

```python
@dataclass
class IngressOrderReservation:
    """Receive-order reservation for ingress before canonical key resolution."""

    room_id: str
    requester_user_id: str
    received_order: int
    receipt_time: float
    released: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)
```

Change `_QueuedEvent.received_at` to `receipt_time`.
Use `time.monotonic()` for receipt/debounce calculations.
Keep Matrix `server_timestamp` on events only.
Keep `PendingEvent.enqueue_time` as wall-clock diagnostic metadata unless a separate diagnostics refactor intentionally renames it.
Do not store monotonic `receipt_time` into `PendingEvent.enqueue_time`.

```python
def reserve_order(
    self,
    *,
    room_id: str,
    requester_user_id: str,
    receipt_time: float | None = None,
) -> IngressOrderReservation:
    reservation = IngressOrderReservation(
        room_id=room_id,
        requester_user_id=requester_user_id,
        received_order=self._next_order(),
        receipt_time=receipt_time if receipt_time is not None else time.monotonic(),
    )
    self._order_reservations.append(reservation)
    self._wake_owner(room_id, requester_user_id)
    return reservation
```

Change `CoalescingGate.enqueue()` so compatibility callers also get local monotonic receipt time.
It should call `admit` with `receipt_time=time.monotonic()` or omit timing and let `admit` capture `time.monotonic()`.
It must not derive `_QueuedEvent.receipt_time` from `pending_event.enqueue_time`.

Add this test:

```python
@pytest.mark.asyncio
async def test_enqueue_uses_gate_monotonic_receipt_time(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clock = FakeMonotonicClock(20.0)
    monkeypatch.setattr(time, "monotonic", fake_clock)
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    pending = _pending(_text_event("$event:localhost", "body", 1000))
    pending.enqueue_time = 1.0

    await gate.enqueue(key, pending)
    gate_entry = gate._gates[key]

    assert gate_entry.queue[0].receipt_time == 20.0
    assert pending.enqueue_time == 1.0
```

- [ ] **Step 4: Run focused gate timing tests**

```bash
uv run pytest tests/test_coalescing.py::test_reserve_order_uses_local_monotonic_receipt_time tests/test_coalescing.py::test_enqueue_uses_gate_monotonic_receipt_time -n auto --no-cov -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/coalescing.py tests/test_coalescing.py
git commit -m "Use local receipt time for coalescing reservations"
```

## Task 2: Add a Single Reservation Owner in TurnController

**Files:**
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/coalescing.py`
- Test: `tests/test_turn_controller.py`
- Test: `tests/test_multi_agent_bot.py`
- Test: `tests/test_voice_bot_threading.py`

- [ ] **Step 1: Add late-admit rejection test**

Add to `tests/test_coalescing.py`:

```python
@pytest.mark.asyncio
async def test_admit_rejects_released_reservation() -> None:
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    reservation = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")
    gate.release_order_reservation(reservation)

    with pytest.raises(IngressAdmissionClosedError):
        await gate.admit(
            CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            ready_result=ReadyPendingEvent(
                pending_event=_pending(_text_event("$late:localhost", "late", 1000)),
            ),
            order_reservation=reservation,
        )
```

Add to `tests/test_turn_controller.py`:

```python
@pytest.mark.asyncio
async def test_late_admit_rejection_closes_completed_ready_task_metadata_once() -> None:
    close_count = 0

    def close_metadata() -> None:
        nonlocal close_count
        close_count += 1

    pending_event = _pending(_text_event("$late:localhost", "late", 1000))
    pending_event.dispatch_metadata = (
        PendingDispatchMetadata(
            kind="test",
            payload=object(),
            close=close_metadata,
            requires_solo_batch=False,
        ),
    )

    async def ready() -> ReadyPendingEvent:
        return ReadyPendingEvent(pending_event=pending_event)

    class RejectingGate:
        async def admit(self, *_args: object, **_kwargs: object) -> None:
            raise IngressAdmissionClosedError("closed")

        def release_order_reservation(self, reservation: IngressOrderReservation) -> None:
            reservation.released = True
            reservation.settled.set()

    reservation = IngressOrderReservation(
        room_id="!room:localhost",
        requester_user_id="@user:localhost",
        received_order=1,
        receipt_time=1.0,
    )
    owner = _PromptIngressReservationOwner(gate=RejectingGate(), reservation=reservation)
    ready_task = asyncio.create_task(ready())
    await ready_task

    with pytest.raises(IngressAdmissionClosedError):
        await owner.admit(
            CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            ready_task=ready_task,
            source_event_id="$late:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
        )

    await owner.release()

    assert close_count == 1
```

Add a cancellation-returning-ready variant:

```python
@pytest.mark.asyncio
async def test_owner_cancel_ready_task_closes_ready_result_returned_during_cancellation() -> None:
    close_count = 0
    cancelled = asyncio.Event()

    def close_metadata() -> None:
        nonlocal close_count
        close_count += 1

    pending_event = _pending(_text_event("$late:localhost", "late", 1000))
    pending_event.dispatch_metadata = (
        PendingDispatchMetadata(
            kind="test",
            payload=object(),
            close=close_metadata,
            requires_solo_batch=False,
        ),
    )

    async def ready() -> ReadyPendingEvent:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return ReadyPendingEvent(pending_event=pending_event)

    reservation = IngressOrderReservation(
        room_id="!room:localhost",
        requester_user_id="@user:localhost",
        received_order=1,
        receipt_time=1.0,
    )
    owner = _PromptIngressReservationOwner(gate=MagicMock(), reservation=reservation)
    owner.ready_task = asyncio.create_task(ready())

    await owner.cancel_ready_task()

    assert cancelled.is_set()
    assert close_count == 1
    await owner.cancel_ready_task()
    assert close_count == 1
```

- [ ] **Step 2: Add the gate error**

In `src/mindroom/coalescing.py`:

```python
class IngressAdmissionClosedError(RuntimeError):
    """Raised when an ingress callback tries to admit a released reservation."""
```

At the start of `admit`, reject released reservations before creating queue work.

```python
if order_reservation is not None and order_reservation.released:
    raise IngressAdmissionClosedError("Cannot admit a released ingress reservation")
```

- [ ] **Step 3: Add a reservation owner helper**

In `src/mindroom/turn_controller.py`, add a small helper near `_PrecheckedEvent`.
Add one shared cleanup helper in `src/mindroom/coalescing.py` so owner-side and gate-side ready-task cancellation use the same ownership rule.

```python
def close_ready_task_result_metadata(result: object) -> int:
    if isinstance(result, ReadyPendingEvent):
        close_pending_event_metadata([result.pending_event])
        return 1
    return 0
```

```python
@dataclass
class _PromptIngressReservationOwner:
    gate: CoalescingGate
    reservation: IngressOrderReservation
    admitted: bool = False
    ready_task: asyncio.Task[ReadyPendingEvent | None] | None = None

    async def admit(
        self,
        key: CoalescingKey,
        *,
        source_event_id: str | None,
        source_kind: str,
        ready_result: ReadyPendingEvent | None = None,
        ready_task: asyncio.Task[ReadyPendingEvent | None] | None = None,
    ) -> None:
        if ready_task is not None:
            self.ready_task = ready_task
        metadata_transferred = False
        try:
            await self.gate.admit(
                key,
                ready_result=ready_result,
                ready_task=ready_task,
                source_event_id=source_event_id,
                source_kind=source_kind,
                order_reservation=self.reservation,
            )
            metadata_transferred = True
        except BaseException:
            await self.cancel_ready_task()
            if ready_result is not None and not metadata_transferred:
                close_pending_event_metadata([ready_result.pending_event])
            raise
        self.admitted = True

    async def cancel_ready_task(self) -> None:
        if self.ready_task is None:
            return
        ready_task = self.ready_task
        self.ready_task = None
        if ready_task.done():
            result = await asyncio.gather(ready_task, return_exceptions=True)
            close_ready_task_result_metadata(result[0])
            return
        ready_task.cancel()
        result = await asyncio.gather(ready_task, return_exceptions=True)
        close_ready_task_result_metadata(result[0])

    async def release(self) -> None:
        if self.admitted:
            return
        await self.cancel_ready_task()
        self.gate.release_order_reservation(self.reservation)
```

Setting `self.ready_task = None` before reading or cancelling the task makes cleanup idempotent.
This matters because `admit()` failure and the caller's `finally: await owner.release()` can both ask the owner to clean up the same ready task.

Add a factory:

```python
def _reserve_prompt_ingress_order(
    self,
    room: nio.MatrixRoom,
    requester_user_id: str,
) -> _PromptIngressReservationOwner:
    return _PromptIngressReservationOwner(
        gate=self.deps.coalescing_gate,
        reservation=self.deps.coalescing_gate.reserve_order(
            room_id=room.room_id,
            requester_user_id=requester_user_id,
        ),
    )
```

`CoalescingGate.admit()` must make ownership transfer atomic.
Validate arguments before creating gate-owned state.
After queue insertion begins, reservation release, wake scheduling, drain scheduling, and logging must either be no-throw operations or have failures caught inside `admit`.
There must be no `await` after ownership transfer and before returning.
If a failure happens before queue insertion, caller ownership remains and the reservation owner closes or cancels its ready work.
If a failure happens after queue insertion, gate ownership remains and `admit` logs internally instead of raising to the caller.

Every reserved path must follow this shape:

```python
owner = self._reserve_prompt_ingress_order(room, requester_user_id)
try:
    outcome = await self._dispatch_prepared_text_like_ingress(
        room=room,
        prepared_event=prepared_event,
        dispatch_event=dispatch_event,
        requester_user_id=requester_user_id,
        dispatch_timing=dispatch_timing,
        reservation_owner=owner,
    )
finally:
    await owner.release()
```

`owner.release()` is a no-op after a successful admit.
`IngressAdmissionClosedError` during shutdown is an expected closed-admission path and should be logged at debug level by the caller after `owner.release()` has run.

- [ ] **Step 4: Use the owner for normal voice and fallback voice**

Change `_on_audio_media_message` so the media handler passes `reservation_owner` into it.
Both normal and fallback paths must create ready tasks and call `reservation_owner.admit`.
No voice fallback preparation may be awaited before admission.

```python
ready_task = asyncio.create_task(
    self._ready_voice_event(
        room=room,
        prechecked_event=prechecked_event,
        voice_target=voice_target,
        dispatch_timing=dispatch_timing,
    ),
    name=f"voice_ready:{room.room_id}:{event.event_id}",
)
await reservation_owner.admit(
    admission_key,
    ready_task=ready_task,
    source_event_id=event.event_id,
    source_kind=VOICE_SOURCE_KIND,
)
```

Use the same pattern for `_ready_voice_fallback_event`.

- [ ] **Step 5: Run focused owner tests**

```bash
uv run pytest tests/test_coalescing.py::test_admit_rejects_released_reservation tests/test_turn_controller.py::test_late_admit_rejection_closes_completed_ready_task_metadata_once tests/test_turn_controller.py::test_owner_cancel_ready_task_closes_ready_result_returned_during_cancellation tests/test_voice_bot_threading.py -n auto --no-cov -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mindroom/coalescing.py src/mindroom/turn_controller.py tests/test_coalescing.py tests/test_turn_controller.py tests/test_voice_bot_threading.py
git commit -m "Add single ingress reservation ownership path"
```

## Task 3: Reserve Text and Media Before Async Work

**Files:**
- Modify: `src/mindroom/turn_controller.py`
- Test: `tests/test_multi_agent_bot.py`
- Test: `tests/test_voice_bot_threading.py`

- [ ] **Step 1: Add bot-level text-before-voice race test**

Add to `tests/test_multi_agent_bot.py` near existing coalescing tests:

```python
@pytest.mark.asyncio
async def test_text_reserves_receive_order_before_thread_lookup(
    self,
    mock_agent_user: AgentMatrixUser,
    tmp_path: Path,
) -> None:
    """An earlier text message must not be overtaken by a later voice message."""
    config = self._config_for_storage(tmp_path)
    bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
    room = MagicMock()
    room.room_id = "!test:localhost"
    text_event = self._make_handler_event("message", sender="@user:localhost", event_id="$typed")
    text_event.body = "typed first"
    text_event.source = {"content": {"body": "typed first"}}
    voice_event = self._make_handler_event("voice", sender="@user:localhost", event_id="$voice")
    release_text_lookup = asyncio.Event()
    dispatches: list[list[str]] = []

    async def coalescing_thread_id(_room: nio.MatrixRoom, event: nio.Event) -> str | None:
        if event.event_id == "$typed":
            await release_text_lookup.wait()
        return "$thread-root"

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        dispatches.append(list(batch.source_event_ids))

    bot._coalescing_gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=bot._coalescing_gate)
    bot._turn_controller.deps.resolver.coalescing_thread_id = AsyncMock(side_effect=coalescing_thread_id)
    bot._turn_controller._resolve_ready_voice_target = AsyncMock(
        return_value=(
            bot._turn_controller.deps.resolver.build_message_target(
                room_id=room.room_id,
                thread_id="$thread-root",
                reply_to_event_id=voice_event.event_id,
                event_source=voice_event.source,
            ),
            CoalescingKey(room.room_id, "$thread-root", "@user:localhost"),
        ),
    )
    bot._turn_controller._ready_voice_event = AsyncMock(
        return_value=ReadyPendingEvent(
            pending_event=PendingEvent(
                event=PreparedTextEvent(
                    sender="@user:localhost",
                    event_id="$voice",
                    body="voice second",
                    source={
                        "content": {
                            "body": "voice second",
                            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
                            SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
                        },
                    },
                    source_kind_override=VOICE_SOURCE_KIND,
                ),
                room=room,
                source_kind=VOICE_SOURCE_KIND,
            ),
        ),
    )

    text_task = asyncio.create_task(bot._turn_controller.handle_text_event(room, text_event))
    await asyncio.sleep(0)
    await bot._turn_controller.handle_media_event(room, voice_event)
    await asyncio.sleep(0.03)

    assert dispatches == []

    release_text_lookup.set()
    await text_task
    await bot._coalescing_gate.drain_all()

    assert dispatches == [["$typed", "$voice"]]
```

- [ ] **Step 1b: Add non-audio media reservation tests**

Add two focused bot-level tests next to `test_text_reserves_receive_order_before_thread_lookup`:

Add a test named `test_media_reserves_receive_order_before_thread_lookup`.
Its docstring should say: `An earlier non-audio media event must reserve before thread lookup can block.`

Test shape:

1. Create an image or file media event received first.
2. Block `coalescing_thread_id()` or live-cache append for that media event.
3. Send a later prompt-like text or voice event for the same room/requester inside the debounce window.
4. Assert no dispatch occurs before the media reservation settles.
5. Release the media lookup, drain the gate, and assert the dispatched source IDs preserve receive order.

Add a sibling sidecar-preview test:

Add a test named `test_file_sidecar_preview_reserves_receive_order_before_preview_normalization`.
Its docstring should say: `An earlier file sidecar text preview must reserve before preview normalization can block.`

Test shape:

1. Create a file/media event whose text sidecar preview is prompt-like.
2. Block sidecar preview normalization after reservation but before admission.
3. Send a later prompt-like event inside the debounce window.
4. Assert no dispatch occurs before the preview admission or release path settles.
5. Assert the final dispatch either includes the preview before the later prompt or, if preview is ignored, releases the reservation without letting the later prompt dispatch before that release.

These tests prove the invariant for non-audio media and sidecar previews, not only for voice.

- [ ] **Step 2: Make text reserve before thread lookup**

In `_handle_message_inner`, the order must be:

1. Parse `EventInfo`.
2. Return for non-string body.
3. Return for streaming status events.
4. Return for edits before reservation.
5. Run `_precheck_dispatch_event`.
6. Run router skip only if it can complete without async thread snapshot.
7. Reserve with `_reserve_prompt_ingress_order`.
8. Await thread lookup, router async skip, live-cache append, normalization, and admission.
9. Release in `finally` if not admitted.

If `_should_skip_router_before_shared_ingress_work` needs async thread history, move that async part after reservation and make the skip path call `reservation_owner.release()`.
The edit path must remain no-reservation and must preserve existing live-cache append plus edit-regenerator behavior.
Do not route edits through the new reservation owner in this PR.

- [ ] **Step 3: Make prepared text helpers return an admission outcome**

Add a local enum in `turn_controller.py`:

```python
class _IngressAdmissionOutcome(Enum):
    ADMITTED = "admitted"
    CONSUMED = "consumed"
    IGNORED = "ignored"
```

Change `_dispatch_prepared_text_like_ingress`, `_enqueue_prepared_text_for_dispatch`, `_enqueue_active_thread_follow_up`, `_dispatch_file_sidecar_text_preview`, and `_enqueue_media_for_dispatch` so callers can distinguish admission from consumed/ignored paths.
The caller releases the reservation owner for `CONSUMED` or `IGNORED`.

Split prompt-like Matrix admission from internal enqueue.
`_enqueue_for_dispatch` must require `reservation_owner: _PromptIngressReservationOwner` and is used only by prompt-like Matrix ingress.
Create a separate `_enqueue_internal_for_dispatch` helper only for internal, scheduled, hook, or already-synthetic dispatch entry points that do not originate as prompt-like Matrix ingress.
The internal helper must require an already resolved `CoalescingKey` and must reject `MESSAGE_SOURCE_KIND`, `VOICE_SOURCE_KIND`, `IMAGE_SOURCE_KIND`, and `MEDIA_SOURCE_KIND`.
No Matrix text/media/voice path may call `CoalescingGate.enqueue()` directly.

```python
async def _enqueue_for_dispatch(
    self,
    event: DispatchEvent,
    room: nio.MatrixRoom,
    *,
    source_kind: str,
    requester_user_id: str,
    reservation_owner: _PromptIngressReservationOwner,
    dispatch_policy_source_kind: str | None = None,
    hook_source: str | None = None,
    message_received_depth: int = 0,
    coalescing_key: CoalescingKey | None = None,
    queued_notice_reservation: QueuedHumanNoticeReservation | None = None,
    queued_notice_target: MessageTarget | None = None,
    trust_internal_payload_metadata: bool | None = None,
) -> _IngressAdmissionOutcome:
    source_kind_allows_relay_detection = source_kind in {
        "",
        MESSAGE_SOURCE_KIND,
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    }
    if source_kind_allows_relay_detection and self._is_trusted_internal_relay_event(event):
        source_kind = TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    resolved_trust_internal_payload_metadata = (
        self._should_trust_internal_payload_metadata(event)
        if trust_internal_payload_metadata is None
        else trust_internal_payload_metadata
    )
    resolved_key = coalescing_key or await self._coalescing_key_for_event(room, event, requester_user_id)
    pending_event = PendingEvent(
        event=event,
        room=room,
        source_kind=source_kind,
        dispatch_policy_source_kind=dispatch_policy_source_kind,
        hook_source=hook_source,
        message_received_depth=message_received_depth,
        trust_internal_payload_metadata=resolved_trust_internal_payload_metadata,
        dispatch_metadata=_queued_notice_dispatch_metadata(queued_notice_reservation, queued_notice_target),
    )
    await reservation_owner.admit(
        resolved_key,
        ready_result=ReadyPendingEvent(pending_event=pending_event),
        source_event_id=event.event_id,
        source_kind=source_kind,
    )
    return _IngressAdmissionOutcome.ADMITTED
```

Add the internal helper separately:

```python
async def _enqueue_internal_for_dispatch(
    self,
    key: CoalescingKey,
    pending_event: PendingEvent,
) -> None:
    if pending_event.source_kind in {
        MESSAGE_SOURCE_KIND,
        VOICE_SOURCE_KIND,
        IMAGE_SOURCE_KIND,
        MEDIA_SOURCE_KIND,
    }:
        msg = f"prompt-like Matrix ingress requires a reservation owner: {pending_event.source_kind}"
        raise AssertionError(msg)
    await self.deps.coalescing_gate.enqueue(key, pending_event)
```

If this helper returns `CONSUMED` or `IGNORED` from an earlier branch, it must close or consume any owned metadata before returning.
Interactive selections and trusted visible echo suppression are `CONSUMED`.

- [ ] **Step 4: Make media reserve before thread lookup**

In `_handle_media_message_inner`, reserve after `_precheck_dispatch_event` and before `coalescing_thread_id`.
Pass the same owner through audio, sidecar text preview, and non-audio media.
Do not create a separate voice reservation inside `_on_audio_media_message`.

- [ ] **Step 5: Run focused ingress tests**

```bash
uv run pytest tests/test_multi_agent_bot.py::TestAgentBot::test_text_reserves_receive_order_before_thread_lookup tests/test_multi_agent_bot.py::TestAgentBot::test_media_reserves_receive_order_before_thread_lookup tests/test_multi_agent_bot.py::TestAgentBot::test_file_sidecar_preview_reserves_receive_order_before_preview_normalization tests/test_voice_bot_threading.py -n auto --no-cov -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mindroom/turn_controller.py tests/test_multi_agent_bot.py tests/test_voice_bot_threading.py
git commit -m "Reserve receive order for text and media ingress"
```

## Task 4: Make Debounce Wait For Relevant Unresolved Reservations

**Files:**
- Modify: `src/mindroom/coalescing.py`
- Test: `tests/test_coalescing.py`

- [ ] **Step 1: Add inside-window and outside-window tests**

Add to `tests/test_coalescing.py`:

```python
@pytest.mark.asyncio
async def test_debounce_waits_for_later_same_owner_reservation_inside_window() -> None:
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.03,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await gate.enqueue(key, _pending(_text_event("$text:localhost", "typed first", 1_000_000)))
    await asyncio.sleep(0.005)
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    await asyncio.sleep(0.05)

    assert batches == []

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice second", 1_000_005)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost", "$voice:localhost"]]
```

```python
@pytest.mark.asyncio
async def test_debounce_does_not_wait_for_later_reservation_outside_window() -> None:
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await gate.enqueue(key, _pending(_text_event("$text:localhost", "typed first", 1_000_000)))
    await asyncio.sleep(0.03)
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    await asyncio.sleep(0.01)

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"]]

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice later", 1_000_050)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$voice:localhost"]]
```

- [ ] **Step 2: Add a debounce barrier preservation test**

Add to `tests/test_coalescing.py`:

```python
@pytest.mark.asyncio
async def test_debounce_still_releases_prompt_when_command_barrier_arrives() -> None:
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await gate.enqueue(key, _pending(_text_event("$text:localhost", "normal", 1_000_000)))
    await gate.enqueue(key, _pending(_text_event("$command:localhost", "!help", 1_000_001)))
    await asyncio.sleep(0.05)

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$command:localhost"]]
```

- [ ] **Step 3: Add barrier-bounded reservation tests**

Add to `tests/test_coalescing.py`:

```python
@pytest.mark.asyncio
async def test_command_barrier_does_not_wait_for_unresolved_reservation_after_barrier() -> None:
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await gate.enqueue(key, _pending(_text_event("$text:localhost", "normal", 1_000_000)))
    await gate.enqueue(key, _pending(_text_event("$command:localhost", "!help", 1_000_001)))
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    await asyncio.sleep(0.05)

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$command:localhost"]]

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice", 1_000_002)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [
        ["$text:localhost"],
        ["$command:localhost"],
        ["$voice:localhost"],
    ]
```

Add this bypass metadata barrier test:

```python
@pytest.mark.asyncio
async def test_bypass_barrier_does_not_wait_for_unresolved_reservation_after_barrier() -> None:
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    bypass = _pending(_text_event("$bypass:localhost", "solo", 1_000_001))
    bypass.dispatch_metadata = (
        PendingDispatchMetadata(kind="test", payload=object(), close=lambda: None, requires_solo_batch=True),
    )

    await gate.enqueue(key, _pending(_text_event("$text:localhost", "normal", 1_000_000)))
    await gate.enqueue(key, bypass)
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    await asyncio.sleep(0.05)

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$bypass:localhost"]]

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice", 1_000_002)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [
        ["$text:localhost"],
        ["$bypass:localhost"],
        ["$voice:localhost"],
    ]
```

Add front-barrier variants so a command or bypass already at the queue front cannot be delayed by a later unresolved reservation:

```python
@pytest.mark.asyncio
async def test_front_command_does_not_wait_for_later_unresolved_reservation() -> None:
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await gate.enqueue(key, _pending(_text_event("$command:localhost", "!help", 1_000_000)))
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    await asyncio.sleep(0.05)

    assert [batch.source_event_ids for batch in batches] == [["$command:localhost"]]

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice", 1_000_001)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$command:localhost"], ["$voice:localhost"]]
```

```python
@pytest.mark.asyncio
async def test_front_bypass_does_not_wait_for_later_unresolved_reservation() -> None:
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    bypass = _pending(_text_event("$bypass:localhost", "solo", 1_000_000))
    bypass.dispatch_metadata = (
        PendingDispatchMetadata(kind="test", payload=object(), close=lambda: None, requires_solo_batch=True),
    )

    await gate.enqueue(key, bypass)
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    await asyncio.sleep(0.05)

    assert [batch.source_event_ids for batch in batches] == [["$bypass:localhost"]]

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice", 1_000_001)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$bypass:localhost"], ["$voice:localhost"]]
```

- [ ] **Step 4: Return debounce result with barrier boundary**

Change `_wait_for_debounce` to return the final monotonic quiet deadline and the first barrier order, if a barrier is present.
Preserve the existing trailing quiet-period behavior, command/bypass barrier early return, shutdown early return, `drain_all_requested` early return, wake-generation checks, and phase/deadline updates.
Only add a return value.

```python
@dataclass(frozen=True)
class _DebounceWaitResult:
    quiet_deadline: float
    before_order: int | None = None


async def _wait_for_debounce(
    self,
    gate: _GateEntry,
    *,
    coalesce_normal_events: Callable[[], bool],
) -> _DebounceWaitResult:
    gate.phase = GatePhase.DEBOUNCE
    gate.grace_deadline = None
    debounce_seconds = max(self._debounce_seconds(), 0.0)
    if debounce_seconds <= 0 or self._is_shutting_down() or gate.drain_all_requested:
        gate.deadline = time.monotonic()
        return _DebounceWaitResult(quiet_deadline=gate.deadline)
    barrier_order = self._first_barrier_after_front_normal_run_order(
        gate,
        coalesce_normal_events=coalesce_normal_events(),
    )
    if barrier_order is not None:
        gate.deadline = time.monotonic()
        return _DebounceWaitResult(quiet_deadline=gate.deadline, before_order=barrier_order)
    gate.deadline = time.monotonic() + debounce_seconds
    while True:
        deadline = gate.deadline or time.monotonic()
        if not await self._wait_for_deadline(gate, deadline):
            return _DebounceWaitResult(quiet_deadline=deadline)
        barrier_order = self._first_barrier_after_front_normal_run_order(
            gate,
            coalesce_normal_events=coalesce_normal_events(),
        )
        if (
            self._is_shutting_down()
            or gate.drain_all_requested
            or barrier_order is not None
        ):
            return _DebounceWaitResult(quiet_deadline=time.monotonic(), before_order=barrier_order)
        gate.deadline = time.monotonic() + debounce_seconds
```

- [ ] **Step 5: Add reservation-wait helpers**

Add:

```python
async def _wait_until_front_claimable(
    self,
    key: CoalescingKey,
    gate: _GateEntry,
    *,
    front_order: int,
) -> None:
    older_reservations = [
        reservation
        for reservation in self._order_reservations
        if not reservation.released
        and reservation.room_id == key.room_id
        and reservation.requester_user_id == key.requester_user_id
        and reservation.received_order < front_order
    ]
    await self._wait_for_reservations(
        gate,
        older_reservations,
    )

async def _wait_for_reservations(
    self,
    gate: _GateEntry,
    reservations: list[IngressOrderReservation],
) -> None:
    while True:
        unsettled = [reservation for reservation in reservations if not reservation.released]
        if not unsettled:
            return
        waits = [reservation.settled.wait() for reservation in unsettled]
        drain_context = self._current_drain_context(gate)
        ready_timeout_seconds = drain_context.ready_timeout_seconds if drain_context else None
        try:
            if ready_timeout_seconds is None:
                await asyncio.gather(*waits)
            else:
                await asyncio.wait_for(asyncio.gather(*waits), timeout=ready_timeout_seconds)
        except asyncio.TimeoutError:
            drain_context = self._current_drain_context(gate)
            if drain_context is None:
                raise
            for reservation in unsettled:
                if not reservation.released:
                    self._release_order_reservation(reservation, wake=True)
                    drain_context.result.released_reservation_count += 1
            return
```

Use `_wait_until_front_claimable` before every command, bypass, and normal claim.
For command and bypass front items, this helper waits only for older unresolved reservations and never for later reservations.
For normal front items, call this helper before debounce, then use `_unsettled_owner_reservations_in_window` after debounce for later same-window reservations.

Add:

```python
def _unsettled_owner_reservations_in_window(
    self,
    key: CoalescingKey,
    *,
    after_order: int,
    before_order: int | None,
    before_or_at_receipt_time: float,
) -> list[IngressOrderReservation]:
    return [
        reservation
        for reservation in self._order_reservations
        if not reservation.released
        and reservation.room_id == key.room_id
        and reservation.requester_user_id == key.requester_user_id
        and reservation.received_order > after_order
        and (before_order is None or reservation.received_order < before_order)
        and reservation.receipt_time <= before_or_at_receipt_time
    ]
```

- [ ] **Step 6: Wait for same-window same-owner reservations**

After debounce and before claiming:

```python
debounce_result = await self._wait_for_debounce(
    gate,
    coalesce_normal_events=lambda key=key, entry=gate: self._should_coalesce_normal_events(key, entry),
)
if not gate.queue:
    continue
front = gate.queue[0]
same_window_reservations = self._unsettled_owner_reservations_in_window(
    key,
    after_order=front.received_order,
    before_order=debounce_result.before_order,
    before_or_at_receipt_time=debounce_result.quiet_deadline,
)
if same_window_reservations:
    await self._wait_for_reservations(
        gate,
        same_window_reservations,
    )
    continue
```

Restarting the loop is required so newly admitted work is inserted by receive order and command/bypass barriers are re-evaluated.
Reservations received after the first command/bypass barrier order must not delay work before that barrier.

- [ ] **Step 7: Run debounce tests**

```bash
uv run pytest tests/test_coalescing.py::test_debounce_waits_for_later_same_owner_reservation_inside_window tests/test_coalescing.py::test_debounce_does_not_wait_for_later_reservation_outside_window tests/test_coalescing.py::test_debounce_still_releases_prompt_when_command_barrier_arrives tests/test_coalescing.py::test_command_barrier_does_not_wait_for_unresolved_reservation_after_barrier tests/test_coalescing.py::test_bypass_barrier_does_not_wait_for_unresolved_reservation_after_barrier tests/test_coalescing.py::test_front_command_does_not_wait_for_later_unresolved_reservation tests/test_coalescing.py::test_front_bypass_does_not_wait_for_later_unresolved_reservation -n auto --no-cov -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/mindroom/coalescing.py tests/test_coalescing.py
git commit -m "Make debounce aware of unresolved ingress reservations"
```

## Task 5: Replace Claim Cleanup With One Claim Lifecycle Helper

**Files:**
- Modify: `src/mindroom/coalescing.py`
- Test: `tests/test_coalescing.py`
- Test: `tests/test_live_message_coalescing.py`

- [ ] **Step 1: Add lifecycle regression tests**

Add concrete tests:

- `test_claim_count_stops_before_unresolved_older_reservation`
- `test_different_canonical_threads_do_not_serialize_after_admission`
- `test_zero_ready_claim_clears_claimed_state_and_wakes_waiters`
- `test_partial_ready_failure_dispatches_ready_events_and_clears_claim`
- `test_cancelled_resolve_requeues_claimed_admissions`
- `test_dispatch_exception_closes_metadata_and_reports_unsafe_result`
- `test_dispatch_cancellation_closes_current_segment_metadata_once`
- `test_multi_segment_dispatch_cancellation_closes_later_segments_once`
- `test_upload_grace_cancellation_after_requeue_does_not_close_requeued_metadata`
- `test_segment_cancellation_before_dispatch_start_closes_metadata_once`
- `test_segment_cancellation_before_dispatch_start_marks_shutdown_drain_incomplete`
- `test_upload_grace_requeue_removes_admissions_from_claimed_state`
- `test_multi_segment_claim_remains_visible_until_last_segment_finishes`
- `test_same_window_reservation_resolving_to_different_thread_waits_then_splits`

Keep `tests/test_live_message_coalescing.py::test_interrupted_claimed_admission_is_retried_on_next_drain` in the focused suite because it protects the existing requeue-on-cancellation behavior.

Acceptance criteria for these tests:

- `test_claim_count_stops_before_unresolved_older_reservation`: create two same-owner reservations where the older one is unresolved and a later admitted normal event is queued; assert the later event is not claimed until the older reservation is released or admitted.
- `test_different_canonical_threads_do_not_serialize_after_admission`: admit ready events for the same room/requester under two different non-null `CoalescingKey.thread_id` values; assert the first dispatch does not wait for the second gate merely because the requester matches.
- `test_zero_ready_claim_clears_claimed_state_and_wakes_waiters`: claim a ready task that returns `None`; assert `claimed_admissions` is empty, no batch dispatches, and a later queued event for the same owner drains.
- `test_partial_ready_failure_dispatches_ready_events_and_clears_claim`: claim two admissions where one ready task returns a `ReadyPendingEvent` and one returns `None`; assert the ready event dispatches, the failed event is absent, and the claim is cleared.
- `test_cancelled_resolve_requeues_claimed_admissions`: cancel while `_resolve_claimed_admissions()` is awaiting a shielded ready task; assert the original `_QueuedEvent` returns to `gate.queue` with its original receive order.
- `test_dispatch_exception_closes_metadata_and_reports_unsafe_result`: make `_dispatch_batch()` raise after a segment is claimed; assert close count is one and `dispatch_failure_count == 1` during shutdown drain.
- `test_dispatch_cancellation_closes_current_segment_metadata_once`: make `_dispatch_batch()` raise `asyncio.CancelledError`; assert close count is one and `dispatch_cancelled_count == 1` during shutdown drain.
- `test_multi_segment_dispatch_cancellation_closes_later_segments_once`: split one claim into two dispatch segments, cancel during the first segment, and assert the second segment's metadata closes once without dispatching.
- `test_upload_grace_cancellation_after_requeue_does_not_close_requeued_metadata`: force upload grace, verify admissions are back in `gate.queue`, cancel during `_wait_for_upload_grace()`, and assert metadata close count is zero until the retried dispatch/drop owns it.
- `test_segment_cancellation_before_dispatch_start_closes_metadata_once`: cancel at a test hook immediately before `_dispatch_claimed_events()` enters `_dispatch_batch()`; assert the segment owner closes metadata once.
- `test_segment_cancellation_before_dispatch_start_marks_shutdown_drain_incomplete`: run the same pre-dispatch cancellation under an active shutdown drain; assert metadata closes once and `CoalescingDrainResult.completed is False`.
- `test_upload_grace_requeue_removes_admissions_from_claimed_state`: force upload grace and assert `claimed_admissions` is empty before the grace await starts.
- `test_multi_segment_claim_remains_visible_until_last_segment_finishes`: split one claim into two dispatch segments, block the first dispatch, and assert later same-owner gates still see the in-flight claim until both segments finish.
- `test_same_window_reservation_resolving_to_different_thread_waits_then_splits`: enqueue a thread-A text event, reserve a later same-room/requester event inside debounce, assert thread A does not dispatch before the reservation settles, then admit the reservation under thread B and assert the result is two separate batches keyed to thread A and thread B.

- [ ] **Step 2: Add a claimability helper**

Different canonical thread keys should not block each other after admission.
The claimability helper therefore stops only for unresolved older same-room/requester reservations.
It does not treat already-admitted work in other canonical thread gates as a blocker.

```python
def _has_older_unresolved_owner_reservation(self, key: CoalescingKey, received_order: int) -> bool:
    return any(
        not reservation.released
        and reservation.room_id == key.room_id
        and reservation.requester_user_id == key.requester_user_id
        and reservation.received_order < received_order
        for reservation in self._order_reservations
    )

def _claimable_front_normal_run_length(
    self,
    key: CoalescingKey,
    gate: _GateEntry,
    *,
    coalesce_normal_events: bool,
    max_received_order: int | None,
) -> int:
    base_count = self._front_normal_run_length(
        gate,
        coalesce_normal_events=coalesce_normal_events,
        max_received_order=max_received_order,
    )
    claimable_count = 0
    for queued in list(gate.queue)[:base_count]:
        if self._has_older_unresolved_owner_reservation(key, queued.received_order):
            break
        claimable_count += 1
    return claimable_count
```

Use this helper for every normal claim and recompute it after every await before claiming.
Replace current cross-gate same-owner blocking in `_has_older_owner_work`.
After this task, other admitted canonical thread gates must not block each other merely because they share requester and room.
The only cross-gate wait is for unresolved reservations whose canonical key is not known yet.

- [ ] **Step 3: Add a phase-aware claim lifecycle helper**

In `src/mindroom/coalescing.py`, replace scattered claim/resolve/dispatch cleanup with one helper.

```python
@dataclass
class _ClaimedSegmentOwner:
    pending_events: list[PendingEvent]
    metadata_closed: bool = False

    def event_ids(self) -> set[str]:
        return {pending_event.event.event_id for pending_event in self.pending_events}

    def close_metadata_once(self) -> None:
        if self.metadata_closed:
            return
        close_pending_event_metadata(self.pending_events)
        self.metadata_closed = True


async def _dispatch_claim(
    self,
    key: CoalescingKey,
    gate: _GateEntry,
    admissions: list[_QueuedEvent],
    *,
    bypass_grace: bool,
    allow_upload_grace: bool,
) -> None:
    ready_admissions: list[_ReadyAdmission] = []
    closed_or_transferred: set[str] = set()
    unresolved_segment_owners: list[_ClaimedSegmentOwner] = []
    try:
        try:
            ready_admissions = await self._resolve_claimed_admissions(
                gate,
                admissions,
            )
        except BaseException:
            self._requeue_claimed_admissions(gate, admissions)
            raise
        if not ready_admissions:
            return
        segments = self._ready_admission_segments(ready_admissions)
        candidate_events = [event for _segment_key, pending_events in segments for event in pending_events]
        if allow_upload_grace and self._should_wait_for_upload_grace(candidate_events):
            self._requeue_claimed_admissions(gate, admissions)
            closed_or_transferred.update(
                ready_admission.pending_event.event.event_id
                for ready_admission in ready_admissions
            )
            ready_admissions = []
            await self._wait_for_upload_grace(
                gate,
                len(admissions),
                timing_scope=event_timing_scope(candidate_events[-1].event.event_id),
            )
            return
        for segment_key, pending_events in segments:
            segment_owner = _ClaimedSegmentOwner(pending_events=list(pending_events))
            unresolved_segment_owners.append(segment_owner)
            await self._dispatch_claimed_events(
                segment_key,
                gate,
                segment_owner,
                bypass_grace=bypass_grace,
            )
            closed_or_transferred.update(segment_owner.event_ids())
            unresolved_segment_owners.remove(segment_owner)
    except BaseException:
        closed_segment_count = 0
        for segment_owner in unresolved_segment_owners:
            segment_owner.close_metadata_once()
            segment_event_ids = segment_owner.event_ids()
            closed_segment_count += len(segment_event_ids)
            closed_or_transferred.update(segment_event_ids)
        closed_unsegmented_count = 0
        unresolved_events = [
            ready_admission.pending_event
            for ready_admission in ready_admissions
            if ready_admission.pending_event.event.event_id not in closed_or_transferred
        ]
        closed_unsegmented_count += len(unresolved_events)
        close_pending_event_metadata(unresolved_events)
        closed_ready_count = closed_segment_count + closed_unsegmented_count
        if closed_ready_count and (drain_context := self._current_drain_context(gate)) is not None:
            drain_context.result.dropped_ready_count += closed_ready_count
        raise
    finally:
        self._clear_claimed_admissions(gate, admissions)
        self._wake_owner_gates(key)
```

Rules:

- cancellation while resolving requeues claimed admissions
- zero-ready clears claimed state and wakes waiters
- partial-ready dispatches ready admissions and treats `None` admissions as no-ready
- upload grace requeues, marks resolved ready admissions as queue-owned again before awaiting grace, and clears claimed state before returning
- `_ClaimedSegmentOwner` is the metadata-closure owner for each split dispatch segment
- `_dispatch_claimed_events` accepts `_ClaimedSegmentOwner`, reads events from it, calls `segment_owner.close_metadata_once()` on dispatch exception or cancellation, updates shutdown counters from `_current_drain_context(gate)`, and never also lets the outer unsegmented cleanup close that owner's events
- `_dispatch_claim` keeps unresolved segment owners in a list until dispatch succeeds, so cancellation at the await boundary still has a cleanup owner
- `_dispatch_claim` adds unresolved segment-owner event IDs to `closed_or_transferred` before building the fallback unsegmented close list
- if `_dispatch_claim` closes resolved-but-undispatched events while a drain context is active, it increments `dropped_ready_count` so shutdown cannot be reported safe
- dispatch exceptions close metadata through `_dispatch_claimed_events` and increment drain failure counters
- cancellation or exception after readiness closes metadata exactly once for each resolved-but-undispatched pending event

- [ ] **Step 4: Preserve room-scope batching policy without voice-only ordering**

Replace `_front_admissions_have_voice` with a front-run dispatch-policy helper.
It must inspect only the front normal run and include unresolved admissions by queued source-kind policy.
It must not scan past command/bypass barriers.
This is allowed product dispatch policy for media-like room-level prompts, not a voice-specific receive-order rule.

```python
ROOM_SCOPE_BATCHING_SOURCE_KINDS: frozenset[str] = frozenset(
    {VOICE_SOURCE_KIND, IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND},
)


def _front_admissions_allow_room_scope_coalescing(gate: _GateEntry) -> bool:
    for queued in gate.queue:
        if CoalescingGate._queued_kind(queued) is not _QueueKind.NORMAL:
            return False
        if queued.source_kind in ROOM_SCOPE_BATCHING_SOURCE_KINDS:
            return True
        if queued.ready_result is not None and is_media_dispatch_event(queued.ready_result.pending_event.event):
            return True
    return False


def _can_merge_room_scope_segment(
    current_key: CoalescingKey,
    next_key: CoalescingKey,
    *,
    room_scope_batching_allowed: bool,
) -> bool:
    if current_key != next_key:
        return False
    if current_key.thread_id is not None:
        return True
    return room_scope_batching_allowed
```

Use `_can_merge_room_scope_segment` from `_ready_admission_segments()` instead of `_can_merge_ready_segment`.
It must be driven by the resolved claim's `room_scope_batching_allowed` boolean, not by scanning for voice after readiness.
`room_scope_batching_allowed` must be computed from surviving ready admissions in the resolved claim, not from queued unresolved admissions that later returned `None`.
If a voice/media admission enabled room-scope claiming but then resolves to `None`, the surviving room-level text roots must not merge unless another surviving ready admission in the resolved claim still satisfies `ROOM_SCOPE_BATCHING_SOURCE_KINDS` or `is_media_dispatch_event`.
This helper must never merge two different canonical keys.
Document that room-level media-like coalescing is dispatch policy, not a separate receive-order path.

Add a focused regression in `tests/test_coalescing.py`:

```python
@pytest.mark.asyncio
async def test_failed_room_media_signal_does_not_merge_surviving_room_text_roots() -> None:
    """A dropped media-like admission must not make room-level text roots coalesce."""
```

Test shape:

1. Queue room-level text, a media-like admission whose ready task returns `None`, and another room-level text under the same room/requester.
2. Let the media-like admission widen the claim before readiness.
3. Resolve it to `None`.
4. Assert the surviving text events dispatch as separate room-level batches.

- [ ] **Step 5: Run lifecycle tests**

```bash
uv run pytest tests/test_coalescing.py tests/test_live_message_coalescing.py::test_interrupted_claimed_admission_is_retried_on_next_drain -n auto --no-cov -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mindroom/coalescing.py tests/test_coalescing.py tests/test_live_message_coalescing.py
git commit -m "Centralize coalescing claim lifecycle"
```

## Task 6: Normalize Synthetic Handoff Relations From Batch Key

**Files:**
- Modify: `src/mindroom/dispatch_handoff.py`
- Test: `tests/test_live_message_coalescing.py`

- [ ] **Step 1: Add room-level plain-reply regression**

Add to `tests/test_live_message_coalescing.py`:

```python
def test_room_level_batch_removes_plain_reply_relation() -> None:
    room = _make_room()
    typed_reply = _reply_event(
        event_id="$typed",
        reply_to_event_id="$voice",
        body="typed follow-up",
        server_timestamp=1001,
    )
    batch = build_coalesced_batch(
        CoalescingKey(room.room_id, None, "@user:localhost"),
        [PendingEvent(event=typed_reply, room=room, source_kind=MESSAGE_SOURCE_KIND)],
    )

    handoff = build_dispatch_handoff(batch)

    assert "m.relates_to" not in handoff.event.source["content"]
```

- [ ] **Step 2: Normalize every relation when batch key is room-level**

In `src/mindroom/dispatch_handoff.py`:

```python
def _normalize_batch_thread_relation(content: dict[str, Any], batch: CoalescedBatch) -> None:
    thread_id = batch.coalescing_key.thread_id
    if thread_id is None:
        content.pop("m.relates_to", None)
        return
    content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
```

Update `_batch_requires_thread_relation_normalization` so room-level batches normalize whenever `"m.relates_to" in content`.

- [ ] **Step 3: Run handoff tests**

```bash
uv run pytest tests/test_live_message_coalescing.py::test_room_level_batch_removes_plain_reply_relation tests/test_live_message_coalescing.py -n auto --no-cov -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/mindroom/dispatch_handoff.py tests/test_live_message_coalescing.py
git commit -m "Normalize coalesced handoff relations from batch key"
```

## Task 7: Make Shutdown Bounded Without Losing Sync Safety

**Files:**
- Modify: `src/mindroom/coalescing.py`
- Modify: `src/mindroom/bot.py`
- Test: `tests/test_matrix_sync_tokens.py`

- [ ] **Step 1: Add shutdown result tests**

Add to `tests/test_matrix_sync_tokens.py`:

```python
@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_save_checkpoint_for_cancelled_ingress(tmp_path: Path) -> None:
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(
        return_value=CoalescingDrainResult(completed=False, cancelled_unready_count=1),
    )

    await bot.prepare_for_sync_shutdown()

    assert _load_sync_token_value(tmp_path, bot.agent_name) is None
```

Add a direct gate test:

```python
@pytest.mark.asyncio
async def test_shutdown_drain_cancels_stuck_ready_task_without_cancelling_dispatch() -> None:
    cancelled = asyncio.Event()

    async def stuck_ready() -> ReadyPendingEvent | None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    await gate.admit(
        CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
        ready_task=asyncio.create_task(stuck_ready()),
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
    )

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.cancelled_unready_count == 1
    assert cancelled.is_set()
```

Add a reserved-state shutdown test:

```python
@pytest.mark.asyncio
async def test_shutdown_drain_releases_stuck_pre_admission_reservation() -> None:
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    reservation = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.released_reservation_count == 1
    assert reservation.released is True
    with pytest.raises(IngressAdmissionClosedError):
        await gate.admit(
            CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            ready_result=ReadyPendingEvent(
                pending_event=_pending(_text_event("$late:localhost", "late", 1000)),
            ),
            order_reservation=reservation,
        )
```

Add tests for already-running drains and self-cancelled ready work:

```python
@pytest.mark.asyncio
async def test_shutdown_ready_timeout_closes_ready_result_returned_during_cancellation() -> None:
    close_count = 0
    cancelled = asyncio.Event()

    def close_metadata() -> None:
        nonlocal close_count
        close_count += 1

    pending_event = _pending(_text_event("$voice:localhost", "voice", 1000))
    pending_event.dispatch_metadata = (
        PendingDispatchMetadata(
            kind="test",
            payload=object(),
            close=close_metadata,
            requires_solo_batch=False,
        ),
    )

    async def ready() -> ReadyPendingEvent:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return ReadyPendingEvent(pending_event=pending_event)

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    await gate.admit(
        CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
        ready_task=asyncio.create_task(ready()),
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
    )

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert cancelled.is_set()
    assert close_count == 1
    assert result.completed is False
    assert result.cancelled_unready_count == 1
    assert result.dropped_ready_count == 1
```

```python
@pytest.mark.asyncio
async def test_shutdown_timeout_reaches_already_running_ready_wait() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck_ready() -> ReadyPendingEvent | None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await gate.admit(
        key,
        ready_task=asyncio.create_task(stuck_ready()),
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
    )
    await started.wait()
    for _ in range(100):
        if gate._gates[key].claimed_admissions:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("gate did not claim admission before shutdown drain")

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.cancelled_unready_count == 1
    assert cancelled.is_set()
```

```python
@pytest.mark.asyncio
async def test_ready_task_self_cancellation_finishes_no_ready() -> None:
    async def cancelled_ready() -> ReadyPendingEvent | None:
        raise asyncio.CancelledError

    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    await gate.admit(
        CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
        ready_task=asyncio.create_task(cancelled_ready()),
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
    )

    await gate.drain_all()

    assert batches == []
```

This self-cancellation test is regression coverage.
The shutdown-specific counter behavior is covered by the timeout, reservation, and in-flight dispatch tests above.

Add a test where shutdown starts while a new reservation is attempted.
It must prove the reservation is immediately released, `released_reservation_count` increases, and a later `admit()` with that reservation raises `IngressAdmissionClosedError`.

```python
@pytest.mark.asyncio
async def test_reserve_order_during_active_bounded_shutdown_returns_released_counted_reservation() -> None:
    shutting_down = False

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: shutting_down,
    )
    old_reservation = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")
    shutting_down = True
    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.05))
    await asyncio.sleep(0)

    reservation = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")

    assert reservation.released is True
    assert reservation.settled.is_set()

    with pytest.raises(IngressAdmissionClosedError):
        await gate.admit(
            CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            ready_result=ReadyPendingEvent(
                pending_event=_pending(_text_event("$late:localhost", "late", 1000)),
            ),
            order_reservation=reservation,
        )

    result = await drain_task

    assert old_reservation.released is True
    assert result.completed is False
    assert result.released_reservation_count == 2
```

Add tests for active waits and in-flight dispatch:

```python
@pytest.mark.asyncio
async def test_shutdown_timeout_reaches_already_running_same_window_reservation_wait() -> None:
    shutting_down = False
    wait_entered = asyncio.Event()
    target_reservation: IngressOrderReservation | None = None

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: shutting_down,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    original_wait_for_reservations = gate._wait_for_reservations

    async def spy_wait_for_reservations(
        wait_gate: _GateEntry,
        reservations: list[IngressOrderReservation],
    ) -> None:
        if target_reservation is not None and target_reservation in reservations:
            wait_entered.set()
        await original_wait_for_reservations(wait_gate, reservations)

    gate._wait_for_reservations = spy_wait_for_reservations

    await gate.enqueue(key, _pending(_text_event("$text:localhost", "typed", 1000)))
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    target_reservation = reservation
    await asyncio.wait_for(wait_entered.wait(), timeout=1.0)

    shutting_down = True
    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert reservation.released is True
    assert result.completed is False
    assert result.released_reservation_count == 1
```

```python
@pytest.mark.asyncio
async def test_shutdown_in_flight_dispatch_failure_marks_drain_incomplete() -> None:
    dispatch_entered = asyncio.Event()
    fail_dispatch = asyncio.Event()

    async def dispatch_batch(_batch: CoalescedBatch) -> None:
        dispatch_entered.set()
        await fail_dispatch.wait()
        raise RuntimeError("dispatch failed")

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await gate.enqueue(key, _pending(_text_event("$text:localhost", "typed", 1000)))
    await dispatch_entered.wait()

    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.01))
    for _ in range(100):
        if gate._active_drain_context is not None and gate._gates[key].drain_context is gate._active_drain_context:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("drain context was not installed before dispatch failure")
    fail_dispatch.set()
    result = await drain_task

    assert result.completed is False
    assert result.dispatch_failure_count == 1
```

```python
@pytest.mark.asyncio
async def test_shutdown_in_flight_dispatch_cancellation_marks_drain_incomplete() -> None:
    dispatch_entered = asyncio.Event()
    dispatch_raised_self_cancel = asyncio.Event()
    cancel_dispatch = asyncio.Event()

    async def dispatch_batch(_batch: CoalescedBatch) -> None:
        dispatch_entered.set()
        await cancel_dispatch.wait()
        dispatch_raised_self_cancel.set()
        raise asyncio.CancelledError

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await gate.enqueue(key, _pending(_text_event("$text:localhost", "typed", 1000)))
    await dispatch_entered.wait()

    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.01))
    for _ in range(100):
        if gate._active_drain_context is not None and gate._gates[key].drain_context is gate._active_drain_context:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("drain context was not installed before dispatch cancellation")
    cancel_dispatch.set()
    result = await drain_task

    assert dispatch_raised_self_cancel.is_set()
    assert result.completed is False
    assert result.dispatch_cancelled_count == 1
```

- [ ] **Step 2: Add `CoalescingDrainResult`**

In `src/mindroom/coalescing.py`:

```python
@dataclass(frozen=True)
class CoalescingDrainResult:
    completed: bool
    released_reservation_count: int = 0
    cancelled_unready_count: int = 0
    failed_ready_count: int = 0
    dropped_ready_count: int = 0
    dispatch_failure_count: int = 0
    dispatch_cancelled_count: int = 0


@dataclass
class _MutableDrainResult:
    released_reservation_count: int = 0
    cancelled_unready_count: int = 0
    failed_ready_count: int = 0
    dropped_ready_count: int = 0
    dispatch_failure_count: int = 0
    dispatch_cancelled_count: int = 0

    def freeze(self) -> CoalescingDrainResult:
        return CoalescingDrainResult(
            completed=not any(
                (
                    self.released_reservation_count,
                    self.cancelled_unready_count,
                    self.failed_ready_count,
                    self.dropped_ready_count,
                    self.dispatch_failure_count,
                    self.dispatch_cancelled_count,
                )
            ),
            released_reservation_count=self.released_reservation_count,
            cancelled_unready_count=self.cancelled_unready_count,
            failed_ready_count=self.failed_ready_count,
            dropped_ready_count=self.dropped_ready_count,
            dispatch_failure_count=self.dispatch_failure_count,
            dispatch_cancelled_count=self.dispatch_cancelled_count,
        )


@dataclass
class _DrainContext:
    ready_timeout_seconds: float | None
    result: _MutableDrainResult
```

`drain_all()` returns `CoalescingDrainResult`.
Each `_GateEntry` gets `drain_context: _DrainContext | None = None`.
`CoalescingGate` also gets `self._active_drain_context: _DrainContext | None = None`.
`drain_all(ready_timeout_seconds=5.0)` creates one `_DrainContext`, stores it in `self._active_drain_context`, installs the same object on every existing gate, and clears it in a `finally` after the drain result is frozen.
Any gate created while `self._active_drain_context` is set receives that same context.
`reserve_order()` checks `self._active_drain_context` before appending to `_order_reservations`.
If the context exists, `reserve_order()` returns an already released and settled reservation, increments `context.result.released_reservation_count`, wakes owner gates, and creates no unsettled work.
If a drain task is already running and the gate phase is not `GatePhase.IN_FLIGHT`, `drain_all()` cancels and restarts that drain task with the context.
This includes drains blocked in debounce, upload grace, same-window reservation waits, order-reservation waits, and shielded ready waits.
`drain_all()` must await each cancelled non-`IN_FLIGHT` drain task with `return_exceptions=True` before scheduling the replacement drain.
The replacement drain must start only after the old drain has requeued, cleared, or closed its claimed work.
If the gate phase is `GatePhase.IN_FLIGHT`, do not cancel it; dispatch must be allowed to finish.
`drain_all()` must still await already-running `IN_FLIGHT` drain tasks before freezing the drain result and clearing `self._active_drain_context`.
In-flight dispatch failure/cancellation reporting must read `gate.drain_context` or `self._active_drain_context` at failure time, not rely only on the `drain_result` argument captured before shutdown began.

Add:

```python
def _current_drain_context(self, gate: _GateEntry | None = None) -> _DrainContext | None:
    if gate is not None and gate.drain_context is not None:
        return gate.drain_context
    return self._active_drain_context


def _close_late_ready_task_result(self, task: asyncio.Task[ReadyPendingEvent | None]) -> None:
    try:
        result = task.result()
    except BaseException:
        return
    close_ready_task_result_metadata(result)
```

- [ ] **Step 3: Bound unresolved reservation waits**

Do not wait forever in `_wait_for_order_reservations()` during shutdown drain.
When `ready_timeout_seconds` is provided, wait that long for current reservations.
If any remain unsettled, release them, increment `released_reservation_count`, wake owner gates, and continue draining admitted work.
Late `admit()` calls for those reservations fail with `IngressAdmissionClosedError`.

```python
async def _wait_for_order_reservations_for_drain(
    self,
    *,
    ready_timeout_seconds: float | None,
    drain_result: _MutableDrainResult,
) -> None:
    reservations = [reservation for reservation in self._order_reservations if not reservation.released]
    if not reservations:
        return
    waits = [reservation.settled.wait() for reservation in reservations]
    try:
        if ready_timeout_seconds is None:
            await asyncio.gather(*waits)
        else:
            await asyncio.wait_for(asyncio.gather(*waits), timeout=ready_timeout_seconds)
    except asyncio.TimeoutError:
        for reservation in list(self._order_reservations):
            if not reservation.released:
                self._release_order_reservation(reservation, wake=True)
                drain_result.released_reservation_count += 1
```

Use the same bounded behavior in Task 4's `_wait_for_reservations()` whenever `_current_drain_context(gate)` returns a context.
Do not pass stale `ready_timeout_seconds` and `drain_result` values captured before shutdown.
The helper must re-read `_current_drain_context(gate)` before every wait loop so an already-running `_drain_gate()` blocked on same-window reservations observes shutdown after `drain_all()` starts.
This matters when `_drain_gate()` is waiting for same-window reservations, not only when `drain_all()` is waiting at the top level.

- [ ] **Step 4: Timeout only unresolved ready waits**

Do not wrap the entire drain or `_dispatch_batch()` in `asyncio.wait_for`.
Apply `ready_timeout_seconds` only when awaiting a not-yet-done `ready_task` during shutdown drain.
Normal ready waits must keep the current `asyncio.shield()` behavior so ordinary drain cancellation does not cancel STT/media preparation.
Only the explicit timeout path cancels the underlying task.

```python
async def _await_ready_task(
    self,
    gate: _GateEntry,
    admission: _QueuedEvent,
    *,
) -> ReadyPendingEvent | None:
    if admission.ready_task is None:
        return admission.ready_result
    drain_context = self._current_drain_context(gate)
    ready_timeout_seconds = drain_context.ready_timeout_seconds if drain_context else None
    drain_result = drain_context.result if drain_context else None
    try:
        if ready_timeout_seconds is None:
            result = await asyncio.shield(admission.ready_task)
        else:
            result = await asyncio.wait_for(asyncio.shield(admission.ready_task), timeout=ready_timeout_seconds)
        if result is None and drain_result is not None:
            drain_result.dropped_ready_count += 1
        return result
    except asyncio.TimeoutError:
        admission.ready_task.cancel()
        try:
            result = await asyncio.wait_for(
                asyncio.gather(admission.ready_task, return_exceptions=True),
                timeout=ready_timeout_seconds,
            )
            if close_ready_task_result_metadata(result[0]):
                if drain_result is not None:
                    drain_result.dropped_ready_count += 1
        except asyncio.TimeoutError:
            admission.ready_task.add_done_callback(self._close_late_ready_task_result)
            pass
        if drain_result is not None:
            drain_result.cancelled_unready_count += 1
        return None
    except asyncio.CancelledError:
        if admission.ready_task.done() and admission.ready_task.cancelled():
            if drain_result is not None:
                drain_result.dropped_ready_count += 1
            return None
        raise
    except Exception:
        if drain_result is not None:
            drain_result.failed_ready_count += 1
        return None
```

Ready tasks that return `None` during shutdown increment `dropped_ready_count`.
Ready tasks that cancel themselves are treated as terminal no-ready admissions, not as outer drain cancellation.
Outer drain cancellation still requeues claimed admissions according to Task 5.
Dispatch failures and cancellations increment `dispatch_failure_count` or `dispatch_cancelled_count` inside `_dispatch_claimed_events`.
Any nonzero drain-result counter makes `completed=False`.

- [ ] **Step 5: Prevent late admission after shutdown release**

When shutdown releases a reservation, `admit` rejection from Task 2 prevents the original handler from recreating work.
The caller's reservation owner cancels any ready task it created before admission.
During bounded shutdown, `reserve_order()` must not create new unsettled work.
It should return a reservation that is already released and settled, increment the gate-level active drain context's `released_reservation_count`, and wake owner gates.

- [ ] **Step 6: Use drain result in `AgentBot.prepare_for_sync_shutdown`**

In `src/mindroom/bot.py`:

```python
drain_result = await self._coalescing_gate.drain_all(ready_timeout_seconds=5.0)
if drain_result.completed and self._sync_trust_state is SyncTrustState.CERTIFIED:
    self._save_sync_checkpoint(self._sync_checkpoint)
elif not drain_result.completed:
    self.logger.warning(
        "sync_checkpoint_not_saved_after_incomplete_coalescing_drain",
        agent_name=self.agent_name,
        released_reservation_count=drain_result.released_reservation_count,
        cancelled_unready_count=drain_result.cancelled_unready_count,
        failed_ready_count=drain_result.failed_ready_count,
        dropped_ready_count=drain_result.dropped_ready_count,
        dispatch_failure_count=drain_result.dispatch_failure_count,
        dispatch_cancelled_count=drain_result.dispatch_cancelled_count,
    )
```

- [ ] **Step 7: Run shutdown tests**

```bash
uv run pytest tests/test_matrix_sync_tokens.py -n auto --no-cov -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/mindroom/coalescing.py src/mindroom/bot.py tests/test_matrix_sync_tokens.py
git commit -m "Bound coalescing shutdown without saving unsafe checkpoints"
```

## Task 8: Delete Voice-Only Ordering Concepts

**Files:**
- Modify: `src/mindroom/coalescing.py`
- Modify: `src/mindroom/turn_controller.py`
- Test: existing focused suites

- [ ] **Step 1: Remove duplicate voice reservation code**

After Task 3, `_handle_media_message_inner` owns reservation creation for every media event.
Delete any separate call to `coalescing_gate.reserve_order` from `_on_audio_media_message`.

- [ ] **Step 2: Classify remaining voice code**

Allowed remaining voice code:

- `VOICE_SOURCE_KIND`
- `_maybe_send_visible_voice_echo`
- `_ready_voice_event`
- `_ready_voice_fallback_event`
- tests that assert router echo suppression, raw-audio fallback, STT failure behavior, and command-looking transcript behavior

Forbidden remaining code:

- `_key_aliases`
- any call to `retarget`
- `VoiceCoalescingGate`
- `turn_ingress_coalescing`
- voice root alias registration
- `_pending_has_voice_source`
- `_can_merge_ready_segment`
- voice-specific room-scope merge checks
- voice-specific upload-grace bypass checks
- voice-specific reservation owner code separate from text/media reservation owner code

- [ ] **Step 3: Verify room-scope batching policy naming**

Task 5 owns replacing `_front_admissions_have_voice`.
This step only verifies the final code names the behavior as room-scope dispatch policy and not as voice-specific ordering.

- [ ] **Step 4: Run scoped stale scans**

```bash
rg -n "_key_aliases|retarget\\(|VoiceCoalescingGate|turn_ingress_coalescing|voice root alias|_front_admissions_have_voice|_pending_has_voice_source|_can_merge_ready_segment|voice-specific room-scope|voice-specific upload-grace" src/mindroom tests
rg -n "provisional.*(coalesc|dispatch)|gate_key.*(coalesc|dispatch)" src/mindroom/coalescing.py src/mindroom/turn_controller.py tests
rg -n "reservation_owner: .*\\| None|reservation_owner is None" src/mindroom/turn_controller.py tests
```

Expected: no matches for removed coalescing concepts.
Do not fail on `PendingDispatchMetadata.target_key`.
Do not fail on the assertion message inside `_enqueue_internal_for_dispatch`; fail on ownerless `_enqueue_for_dispatch` signatures or branches.

- [ ] **Step 5: Run focused suites**

```bash
uv run pytest tests/test_coalescing.py tests/test_live_message_coalescing.py tests/test_voice_bot_threading.py tests/test_multi_agent_bot.py::TestAgentBot::test_text_reserves_receive_order_before_thread_lookup tests/test_multi_agent_bot.py::TestAgentBot::test_media_reserves_receive_order_before_thread_lookup tests/test_multi_agent_bot.py::TestAgentBot::test_file_sidecar_preview_reserves_receive_order_before_preview_normalization tests/test_matrix_sync_tokens.py -n auto --no-cov -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mindroom/coalescing.py src/mindroom/turn_controller.py tests/test_coalescing.py tests/test_live_message_coalescing.py tests/test_voice_bot_threading.py tests/test_multi_agent_bot.py tests/test_matrix_sync_tokens.py
git commit -m "Remove voice-only coalescing order paths"
```

## Task 9: Full Verification and Reviewer Handoff

**Files:**
- No production changes expected.
- May update PR body if behavior or scope text is stale.

- [ ] **Step 1: Run focused tests**

```bash
uv run pytest tests/test_coalescing.py tests/test_live_message_coalescing.py tests/test_voice_bot_threading.py tests/test_matrix_sync_tokens.py tests/test_turn_controller.py::test_late_admit_rejection_closes_completed_ready_task_metadata_once tests/test_turn_controller.py::test_owner_cancel_ready_task_closes_ready_result_returned_during_cancellation tests/test_multi_agent_bot.py::TestAgentBot::test_text_reserves_receive_order_before_thread_lookup tests/test_multi_agent_bot.py::TestAgentBot::test_media_reserves_receive_order_before_thread_lookup tests/test_multi_agent_bot.py::TestAgentBot::test_file_sidecar_preview_reserves_receive_order_before_preview_normalization -n auto --no-cov -q
```

Expected: PASS.

- [ ] **Step 2: Run full tests**

```bash
uv run pytest -n auto --no-cov -q
```

Expected: PASS.

- [ ] **Step 3: Run pre-commit**

```bash
uv run pre-commit run --all-files
```

Expected: PASS.

- [ ] **Step 4: Run hygiene scans**

```bash
git diff --check origin/main -- src/mindroom tests
rg -n "_key_aliases|retarget\\(|VoiceCoalescingGate|turn_ingress_coalescing|voice root alias|_front_admissions_have_voice|_pending_has_voice_source|_can_merge_ready_segment|voice-specific room-scope|voice-specific upload-grace" src/mindroom tests
rg -n "provisional.*(coalesc|dispatch)|gate_key.*(coalesc|dispatch)" src/mindroom/coalescing.py src/mindroom/turn_controller.py tests
rg -n "reservation_owner: .*\\| None|reservation_owner is None" src/mindroom/turn_controller.py tests
git diff --shortstat origin/main -- src/mindroom
```

Expected:

- no whitespace errors
- no stale alias/retarget/second-gate concepts
- `target_key` may remain only as queued-notice metadata
- no ownerless `_enqueue_for_dispatch` path for prompt-like Matrix ingress
- source diff does not grow beyond the current baseline without an explicit written explanation

- [ ] **Step 5: Update PR body**

The PR body must say:

```markdown
This PR now uses one receive-order reservation model for prompt-like text, media, and voice ingress.
Reservations use local monotonic receipt time for debounce; Matrix origin timestamps remain event metadata only.
Voice STT and media preparation remain deferred, but the gate observes receive order before async thread lookup or normalization can reorder turns.
After admission, CoalescingKey(room_id, thread_id, requester_user_id) is the only dispatch scope.
Unresolved reservations may briefly hold same-room/requester gates; admitted different-thread work remains independent.
Incomplete shutdown drains report released reservations, cancelled/failed ready tasks, dropped ready events, and dispatch failures, and unsafe drains do not save certified sync checkpoints.
Queued-notice target_key remains lifecycle metadata and is not a coalescing key alias.
Message edits remain out of scope because they are update flows, not new prompt-like Matrix ingress.
Interactive selections consume and release receive-order reservations when reached through Matrix ingress, but remain out of coalesced dispatch because they call the interactive-selection handler directly.
```

- [ ] **Step 6: Push**

```bash
git push
```

Expected: branch pushes to `origin/codex/issue-225-single-gate`.

## Reviewer Checklist

Ask reviewers to evaluate these claims specifically:

1. Does every prompt-like Matrix ingress path reserve receive order before async lookup, cache append, media prep, fallback prep, or STT?
2. Does every post-reservation path either admit or release exactly once?
3. Is there no ownerless `_enqueue_for_dispatch` or direct `CoalescingGate.enqueue()` path for prompt-like Matrix ingress?
4. Do text, non-audio media, file sidecar preview, voice normal, and voice fallback all prove reservation before their first meaningful await?
5. Does debounce use local monotonic receipt time rather than Matrix origin timestamp?
6. Can a later event dispatch before an earlier same-room/requester unresolved reservation that arrived inside the debounce window?
7. Do unresolved reservation waits stop at command/bypass barriers, including command or bypass already at the queue front?
8. Does a same-window unresolved reservation that later resolves to a different thread hold the earlier debounce until settlement, then split into separate canonical batches?
9. Are admitted different-thread canonical keys independent after admission?
10. Does room-level segment merging require identical canonical keys and a surviving room-scope media-like batching signal for room-level runs?
11. Does the claim lifecycle requeue on resolve cancellation, clear claimed state and wake waiters for command, bypass, normal, zero-ready, partial failure, upload-grace requeue, cancellation, and exceptions?
12. Is metadata closed exactly once for dispatch exceptions, dispatch cancellations, late-admit cleanup, cancellation-returning-ready tasks, and resolved-but-undispatched segments?
13. Is owner-owned ready-task cleanup idempotent when late admit rejection and caller release both run?
14. Does upload-grace requeue transfer metadata ownership back to the queue before any await that can be cancelled?
15. Can shutdown release unresolved reservations or cancel unresolved ready work without saving a certified sync checkpoint?
16. Is there a gate-level active drain context for pre-admission reservations, not only per-gate context?
17. Does shutdown context reach already-running drain tasks, including reservation waits inside `_drain_gate()`?
18. Are in-flight dispatch failures or cancellations during shutdown counted by reading the current drain context at failure time?
19. Are self-cancelled ready tasks treated as terminal no-ready admissions rather than outer drain cancellation?
20. Does synthetic handoff metadata always match `CoalescedBatch.coalescing_key`, including room-level plain replies?
21. Are aliases, provisional dispatch keys, gate retargeting, second gates, and voice-only ordering paths gone?
22. Is any remaining voice-specific code product behavior rather than ordering behavior?
23. Did production diff stay within the accountability budget or explain why it grew?

## Plan Self-Review

- Spec coverage: The plan addresses the repeated reviewer findings around local receipt time, reservation ownership, idempotent owner cleanup, cancellation-returning-ready cleanup, debounce reservations, barrier-bounded reservation waits, same-window different-thread reservation settlement, admitted thread independence, phase-aware claimed lifecycle cleanup, exactly-once metadata closure, upload-grace metadata ownership, shielded ready waits, shutdown reservation release, gate-level active-drain context, dynamic in-flight dispatch shutdown accounting, self-cancelled ready tasks, shutdown sync safety, handoff relation normalization, and stale concept scans.
- Completeness scan: no forbidden open-marker snippets or unspecified edge-case steps remain.
- Type consistency: The plan uses current repo concepts: `CoalescingGate`, `IngressOrderReservation`, `ReadyPendingEvent`, `PendingEvent`, `CoalescedBatch`, `CoalescingKey`, `PendingDispatchMetadata`, `VOICE_SOURCE_KIND`, and `TurnController`.
- Scope check: The plan keeps edits out of coalescing scope explicitly, keeps interactive selections out of coalesced dispatch while still requiring reservation release, keeps queued-notice `target_key` as lifecycle metadata, and does not introduce aliases, provisional keys, retargeting, or a second gate.
