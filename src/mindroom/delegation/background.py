"""Durable ownership, cooperative control, and delivery arbitration for child jobs."""

from __future__ import annotations

import asyncio
import fcntl
import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

from mindroom.background_tasks import (
    run_blocking_until_complete,
    run_coroutine_until_complete,
    wait_for_future_until_complete,
)
from mindroom.delegation.control import (
    HumanMessageSignal,
    SubagentControl,
    current_human_message_signal,
    human_message_signal_context,
    subagent_control_context,
)
from mindroom.delegation.sessions import SubagentSessionError
from mindroom.delegation.state import DelegationChild
from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths

type _BackgroundStatus = Literal[
    "running",
    "paused_for_human",
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
    "denied",
    "interrupted",
]
type _OutcomeStatus = Literal["awaiting_approval", "completed", "failed", "cancelled", "denied"]
_TERMINAL = frozenset({"completed", "failed", "cancelled", "denied", "interrupted"})
_READY = _TERMINAL | {"awaiting_approval"}
_UNAVAILABLE = "Subagent job is not available in this conversation."


@dataclass
class BackgroundOutcome:
    """Native driver's outcome; approval state stays owned by the native driver."""

    status: _OutcomeStatus
    result: str | None = None
    approval_state: dict[str, Any] = field(default_factory=dict)
    live_result: object = None


@dataclass
class _BackgroundDelivery:
    """Immutable send identity and content retained across ambiguous send failures."""

    job_id: str
    generation: int
    content: dict[str, Any]
    transaction_id: str
    acknowledged: bool = False
    event_id: str | None = None


@dataclass
class BackgroundJob:
    """One exact delegation turn, independent of a reusable child conversation."""

    job_id: str
    child: DelegationChild
    owner: ToolExecutionIdentity
    status: _BackgroundStatus = "running"
    result: str | None = None
    approval_state: dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    human_paused: bool = False
    delivery: _BackgroundDelivery | None = None
    wait_acknowledged: bool = False
    live_result: object = field(default=None, repr=False)


@dataclass(frozen=True)
class _BackgroundWait:
    """A result lease acknowledged only after the parent persists its tool result."""

    job: BackgroundJob
    token: str | None = None
    delivery_queued: bool = False


@dataclass
class _Entry:
    job: BackgroundJob
    control: SubagentControl = field(default_factory=SubagentControl)
    human_signal: HumanMessageSignal | None = None
    task: asyncio.Task[None] | None = None
    pause_task: asyncio.Task[None] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    wait_token: str | None = None
    cancel: Callable[[DelegationChild], Awaitable[None]] | None = None
    stopping: bool = False
    cancel_task: asyncio.Task[BackgroundJob] | None = None


_runtimes: dict[Path, BackgroundSubagentRuntime] = {}


def get_background_runtime(runtime_paths: RuntimePaths) -> BackgroundSubagentRuntime | None:
    """Find the managed Matrix runtime for one storage root, if present."""
    return _runtimes.get(runtime_paths.storage_root.resolve())


def register_background_runtime(runtime_paths: RuntimePaths, runtime: BackgroundSubagentRuntime | None) -> None:
    """Publish or withdraw the lifecycle-owned runtime at its storage boundary."""
    key = runtime_paths.storage_root.resolve()
    if runtime is None:
        _runtimes.pop(key, None)
    else:
        _runtimes[key] = runtime


