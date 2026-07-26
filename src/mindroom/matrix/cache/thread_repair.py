"""Single-flight ownership, fan-out bounding, and retained live deltas for thread-cache repair.

Repair admission has two tiers. An *interactive* repair backs a caller that is waiting for the
history right now, so it always runs, queueing only behind a global ceiling set well above any real
dispatch fan-out. A *speculative* repair is launched by a live append that found no cached snapshot;
nobody is waiting on its result, so it is dropped rather than queued whenever it would add load:

1. while a sync replay batch is being applied;
2. while one is already scheduled for that thread, or the pending-schedule budget is spent;
3. while any flight for the same thread is already scanning, whatever caller contract owns it;
4. while that thread is inside its post-repair cooldown;
5. while the speculative concurrency budget is spent or an interactive repair is waiting for a slot.

They are listed in the order ``speculative_suppression_reason`` tests them, because the reason it
returns is what gets logged.

Dropping is safe because the thread stays marked stale, so the next read repairs it interactively.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterator

type _ThreadRepairFlightKey = tuple[str, str, str, bool, bool]
type _ThreadRepairDeltaKey = tuple[str, str, str]

# Retained deltas only cover the window where a homeserver scan can miss a just-certified event.
# Once a delta is older than this, any new scan already observes it, so keeping it only wastes memory.
_DELTA_RETENTION_SECONDS = 60.0

# Ceiling on scans in progress at once. This is a safety valve against a pathological storm, not a
# throttle: it sits well above the widest fan-out a real dispatch produces, because an interactive
# repair is a user-facing read and queueing one behind another is latency a caller pays for.
_MAX_CONCURRENT_THREAD_REPAIRS = 64

# The working bound. Every repair is a full history scan contending for the same serialized cache
# write path the Matrix sync callback is blocked on, and nobody is waiting on a speculative one,
# so only a couple run at a time however many threads are stale.
_MAX_CONCURRENT_SPECULATIVE_THREAD_REPAIRS = 2

# One speculative scan per thread per window. A thread that is still broken afterwards is repaired
# by the next read, which is interactive and exempt.
_SPECULATIVE_THREAD_REPAIR_COOLDOWN_SECONDS = 30.0

# A scheduler whose claim was dropped at a membership boundary while it was still preparing.
_CLAIM_REVOKED = "claim_revoked"


@dataclass(frozen=True, slots=True)
class _RetainedDelta:
    """One certified event source held until a scan or append is proven to include it."""

    event_source: dict[str, Any]
    retained_at: float


@dataclass(slots=True)
class _SpeculativeClaim:
    """One thread's scheduling claim and who currently owns it.

    Ownership moves exactly once, from the scheduling task to the registered flight. Both sides
    release by token identity, so neither can drop a claim belonging to a later attempt.
    """

    token: object
    handed_off: bool = False


@dataclass(frozen=True, slots=True)
class _RepairFailureBackoff:
    """Current capped delay after consecutive repair failures."""

    delay_seconds: float
    retry_after: float


class ThreadRepairBackoffError(RuntimeError):
    """Raised when a failed repair is still inside its bounded retry delay."""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"thread cache repair backoff active for {retry_after_seconds:.3f}s")


class ThreadRepairSuppressedError(RuntimeError):
    """Raised when one speculative repair is dropped to bound global repair fan-out."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"speculative thread cache repair suppressed: {reason}")


