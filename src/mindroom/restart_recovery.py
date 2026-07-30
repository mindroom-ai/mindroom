"""Serialized restart recovery with semantic retry state."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.invited_rooms_store import (
    invited_rooms_path,
    load_invited_rooms,
    should_persist_invited_rooms,
)
from mindroom.matrix.stale_stream_cleanup import (
    InterruptedTargetFreshness,
    StaleStreamCleanupActor,
    build_auto_resume_content,
    cleanup_stale_streaming_room,
    interrupted_target_freshness,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import nio

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.matrix.stale_stream_cleanup import InterruptedThread

logger = get_logger(__name__)

type _TargetKey = tuple[str, str, str]
type _RoomKey = tuple[str, str, bool]


@dataclass(frozen=True)
class RecoveryOwner:
    """One current bot generation available for restart recovery."""

    entity_name: str
    user_id: str
    generation: object
    client: nio.AsyncClient
    conversation_cache: ConversationCacheProtocol
    desired_room_ids: frozenset[str]
    first_sync_complete: bool


def build_restart_recovery_owners(
    bots: Mapping[str, AgentBot | TeamBot],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, RecoveryOwner]:
    """Snapshot current exact owner generations and their durable room scope."""
    owners: dict[str, RecoveryOwner] = {}
    for bot in bots.values():
        client = bot.client
        user_id = bot.agent_user.user_id
        if client is None or not user_id:
            continue
        desired_room_ids = set(bot.rooms)
        if should_persist_invited_rooms(config, bot.agent_name):
            desired_room_ids.update(
                load_invited_rooms(
                    invited_rooms_path(runtime_paths.storage_root, bot.agent_name),
                ),
            )
        owners[user_id] = RecoveryOwner(
            entity_name=bot.agent_name,
            user_id=user_id,
            generation=bot,
            client=client,
            conversation_cache=bot._conversation_cache,
            desired_room_ids=frozenset(desired_room_ids),
            first_sync_complete=bot.running and bot.first_sync_complete,
        )
    return owners


@dataclass(frozen=True)
class _RoomRecoveryRequest:
    """One semantic owner-room recovery request."""

    room_id: str
    startup_cutoff_ms: int | None
    terminal_interrupted_only: bool


@dataclass(frozen=True)
class _RoomRecoveryResult:
    """Result of one owner-room recovery attempt."""

    interrupted_threads: tuple[InterruptedThread, ...] = ()
    retry: bool = False


class _RestartTargetFreshness(Enum):
    """Authoritative freshness state for one interrupted target."""

    CURRENT = auto()
    NEWER_HUMAN = auto()
    RETRY = auto()


class _RecoverRoom(Protocol):
    async def __call__(
        self,
        owner: RecoveryOwner,
        request: _RoomRecoveryRequest,
        owner_user_ids: frozenset[str],
        config: Config,
    ) -> _RoomRecoveryResult:
        """Recover one room through its exact current owner."""
        ...


class _TargetFreshness(Protocol):
    async def __call__(
        self,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> _RestartTargetFreshness:
        """Classify one target through its exact current owner."""
        ...


class _DeliverTarget(Protocol):
    async def __call__(
        self,
        router: RecoveryOwner,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> bool:
        """Deliver one target through the current router."""
        ...


@dataclass(frozen=True)
class _RestartRecoveryOperations:
    """External operations used by the serialized coordinator."""

    recover_room: _RecoverRoom
    target_freshness: _TargetFreshness
    deliver_target: _DeliverTarget


def build_matrix_restart_recovery_operations(runtime_paths: RuntimePaths) -> _RestartRecoveryOperations:
    """Build exact-owner Matrix operations for restart recovery."""

    async def recover_room(
        owner: RecoveryOwner,
        request: _RoomRecoveryRequest,
        owner_user_ids: frozenset[str],
        config: Config,
    ) -> _RoomRecoveryResult:
        try:
            joined_room_ids = await get_joined_rooms(owner.client)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Failed to list owner rooms during restart recovery",
                owner_user_id=owner.user_id,
                exc_info=True,
            )
            return _RoomRecoveryResult(retry=True)
        if joined_room_ids is None or request.room_id not in joined_room_ids:
            return _RoomRecoveryResult(retry=True)

        _cleaned_count, interrupted_threads = await cleanup_stale_streaming_room(
            owner.client,
            room_id=request.room_id,
            actors={
                owner.user_id: StaleStreamCleanupActor(
                    client=owner.client,
                    conversation_cache=owner.conversation_cache,
                ),
            },
            bot_user_ids=set(owner_user_ids),
            config=config,
            runtime_paths=runtime_paths,
            startup_cutoff_ms=request.startup_cutoff_ms,
            terminal_interrupted_only=request.terminal_interrupted_only,
        )
        return _RoomRecoveryResult(interrupted_threads=tuple(interrupted_threads))

    async def target_freshness(
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> _RestartTargetFreshness:
        freshness = await interrupted_target_freshness(
            target,
            config=config,
            runtime_paths=runtime_paths,
            conversation_cache=owner.conversation_cache,
        )
        return {
            InterruptedTargetFreshness.CURRENT: _RestartTargetFreshness.CURRENT,
            InterruptedTargetFreshness.NEWER_HUMAN: _RestartTargetFreshness.NEWER_HUMAN,
            InterruptedTargetFreshness.RETRY: _RestartTargetFreshness.RETRY,
        }[freshness]

    async def deliver_target(
        router: RecoveryOwner,
        owner: RecoveryOwner,
        target: InterruptedThread,
        config: Config,
    ) -> bool:
        content = build_auto_resume_content(
            target,
            config=config,
            runtime_paths=runtime_paths,
            target_user_id=owner.user_id,
            sender_is_owner=router.user_id == owner.user_id,
        )
        delivered = await send_message_result(router.client, target.room_id, content)
        if delivered is None:
            return False
        try:
            router.conversation_cache.notify_outbound_message(
                target.room_id,
                delivered.event_id,
                delivered.content_sent,
            )
        except Exception:
            logger.warning(
                "Failed to record queued auto-resume message",
                room_id=target.room_id,
                event_id=delivered.event_id,
                exc_info=True,
            )
        logger.info(
            "Queued auto-resume after restart",
            room_id=target.room_id,
            thread_id=target.thread_id,
            target_event_id=target.target_event_id,
            event_id=delivered.event_id,
        )
        return True

    return _RestartRecoveryOperations(
        recover_room=recover_room,
        target_freshness=target_freshness,
        deliver_target=deliver_target,
    )


@dataclass(frozen=True)
class _RoomJob:
    owner_user_id: str
    request: _RoomRecoveryRequest
    attempt: int
    due_at: float

    @property
    def key(self) -> _RoomKey:
        return (
            self.owner_user_id,
            self.request.room_id,
            self.request.terminal_interrupted_only,
        )


@dataclass(frozen=True)
class _TargetJob:
    owner_user_id: str
    target: InterruptedThread
    attempt: int
    due_at: float

    @property
    def key(self) -> _TargetKey:
        assert self.target.thread_id is not None
        return self.owner_user_id, self.target.room_id, self.target.thread_id

    @property
    def version(self) -> tuple[int, str]:
        return self.target.timestamp_ms, self.target.target_event_id


type _RecoveryJob = _RoomJob | _TargetJob


def _restart_recovery_retry_delay(attempt: int) -> float:
    """Return capped exponential delay for a one-based retry attempt."""
    return min(60.0, 2.0 ** max(1, attempt))


class RestartRecoveryCoordinator:
    """Own one worker and all semantic restart-recovery work."""

    def __init__(
        self,
        *,
        current_config: Callable[[], Config | None],
        current_owners: Callable[[], Mapping[str, RecoveryOwner]],
        operations: _RestartRecoveryOperations,
        retry_delay: Callable[[int], float] = _restart_recovery_retry_delay,
    ) -> None:
        self._current_config = current_config
        self._current_owners = current_owners
        self._operations = operations
        self._retry_delay = retry_delay
        self._room_jobs: dict[_RoomKey, _RoomJob] = {}
        self._target_jobs: dict[_TargetKey, _TargetJob] = {}
        self._latest_target_versions: dict[_TargetKey, tuple[int, str]] = {}
        self._settled_target_versions: dict[_TargetKey, tuple[int, str]] = {}
        self._active_job: _RecoveryJob | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._startup_cutoff_ms: int | None = None
        self._paused = True
        self._stopped = False

    def start(self, *, startup_cutoff_ms: int) -> None:
        """Start one worker and enqueue current owners' desired rooms."""
        self._startup_cutoff_ms = startup_cutoff_ms
        self._stopped = False
        self._paused = False
        for owner in self._current_owners().values():
            self._enqueue_desired_rooms(owner)
        self._ensure_worker()

    def owner_ready(self, owner_user_id: str) -> None:
        """Wake recovery for one current ready owner generation."""
        owner = self._current_owners().get(owner_user_id)
        if owner is not None:
            self._enqueue_desired_rooms(owner)
        self._expedite_owner_jobs(owner_user_id)
        self._wake.set()

    def enqueue_replacement_rooms(self, owner_user_id: str, room_ids: set[str]) -> None:
        """Retain terminal interrupted-room handoffs across bot replacement."""
        for room_id in room_ids:
            self._enqueue_room(
                owner_user_id,
                _RoomRecoveryRequest(
                    room_id=room_id,
                    startup_cutoff_ms=None,
                    terminal_interrupted_only=True,
                ),
            )

    def discard_owner(self, owner_user_id: str) -> None:
        """Discard work for an entity removed from runtime configuration."""
        self._room_jobs = {key: job for key, job in self._room_jobs.items() if job.owner_user_id != owner_user_id}
        self._target_jobs = {key: job for key, job in self._target_jobs.items() if job.owner_user_id != owner_user_id}
        self._latest_target_versions = {
            key: version for key, version in self._latest_target_versions.items() if key[0] != owner_user_id
        }
        self._settled_target_versions = {
            key: version for key, version in self._settled_target_versions.items() if key[0] != owner_user_id
        }

    async def pause(self) -> None:
        """Pause before config mutation and requeue any active lease."""
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
        """Stop the sole worker."""
        self._stopped = True
        self._paused = True
        self._wake.set()
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _enqueue_desired_rooms(self, owner: RecoveryOwner) -> None:
        if self._startup_cutoff_ms is None:
            return
        for room_id in owner.desired_room_ids:
            self._enqueue_room(
                owner.user_id,
                _RoomRecoveryRequest(
                    room_id=room_id,
                    startup_cutoff_ms=self._startup_cutoff_ms,
                    terminal_interrupted_only=False,
                ),
            )

    def _enqueue_room(self, owner_user_id: str, request: _RoomRecoveryRequest) -> None:
        key = (
            owner_user_id,
            request.room_id,
            request.terminal_interrupted_only,
        )
        if key not in self._room_jobs and not self._active_job_has_key(key):
            self._room_jobs[key] = _RoomJob(
                owner_user_id=owner_user_id,
                request=request,
                attempt=0,
                due_at=asyncio.get_running_loop().time(),
            )
        self._wake.set()

    def _expedite_owner_jobs(self, owner_user_id: str) -> None:
        due_at = asyncio.get_running_loop().time()
        self._room_jobs = {
            key: replace(job, due_at=due_at) if job.owner_user_id == owner_user_id else job
            for key, job in self._room_jobs.items()
        }
        self._target_jobs = {
            key: replace(job, due_at=due_at) if job.owner_user_id == owner_user_id else job
            for key, job in self._target_jobs.items()
        }

    def _active_job_has_key(self, key: _RoomKey) -> bool:
        active_job = self._active_job
        return isinstance(active_job, _RoomJob) and active_job.key == key

    def _enqueue_target(self, owner_user_id: str, target: InterruptedThread) -> None:
        if target.thread_id is None:
            return
        key = (owner_user_id, target.room_id, target.thread_id)
        version = (target.timestamp_ms, target.target_event_id)
        if version <= self._settled_target_versions.get(key, (-1, "")):
            return
        if version <= self._latest_target_versions.get(key, (-1, "")):
            return
        self._latest_target_versions[key] = version
        self._target_jobs[key] = _TargetJob(
            owner_user_id=owner_user_id,
            target=target,
            attempt=0,
            due_at=asyncio.get_running_loop().time(),
        )
        self._wake.set()

    def _ensure_worker(self) -> None:
        if self._paused or self._stopped:
            return
        task = self._worker_task
        if task is not None and not task.done():
            self._wake.set()
            return
        self._worker_task = asyncio.create_task(
            self._run(),
            name="restart_recovery",
        )

    async def _run(self) -> None:
        try:
            while not self._paused and not self._stopped:
                job = self._next_due_job()
                if job is None:
                    await self._wait_for_work()
                    continue
                now = asyncio.get_running_loop().time()
                if job.due_at > now:
                    await self._wait_for_work(delay=job.due_at - now)
                    continue
                self._claim(job)
                retry = True
                try:
                    retry = await self._process(job)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Restart recovery attempt failed", exc_info=True)
                else:
                    if not retry and isinstance(job, _TargetJob):
                        self._settled_target_versions[job.key] = job.version
                finally:
                    if retry:
                        self._retry_or_restore(job, cancelled=self._paused or self._stopped)
                    self._active_job = None
        finally:
            active_job = self._active_job
            if active_job is not None and not self._stopped:
                self._restore(active_job)
                self._active_job = None

    def _next_due_job(self) -> _RecoveryJob | None:
        jobs: list[_RecoveryJob] = [*self._room_jobs.values(), *self._target_jobs.values()]
        if not jobs:
            return None
        return min(
            jobs,
            key=lambda job: (
                job.due_at,
                0 if isinstance(job, _RoomJob) else 1,
                job.key,
            ),
        )

    async def _wait_for_work(self, *, delay: float | None = None) -> None:
        self._wake.clear()
        if delay is None:
            await self._wake.wait()
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=delay)

    def _claim(self, job: _RecoveryJob) -> None:
        if isinstance(job, _RoomJob):
            self._room_jobs.pop(job.key, None)
        else:
            self._target_jobs.pop(job.key, None)
        self._active_job = job

    async def _process(self, job: _RecoveryJob) -> bool:
        if isinstance(job, _RoomJob):
            return await self._process_room(job)
        return await self._process_target(job)

    async def _process_room(self, job: _RoomJob) -> bool:
        config = self._current_config()
        owner = self._current_owners().get(job.owner_user_id)
        if config is None or owner is None or not owner.first_sync_complete:
            return True
        owner_user_ids = frozenset(self._current_owners())
        result = await self._operations.recover_room(
            owner,
            job.request,
            owner_user_ids,
            config,
        )
        if not self._owner_is_current(owner):
            return True
        if config.defaults.auto_resume_after_restart:
            for interrupted_thread in result.interrupted_threads:
                self._enqueue_target(owner.user_id, interrupted_thread)
        return result.retry

    async def _process_target(self, job: _TargetJob) -> bool:
        config = self._current_config()
        owner = self._current_owners().get(job.owner_user_id)
        if config is None or owner is None or not owner.first_sync_complete:
            return True
        if job.target.original_sender_id is None or not config.defaults.auto_resume_after_restart:
            return False
        freshness = await self._operations.target_freshness(owner, job.target, config)
        if not self._owner_is_current(owner):
            return True
        if freshness is _RestartTargetFreshness.RETRY:
            return True
        if freshness is _RestartTargetFreshness.NEWER_HUMAN:
            return False
        router = next(
            (
                candidate
                for candidate in self._current_owners().values()
                if candidate.entity_name == ROUTER_AGENT_NAME and candidate.first_sync_complete
            ),
            None,
        )
        return router is None or not await self._operations.deliver_target(router, owner, job.target, config)

    def _owner_is_current(self, owner: RecoveryOwner) -> bool:
        current = self._current_owners().get(owner.user_id)
        return current is not None and current.generation is owner.generation

    def _retry_or_restore(self, job: _RecoveryJob, *, cancelled: bool) -> None:
        if self._stopped:
            return
        if cancelled:
            self._restore(job)
            return
        attempt = job.attempt + 1
        retried = replace(
            job,
            attempt=attempt,
            due_at=asyncio.get_running_loop().time() + self._retry_delay(attempt),
        )
        self._restore(retried)

    def _restore(self, job: _RecoveryJob) -> None:
        if isinstance(job, _RoomJob):
            self._room_jobs.setdefault(job.key, job)
            return
        existing = self._target_jobs.get(job.key)
        if existing is None or job.version > existing.version:
            self._target_jobs[job.key] = job
