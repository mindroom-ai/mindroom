"""Serialized restart recovery with per-owner semantic retry state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.matrix.stale_stream_cleanup import InterruptedTargetFreshness
from mindroom.orchestration.runtime import create_logged_task
from mindroom.restart_recovery_operations import (
    RecoveryOwner,
    RestartDeliveryOutcome,
    RestartRecoveryOperations,
    RoomRecoveryRequest,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.config.main import Config
    from mindroom.matrix.stale_stream_cleanup import InterruptedThread

logger = get_logger(__name__)

type _TargetKey = tuple[str, str, str]
type _RoomKey = tuple[str, int | None, bool]
type _JobKey = tuple[_RoomKey, str]


_MAX_CONCURRENT_MATRIX_READ_PHASES = 8
_MAX_MATRIX_ATTEMPTS = 6
_MAX_OWNER_DISCOVERY_ATTEMPTS = 6
_MAX_READINESS_PROBES = 6


@dataclass(frozen=True)
class _OwnerRoomWork:
    """One exact owner's retained work for one semantic room request."""

    request: RoomRecoveryRequest
    owner_user_id: str
    generation: object | None
    targets: tuple[InterruptedThread, ...] = ()
    matrix_attempt: int = 0
    readiness_probe: int = 0
    membership_probe: int = 0
    due_at: float | None = 0.0

    @property
    def key(self) -> _JobKey:
        return self.request.key, self.owner_user_id

    @property
    def room_id(self) -> str:
        return self.request.room_id


@dataclass(frozen=True)
class _RoomLease:
    """One shared room scan over exact per-owner jobs."""

    request: RoomRecoveryRequest
    jobs: tuple[_OwnerRoomWork, ...]

    @property
    def room_id(self) -> str:
        return self.request.room_id

    @property
    def all_owner_user_ids(self) -> frozenset[str]:
        return frozenset(job.owner_user_id for job in self.jobs)


@dataclass(frozen=True)
class _TargetWatermark:
    """Monotonic settled state for one owner-room-thread."""

    generation: object
    version: tuple[int, str]
    closed: bool = False


@dataclass(frozen=True)
class _TargetSettlement:
    target: InterruptedThread
    closed: bool


@dataclass(frozen=True)
class _OwnerAttemptResult:
    """One leased owner's independent outcome."""

    work: _OwnerRoomWork
    owner: RecoveryOwner | None
    retry_targets: tuple[InterruptedThread, ...] = ()
    settlements: tuple[_TargetSettlement, ...] = ()
    readiness_unavailable: bool = False
    membership_unavailable: bool = False
    matrix_failed: bool = False
    cancelled: bool = False


@dataclass(frozen=True)
class _OwnerRoomDiscoveryResult:
    """Authoritative joined-room scope for one exact owner generation."""

    room_ids: frozenset[str] | None


def _restart_recovery_retry_delay(attempt: int) -> float:
    """Return capped exponential delay for a one-based retry attempt."""
    return min(60.0, 2.0 ** max(1, attempt))


async def _cancel_and_drain_tasks[T](tasks: tuple[asyncio.Task[T], ...]) -> None:
    """Drain owned tasks despite repeated cancellation of the lifecycle task."""
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    drain = asyncio.gather(*tasks, return_exceptions=True)
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            continue


def _target_version(target: InterruptedThread) -> tuple[int, str]:
    return target.timestamp_ms, target.target_event_id


def _newest_targets(targets: tuple[InterruptedThread, ...]) -> tuple[InterruptedThread, ...]:
    newest: dict[str, InterruptedThread] = {}
    for target in targets:
        if target.thread_id is None:
            continue
        current = newest.get(target.thread_id)
        if current is None or _target_version(target) > _target_version(current):
            newest[target.thread_id] = target
    return tuple(
        sorted(
            newest.values(),
            key=lambda target: (target.thread_id or "", _target_version(target)),
        ),
    )