@dataclass
class ThreadRepairRegistry:
    """Own principal-scoped repair flights, failure backoff, and certified deltas."""

    failure_backoff_seconds: float = 1.0
    max_failure_backoff_seconds: float = 30.0
    delta_retention_seconds: float = _DELTA_RETENTION_SECONDS
    max_concurrent_repairs: int = _MAX_CONCURRENT_THREAD_REPAIRS
    max_concurrent_speculative_repairs: int = _MAX_CONCURRENT_SPECULATIVE_THREAD_REPAIRS
    speculative_cooldown_seconds: float = _SPECULATIVE_THREAD_REPAIR_COOLDOWN_SECONDS
    clock: Callable[[], float] = time.monotonic
    _tasks: dict[_ThreadRepairFlightKey, asyncio.Task[object]] = field(default_factory=dict, init=False)
    _failure_backoffs: dict[_ThreadRepairFlightKey, _RepairFailureBackoff] = field(default_factory=dict, init=False)
    _deltas: dict[_ThreadRepairDeltaKey, dict[str, _RetainedDelta]] = field(default_factory=dict, init=False)
    _speculative_cooldowns: dict[_ThreadRepairDeltaKey, float] = field(default_factory=dict, init=False)
    _interactive_joins: dict[_ThreadRepairFlightKey, int] = field(default_factory=dict, init=False)
    _reserved_speculative: dict[_ThreadRepairDeltaKey, _SpeculativeClaim] = field(
        default_factory=dict,
        init=False,
    )
    _flight_claims: dict[_ThreadRepairFlightKey, object] = field(default_factory=dict, init=False)
    _repair_slots: asyncio.Semaphore = field(init=False, repr=False)
    _running_speculative_repairs: int = field(default=0, init=False)
    _speculative_suppression_depth: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """Bind the global repair ceiling, which is fixed for the life of the registry.

        A semaphore binds its loop on the first blocking acquire, not here, so constructing one
        outside a running loop is fine. It is never replaced: a repair still holding a permit would
        release it into the replacement and raise the ceiling above its own bound.
        """
        self._repair_slots = asyncio.Semaphore(self.max_concurrent_repairs)

    @staticmethod
    def _thread_key(key: _ThreadRepairFlightKey) -> _ThreadRepairDeltaKey:
        """Return the thread one caller contract repairs, without the contract itself."""
        coordination_scope, room_id, thread_id, _hydrate_sidecars, _allow_stale_fallback = key
        return coordination_scope, room_id, thread_id

    def _active_task(self, key: _ThreadRepairFlightKey) -> asyncio.Task[object] | None:
        task = self._tasks.get(key)
        if task is None:
            return None
        if task.done():
            self._tasks.pop(key, None)
            self._release_flight_claim(self._thread_key(key), self._flight_claims.pop(key, None))
            return None
        return task

    def _clear_task(self, key: _ThreadRepairFlightKey, task: asyncio.Task[object]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
            # A flight cancelled before its body ran still owns a claim; only this flight's own
            # token is released, so a later attempt on the same thread keeps its own.
            self._release_flight_claim(self._thread_key(key), self._flight_claims.pop(key, None))

    def _has_active_task(self, key: _ThreadRepairDeltaKey) -> bool:
        """Return whether any caller contract owns this thread's repair."""
        for flight_key in tuple(self._tasks):
            if flight_key[:3] == key and self._active_task(flight_key) is not None:
                return True
        return False

    def _drop_stale_failure_backoffs(self, now: float) -> None:
        stale_before = now - self.max_failure_backoff_seconds
        self._failure_backoffs = {
            key: backoff for key, backoff in self._failure_backoffs.items() if backoff.retry_after > stale_before
        }

    def _record_failure(self, key: _ThreadRepairFlightKey) -> None:
        now = self.clock()
        self._drop_stale_failure_backoffs(now)
        previous = self._failure_backoffs.get(key)
        delay_seconds = (
            self.failure_backoff_seconds
            if previous is None
            else min(previous.delay_seconds * 2, self.max_failure_backoff_seconds)
        )
        self._failure_backoffs[key] = _RepairFailureBackoff(
            delay_seconds=delay_seconds,
            retry_after=now + delay_seconds,
        )

    def retry_after_seconds(self, key: _ThreadRepairFlightKey) -> float:
        """Return remaining repair backoff for one key."""
        backoff = self._failure_backoffs.get(key)
        if backoff is None:
            return 0.0
        return max(0.0, backoff.retry_after - self.clock())

    @contextmanager
    def suppress_speculative_repairs(self) -> Iterator[None]:
        """Drop speculative repairs while one sync replay batch is being applied.

        Replay re-delivers events whose threads are being rewritten anyway, so speculative scans
        started from it are near-certain to lose the guarded replacement race and only add load to
        the write path the sync callback is already blocked on.
        """
        self._speculative_suppression_depth += 1
        try:
            yield
        finally:
            self._speculative_suppression_depth = max(0, self._speculative_suppression_depth - 1)

    def _speculative_cooldown_active(self, key: _ThreadRepairDeltaKey) -> bool:
        """Return whether this thread was scanned too recently.

        A pure read: expired entries are dropped when the next cooldown is armed, which keeps this
        O(1) on the hottest path and keeps the public suppression query free of side effects.
        """
        retry_after = self._speculative_cooldowns.get(key)
        return retry_after is not None and retry_after > self.clock()

    def speculative_suppression_reason(
        self,
        key: _ThreadRepairDeltaKey,
        *,
        ignore_active_flight: bool = False,
        ignore_reservation: bool = False,
    ) -> str | None:
        """Return why one speculative repair must be dropped, or ``None`` when it may run.

        The key is the thread, not the caller contract, so a speculative trigger never opens a
        second scan of a thread another contract is already scanning. A flight re-checking itself
        just before it scans passes ``ignore_active_flight`` so it does not see its own ownership,
        and a caller carrying this thread's scheduling reservation passes ``ignore_reservation`` so
        it does not see its own claim.
        """
        # Kept lazy and in order: the reason returned is the one that gets logged, and
        # ``_has_active_task`` drops finished flights as it looks.
        declines: tuple[tuple[Callable[[], bool], str], ...] = (
            (lambda: self._speculative_suppression_depth > 0, "sync_replay"),
            (lambda: not ignore_reservation and key in self._reserved_speculative, "repair_pending"),
            (
                lambda: (
                    not ignore_reservation
                    and len(self._reserved_speculative) >= self.max_concurrent_speculative_repairs
                ),
                "speculative_pending_limit",
            ),
            (lambda: not ignore_active_flight and self._has_active_task(key), "repair_in_flight"),
            (lambda: self._speculative_cooldown_active(key), "recently_repaired"),
            (
                lambda: self._running_speculative_repairs >= self.max_concurrent_speculative_repairs,
                "speculative_concurrency_limit",
            ),
            # ``locked`` is true while anyone is queued as well as at capacity, so a speculative
            # caller never steps in front of an interactive one waiting for a slot.
            (self._repair_slots.locked, "repair_concurrency_limit"),
        )
        return next((reason for declined, reason in declines if declined()), None)

    def reserve_speculative_repair(self, key: _ThreadRepairDeltaKey) -> object | None:
        """Claim the right to schedule one speculative repair, returning a token, or ``None``.

        Admission inside :meth:`run` cannot bound a burst, because a burst is synchronous: nothing
        has reached ``run`` yet, so every caller sees free capacity and adds another task. This is
        the check that has to be atomic with the decision to create one, so it both tests and marks.

        The claim is held until :meth:`run` takes over ownership of the thread, which covers the
        awaited preparation in between. The token identifies the holder so a late release can never
        drop a claim that has since been handed to somebody else.
        """
        if self.speculative_suppression_reason(key) is not None:
            return None
        token = object()
        self._reserved_speculative[key] = _SpeculativeClaim(token=token)
        return token

    def release_speculative_repair(self, key: _ThreadRepairDeltaKey, token: object) -> None:
        """Drop a scheduling claim, but only while the scheduling task still owns this token.

        Once the claim has been handed to a registered flight the scheduling task is no longer its
        owner, so cancelling that task must leave the claim in place: the flight it registered is
        still queued and still has to be bounded.
        """
        claim = self._reserved_speculative.get(key)
        if claim is not None and claim.token is token and not claim.handed_off:
            del self._reserved_speculative[key]

    def _owns_claim(self, key: _ThreadRepairDeltaKey, token: object) -> bool:
        """Return whether this exact token is still the live claim on this thread."""
        claim = self._reserved_speculative.get(key)
        return claim is not None and claim.token is token

    def _hand_off_claim(self, key: _ThreadRepairDeltaKey, token: object) -> object | None:
        """Move this thread's claim to the flight being registered, if this token still holds it.

        Keyed on the token rather than the thread, because a scheduler blocked in preparation can
        resume after a ``clear_room`` and find a later attempt holding a different claim; handing
        that one off would register a stale flight against a newer caller's token.
        """
        if not self._owns_claim(key, token):
            return None
        claim = self._reserved_speculative[key]
        claim.handed_off = True
        return claim.token

    def _discard_flight_claim(self, key: _ThreadRepairFlightKey, token: object | None) -> None:
        """Forget a flight's cleanup token, but only while it is still that flight's."""
        if token is not None and self._flight_claims.get(key) is token:
            del self._flight_claims[key]

    def _release_flight_claim(self, key: _ThreadRepairDeltaKey, token: object | None) -> None:
        """Drop a claim a flight owns, identified by token so a stale body cannot take a newer one."""
        if token is None:
            return
        claim = self._reserved_speculative.get(key)
        if claim is not None and claim.token is token:
            del self._reserved_speculative[key]

    @contextmanager
    def _joined_interactively(self, key: _ThreadRepairFlightKey) -> Iterator[None]:
        """Record that a waiting caller depends on this flight for as long as it is joined."""
        self._interactive_joins[key] = self._interactive_joins.get(key, 0) + 1
        try:
            yield
        finally:
            remaining = self._interactive_joins.get(key, 1) - 1
            if remaining > 0:
                self._interactive_joins[key] = remaining
            else:
                self._interactive_joins.pop(key, None)

    async def _acquire_repair_slot(self, *, speculative: bool) -> None:
        """Take one global repair slot, waiting only for callers someone is blocked on.

        The slot is taken immediately before the scan, never while the flight is still queued behind
        same-thread predecessors, so a slot always measures work actually in progress. Speculative
        callers test capacity without blocking first, so only interactive callers ever queue here.
        """
        await self._repair_slots.acquire()
        if speculative:
            self._running_speculative_repairs += 1

    def _release_repair_slot(self, *, speculative: bool) -> None:
        if speculative:
            self._running_speculative_repairs = max(0, self._running_speculative_repairs - 1)
        self._repair_slots.release()

    def _arm_speculative_cooldown(self, key: _ThreadRepairFlightKey) -> None:
        """Hold off further speculative scans of this thread after one has just run.

        Expired entries are swept here rather than on the append path: a repair completing is rare
        next to an append, and a thread that is never speculatively re-checked would otherwise keep
        its entry for the life of the process.
        """
        now = self.clock()
        self._speculative_cooldowns = {
            cooled_key: retry_after
            for cooled_key, retry_after in self._speculative_cooldowns.items()
            if retry_after > now
        }
        self._speculative_cooldowns[self._thread_key(key)] = now + self.speculative_cooldown_seconds

    def _admission_error(
        self,
        key: _ThreadRepairFlightKey,
        *,
        speculative: bool,
        bypass_failure_backoff: bool,
    ) -> Exception | None:
        """Return why this caller may not start a new scan right now."""
        if speculative:
            suppression_reason = self.speculative_suppression_reason(
                self._thread_key(key),
                ignore_reservation=True,
            )
            if suppression_reason is not None:
                return ThreadRepairSuppressedError(suppression_reason)
        retry_after_seconds = self.retry_after_seconds(key)
        if retry_after_seconds > 0 and not bypass_failure_backoff:
            return ThreadRepairBackoffError(retry_after_seconds)
        return None

    async def _run_in_repair_slot[T](
        self,
        key: _ThreadRepairFlightKey,
        run_repair: Callable[[], Awaitable[T]],
        *,
        speculative: bool,
        claim_token: object | None = None,
    ) -> T:
        """Run one admitted repair while holding exactly one global slot.

        Reached only once same-thread predecessors and the coordinator barrier have drained, so a
        held slot always measures a scan in progress. Capacity is re-checked for speculative work because that queue wait can be
        long enough for the runtime to have filled up, or for another flight to have fixed a thread.

        A flight an interactive caller has joined stops being speculative: declining it would raise
        into a read that is waiting on this exact result, and that caller is owed the scan.
        """
        if speculative:
            # Registering the flight is not the end of the claim: `schedule` only queues the body,
            # which then waits behind the coordinator barrier without holding a slot or counting
            # against the speculative budget. Releasing at registration would leave that whole wait
            # unbounded, so the claim is carried until here, where the scan gates below take over.
            # Released by the token this flight was registered with, so a body that outlived a
            # `clear_room` cannot drop the claim a later attempt on the same thread now holds.
            self._release_flight_claim(self._thread_key(key), claim_token)
            self._discard_flight_claim(key, claim_token)
        if speculative and self._interactive_joins.get(key):
            speculative = False
        if speculative:
            deferred_reason = self.speculative_suppression_reason(
                self._thread_key(key),
                ignore_active_flight=True,
            )
            if deferred_reason is not None:
                raise ThreadRepairSuppressedError(deferred_reason)
        await self._acquire_repair_slot(speculative=speculative)
        try:
            return await run_repair()
        finally:
            self._release_repair_slot(speculative=speculative)
            self._arm_speculative_cooldown(key)

    async def _join_running_flight[T](
        self,
        key: _ThreadRepairFlightKey,
        active_task: asyncio.Task[object],
        *,
        speculative: bool,
    ) -> T:
        """Await the flight this caller joins.

        A speculative flight scans a lost guarded replacement once where an interactive one scans
        twice, but the loser still returns the history it just fetched -- only the durable snapshot
        is missing, and the thread stays stale for the next read to install it. So a joining reader
        inherits a complete answer either way and is never owed a second scan for the difference.
        """
        if speculative:
            return cast("T", await asyncio.shield(active_task))
        with self._joined_interactively(key):
            return cast("T", await asyncio.shield(active_task))

    async def run[T](
        self,
        key: _ThreadRepairFlightKey,
        *,
        schedule: Callable[[Callable[[], Awaitable[T]]], asyncio.Task[T]],
        repair: Callable[[], Awaitable[T]],
        result_arms_backoff: Callable[[T], bool],
        bypass_failure_backoff: bool = False,
        speculative: bool = False,
        claim_token: object | None = None,
    ) -> T:
        """Join or start one shielded repair and update backoff from its outcome.

        Authoritative untimed reads may bypass an existing delay while preserving its failure count.
        A speculative caller raises ``ThreadRepairSuppressedError`` instead of adding a scan whenever
        the fan-out gate declines it.
        """

        async def run_repair() -> T:
            try:
                value = await repair()
            except Exception:
                if self._tasks.get(key) is asyncio.current_task():
                    self._record_failure(key)
                raise
            if self._tasks.get(key) is asyncio.current_task():
                if result_arms_backoff(value):
                    self._record_failure(key)
                else:
                    self._failure_backoffs.pop(key, None)
            return value

        if speculative and claim_token is not None and not self._owns_claim(self._thread_key(key), claim_token):
            # This caller's claim was dropped at a membership boundary while it was still preparing.
            # Whatever holds the thread now belongs to a later attempt, and joining or registering
            # against it would let a pre-clear flight answer a post-clear caller.
            raise ThreadRepairSuppressedError(_CLAIM_REVOKED)
        active_task = self._active_task(key)
        if active_task is not None:
            return await self._join_running_flight(key, active_task, speculative=speculative)
        admission_error = self._admission_error(
            key,
            speculative=speculative,
            bypass_failure_backoff=bypass_failure_backoff,
        )
        if admission_error is not None:
            raise admission_error

        flight_token = (
            self._hand_off_claim(self._thread_key(key), claim_token)
            if speculative and claim_token is not None
            else None
        )
        task = schedule(
            lambda: self._run_in_repair_slot(key, run_repair, speculative=speculative, claim_token=flight_token),
        )
        self._tasks[key] = task
        if flight_token is not None:
            self._flight_claims[key] = flight_token
        task.add_done_callback(lambda done_task: self._clear_task(key, done_task))
        return await asyncio.shield(task)

    def _drop_expired_deltas(self) -> None:
        cutoff = self.clock() - self.delta_retention_seconds
        for key, deltas in list(self._deltas.items()):
            if self._has_active_task(key):
                # Retention assumes a later scan already observes the event, which only holds for a
                # scan that starts after it. A running scan may have started earlier and paginate past
                # the window, so its deltas stay until that flight ends and a fresh scan can see them.
                continue
            for event_id, delta in list(deltas.items()):
                if delta.retained_at <= cutoff:
                    del deltas[event_id]
            if not deltas:
                self._deltas.pop(key, None)

    def retain_delta(self, key: _ThreadRepairDeltaKey, event_source: dict[str, Any]) -> None:
        """Retain one certified thread event until append or repair durably includes it."""
        event_id = event_source.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return
        self._drop_expired_deltas()
        self._deltas.setdefault(key, {})[event_id] = _RetainedDelta(
            event_source=dict(event_source),
            retained_at=self.clock(),
        )

    def pending_deltas(self, key: _ThreadRepairDeltaKey) -> tuple[dict[str, Any], ...]:
        """Return retained deltas in deterministic retention order."""
        self._drop_expired_deltas()
        deltas = self._deltas.get(key, {})
        return tuple(dict(delta.event_source) for delta in deltas.values())

    def acknowledge_deltas(self, key: _ThreadRepairDeltaKey, event_ids: Collection[str]) -> None:
        """Forget retained deltas proven present in a usable snapshot."""
        deltas = self._deltas.get(key)
        if deltas is None:
            return
        for event_id in event_ids:
            deltas.pop(event_id, None)
        if not deltas:
            self._deltas.pop(key, None)

    def clear_room(self, coordination_scope: str, room_id: str) -> None:
        """Drop retained deltas and failure history at one membership boundary."""
        self._tasks = {key: task for key, task in self._tasks.items() if key[:2] != (coordination_scope, room_id)}
        self._deltas = {key: deltas for key, deltas in self._deltas.items() if key[:2] != (coordination_scope, room_id)}
        self._failure_backoffs = {
            key: backoff for key, backoff in self._failure_backoffs.items() if key[:2] != (coordination_scope, room_id)
        }
        self._speculative_cooldowns = {
            key: retry_after
            for key, retry_after in self._speculative_cooldowns.items()
            if key[:2] != (coordination_scope, room_id)
        }
        self._reserved_speculative = {
            key: claim for key, claim in self._reserved_speculative.items() if key[:2] != (coordination_scope, room_id)
        }
        self._flight_claims = {
            key: token for key, token in self._flight_claims.items() if key[:2] != (coordination_scope, room_id)
        }

    def clear(self) -> None:
        """Drop runtime-only ownership after all coordinator tasks drained."""
        self._tasks.clear()
        self._failure_backoffs.clear()
        self._deltas.clear()
        self._speculative_cooldowns.clear()
        self._interactive_joins.clear()
        self._reserved_speculative.clear()
        self._flight_claims.clear()
        self._running_speculative_repairs = 0
