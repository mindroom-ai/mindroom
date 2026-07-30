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
type _RoomKey = tuple[str, str]


_MAX_CONCURRENT_ROOMS = 2


@dataclass(frozen=True)
class _RoomWork:
    """All retained recovery work for one owner-room lease."""

    owner_user_id: str
    room_id: str
    requests: tuple[RoomRecoveryRequest, ...] = ()
    targets: tuple[InterruptedThread, ...] = ()
    attempt: int = 0
    due_at: float = 0.0

    @property
    def key(self) -> _RoomKey:
        return self.owner_user_id, self.room_id


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
class _RoomAttemptResult:
    owner: RecoveryOwner | None
    retry_requests: tuple[RoomRecoveryRequest, ...]
    retry_targets: tuple[InterruptedThread, ...]
    settlements: tuple[_TargetSettlement, ...]


def _restart_recovery_retry_delay(attempt: int) -> float:
    """Return capped exponential delay for a one-based retry attempt."""
    return min(60.0, 2.0 ** max(1, attempt))


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
    return tuple(sorted(newest.values(), key=lambda target: (target.thread_id or "", _target_version(target))))


def _merge_work(left: _RoomWork, right: _RoomWork) -> _RoomWork:
    assert left.key == right.key
    requests = tuple(
        sorted(
            {*left.requests, *right.requests},
            key=lambda request: (
                request.terminal_interrupted_only,
                request.startup_cutoff_ms or -1,
            ),
        ),
    )
    return _RoomWork(
        owner_user_id=left.owner_user_id,
        room_id=left.room_id,
        requests=requests,
        targets=_newest_targets((*left.targets, *right.targets)),
        attempt=min(left.attempt, right.attempt),
        due_at=min(left.due_at, right.due_at),
    )


class RestartRecoveryCoordinator:
    """Own bounded owner-room recovery leases and their monotonic target watermarks."""

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
        """Wake retained work for one ready owner generation."""
        self._settle_finished_attempts()
        owner = self._current_owners().get(owner_user_id)
        if owner is not None:
            self._target_watermarks = {
                key: watermark
                for key, watermark in self._target_watermarks.items()
                if key[0] != owner_user_id or watermark.generation is owner.generation
            }
            self._enqueue_desired_rooms(owner)
        due_at = asyncio.get_running_loop().time()
        self._room_jobs = {
            key: replace(work, due_at=due_at) if work.owner_user_id == owner_user_id else work
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
        self._room_jobs = {key: work for key, work in self._room_jobs.items() if work.owner_user_id != owner_user_id}
        self._target_watermarks = {
            key: watermark for key, watermark in self._target_watermarks.items() if key[0] != owner_user_id
        }
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
        await self._operations.close()

    def _enqueue_desired_rooms(self, owner: RecoveryOwner) -> None:
        if self._startup_cutoff_ms is None:
            return
        for room_id in owner.desired_room_ids:
            self._enqueue_request(
                owner.user_id,
                RoomRecoveryRequest(
                    room_id=room_id,
                    startup_cutoff_ms=self._startup_cutoff_ms,
                    terminal_interrupted_only=False,
                ),
            )

    def _enqueue_request(self, owner_user_id: str, request: RoomRecoveryRequest) -> None:
        work = _RoomWork(
            owner_user_id=owner_user_id,
            room_id=request.room_id,
            requests=(request,),
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

    def _next_work(self) -> _RoomWork | None:
        active_keys = {work.key for work in self._active_attempts.values()}
        eligible = [work for work in self._room_jobs.values() if work.key not in active_keys]
        if not eligible:
            return None
        active_owners = {work.owner_user_id for work in self._active_attempts.values()}
        fair = [work for work in eligible if work.owner_user_id not in active_owners]
        return min(fair or eligible, key=lambda work: (work.due_at, work.key))

    def _start_due_attempts(self) -> None:
        now = asyncio.get_running_loop().time()
        while len(self._active_attempts) < _MAX_CONCURRENT_ROOMS:
            work = self._next_work()
            if work is None or work.due_at > now:
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
        work = self._next_work()
        if work is None:
            return None
        return max(0.0, work.due_at - asyncio.get_running_loop().time())

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
        if result.owner is None or not self._owner_is_current(result.owner):
            self._restore(work, cancelled=self._paused or self._stopped)
            return
        for settlement in result.settlements:
            self._advance_watermark(result.owner, settlement)
        remaining = replace(
            work,
            requests=result.retry_requests,
            targets=result.retry_targets,
        )
        if remaining.requests or remaining.targets:
            self._restore(remaining, cancelled=self._paused or self._stopped)

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
        owner = self._current_owners().get(work.owner_user_id)
        if config is None or owner is None or not owner.first_sync_complete:
            return _RoomAttemptResult(owner, work.requests, work.targets, ())

        retry_requests: list[RoomRecoveryRequest] = []
        targets = list(work.targets)
        owner_user_ids = frozenset(self._current_owners())
        for request in work.requests:
            result = await self._operations.recover_room(owner, request, owner_user_ids, config)
            if not self._owner_is_current(owner):
                return _RoomAttemptResult(owner, work.requests, work.targets, ())
            if result.retry:
                retry_requests.append(request)
            targets.extend(result.interrupted_threads)

        retry_targets: list[InterruptedThread] = []
        settlements: list[_TargetSettlement] = []
        eligible_targets = self._eligible_targets(owner, tuple(targets))
        for index, target in enumerate(eligible_targets):
            attempt = await self._process_target(owner, target, config)
            if not self._owner_is_current(owner):
                return _RoomAttemptResult(owner, work.requests, work.targets, ())
            if attempt is None:
                retry_targets.append(target)
            else:
                settlements.append(attempt)
            if (lease := asyncio.current_task()) is not None and lease.cancelling():
                retry_targets.extend(eligible_targets[index + 1 :])
                break
        return _RoomAttemptResult(owner, tuple(retry_requests), tuple(retry_targets), tuple(settlements))

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
        freshness = await self._operations.target_freshness(owner, target, config)
        if freshness is RestartTargetFreshness.RETRY:
            return None
        if freshness in {
            RestartTargetFreshness.NEWER_HUMAN,
            RestartTargetFreshness.UNRECOVERABLE,
        }:
            return _TargetSettlement(target, closed=False)
        router = next(
            (
                candidate
                for candidate in self._current_owners().values()
                if candidate.entity_name == ROUTER_AGENT_NAME and candidate.first_sync_complete
            ),
            None,
        )
        if router is None:
            return None
        delivered = await self._deliver_target(router, owner, target, config)
        return _TargetSettlement(target, closed=delivered) if delivered else None

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

    def _owner_is_current(self, owner: RecoveryOwner) -> bool:
        current = self._current_owners().get(owner.user_id)
        return current is not None and current.generation is owner.generation

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