class BackgroundSubagentRuntime:
    """Keep child execution alive while individual foreground waiters come and go."""

    def __init__(
        self,
        storage_root: Path,
        *,
        authorize: Callable[[BackgroundJob], bool] | None = None,
        cancel: Callable[[DelegationChild], Awaitable[None]] | None = None,
    ) -> None:
        self._root = storage_root / "background_subagents"
        if self._root.is_symlink():
            msg = "Background subagent storage must not use symlinks."
            raise ValueError(msg)
        create_directory_durable(self._root, mode=0o700)
        self._lease = (self._root / "runtime.lock").open("a")
        try:
            fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._lease.close()
            raise
        self._authorize = authorize
        self._cancel = cancel
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self.changed = asyncio.Event()
        self._human_signals: WeakValueDictionary[tuple[str, str, str | None], HumanMessageSignal] = (
            WeakValueDictionary()
        )

    def human_signal_for(self, transport_agent_name: str, room_id: str, thread_id: str | None) -> HumanMessageSignal:
        """Retain one conversation signal while a runner or background job uses it."""
        key = (transport_agent_name, room_id, thread_id)
        signal = self._human_signals.get(key)
        if signal is None:
            signal = HumanMessageSignal()
            self._human_signals[key] = signal
        return signal

    def _path(self, job_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", job_id) is None:
            raise SubagentSessionError(_UNAVAILABLE)
        path = self._root / f"{job_id}.json"
        if path.is_symlink():
            raise SubagentSessionError(_UNAVAILABLE)
        return path

    @staticmethod
    def _owner(owner: ToolExecutionIdentity) -> ToolExecutionIdentity:
        return replace(owner, thread_id=owner.resolved_thread_id)

    def _allowed(self, job: BackgroundJob) -> bool:
        return self._authorize is None or self._authorize(job)

    def _ensure_open(self) -> None:
        if self._closed:
            msg = "Background subagent runtime is closed."
            raise SubagentSessionError(msg)

    def _entry(self, job_id: str, owner: ToolExecutionIdentity, depth: int) -> _Entry:
        self._ensure_open()
        entry = self._entries.get(job_id)
        if (
            entry is None
            or not owner.session_id
            or entry.job.owner != self._owner(owner)
            or entry.job.child.caller_agent_name != owner.agent_name
            or entry.job.child.depth != depth + 1
            or not self._allowed(entry.job)
        ):
            raise SubagentSessionError(_UNAVAILABLE)
        return entry

    async def _persist(self, entry: _Entry) -> None:
        job = entry.job
        job.human_paused = entry.control.paused.is_set()
        if job.status in {"running", "paused_for_human"}:
            job.status = "paused_for_human" if job.human_paused else "running"
        payload = {
            "schema_version": 1,
            "job_id": job.job_id,
            "child": asdict(job.child),
            "owner": asdict(job.owner),
            "status": job.status,
            "result": job.result,
            "approval_state": job.approval_state,
            "generation": job.generation,
            "human_paused": job.human_paused,
            "delivery": asdict(job.delivery) if job.delivery is not None else None,
            "wait_acknowledged": job.wait_acknowledged,
        }
        await run_blocking_until_complete(write_json_file_durable, self._path(job.job_id), payload)
        entry.changed.set()
        entry.changed = asyncio.Event()
        self.changed.set()

    async def recover(self) -> list[BackgroundJob]:
        """Restore outcomes and approval snapshots, never automatically replay execution."""
        async with self._lock:
            self._ensure_open()
            if self._entries:
                return [entry.job for entry in self._entries.values()]
            for path in sorted(self._root.glob("*.json")):
                if path != self._path(path.stem):
                    raise ValueError(_UNAVAILABLE)
                payload = json.loads(await asyncio.to_thread(path.read_text))
                if payload.pop("schema_version") != 1 or payload["job_id"] != path.stem:
                    msg = "Invalid background subagent snapshot."
                    raise ValueError(msg)
                payload["child"] = DelegationChild(**payload["child"])
                payload["owner"] = parse_tool_execution_identity_payload(payload["owner"], strict=True)
                if payload["delivery"] is not None:
                    payload["delivery"] = _BackgroundDelivery(**payload["delivery"])
                job = BackgroundJob(**payload)
                entry = _Entry(job)
                if job.human_paused:
                    entry.control.pause()
                if job.status not in _READY:
                    if self._cancel is not None:
                        await self._cancel(job.child)
                    self._settle_stopped(
                        entry,
                        status="interrupted",
                        reason="Subagent execution was interrupted by a runtime restart; it was not replayed.",
                    )
                self._entries[job.job_id] = entry
                self._restore_approval_control(entry)
                await self._persist(entry)
            return [entry.job for entry in self._entries.values()]

    def _restore_approval_control(self, entry: _Entry) -> None:
        """Observe future human ingress while a recovered native approval awaits reattachment."""
        if entry.job.status != "awaiting_approval":
            return
        owner = entry.job.owner
        if owner.room_id is not None:
            entry.human_signal = self.human_signal_for(
                owner.transport_agent_name or owner.agent_name,
                owner.room_id,
                owner.resolved_thread_id,
            )
            entry.human_signal.subscribe(entry.control.pause)
        entry.pause_task = asyncio.create_task(self._watch_pause(entry))

    async def start(
        self,
        child: DelegationChild,
        *,
        owner: ToolExecutionIdentity,
        operation: Callable[[], Awaitable[BackgroundOutcome]],
        human_signal: HumanMessageSignal | None = None,
        cancel: Callable[[DelegationChild], Awaitable[None]] | None = None,
    ) -> BackgroundJob:
        """Durably accept exact child ownership before spawning its native execution."""
        async with self._lock:
            if self._closed:
                msg = "Background subagent runtime is closed."
                raise RuntimeError(msg)
            if child.delegation_id in self._entries or self._path(child.delegation_id).exists():
                msg = "Background subagent job already exists."
                raise ValueError(msg)
            job = BackgroundJob(child.delegation_id, child, self._owner(owner))
            if not owner.session_id or child.caller_agent_name != owner.agent_name or not self._allowed(job):
                raise SubagentSessionError(_UNAVAILABLE)
            entry = _Entry(job, human_signal=human_signal or current_human_message_signal(), cancel=cancel)
            self._entries[job.job_id] = entry
            if entry.human_signal is not None:
                entry.human_signal.subscribe(entry.control.pause)
            await run_coroutine_until_complete(self._persist_and_launch(entry, operation))
            return job

    async def _persist_and_launch(self, entry: _Entry, operation: Callable[[], Awaitable[BackgroundOutcome]]) -> None:
        """Finish accepted admission before propagating cancellation to its former parent."""
        await self._persist(entry)
        self._launch(entry, operation)

    def owns_child(self, child: DelegationChild) -> bool:
        """Recognize the exact live child object accepted from a cancelled native parent."""
        entry = self._entries.get(child.delegation_id)
        return entry is not None and entry.job.child is child and entry.task is not None

    def _launch(self, entry: _Entry, operation: Callable[[], Awaitable[BackgroundOutcome]]) -> None:
        entry.task = asyncio.create_task(self._run(entry, operation), name=f"subagent:{entry.job.job_id}")
        if entry.pause_task is None:
            entry.pause_task = asyncio.create_task(self._watch_pause(entry))

    async def _watch_pause(self, entry: _Entry) -> None:
        while entry.job.status not in _TERMINAL:
            await entry.control.paused.wait()
            async with self._lock:
                await self._persist(entry)
            await entry.control.resumed.wait()

    async def _run(self, entry: _Entry, operation: Callable[[], Awaitable[BackgroundOutcome]]) -> None:
        # The notice implementation imports Agno/storage; keep the job/control import surface light.
        from mindroom.ai_runtime import (  # noqa: PLC0415
            finalize_queued_notice_response_turn_async,
            queued_message_signal_context,
        )

        try:
            with (
                human_message_signal_context(entry.human_signal),
                subagent_control_context(entry.control),
                queued_message_signal_context(None) as notice,
            ):
                try:
                    outcome = await operation()
                finally:
                    await finalize_queued_notice_response_turn_async(notice)
            async with self._lock:
                if entry.job.status not in _TERMINAL:
                    entry.job.status = outcome.status
                    entry.job.result = outcome.result
                    entry.job.approval_state = outcome.approval_state
                    entry.job.live_result = outcome.live_result
                    await self._persist(entry)
        except asyncio.CancelledError:
            async with self._lock:
                if entry.job.status not in _TERMINAL and not entry.stopping:
                    entry.job.status = "interrupted" if self._closed else "cancelled"
                    await self._persist(entry)
            raise
        except Exception as error:
            async with self._lock:
                entry.job.status = "failed"
                entry.job.result = str(error)
                await self._persist(entry)
        finally:
            if entry.job.status in _TERMINAL:
                self._release_control(entry)

    @staticmethod
    def _release_control(entry: _Entry) -> None:
        if entry.human_signal is not None:
            entry.human_signal.unsubscribe(entry.control.pause)
        if entry.pause_task is not None:
            entry.pause_task.cancel()

    async def lookup(self, job_id: str, *, owner: ToolExecutionIdentity, depth: int) -> BackgroundJob:
        """Inspect an exact job after validating its current caller and authorization."""
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            if entry.control.paused.is_set() != entry.job.human_paused:
                await self._persist(entry)
            return entry.job

    async def wait(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        timeout: float = 10.0,  # noqa: ASYNC109
    ) -> _BackgroundWait:
        """Wait without cancelling execution; retain ready-result ownership until acknowledgement."""
        if timeout < 0 or not float("inf") > timeout:
            msg = "Subagent wait timeout must be finite and non-negative."
            raise ValueError(msg)
        token = uuid4().hex
        deadline = asyncio.get_running_loop().time() + timeout
        retained = False
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            delivery = entry.job.delivery
            if entry.wait_token is not None or (
                delivery is not None and not (entry.job.status == "awaiting_approval" and delivery.acknowledged)
            ):
                return _BackgroundWait(entry.job, delivery_queued=True)
            entry.wait_token = token
        try:
            while True:
                async with self._lock:
                    self._entry(job_id, owner, depth)
                    if entry.job.status in _READY:
                        retained = True
                        return _BackgroundWait(entry.job, token)
                    if entry.control.paused.is_set():
                        await self._persist(entry)
                        return _BackgroundWait(entry.job)
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return _BackgroundWait(entry.job)
                    changed = entry.changed
                changed_wait = asyncio.create_task(changed.wait())
                human_wait = asyncio.create_task(entry.control.paused.wait())
                try:
                    await asyncio.wait(
                        {changed_wait, human_wait},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    changed_wait.cancel()
                    human_wait.cancel()
                    await asyncio.gather(changed_wait, human_wait, return_exceptions=True)
        finally:
            if not retained:
                await self.release_wait(job_id, token)

    async def release_wait(self, job_id: str, token: str | None) -> None:
        """Release an unpersisted result claim so completion delivery remains possible."""
        async with self._lock:
            entry = self._entries[job_id]
            if token is not None and entry.wait_token == token:
                entry.wait_token = None
                self.changed.set()

    async def acknowledge_wait(self, job_id: str, token: str | None) -> None:
        """Acknowledge only after the exact parent tool result has been durably saved."""
        async with self._lock:
            self._ensure_open()
            entry = self._entries[job_id]
            if token is None or entry.wait_token != token:
                msg = "Subagent wait claim no longer belongs to this waiter."
                raise ValueError(msg)
            entry.job.wait_acknowledged = True
            await self._persist(entry)
            entry.wait_token = None

    async def resume(self, job_id: str, *, owner: ToolExecutionIdentity, depth: int) -> BackgroundJob:
        """Release a human hold; native tool approvals are unchanged."""
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            entry.control.resume()
            await self._persist(entry)
            return entry.job

    async def cancel(self, job_id: str, *, owner: ToolExecutionIdentity, depth: int) -> BackgroundJob:
        """Stop the exact owned execution, preserving other jobs in the child conversation."""
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            task = self._cancellation_task(entry)
        return await wait_for_future_until_complete(task)

    async def cancel_retained(self, child: DelegationChild) -> bool:
        """Settle an exact trusted parent snapshot even after external authorization is revoked."""
        async with self._lock:
            if self._closed:
                return False
            entry = self._entries.get(child.delegation_id)
            if entry is None:
                return False
            retained = entry.job.child
            if (
                retained.caller_agent_name != child.caller_agent_name
                or retained.child_agent_name != child.child_agent_name
                or retained.session_id != child.session_id
                or retained.run_id != child.run_id
                or retained.depth != child.depth
                or retained.execution_identity != child.execution_identity
            ):
                return False
            task = self._cancellation_task(entry)
        await wait_for_future_until_complete(task)
        child.status = retained.status
        child.result = retained.result
        child.run_id = retained.run_id
        child.model_name = retained.model_name
        return True

    def _cancellation_task(self, entry: _Entry) -> asyncio.Task[BackgroundJob]:
        """Accept exactly one cleanup while the caller holds the runtime admission lock."""
        if entry.cancel_task is None:
            entry.cancel_task = asyncio.create_task(self._cancel_entry(entry))
        return entry.cancel_task

    async def _cancel_entry(self, entry: _Entry) -> BackgroundJob:
        async with self._lock:
            if entry.job.status in _TERMINAL:
                return entry.job
            was_approval = entry.job.status == "awaiting_approval"
            entry.control.cancel()
            entry.stopping = True
            task = entry.task
            if task is not None:
                task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        cleanup = entry.cancel or self._cancel
        if cleanup is not None and entry.job.child.status not in _TERMINAL:
            await cleanup(entry.job.child)
        async with self._lock:
            if entry.job.status not in _TERMINAL:
                self._settle_stopped(entry, status="cancelled", reason=entry.job.child.result)
                if entry.job.child.status not in _TERMINAL:
                    entry.job.child.status = "cancelled"
            if was_approval:
                entry.job.generation += 1
                entry.job.wait_acknowledged = False
                entry.job.delivery = None
                entry.wait_token = None
            await self._persist(entry)
            self._release_control(entry)
            return entry.job

    @staticmethod
    def _settle_stopped(
        entry: _Entry,
        *,
        status: Literal["cancelled", "interrupted"],
        reason: str | None,
    ) -> None:
        """Prefer native terminal evidence over cancellation or restart classification."""
        match entry.job.child.status:
            case "completed" | "failed" | "denied" as terminal:
                entry.job.status = terminal
                entry.job.result = entry.job.child.result
            case _:
                entry.job.status = status
                entry.job.result = reason

    async def continue_job(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        operation: Callable[[], Awaitable[BackgroundOutcome]],
    ) -> BackgroundJob:
        """Continue the same native approval wait without granting its human hold."""
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            if entry.job.status != "awaiting_approval" or self._closed:
                msg = "Subagent job is not awaiting approval continuation."
                raise ValueError(msg)
            entry.job.status = "running"
            entry.job.generation += 1
            entry.job.delivery = None
            entry.job.wait_acknowledged = False
            entry.job.live_result = None
            entry.wait_token = None
            if entry.human_signal is None:
                entry.human_signal = current_human_message_signal()
                if entry.human_signal is not None:
                    entry.human_signal.subscribe(entry.control.pause)
            await run_coroutine_until_complete(self._persist_and_launch(entry, operation))
            return entry.job

    async def pending_deliveries(self) -> list[BackgroundJob]:
        """List current authorized outcomes without a live waiter or acknowledged delivery."""
        async with self._lock:
            if self._closed:
                return []
            return [
                entry.job
                for entry in self._entries.values()
                if entry.job.status in _READY
                and entry.wait_token is None
                and not entry.job.wait_acknowledged
                and (entry.job.delivery is None or not entry.job.delivery.acknowledged)
                and self._allowed(entry.job)
            ]

    async def claim_delivery(
        self,
        job_id: str,
        *,
        content: dict[str, Any],
        transaction_id: str,
    ) -> _BackgroundDelivery | None:
        """Freeze a notification before send, or return its exact retry payload."""
        async with self._lock:
            if self._closed:
                return None
            entry = self._entries[job_id]
            job = entry.job
            if (
                entry.wait_token is not None
                or job.wait_acknowledged
                or job.status not in _READY
                or not self._allowed(job)
            ):
                return None
            if job.delivery is None:
                job.delivery = _BackgroundDelivery(
                    job_id,
                    job.generation,
                    json.loads(json.dumps(content)),
                    transaction_id,
                )
                await self._persist(entry)
            return job.delivery

    async def acknowledge_delivery(self, job_id: str, transaction_id: str, *, event_id: str | None = None) -> None:
        """Record a confirmed send under its immutable transaction identity."""
        async with self._lock:
            self._ensure_open()
            entry = self._entries[job_id]
            delivery = entry.job.delivery
            if delivery is None or delivery.transaction_id != transaction_id:
                msg = "Subagent delivery transaction does not match its claim."
                raise ValueError(msg)
            delivery.acknowledged = True
            delivery.event_id = event_id
            await self._persist(entry)

    async def shutdown(self) -> None:
        """Settle owned work as interrupted and release the process liveness lease."""
        if self._shutdown_task is None:
            self._closed = True
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await wait_for_future_until_complete(self._shutdown_task)

    async def _shutdown(self) -> None:
        tasks = []
        cancellations = []
        async with self._lock:
            for entry in self._entries.values():
                if entry.job.status not in _READY:
                    entry.stopping = True
                    entry.control.cancel()
                else:
                    await self._persist(entry)
                self._release_control(entry)
                if entry.cancel_task is not None:
                    cancellations.append(entry.cancel_task)
                for task in (entry.task, entry.pause_task):
                    if task is not None and not task.done():
                        if task is not entry.task or entry.cancel_task is None:
                            task.cancel()
                        tasks.append(task)
        try:
            await asyncio.gather(*tasks, *cancellations, return_exceptions=True)
            for entry in self._entries.values():
                cleanup = entry.cancel or self._cancel
                if entry.job.status not in _READY:
                    if entry.job.child.status not in _TERMINAL and cleanup is not None:
                        await cleanup(entry.job.child)
                    self._settle_stopped(
                        entry,
                        status="interrupted",
                        reason="Subagent execution was interrupted by runtime shutdown; it was not replayed.",
                    )
                    await self._persist(entry)
        finally:
            self._lease.close()
