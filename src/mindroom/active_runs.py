"""Per-agent live reply runs published as room state so clients show one source of truth for Working."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import ACTIVE_RUNS_EVENT_TYPE
from mindroom.matrix.state_failures import StateFailure, state_failure

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable, Iterator

logger = get_logger(__name__)
_VERSION = 1
_TTL_MS = 180_000
_HEARTBEAT_SECONDS = 60.0
_RETRY_BACKOFFS = (2.0, 5.0)
_MAX_LISTED_BYTES = 27_000
_SWEEP_CONCURRENCY = 4
_SWEEP_READ_TIMEOUT_SECONDS = 10.0


@dataclass(eq=False)
class _ActiveRunLease:
    """In-memory handle for one tracked reply run."""

    publisher: ActiveRunPublisher
    room_id: str
    thread_id: str | None
    pauses: int = 0
    registered: bool = False


_CURRENT_LEASE: ContextVar[_ActiveRunLease | None] = ContextVar("mindroom_active_run_lease", default=None)


@contextmanager
def pause_current_active_run() -> Iterator[None]:
    """Pause the current task's active-run lease while waiting on a foreground human decision."""
    lease = _CURRENT_LEASE.get()
    if lease is None:
        yield
        return
    lease.pauses += 1
    if lease.registered:
        lease.publisher._wake(lease.room_id)
    try:
        yield
    finally:
        lease.pauses -= 1
        if lease.registered:
            lease.publisher._wake(lease.room_id)