class RestartRecoveryCoordinator:
    """Own per-owner room jobs and monotonic owner-target watermarks."""

    def __init__(
        self,
        *,
        current_config: Callable[[], Config | None],
        current_owners: Callable[[], Mapping[str, RecoveryOwner]],
        operations: RestartRecoveryOperations,
        retry_delay: Callable[[int], float] = _restart_recovery_retry_delay,
    ) -> None:
        self._current_config = current_config
        self._current_owners = current_owners
        self._operations = operations
        self._retry_delay = retry_delay
        self._room_jobs: dict[_JobKey, _OwnerRoomWork] = {}
        self._target_watermarks: dict[_TargetKey, _TargetWatermark] = {}
        self._completed_startup_scans: set[tuple[_RoomKey, str]] = set()
        self._active_attempts: dict[asyncio.Task[tuple[_OwnerAttemptResult, ...]], _RoomLease] = {}
        self._owner_room_discoveries: dict[
            asyncio.Task[_OwnerRoomDiscoveryResult],
            RecoveryOwner,
        ] = {}
        self._matrix_read_slots = asyncio.Semaphore(_MAX_CONCURRENT_MATRIX_READ_PHASES)
        self._delivery_lock = asyncio.Lock()
        self._worker_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._startup_cutoff_ms: int | None = None
        self._paused = True
        self._stopped = False

    def start(self, *, startup_cutoff_ms: int) -> None:
        """Start recovery and enqueue current owners' desired rooms."""
        self._startup_cutoff_ms = startup_cutoff_ms
        self._stopped = False
        self._paused = False
        for owner in self._current_owners().values():
            self._enqueue_desired_rooms(owner)
            self._start_owner_room_discovery(owner)
        self._ensure_worker()

    def owner_ready(self, owner_user_id: str) -> None:
        """Refresh one owner and grant retained work a fresh bounded budget."""
        if self._stopped:
            return
        self._settle_finished_attempts()
        owner = self._current_owners().get(owner_user_id)
        if owner is not None:
            self._operations.discard_owner(owner_user_id)
            self._cancel_owner_room_discoveries(owner_user_id)
            self._target_watermarks = {
                key: watermark
                for key, watermark in self._target_watermarks.items()
                if key[0] != owner_user_id or watermark.generation is owner.generation
            }
            self._refresh_owner_jobs(owner, grant_fresh_budget=True)
            self._enqueue_desired_rooms(owner)
            if not self._paused:
                self._start_owner_room_discovery(owner)
        self._ensure_worker()

    def enqueue_replacement_rooms(self, owner_user_id: str, room_ids: set[str]) -> None:
        """Retain terminal interrupted-room handoffs across bot replacement."""
        if self._stopped:
            return
        if not self._require_config().defaults.auto_resume_after_restart:
            return
        for room_id in room_ids:
            self._enqueue_request(
                owner_user_id,
                RoomRecoveryRequest(
                    room_id=room_id,
                    startup_cutoff_ms=None,
                    terminal_interrupted_only=True,
                ),
                grant_fresh_budget=True,
            )
        self._ensure_worker()

    def discard_owner(self, owner_user_id: str) -> None:
        """Discard one removed owner's jobs, watermarks, and membership snapshot."""
        self._room_jobs = {key: work for key, work in self._room_jobs.items() if work.owner_user_id != owner_user_id}
        self._target_watermarks = {
            key: watermark for key, watermark in self._target_watermarks.items() if key[0] != owner_user_id
        }
        self._completed_startup_scans = {key for key in self._completed_startup_scans if key[1] != owner_user_id}
        self._cancel_owner_room_discoveries(owner_user_id)
        self._operations.discard_owner(owner_user_id)
        self._wake.set()

    async def pause(self) -> None:
        """Pause before config mutation and drain active leases."""
        self._paused = True
        self._wake.set()
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def resume(self) -> None:
        """Resume retained work against current config and owner generations."""
        if self._stopped:
            return
        if not self._require_config().defaults.auto_resume_after_restart:
            self._room_jobs = {
                key: work for key, work in self._room_jobs.items() if not work.request.terminal_interrupted_only
            }
        owners = tuple(self._current_owners().values()) if self._startup_cutoff_ms is not None else ()
        if self._room_jobs:
            for owner in owners:
                self._refresh_owner_jobs(owner, grant_fresh_budget=True)
        self._paused = False
        for owner in owners:
            self._operations.discard_owner(owner.user_id)
            self._start_owner_room_discovery(owner)
        self._ensure_worker()

    async def stop(self) -> None:
        """Stop recovery and release current membership snapshots."""
        self._stopped = True
        self._paused = True
        self._wake.set()
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._room_jobs.clear()
        self._target_watermarks.clear()
        self._completed_startup_scans.clear()
        await self._drain_owner_room_discoveries()
        await self._operations.close()

    def _require_config(self) -> Config:
        config = self._current_config()
        assert config is not None
        return config

    def _refresh_owner_jobs(
        self,
        owner: RecoveryOwner,
        *,
        grant_fresh_budget: bool,
    ) -> None:
        now = asyncio.get_running_loop().time()
        for key, work in tuple(self._room_jobs.items()):
            if work.owner_user_id != owner.user_id:
                continue
            generation_changed = work.generation is not owner.generation
            if generation_changed or grant_fresh_budget:
                self._room_jobs[key] = replace(
                    work,
                    generation=owner.generation,
                    matrix_attempt=0,
                    readiness_probe=0,
                    membership_probe=0,
                    due_at=now,
                )
        self._wake.set()

    def _enqueue_desired_rooms(self, owner: RecoveryOwner) -> None:
        if self._startup_cutoff_ms is None:
            return
        for room_id in owner.desired_room_ids:
            self._enqueue_startup_room(owner.user_id, room_id)

    def _enqueue_startup_room(self, owner_user_id: str, room_id: str) -> None:
        if self._startup_cutoff_ms is None:
            return
        request = RoomRecoveryRequest(
            room_id=room_id,
            startup_cutoff_ms=self._startup_cutoff_ms,
            terminal_interrupted_only=False,
        )
        key = request.key, owner_user_id
        if (request.key, owner_user_id) in self._completed_startup_scans or key in self._room_jobs:
            return
        self._enqueue_request(owner_user_id, request)

    def _enqueue_request(
        self,
        owner_user_id: str,
        request: RoomRecoveryRequest,
        *,
        grant_fresh_budget: bool = False,
    ) -> None:
        if not request.room_id.startswith("!"):
            return
        key = request.key, owner_user_id
        existing = self._room_jobs.get(key)
        owner = self._current_owners().get(owner_user_id)
        generation = None if owner is None else owner.generation
        now = asyncio.get_running_loop().time()
        if existing is None:
            self._room_jobs[key] = _OwnerRoomWork(
                request=request,
                owner_user_id=owner_user_id,
                generation=generation,
                due_at=now,
            )
        elif existing.generation is not generation or grant_fresh_budget:
            self._room_jobs[key] = replace(
                existing,
                generation=generation,
                matrix_attempt=0,
                readiness_probe=0,
                membership_probe=0,
                due_at=now,
            )
        self._wake.set()

    def _ensure_worker(self) -> None:
        if self._paused or self._stopped:
            return
        task = self._worker_task
        if task is not None and not task.done():
            self._wake.set()
            return
        self._worker_task = create_logged_task(
            self._run(),
            name="restart_recovery",
            failure_message="Restart recovery worker failed",
        )

    async def _run(self) -> None:
        try:
            while not self._paused and not self._stopped:
                self._settle_finished_owner_room_discoveries()
                self._settle_finished_attempts()
                self._start_due_attempts()
                await self._wait_for_progress(delay=self._next_start_delay())
        finally:
            await self._drain_owner_room_discoveries()
            await self._drain_active_attempts()

    def _start_owner_room_discovery(self, owner: RecoveryOwner) -> None:
        self._cancel_owner_room_discoveries(owner.user_id)
        task = asyncio.create_task(
            self._discover_owner_rooms(owner),
            name=f"restart_recovery_joined_rooms:{owner.user_id}",
        )
        self._owner_room_discoveries[task] = owner
        task.add_done_callback(lambda _task: self._wake.set())

    def _cancel_owner_room_discoveries(self, owner_user_id: str) -> None:
        for task, owner in self._owner_room_discoveries.items():
            if owner.user_id == owner_user_id and not task.done():
                task.cancel()

    async def _discover_owner_rooms(
        self,
        owner: RecoveryOwner,
    ) -> _OwnerRoomDiscoveryResult:
        room_ids: list[str] | None = None
        for attempt in range(1, _MAX_OWNER_DISCOVERY_ATTEMPTS + 1):
            try:
                async with self._matrix_read_slots:
                    room_ids = await self._operations.joined_rooms(owner)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Failed to discover owner rooms during restart recovery",
                    attempt=attempt,
                    owner_user_id=owner.user_id,
                    exc_info=True,
                )
            if room_ids is not None:
                return _OwnerRoomDiscoveryResult(
                    room_ids=frozenset(room_id for room_id in room_ids if room_id.startswith("!")),
                )
            if attempt < _MAX_OWNER_DISCOVERY_ATTEMPTS:
                await asyncio.sleep(self._retry_delay(attempt))
        return _OwnerRoomDiscoveryResult(room_ids=None)

    def _settle_finished_owner_room_discoveries(self) -> None:
        finished = tuple((task, owner) for task, owner in self._owner_room_discoveries.items() if task.done())
        if not finished:
            return
        current_owners = self._current_owners()
        for task, owner in finished:
            self._owner_room_discoveries.pop(task)
            try:
                result = task.result()
            except asyncio.CancelledError:
                continue
            current_owner = current_owners.get(owner.user_id)
            if current_owner is None or current_owner.generation is not owner.generation:
                continue
            if result.room_ids is None:
                logger.warning(
                    "Restart recovery could not discover owner room scope",
                    owner_user_id=owner.user_id,
                )
                continue
            for room_id in result.room_ids:
                self._enqueue_startup_room(owner.user_id, room_id)

    async def _drain_owner_room_discoveries(self) -> None:
        tasks = tuple(self._owner_room_discoveries)
        await _cancel_and_drain_tasks(tasks)
        self._owner_room_discoveries.clear()

    def _active_room_ids(self) -> set[str]:
        return {lease.room_id for lease in self._active_attempts.values()}

    def _eligible_work(self) -> list[_OwnerRoomWork]:
        active_room_ids = self._active_room_ids()
        return [
            work for work in self._room_jobs.values() if work.room_id not in active_room_ids and work.due_at is not None
        ]

    def _next_due_work(self, now: float) -> _OwnerRoomWork | None:
        eligible = [work for work in self._eligible_work() if work.due_at is not None and work.due_at <= now]
        if not eligible:
            return None
        active_owners = set().union(
            *(lease.all_owner_user_ids for lease in self._active_attempts.values()),
        )
        return min(
            eligible,
            key=lambda work: (
                work.owner_user_id in active_owners,
                work.due_at or 0.0,
                work.room_id,
                work.request.startup_cutoff_ms or -1,
                work.request.terminal_interrupted_only,
                work.owner_user_id,
            ),
        )

    def _start_due_attempts(self) -> None:
        now = asyncio.get_running_loop().time()
        while True:
            seed = self._next_due_work(now)
            if seed is None:
                return
            jobs = tuple(
                sorted(
                    (
                        work
                        for work in self._room_jobs.values()
                        if work.request == seed.request and work.due_at is not None and work.due_at <= now
                    ),
                    key=lambda work: work.owner_user_id,
                ),
            )
            lease = _RoomLease(seed.request, jobs)
            task = asyncio.create_task(
                self._process_room(lease),
                name=f"restart_recovery_room:{seed.request.key}",
            )
            self._active_attempts[task] = lease

    def _next_start_delay(self) -> float | None:
        eligible = self._eligible_work()
        if not eligible:
            return None
        due_at = min(work.due_at for work in eligible if work.due_at is not None)
        return max(0.0, due_at - asyncio.get_running_loop().time())

    async def _wait_for_progress(self, *, delay: float | None) -> None:
        self._wake.clear()
        active_tasks = tuple(self._active_attempts)
        if any(task.done() for task in active_tasks):
            return
        if not active_tasks and delay is None:
            await self._wake.wait()
            return
        wake_task = asyncio.create_task(self._wake.wait(), name="restart_recovery_wake")
        try:
            await asyncio.wait(
                (wake_task, *active_tasks),
                timeout=delay,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            wake_task.cancel()
            await asyncio.gather(wake_task, return_exceptions=True)

    def _settle_finished_attempts(self) -> None:
        for task, lease in tuple(self._active_attempts.items()):
            if not task.done():
                continue
            self._active_attempts.pop(task)
            self._settle_attempt(lease, task)

    def _settle_attempt(
        self,
        lease: _RoomLease,
        task: asyncio.Task[tuple[_OwnerAttemptResult, ...]],
    ) -> None:
        try:
            result = task.result()
        except asyncio.CancelledError:
            self._restore_cancelled(lease)
            return
        except Exception:
            logger.warning("Restart recovery attempt failed", exc_info=True)
            self._restore_failed_lease(lease)
            return
        for outcome in result:
            self._settle_owner_outcome(outcome)

    def _settle_owner_outcome(self, outcome: _OwnerAttemptResult) -> None:
        work = outcome.work
        current_work = self._room_jobs.get(work.key)
        owner = outcome.owner
        if current_work is not None and owner is not None and current_work.generation is owner.generation:
            for settlement in outcome.settlements:
                self._advance_watermark(owner, settlement)
        if current_work is not work:
            return
        if outcome.cancelled:
            self._room_jobs[work.key] = replace(
                work,
                targets=_newest_targets(outcome.retry_targets),
                due_at=asyncio.get_running_loop().time(),
            )
            return
        if outcome.readiness_unavailable:
            self._retry_readiness(work)
            return
        if outcome.membership_unavailable:
            self._retry_membership(work)
            return
        if outcome.matrix_failed:
            self._retry_matrix(work, outcome.retry_targets)
            return
        self._room_jobs.pop(work.key)
        if not work.request.terminal_interrupted_only:
            self._completed_startup_scans.add((work.request.key, work.owner_user_id))

    def _restore_cancelled(self, lease: _RoomLease) -> None:
        if self._stopped:
            return
        now = asyncio.get_running_loop().time()
        for work in lease.jobs:
            if self._room_jobs.get(work.key) is work:
                self._room_jobs[work.key] = replace(work, due_at=now)

    def _restore_failed_lease(self, lease: _RoomLease) -> None:
        if self._stopped:
            return
        owners = self._current_owners()
        for work in lease.jobs:
            if self._room_jobs.get(work.key) is not work:
                continue
            owner = owners.get(work.owner_user_id)
            if owner is None or owner.generation is not work.generation or not owner.first_sync_complete:
                self._retry_readiness(work)
            else:
                self._retry_matrix(work, work.targets)

    def _retry_readiness(self, work: _OwnerRoomWork) -> None:
        if work.readiness_probe >= _MAX_READINESS_PROBES:
            due_at: float | None = None
            logger.warning(
                "Restart recovery owner unavailable; parking retained owner work",
                owner_user_id=work.owner_user_id,
                probe=work.readiness_probe,
                room_id=work.room_id,
            )
        else:
            probe = work.readiness_probe + 1
            due_at = asyncio.get_running_loop().time() + self._retry_delay(probe)
            work = replace(work, readiness_probe=probe)
        self._room_jobs[work.key] = replace(work, due_at=due_at)

    def _retry_membership(self, work: _OwnerRoomWork) -> None:
        """Refresh one missing membership once, then wait for owner readiness."""
        if work.membership_probe == 0:
            work = replace(work, membership_probe=1)
            due_at = asyncio.get_running_loop().time() + self._operations.membership_refresh_delay_seconds
        else:
            due_at = None
        self._room_jobs[work.key] = replace(work, due_at=due_at)

    def _retry_matrix(
        self,
        work: _OwnerRoomWork,
        retry_targets: tuple[InterruptedThread, ...],
    ) -> None:
        attempt = work.matrix_attempt + 1
        if attempt >= _MAX_MATRIX_ATTEMPTS:
            self._room_jobs[work.key] = replace(
                work,
                targets=(),
                matrix_attempt=attempt,
                readiness_probe=0,
                membership_probe=0,
                due_at=None,
            )
            logger.warning(
                "Restart recovery exhausted retries; parking retained owner work",
                attempt=attempt,
                owner_user_id=work.owner_user_id,
                room_id=work.room_id,
                target_event_ids=sorted(target.target_event_id for target in retry_targets),
            )
            return
        self._room_jobs[work.key] = replace(
            work,
            targets=_newest_targets(retry_targets),
            matrix_attempt=attempt,
            readiness_probe=0,
            membership_probe=0,
            due_at=asyncio.get_running_loop().time() + self._retry_delay(attempt),
        )

    def _advance_watermark(self, owner: RecoveryOwner, settlement: _TargetSettlement) -> None:
        target = settlement.target
        assert target.thread_id is not None
        key = owner.user_id, target.room_id, target.thread_id
        version = _target_version(target)
        current = self._target_watermarks.get(key)
        if current is None or current.generation is not owner.generation or version > current.version:
            self._target_watermarks[key] = _TargetWatermark(owner.generation, version, settlement.closed)
        elif version == current.version and settlement.closed and not current.closed:
            self._target_watermarks[key] = replace(current, closed=True)

    async def _drain_active_attempts(self) -> None:
        # The lease drain must survive repeated cancellation while an admitted
        # delivery independently drains its already-started Matrix side effect.
        tasks = tuple(self._active_attempts)
        await _cancel_and_drain_tasks(tasks)
        self._settle_finished_attempts()

    async def _process_room(self, lease: _RoomLease) -> tuple[_OwnerAttemptResult, ...]:
        config = self._require_config()
        owners = dict(self._current_owners())
        ready, outcomes = self._partition_ready_jobs(lease, owners)
        if not ready:
            return tuple(outcomes.values())

        async with self._matrix_read_slots:
            recovered = await self._operations.recover_room(
                tuple(ready[owner_user_id] for owner_user_id in sorted(ready)),
                lease.request,
                frozenset(owners),
                config,
            )
        recovered_by_owner: dict[str, list[InterruptedThread]] = {owner_user_id: [] for owner_user_id in ready}
        owners_by_entity = {owner.entity_name: owner for owner in ready.values()}
        for target in recovered.interrupted_threads:
            owner = owners_by_entity[target.agent_name]
            if target.original_sender_id is None and owner.user_id in recovered.retry_owner_user_ids:
                continue
            recovered_by_owner[owner.user_id].append(target)

        for work in lease.jobs:
            owner = ready.get(work.owner_user_id)
            if owner is None:
                continue
            targets = _newest_targets(
                (*work.targets, *recovered_by_owner[work.owner_user_id]),
            )
            if (task := asyncio.current_task()) is not None and task.cancelling():
                outcomes[work.owner_user_id] = _OwnerAttemptResult(
                    work=work,
                    owner=owner,
                    retry_targets=targets,
                    cancelled=True,
                )
                continue
            if owner.user_id in recovered.unjoined_owner_user_ids:
                outcomes[work.owner_user_id] = _OwnerAttemptResult(
                    work=work,
                    owner=owner,
                    retry_targets=targets,
                    membership_unavailable=True,
                )
                continue
            outcome = await self._process_owner_targets(
                work,
                owner,
                targets,
                config,
                scan_failed=owner.user_id in recovered.retry_owner_user_ids,
            )
            outcomes[work.owner_user_id] = outcome
        return tuple(outcomes[job.owner_user_id] for job in lease.jobs)

    @staticmethod
    def _partition_ready_jobs(
        lease: _RoomLease,
        owners: Mapping[str, RecoveryOwner],
    ) -> tuple[dict[str, RecoveryOwner], dict[str, _OwnerAttemptResult]]:
        """Split one lease into exact ready owners and retained readiness waits."""
        ready: dict[str, RecoveryOwner] = {}
        outcomes: dict[str, _OwnerAttemptResult] = {}
        for work in lease.jobs:
            owner = owners.get(work.owner_user_id)
            if owner is None or owner.generation is not work.generation or not owner.first_sync_complete:
                outcomes[work.owner_user_id] = _OwnerAttemptResult(
                    work=work,
                    owner=owner,
                    retry_targets=work.targets,
                    readiness_unavailable=True,
                )
            else:
                ready[work.owner_user_id] = owner
        return ready, outcomes

    async def _process_owner_targets(
        self,
        work: _OwnerRoomWork,
        owner: RecoveryOwner,
        targets: tuple[InterruptedThread, ...],
        config: Config,
        *,
        scan_failed: bool,
    ) -> _OwnerAttemptResult:
        retry_targets: list[InterruptedThread] = []
        settlements: list[_TargetSettlement] = []
        cancelled = False
        eligible_targets = self._eligible_targets(owner, targets)
        for index, target in enumerate(eligible_targets):
            try:
                attempt = await self._process_target(owner, target, config)
            except asyncio.CancelledError:
                retry_targets.extend(eligible_targets[index:])
                cancelled = True
                break
            if attempt is None:
                retry_targets.append(target)
            else:
                settlements.append(attempt)
            if (task := asyncio.current_task()) is not None and task.cancelling():
                retry_targets.extend(eligible_targets[index + 1 :])
                cancelled = True
                break
        return _OwnerAttemptResult(
            work=work,
            owner=owner,
            retry_targets=tuple(retry_targets),
            settlements=tuple(settlements),
            matrix_failed=scan_failed or bool(retry_targets),
            cancelled=cancelled,
        )

    def _eligible_targets(
        self,
        owner: RecoveryOwner,
        targets: tuple[InterruptedThread, ...],
    ) -> tuple[InterruptedThread, ...]:
        eligible: list[InterruptedThread] = []
        for target in _newest_targets(targets):
            assert target.thread_id is not None
            watermark = self._target_watermarks.get((owner.user_id, target.room_id, target.thread_id))
            if (
                watermark is None
                or watermark.generation is not owner.generation
                or (not watermark.closed and _target_version(target) > watermark.version)
            ):
                eligible.append(target)
        return tuple(eligible)

    async def _process_target(
        self,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> _TargetSettlement | None:
        if target.original_sender_id is None or not config.defaults.auto_resume_after_restart:
            return _TargetSettlement(target, closed=False)
        async with self._matrix_read_slots:
            freshness = await self._operations.target_freshness(owner, target, config)
        if freshness is InterruptedTargetFreshness.RETRY:
            return None
        if freshness in {
            InterruptedTargetFreshness.NEWER_HUMAN,
            InterruptedTargetFreshness.UNRECOVERABLE,
        }:
            return _TargetSettlement(target, closed=False)
        delivery = await self._deliver_target(owner, target, config)
        if delivery is RestartDeliveryOutcome.RETRY:
            return None
        return _TargetSettlement(
            target,
            closed=delivery is RestartDeliveryOutcome.DELIVERED,
        )

    async def _deliver_target(
        self,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> RestartDeliveryOutcome:
        """Drain one exact delivery through repeated coordinator cancellation."""
        async with self._delivery_lock:
            # Matrix may commit before cancellation is observable.
            # Drain the exact outcome so pause can settle its watermark; the
            # deterministic transaction ID is a replay guard, not settlement.
            task = asyncio.create_task(
                self._operations.deliver_target(owner, target, config),
                name=f"restart_recovery_delivery:{target.target_event_id}",
            )
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                return task.result()
