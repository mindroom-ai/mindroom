"""Serialized restart recovery with semantic retry state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.restart_recovery_operations import (
    RecoveryOwner,
    RestartRecoveryOperations,
    RestartTargetFreshness,
    RoomRecoveryRequest,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.config.main import Config
    from mindroom.matrix.stale_stream_cleanup import InterruptedThread

logger = get_logger(__name__)

type _TargetKey = tuple[str, str, str]
type _RoomKey = tuple[str, int | None, bool]


_MAX_CONCURRENT_ROOMS = 8


@dataclass(frozen=True)
class _OwnedTarget:
    """One interrupted target bound to its exact recovery owner."""

    owner_user_id: str
    target: InterruptedThread


@dataclass(frozen=True)
class _RoomWork:
    """All retained recovery work for one room scan scope."""

    room_id: str
    startup_cutoff_ms: int | None
    terminal_interrupted_only: bool
    owner_user_ids: frozenset[str] = frozenset()
    targets: tuple[_OwnedTarget, ...] = ()
    attempt: int = 0
    due_at: float = 0.0

    @property
    def key(self) -> _RoomKey:
        return (
            self.room_id,
            self.startup_cutoff_ms,
            self.terminal_interrupted_only,
        )

    @property
    def request(self) -> RoomRecoveryRequest:
        return RoomRecoveryRequest(
            room_id=self.room_id,
            startup_cutoff_ms=self.startup_cutoff_ms,
            terminal_interrupted_only=self.terminal_interrupted_only,
        )

    @property
    def all_owner_user_ids(self) -> frozenset[str]:
        return self.owner_user_ids | frozenset(target.owner_user_id for target in self.targets)


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
    owner_user_id: str


@dataclass(frozen=True)
class _RoomAttemptResult:
    owners: Mapping[str, RecoveryOwner]
    retry_owner_user_ids: frozenset[str]
    retry_targets: tuple[_OwnedTarget, ...]
    settlements: tuple[_TargetSettlement, ...]


def _restart_recovery_retry_delay(attempt: int) -> float:
    """Return capped exponential delay for a one-based retry attempt."""
    return min(60.0, 2.0 ** max(1, attempt))


def _target_version(target: InterruptedThread) -> tuple[int, str]:
    return target.timestamp_ms, target.target_event_id


def _newest_targets(targets: tuple[_OwnedTarget, ...]) -> tuple[_OwnedTarget, ...]:
    newest: dict[tuple[str, str], _OwnedTarget] = {}
    for owned_target in targets:
        target = owned_target.target
        if target.thread_id is None:
            continue
        key = owned_target.owner_user_id, target.thread_id
        current = newest.get(key)
        if current is None or _target_version(target) > _target_version(current.target):
            newest[key] = owned_target
    return tuple(
        sorted(
            newest.values(),
            key=lambda owned_target: (
                owned_target.owner_user_id,
                owned_target.target.thread_id or "",
                _target_version(owned_target.target),
            ),
        ),
    )


def _merge_work(left: _RoomWork, right: _RoomWork) -> _RoomWork:
    assert left.key == right.key
    return _RoomWork(
        room_id=left.room_id,
        startup_cutoff_ms=left.startup_cutoff_ms,
        terminal_interrupted_only=left.terminal_interrupted_only,
        owner_user_ids=left.owner_user_ids | right.owner_user_ids,
        targets=_newest_targets((*left.targets, *right.targets)),
        attempt=min(left.attempt, right.attempt),
        due_at=min(left.due_at, right.due_at),
    )


class RestartRecoveryCoordinator:
    """Own bounded room recovery leases and monotonic owner-target watermarks."""

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
        self._room_jobs: dict[_RoomKey, _RoomWork] = {}
        self._target_watermarks: dict[_TargetKey, _TargetWatermark] = {}
        self._completed_scan_generations: dict[tuple[_RoomKey, str], object] = {}
        self._ready_generations: dict[str, object] = {}
        self._active_attempts: dict[asyncio.Task[_RoomAttemptResult], _RoomWork] = {}
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
        self._ensure_worker()

    def owner_ready(self, owner_user_id: str) -> None:
        """Refresh desired rooms and wake retained work for one owner."""
        self._settle_finished_attempts()
        owner = self._current_owners().get(owner_user_id)
        if owner is not None:
            self._ready_generations[owner_user_id] = owner.generation
            self._target_watermarks = {
                key: watermark
                for key, watermark in self._target_watermarks.items()
                if key[0] != owner_user_id or watermark.generation is owner.generation
            }
            self._enqueue_desired_rooms(owner)
        due_at = asyncio.get_running_loop().time()
        self._room_jobs = {
            key: replace(work, due_at=due_at) if owner_user_id in work.all_owner_user_ids else work
            for key, work in self._room_jobs.items()
        }
        self._wake.set()

    def enqueue_replacement_rooms(self, owner_user_id: str, room_ids: set[str]) -> None:
        """Retain terminal interrupted-room handoffs across bot replacement."""
        for room_id in room_ids:
            self._enqueue_request(
                owner_user_id,
                RoomRecoveryRequest(
                    room_id=room_id,
                    startup_cutoff_ms=None,
                    terminal_interrupted_only=True,
                ),
            )

    def discard_owner(self, owner_user_id: str) -> None:
        """Discard one removed owner's work, watermark, and membership snapshot."""
        retained_jobs: dict[_RoomKey, _RoomWork] = {}
        for key, work in self._room_jobs.items():
            retained = replace(
                work,
                owner_user_ids=work.owner_user_ids - {owner_user_id},
                targets=tuple(target for target in work.targets if target.owner_user_id != owner_user_id),
            )
            if retained.owner_user_ids or retained.targets:
                retained_jobs[key] = retained
        self._room_jobs = retained_jobs
        self._target_watermarks = {
            key: watermark for key, watermark in self._target_watermarks.items() if key[0] != owner_user_id
        }
        self._completed_scan_generations = {
            key: generation for key, generation in self._completed_scan_generations.items() if key[1] != owner_user_id
        }
        self._ready_generations.pop(owner_user_id, None)
        self._operations.discard_owner(owner_user_id)

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
        """Resume retained work against current owner generations."""
        if self._stopped:
            return
        self._paused = False
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
        self._completed_scan_generations.clear()
        self._ready_generations.clear()
        await self._operations.close()

    def _enqueue_desired_rooms(self, owner: RecoveryOwner) -> None:
        if self._startup_cutoff_ms is None:
            return
        for room_id in owner.desired_room_ids:
            request = RoomRecoveryRequest(
                room_id=room_id,
                startup_cutoff_ms=self._startup_cutoff_ms,
                terminal_interrupted_only=False,
            )
            key = (
                request.room_id,
                request.startup_cutoff_ms,
                request.terminal_interrupted_only,
            )
            if self._completed_scan_generations.get(
                (key, owner.user_id),
            ) is owner.generation or self._owner_has_pending_work(key, owner.user_id):
                continue
            self._enqueue_request(owner.user_id, request)

    def _owner_has_pending_work(
        self,
        key: _RoomKey,
        owner_user_id: str,
    ) -> bool:
        queued = self._room_jobs.get(key)
        if queued is not None and owner_user_id in queued.all_owner_user_ids:
            return True
        return any(
            work.key == key and owner_user_id in work.all_owner_user_ids for work in self._active_attempts.values()
        )

    def _enqueue_request(self, owner_user_id: str, request: RoomRecoveryRequest) -> None:
        if not request.room_id.startswith("!"):
            return
        work = _RoomWork(
            room_id=request.room_id,
            startup_cutoff_ms=request.startup_cutoff_ms,
            terminal_interrupted_only=request.terminal_interrupted_only,
            owner_user_ids=frozenset({owner_user_id}),
            due_at=asyncio.get_running_loop().time(),
        )
        self._queue(work)
        self._wake.set()

    def _queue(self, work: _RoomWork) -> None:
        existing = self._room_jobs.get(work.key)
        self._room_jobs[work.key] = work if existing is None else _merge_work(existing, work)

    def _ensure_worker(self) -> None:
        if self._paused or self._stopped:
            return
        task = self._worker_task
        if task is not None and not task.done():
            self._wake.set()
            return
        self._worker_task = asyncio.create_task(self._run(), name="restart_recovery")

    async def _run(self) -> None:
        try:
            while not self._paused and not self._stopped:
                self._settle_finished_attempts()
                self._start_due_attempts()
                await self._wait_for_progress(delay=self._next_start_delay())
        finally:
            await self._drain_active_attempts()

    def _eligible_work(self) -> list[_RoomWork]:
        active_room_ids = {work.room_id for work in self._active_attempts.values()}
        return [work for work in self._room_jobs.values() if work.room_id not in active_room_ids]

    def _next_due_work(self, now: float) -> _RoomWork | None:
        eligible = [work for work in self._eligible_work() if work.due_at <= now]
        if not eligible:
            return None
        active_owners = set().union(
            *(work.all_owner_user_ids for work in self._active_attempts.values()),
        )
        return min(
            eligible,
            key=lambda work: (
                work.due_at,
                not work.all_owner_user_ids.isdisjoint(active_owners),
                work.room_id,
                work.startup_cutoff_ms or -1,
                work.terminal_interrupted_only,
            ),
        )

    def _start_due_attempts(self) -> None:
        now = asyncio.get_running_loop().time()
        while len(self._active_attempts) < _MAX_CONCURRENT_ROOMS:
            work = self._next_due_work(now)
            if work is None:
                return
            self._room_jobs.pop(work.key)
            task = asyncio.create_task(
                self._process_room(work),
                name=f"restart_recovery_room:{work.key}",
            )
            self._active_attempts[task] = work

    def _next_start_delay(self) -> float | None:
        if len(self._active_attempts) >= _MAX_CONCURRENT_ROOMS:
            return None
        eligible = self._eligible_work()
        if not eligible:
            return None
        due_at = min(work.due_at for work in eligible)
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
        for task, work in tuple(self._active_attempts.items()):
            if not task.done():
                continue
            self._active_attempts.pop(task)
            self._settle_attempt(work, task)

    def _settle_attempt(
        self,
        work: _RoomWork,
        task: asyncio.Task[_RoomAttemptResult],
    ) -> None:
        try:
            result = task.result()
        except asyncio.CancelledError:
            self._restore(work, cancelled=True)
            return
        except Exception:
            logger.warning("Restart recovery attempt failed", exc_info=True)
            self._restore(work, cancelled=self._paused or self._stopped)
            return
        for settlement in result.settlements:
            owner = result.owners.get(settlement.owner_user_id)
            if owner is not None:
                self._advance_watermark(owner, settlement)
        completed_owner_user_ids = work.owner_user_ids - result.retry_owner_user_ids
        for owner_user_id in completed_owner_user_ids:
            owner = result.owners.get(owner_user_id)
            if owner is not None:
                self._completed_scan_generations[(work.key, owner_user_id)] = owner.generation
        remaining = replace(
            work,
            owner_user_ids=result.retry_owner_user_ids,
            targets=result.retry_targets,
        )
        if remaining.owner_user_ids or remaining.targets:
            became_ready = any(
                (owner := result.owners.get(owner_user_id)) is not None
                and not owner.first_sync_complete
                and self._ready_generations.get(owner_user_id) is owner.generation
                for owner_user_id in result.retry_owner_user_ids
            )
            self._restore(
                remaining,
                cancelled=self._paused or self._stopped or became_ready,
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
        tasks = tuple(self._active_attempts)
        for task in tasks:
            task.cancel()
        if tasks:
            drain = asyncio.gather(*tasks, return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    continue
        self._settle_finished_attempts()

    async def _process_room(self, work: _RoomWork) -> _RoomAttemptResult:
        config = self._current_config()
        owners = dict(self._current_owners())
        if config is None:
            return _RoomAttemptResult(owners, work.owner_user_ids, work.targets, ())

        unavailable_owner_user_ids = {
            owner_user_id
            for owner_user_id in work.owner_user_ids
            if (owner := owners.get(owner_user_id)) is None or not owner.first_sync_complete
        }
        if unavailable_owner_user_ids:
            return _RoomAttemptResult(
                owners,
                work.owner_user_ids,
                work.targets,
                (),
            )
        retry_owner_user_ids: set[str] = set()
        scan_owners = tuple(
            owner for owner_user_id in sorted(work.owner_user_ids) if (owner := owners.get(owner_user_id)) is not None
        )
        targets = list(work.targets)
        if scan_owners:
            scan_retry_owner_user_ids, recovered_targets = await self._recover_scan(
                scan_owners,
                work,
                owners,
                config,
            )
            retry_owner_user_ids.update(scan_retry_owner_user_ids)
            targets.extend(recovered_targets)

        retry_targets: list[_OwnedTarget] = []
        settlements: list[_TargetSettlement] = []
        eligible_targets = self._eligible_targets(owners, tuple(targets))
        for index, owned_target in enumerate(eligible_targets):
            owner = owners.get(owned_target.owner_user_id)
            if owner is None:
                retry_targets.append(owned_target)
                continue
            attempt = await self._process_target(
                owner,
                owned_target.target,
                config,
                owners,
            )
            if attempt is None:
                retry_targets.append(owned_target)
            else:
                settlements.append(attempt)
            if (lease := asyncio.current_task()) is not None and lease.cancelling():
                retry_targets.extend(eligible_targets[index + 1 :])
                break
        return _RoomAttemptResult(
            owners,
            frozenset(retry_owner_user_ids),
            tuple(retry_targets),
            tuple(settlements),
        )

    async def _recover_scan(
        self,
        scan_owners: tuple[RecoveryOwner, ...],
        work: _RoomWork,
        owners: Mapping[str, RecoveryOwner],
        config: Config,
    ) -> tuple[set[str], list[_OwnedTarget]]:
        result = await self._operations.recover_room(
            scan_owners,
            work.request,
            frozenset(owners),
            config,
        )
        retry_owner_user_ids = set(result.retry_owner_user_ids)
        if result.retry:
            retry_owner_user_ids.update(owner.user_id for owner in scan_owners)
        owners_by_entity = {owner.entity_name: owner for owner in scan_owners}
        recovered_targets: list[_OwnedTarget] = []
        for target in result.interrupted_threads:
            owner = owners_by_entity.get(target.agent_name)
            if owner is None:
                msg = f"Restart target has no owner in room scan: {target.agent_name}"
                raise ValueError(msg)
            recovered_targets.append(_OwnedTarget(owner.user_id, target))
        return retry_owner_user_ids, recovered_targets

    def _eligible_targets(
        self,
        owners: Mapping[str, RecoveryOwner],
        targets: tuple[_OwnedTarget, ...],
    ) -> tuple[_OwnedTarget, ...]:
        eligible: list[_OwnedTarget] = []
        for owned_target in _newest_targets(targets):
            owner = owners.get(owned_target.owner_user_id)
            if owner is None:
                eligible.append(owned_target)
                continue
            target = owned_target.target
            assert target.thread_id is not None
            watermark = self._target_watermarks.get((owner.user_id, target.room_id, target.thread_id))
            if (
                watermark is None
                or watermark.generation is not owner.generation
                or (not watermark.closed and _target_version(target) > watermark.version)
            ):
                eligible.append(owned_target)
        return tuple(eligible)

    async def _process_target(
        self,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
        owners: Mapping[str, RecoveryOwner],
    ) -> _TargetSettlement | None:
        if target.original_sender_id is None or not config.defaults.auto_resume_after_restart:
            return _TargetSettlement(target, closed=False, owner_user_id=owner.user_id)
        freshness = await self._operations.target_freshness(owner, target, config)
        if freshness is RestartTargetFreshness.RETRY:
            return None
        if freshness in {
            RestartTargetFreshness.NEWER_HUMAN,
            RestartTargetFreshness.UNRECOVERABLE,
        }:
            return _TargetSettlement(target, closed=False, owner_user_id=owner.user_id)
        router = next(
            (
                candidate
                for candidate in owners.values()
                if candidate.entity_name == ROUTER_AGENT_NAME and candidate.first_sync_complete
            ),
            None,
        )
        if router is None:
            return None
        delivered = await self._deliver_target(router, owner, target, config)
        return _TargetSettlement(target, closed=True, owner_user_id=owner.user_id) if delivered else None

    async def _deliver_target(
        self,
        router: RecoveryOwner,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> bool:
        """Drain one exact delivery through repeated coordinator cancellation."""
        task = asyncio.create_task(
            self._operations.deliver_target(router, owner, target, config),
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

    def _restore(self, work: _RoomWork, *, cancelled: bool) -> None:
        if self._stopped:
            return
        if not cancelled:
            attempt = work.attempt + 1
            work = replace(
                work,
                attempt=attempt,
                due_at=asyncio.get_running_loop().time() + self._retry_delay(attempt),
            )
        self._queue(work)