@dataclass
class ActiveRunPublisher:
    """Serialize and coalesce each room's ``io.mindroom.active_runs`` state writes for one bot."""

    get_client: Callable[[], nio.AsyncClient | None]
    user_id: str
    heartbeat_seconds: float = _HEARTBEAT_SECONDS
    retry_backoffs: tuple[float, ...] = _RETRY_BACKOFFS
    _leases: dict[str, list[_ActiveRunLease]] = field(default_factory=dict, init=False)
    _published: dict[str, list[str | None] | None] = field(default_factory=dict, init=False)
    _dirty: dict[str, asyncio.Event] = field(default_factory=dict, init=False)
    _workers: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _refused: set[str] = field(default_factory=set, init=False)
    _swept: set[str] = field(default_factory=set, init=False)
    _sweep_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _sweep_semaphore: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(_SWEEP_CONCURRENCY),
        init=False,
    )
    _closing: bool = field(default=False, init=False)

    @asynccontextmanager
    async def track(
        self,
        room_id: str,
        thread_id: str | None,
        *,
        active: bool = True,
    ) -> AsyncIterator[_ActiveRunLease]:
        """Track a live reply run in ``room_id`` and bind its lease for foreground human-wait pauses."""
        lease = _ActiveRunLease(publisher=self, room_id=room_id, thread_id=thread_id)
        token = _CURRENT_LEASE.set(lease)
        if active:
            self._register(lease)
        try:
            yield lease
        finally:
            _CURRENT_LEASE.reset(token)
            self._unregister(lease)

    @asynccontextmanager
    async def activate(self, lease: _ActiveRunLease) -> AsyncIterator[None]:
        """Register a deferred lease once progress becomes visible and unregister it on exit."""
        self._register(lease)
        try:
            yield
        finally:
            self._unregister(lease)

    def cancel(self) -> None:
        """Synchronously stop background workers and sweeps without yielding."""
        self._closing = True
        for task in (*self._workers.values(), *self._sweep_tasks):
            task.cancel()
        self._workers.clear()
        self._dirty.clear()
        self._published.clear()
        self._swept.clear()

    def reopen(self) -> None:
        """Resume publishing after an in-process restart and require fresh room reconciliation."""
        self._closing = False
        self._published.clear()
        self._swept.clear()
        for room_id in list(self._leases):
            self._wake(room_id)

    def schedule_startup_sweep(self, room_ids: Iterable[str]) -> None:
        """Check rooms once in the background and clear any non-empty state left by an earlier process."""
        if self._closing:
            return
        pending = [
            room_id
            for room_id in dict.fromkeys(room_ids)
            if room_id not in self._swept and room_id not in self._published and room_id not in self._refused
        ]
        if not pending:
            return
        self._swept.update(pending)

        async def sweep() -> None:
            await asyncio.gather(*(self._sweep_one(room_id) for room_id in pending))

        task = asyncio.create_task(sweep(), name="active-runs-sweep", context=Context())
        self._sweep_tasks.add(task)
        task.add_done_callback(self._sweep_tasks.discard)

    async def shutdown(self, timeout_seconds: float = 1.5) -> None:
        """Cancel background workers and best-effort clear any non-empty or unacked rooms within ``timeout_seconds``."""
        self._closing = True
        tasks = [*self._workers.values(), *self._sweep_tasks]
        for task in tasks:
            task.cancel()
        if not tasks and not self._dirty_rooms_to_clear():
            return
        with suppress(TimeoutError):
            async with asyncio.timeout(timeout_seconds):
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                rooms_to_clear = self._dirty_rooms_to_clear()
                if rooms_to_clear and self.get_client() is not None:
                    await asyncio.gather(*(self._put_state(room_id, []) for room_id in rooms_to_clear))
        for room_id in self._dirty_rooms_to_clear():
            self._published.pop(room_id, None)
        self._swept.clear()

    def _dirty_rooms_to_clear(self) -> list[str]:
        return [room_id for room_id, state in self._published.items() if state != [] and room_id not in self._refused]

    def _register(self, lease: _ActiveRunLease) -> None:
        if not lease.registered:
            lease.registered = True
            self._leases.setdefault(lease.room_id, []).append(lease)
            self._wake(lease.room_id)

    def _unregister(self, lease: _ActiveRunLease) -> None:
        if not lease.registered:
            return
        lease.registered = False
        leases = self._leases.get(lease.room_id)
        if leases is not None:
            with suppress(ValueError):
                leases.remove(lease)
            if not leases:
                self._leases.pop(lease.room_id, None)
        self._wake(lease.room_id)

    def _wake(self, room_id: str) -> None:
        if self._closing:
            return
        self._dirty.setdefault(room_id, asyncio.Event()).set()
        worker = self._workers.get(room_id)
        if worker is None or worker.done():
            self._workers[room_id] = asyncio.create_task(
                self._room_worker(room_id),
                name=f"active-runs:{room_id}",
                context=Context(),
            )

    def _desired_threads(self, room_id: str) -> list[str | None]:
        threads: list[str | None] = []
        seen: set[str | None] = set()
        size = 0
        for lease in self._leases.get(room_id, ()):
            if lease.pauses > 0 or lease.thread_id in seen:
                continue
            entry_size = len(json.dumps({"thread_id": lease.thread_id}, separators=(",", ":"))) + 1
            if size + entry_size > _MAX_LISTED_BYTES:
                break
            seen.add(lease.thread_id)
            threads.append(lease.thread_id)
            size += entry_size
        return threads

    def _is_settled_idle(self, room_id: str) -> bool:
        return not self._desired_threads(room_id) and (
            room_id not in self._published or self._published.get(room_id) == []
        )

    async def _flush_room(self, room_id: str, *, force: bool, retries: int) -> tuple[float | None, int] | None:
        desired = self._desired_threads(room_id)
        if not force and room_id in self._published and self._published.get(room_id) == desired:
            return (None if room_id in self._refused else self.heartbeat_seconds, retries)
        if await self._put_state(room_id, desired):
            return (None if room_id in self._refused else self.heartbeat_seconds, 0)
        if self._closing:
            return None
        if retries < len(self.retry_backoffs):
            return (self.retry_backoffs[retries], retries + 1)
        return (self.heartbeat_seconds, 0)

    async def _room_worker(self, room_id: str) -> None:
        event = self._dirty.setdefault(room_id, asyncio.Event())
        heartbeat_due = False
        retries = 0
        try:
            while not self._closing and not self._is_settled_idle(room_id):
                event.clear()
                outcome = await self._flush_room(room_id, force=heartbeat_due, retries=retries)
                heartbeat_due = False
                if outcome is None:
                    return
                timeout, retries = outcome
                if event.is_set():
                    retries = 0
                    continue
                if self._is_settled_idle(room_id):
                    return
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                except TimeoutError:
                    heartbeat_due = True
                else:
                    retries = 0
        finally:
            if self._workers.get(room_id) is asyncio.current_task():
                self._workers.pop(room_id, None)
                if not event.is_set():
                    self._dirty.pop(room_id, None)

    async def _put_state(self, room_id: str, desired: list[str | None]) -> bool:
        client = self.get_client()
        if client is None:
            return False
        self._published[room_id] = None
        content = {
            "version": _VERSION,
            "updated_at": int(time.time() * 1000),
            "ttl_ms": _TTL_MS,
            "threads": [{"thread_id": thread_id} for thread_id in desired],
        }
        try:
            response = await client.room_put_state(
                room_id=room_id,
                event_type=ACTIVE_RUNS_EVENT_TYPE,
                content=content,
                state_key=self.user_id,
            )
        except Exception as exc:
            logger.warning("Active runs state write failed", room_id=room_id, error=f"{type(exc).__name__}: {exc}")
            return False
        if isinstance(response, nio.RoomPutStateResponse):
            self._published[room_id] = desired
            self._refused.discard(room_id)
            return True
        if isinstance(response, nio.RoomPutStateError) and state_failure(response) is StateFailure.REFUSED:
            if room_id not in self._refused:
                self._refused.add(room_id)
                logger.warning(
                    "Active runs state was refused in this room",
                    room_id=room_id,
                    status_code=response.status_code or f"HTTP {response.http_status}",
                )
            self._published[room_id] = desired
            return True
        logger.warning("Active runs state write returned an error", room_id=room_id, response=str(response))
        return False

    async def _sweep_one(self, room_id: str) -> None:
        async with self._sweep_semaphore:
            if self._closing or room_id in self._published or room_id in self._leases or room_id in self._refused:
                return
            client = self.get_client()
            if client is None:
                self._swept.discard(room_id)
                return
            try:
                async with asyncio.timeout(_SWEEP_READ_TIMEOUT_SECONDS):
                    response = await client.room_get_state_event(room_id, ACTIVE_RUNS_EVENT_TYPE, self.user_id)
            except Exception as exc:
                logger.debug("Active runs startup sweep read failed", room_id=room_id, error_type=type(exc).__name__)
                return
            if not isinstance(response, nio.RoomGetStateEventResponse):
                return
            threads = response.content.get("threads") if isinstance(response.content, dict) else None
            if not isinstance(threads, list) or not threads:
                return
            if self._closing or room_id in self._published or room_id in self._leases or room_id in self._refused:
                return
            self._published[room_id] = None
            self._wake(room_id)
