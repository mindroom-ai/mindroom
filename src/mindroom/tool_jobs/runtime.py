"""Durable ownership, cooperative control, and delivery arbitration for application tool jobs."""

from __future__ import annotations

import asyncio
import fcntl
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

from mindroom.background_tasks import (
    run_blocking_until_complete,
    run_coroutine_until_complete,
    wait_for_future_until_complete,
)
from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.tool_jobs.control import (
    HumanMessageSignal,
    JobControl,
    current_human_message_signal,
    human_message_signal_context,
    job_control_context,
)
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths

type _BackgroundStatus = Literal[
    "running",
    "cancel_requested",
    "paused_for_human",
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
    "denied",
    "interrupted",
]
type _OutcomeStatus = Literal["awaiting_approval", "completed", "failed", "cancelled", "denied", "interrupted"]
_TERMINAL = frozenset({"completed", "failed", "cancelled", "denied", "interrupted"})
_READY = _TERMINAL | {"awaiting_approval"}
_UNAVAILABLE = "Tool job is not available in this conversation."


class JobAccessError(ValueError):
    """The requested job is unavailable to this caller or runtime."""


@dataclass
class JobSpec:
    """Exact execution identity and opaque adapter metadata, independent of its caller turn."""

    job_id: str
    tool_name: str
    depth: int
    kind: str = "tool"
    toolkit_name: str | None = None
    adapter: dict[str, Any] = field(default_factory=dict)


@dataclass
class BackgroundOutcome:
    """Serializable operation outcome; approval semantics remain owned by its adapter."""

    status: _OutcomeStatus
    result: str | None = None
    approval_state: dict[str, Any] = field(default_factory=dict)
    result_payload: Any = None


@dataclass
class _BackgroundDelivery:
    """Immutable send identity and content retained across ambiguous send failures."""

    job_id: str
    generation: int
    content: dict[str, Any]
    transaction_id: str
    acknowledged: bool = False
    event_id: str | None = None
    disposition: Literal["recipient_mismatch"] | None = None


@dataclass
class BackgroundJob:
    """Durable execution and result, scoped to one conversation and requester."""

    job_id: str
    owner: ToolExecutionIdentity
    tool_name: str
    depth: int
    kind: str = "tool"
    toolkit_name: str | None = None
    adapter: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: _BackgroundStatus = "running"
    result: str | None = None
    result_payload: Any = None
    approval_state: dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    human_paused: bool = False
    deliveries: list[_BackgroundDelivery] = field(default_factory=list)
    wait_acknowledged: bool = False

    @property
    def delivery(self) -> _BackgroundDelivery | None:
        """The claim belonging to the current outcome, retaining older claims separately."""
        return next((item for item in self.deliveries if item.generation == self.generation), None)


@dataclass(frozen=True)
class _BackgroundWait:
    """A result lease acknowledged only after the parent persists its tool result."""

    job: BackgroundJob
    token: str | None = None
    delivery_queued: bool = False


@dataclass
class _Entry:
    job: BackgroundJob
    control: JobControl = field(default_factory=JobControl)
    human_signal: HumanMessageSignal | None = None
    task: asyncio.Task[None] | None = None
    pause_task: asyncio.Task[None] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    wait_token: str | None = None
    cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]] | None = None
    stopping: bool = False
    stopped_outcome: BackgroundOutcome | None = None
    cancel_task: asyncio.Task[BackgroundJob] | None = None
    cancel_ready: asyncio.Event = field(default_factory=asyncio.Event)


_runtimes: dict[Path, ToolJobRuntime] = {}


def get_background_runtime(runtime_paths: RuntimePaths) -> ToolJobRuntime | None:
    """Find the managed Matrix runtime for one storage root, if present."""
    return _runtimes.get(runtime_paths.storage_root.resolve())


def register_background_runtime(runtime_paths: RuntimePaths, runtime: ToolJobRuntime | None) -> None:
    """Publish or withdraw the lifecycle-owned runtime at its storage boundary."""
    key = runtime_paths.storage_root.resolve()
    if runtime is None:
        _runtimes.pop(key, None)
    else:
        _runtimes[key] = runtime


class ToolJobRuntime:
    """Keep tool execution alive while individual foreground waiters come and go."""

    def __init__(
        self,
        storage_root: Path,
        *,
        authorize: Callable[[BackgroundJob], bool] | None = None,
        cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]] | None = None,
    ) -> None:
        self._root = storage_root / "tool_jobs"
        if self._root.is_symlink():
            msg = "Tool job storage must not use symlinks."
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
        self._recovered = False
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
            raise JobAccessError(_UNAVAILABLE)
        path = self._root / f"{job_id}.json"
        if path.is_symlink():
            raise JobAccessError(_UNAVAILABLE)
        return path

    @staticmethod
    def _owner(owner: ToolExecutionIdentity) -> ToolExecutionIdentity:
        return replace(owner, thread_id=owner.resolved_thread_id)

    def _allowed(self, job: BackgroundJob) -> bool:
        return self._authorize is None or self._authorize(job)

    def _ensure_open(self) -> None:
        if self._closed:
            msg = "Tool job runtime is closed."
            raise JobAccessError(msg)

    def _entry(self, job_id: str, owner: ToolExecutionIdentity, depth: int) -> _Entry:
        self._ensure_open()
        entry = self._entries.get(job_id)
        if (
            entry is None
            or not owner.session_id
            or entry.job.owner != self._owner(owner)
            or entry.job.depth != depth
            or not self._allowed(entry.job)
        ):
            raise JobAccessError(_UNAVAILABLE)
        return entry

    async def _persist(self, entry: _Entry) -> None:
        job = entry.job
        job.human_paused = entry.control.paused.is_set()
        if job.status in {"running", "paused_for_human"}:
            job.status = "paused_for_human" if entry.control.blocked.is_set() else "running"
        job.updated_at = datetime.now(UTC).isoformat()
        payload = {"schema_version": 1, **deepcopy(asdict(job))}
        await run_blocking_until_complete(write_json_file_durable, self._path(job.job_id), payload)
        entry.changed.set()
        entry.changed = asyncio.Event()
        self.changed.set()

    async def recover(self) -> list[BackgroundJob]:
        """Restore outcomes and approval snapshots, never automatically replay execution."""
        async with self._lock:
            self._ensure_open()
            if self._recovered:
                return [self._snapshot(entry) for entry in self._entries.values()]
            for path in sorted(self._root.glob("*.json")):
                if path.stem in self._entries:
                    continue
                if path != self._path(path.stem):
                    raise ValueError(_UNAVAILABLE)
                payload = json.loads(await asyncio.to_thread(path.read_text))
                if payload.pop("schema_version") != 1 or payload["job_id"] != path.stem:
                    msg = "Invalid background subagent snapshot."
                    raise ValueError(msg)
                payload["owner"] = parse_tool_execution_identity_payload(payload["owner"], strict=True)
                payload["deliveries"] = [_BackgroundDelivery(**item) for item in payload["deliveries"]]
                job = BackgroundJob(**payload)
                entry = _Entry(job)
                if job.human_paused:
                    entry.control.pause()
                if job.status not in _READY:
                    outcome = await self._cancel(job) if self._cancel is not None else None
                    self._settle_stopped(
                        entry,
                        status="interrupted",
                        reason="Tool execution was interrupted by a runtime restart; it was not replayed.",
                        outcome=outcome,
                    )
                await self._persist(entry)
                self._entries[job.job_id] = entry
                self._restore_approval_control(entry)
            self._recovered = True
            return [self._snapshot(entry) for entry in self._entries.values()]

    def _restore_approval_control(self, entry: _Entry) -> None:
        """Observe future human ingress while a recovered approval awaits reattachment."""
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
        spec: JobSpec,
        *,
        owner: ToolExecutionIdentity,
        operation: Callable[[], Awaitable[BackgroundOutcome]],
        human_signal: HumanMessageSignal | None = None,
        cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]] | None = None,
        initial_wait_token: str | None = None,
        reattach: bool = False,
    ) -> BackgroundJob:
        """Durably accept exact operation ownership before spawning execution."""
        async with self._lock:
            if self._closed:
                msg = "Tool job runtime is closed."
                raise RuntimeError(msg)
            if reattach and spec.job_id in self._entries:
                existing = self._entry(spec.job_id, owner, spec.depth)
                if (
                    existing.job.tool_name != spec.tool_name
                    or existing.job.toolkit_name != spec.toolkit_name
                    or existing.job.kind != spec.kind
                    or existing.job.adapter != spec.adapter
                ):
                    raise JobAccessError(_UNAVAILABLE)
                if existing.wait_token is None:
                    existing.wait_token = initial_wait_token
                return self._snapshot(existing)
            if spec.job_id in self._entries or self._path(spec.job_id).exists():
                msg = "Tool job already exists."
                raise ValueError(msg)
            job = BackgroundJob(owner=self._owner(owner), **asdict(spec))
            job.adapter = spec.adapter
            if not owner.session_id or spec.depth < 0 or not self._allowed(job):
                raise JobAccessError(_UNAVAILABLE)
            entry = _Entry(
                job,
                human_signal=human_signal or current_human_message_signal(),
                cancel=cancel,
                wait_token=initial_wait_token,
            )
            self._entries[job.job_id] = entry
            if entry.human_signal is not None:
                entry.human_signal.subscribe(entry.control.pause)
            await run_coroutine_until_complete(self._persist_and_launch(entry, operation))
            return self._snapshot(entry)

    async def _persist_and_launch(
        self,
        entry: _Entry,
        operation: Callable[[], Awaitable[BackgroundOutcome]],
        *,
        previous: tuple[BackgroundJob, str | None] | None = None,
    ) -> None:
        """Reconcile acceptance inside the owned task before propagating parent cancellation."""
        try:
            await self._persist(entry)
        except Exception:
            if previous is not None:
                saved = json.loads(await asyncio.to_thread(self._path(entry.job.job_id).read_text))
                if saved["generation"] == previous[0].generation:
                    entry.job, entry.wait_token = previous
                    raise
            self._release_control(entry)
            if not self._path(entry.job.job_id).exists():
                self._entries.pop(entry.job.job_id)
            else:
                entry.job.status = "interrupted"
                entry.job.result = "Job admission failed after publication; execution was not started."
            raise
        self._launch(entry, operation)

    def owns_execution(self, job_id: str, adapter: dict[str, Any]) -> bool:
        """Recognize exact accepted adapter ownership even if its former waiter was cancelled."""
        entry = self._entries.get(job_id)
        return entry is not None and entry.job.adapter is adapter and entry.task is not None

    def _launch(self, entry: _Entry, operation: Callable[[], Awaitable[BackgroundOutcome]]) -> None:
        entry.task = asyncio.create_task(self._run(entry, operation), name=f"tool-job:{entry.job.job_id}")
        if entry.pause_task is None:
            entry.pause_task = asyncio.create_task(self._watch_pause(entry))

    async def _watch_pause(self, entry: _Entry) -> None:
        while entry.job.status not in _TERMINAL:
            await entry.control.changed.wait()
            async with self._lock:
                entry.control.changed.clear()
                await self._persist(entry)

    async def _run(self, entry: _Entry, operation: Callable[[], Awaitable[BackgroundOutcome]]) -> None:
        # The notice implementation imports Agno/storage; keep the job/control import surface light.
        from mindroom.ai_runtime import (  # noqa: PLC0415
            finalize_queued_notice_response_turn_async,
            queued_message_signal_context,
        )

        try:
            with (
                human_message_signal_context(entry.human_signal),
                job_control_context(entry.control),
                queued_message_signal_context(None) as notice,
            ):
                try:
                    async with execution_resources():
                        outcome = await operation()
                finally:
                    await finalize_queued_notice_response_turn_async(notice)
            async with self._lock:
                if entry.stopping:
                    entry.stopped_outcome = outcome
                elif entry.job.status not in _TERMINAL:
                    entry.job.status = outcome.status
                    entry.job.result = outcome.result
                    entry.job.approval_state = outcome.approval_state
                    entry.job.result_payload = outcome.result_payload
                    await self._persist(entry)
        except asyncio.CancelledError:
            async with self._lock:
                if entry.job.status not in _TERMINAL and not entry.stopping:
                    entry.job.status = "interrupted" if self._closed else "cancelled"
                    await self._persist(entry)
            raise
        except Exception as error:
            async with self._lock:
                if not entry.stopping:
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
            return self._snapshot(entry)

    @staticmethod
    def _snapshot(entry: _Entry) -> BackgroundJob:
        job = deepcopy(entry.job)
        job.human_paused = entry.control.paused.is_set()
        if job.status in {"running", "paused_for_human"}:
            job.status = "paused_for_human" if entry.control.blocked.is_set() else "running"
        return job

    async def list_jobs(
        self,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        limit: int = 20,
        offset: int = 0,
    ) -> list[BackgroundJob]:
        """Discover authorized jobs, active first then recent outcomes, without consuming them."""
        if not 1 <= limit <= 100 or offset < 0:
            msg = "Job listing requires a limit from 1 to 100 and a non-negative offset."
            raise ValueError(msg)
        async with self._lock:
            self._ensure_open()
            entries = [
                entry
                for entry in self._entries.values()
                if owner.session_id
                and entry.job.owner == self._owner(owner)
                and entry.job.depth == depth
                and self._allowed(entry.job)
            ]
            entries.sort(key=lambda entry: entry.job.updated_at, reverse=True)
            entries.sort(key=lambda entry: entry.job.status in _TERMINAL)
            return [self._snapshot(entry) for entry in entries[offset : offset + limit]]

    async def wait(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        timeout: float = 10.0,  # noqa: ASYNC109
        reserved_token: str | None = None,
    ) -> _BackgroundWait:
        """Wait without cancelling execution; retain ready-result ownership until acknowledgement."""
        if timeout < 0 or not float("inf") > timeout:
            msg = "Tool job wait timeout must be finite and non-negative."
            raise ValueError(msg)
        token = reserved_token or uuid4().hex
        deadline = asyncio.get_running_loop().time() + timeout
        retained = False
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            if entry.wait_token is not None and entry.wait_token != reserved_token:
                return _BackgroundWait(self._snapshot(entry), delivery_queued=True)
            entry.wait_token = token
        try:
            while True:
                async with self._lock:
                    self._entry(job_id, owner, depth)
                    if entry.job.status in _READY:
                        retained = True
                        return _BackgroundWait(self._snapshot(entry), token)
                    if entry.control.paused.is_set():
                        return _BackgroundWait(self._snapshot(entry))
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return _BackgroundWait(self._snapshot(entry))
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
            entry = self._entries.get(job_id)
            if entry is not None and token is not None and entry.wait_token == token:
                entry.wait_token = None
                self.changed.set()

    async def acknowledge_wait(self, job_id: str, token: str | None) -> None:
        """Acknowledge only after the exact parent tool result has been durably saved."""
        async with self._lock:
            self._ensure_open()
            entry = self._entries[job_id]
            if token is None or entry.wait_token != token:
                msg = "Tool job wait claim no longer belongs to this waiter."
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
            return self._snapshot(entry)

    async def cancel(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        await_completion: bool = False,
    ) -> BackgroundJob:
        """Request cancellation; retain ownership until execution and cleanup have settled."""
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            task = self._cancellation_task(entry)
        if await_completion:
            return await wait_for_future_until_complete(task)
        admitted = asyncio.create_task(entry.cancel_ready.wait())
        try:
            await asyncio.wait({task, admitted}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return task.result()
            async with self._lock:
                return self._snapshot(entry)
        finally:
            admitted.cancel()
            await asyncio.gather(admitted, return_exceptions=True)

    async def cancel_owned(
        self,
        job_id: str,
        *,
        matches: Callable[[BackgroundJob], bool],
    ) -> BackgroundJob | None:
        """Settle retained adapter ownership during internal cleanup after authority revocation."""
        async with self._lock:
            if self._closed:
                return None
            entry = self._entries.get(job_id)
            if entry is None or not matches(self._snapshot(entry)):
                return None
            task = self._cancellation_task(entry)
        return await wait_for_future_until_complete(task)

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
            entry.job.status = "cancel_requested"
            await self._persist(entry)
            entry.cancel_ready.set()
            entry.control.cancel()
            entry.stopping = True
            task = entry.task
            if task is not None:
                task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        outcome = await self._cleanup(entry)
        async with self._lock:
            if entry.job.status not in _TERMINAL:
                self._settle_stopped(entry, status="cancelled", reason=None, outcome=outcome)
            if was_approval:
                entry.job.generation += 1
                entry.job.wait_acknowledged = False
                entry.wait_token = None
            await self._persist(entry)
            self._release_control(entry)
            return self._snapshot(entry)

    async def _cleanup(self, entry: _Entry) -> BackgroundOutcome | None:
        outcome = await entry.cancel(entry.job) if entry.cancel is not None else None
        if outcome is None and self._cancel is not None:
            outcome = await self._cancel(entry.job)
        return outcome

    @staticmethod
    def _settle_stopped(
        entry: _Entry,
        *,
        status: Literal["cancelled", "interrupted"],
        reason: str | None,
        outcome: BackgroundOutcome | None = None,
    ) -> None:
        """Publish retained terminal evidence only after owned execution and cleanup settle."""
        if (
            entry.stopped_outcome is not None
            and entry.stopped_outcome.status in _TERMINAL
            and (outcome is None or outcome.status not in {"completed", "failed", "denied"})
        ):
            outcome = entry.stopped_outcome
        entry.stopped_outcome = None
        if outcome is not None and outcome.status in _TERMINAL:
            entry.job.status = outcome.status
            entry.job.result = outcome.result
            entry.job.result_payload = outcome.result_payload
        else:
            entry.job.status = status
            entry.job.result = reason

    async def continue_job(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        operation: Callable[[], Awaitable[BackgroundOutcome]],
        adapter: dict[str, Any] | None = None,
    ) -> BackgroundJob:
        """Continue the same approval wait without granting its human hold."""
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            if entry.job.status != "awaiting_approval" or self._closed:
                msg = "Tool job is not awaiting approval continuation."
                raise ValueError(msg)
            previous = deepcopy(entry.job)
            previous_token = entry.wait_token
            if adapter is not None:
                entry.job.adapter = adapter
            entry.job.status = "running"
            entry.job.generation += 1
            entry.job.wait_acknowledged = False
            entry.wait_token = None
            if entry.human_signal is None:
                entry.human_signal = current_human_message_signal()
                if entry.human_signal is not None:
                    entry.human_signal.subscribe(entry.control.pause)
            await run_coroutine_until_complete(
                self._persist_and_launch(entry, operation, previous=(previous, previous_token)),
            )
            return self._snapshot(entry)

    async def pending_deliveries(self) -> list[BackgroundJob]:
        """List current authorized outcomes without a live waiter or acknowledged delivery."""
        async with self._lock:
            if self._closed:
                return []
            return [
                self._snapshot(entry)
                for entry in self._entries.values()
                if entry.job.status in _READY
                and entry.wait_token is None
                and not entry.job.wait_acknowledged
                and (
                    entry.job.delivery is None
                    or (not entry.job.delivery.acknowledged and entry.job.delivery.disposition is None)
                )
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
                job.deliveries.append(
                    _BackgroundDelivery(
                        job_id,
                        job.generation,
                        json.loads(json.dumps(content)),
                        transaction_id,
                    ),
                )
            await self._persist(entry)
            return deepcopy(job.delivery)

    async def acknowledge_delivery(self, job_id: str, transaction_id: str, *, event_id: str | None = None) -> None:
        """Record a confirmed send under its immutable transaction identity."""
        async with self._lock:
            self._ensure_open()
            entry = self._entries[job_id]
            delivery = next((item for item in entry.job.deliveries if item.transaction_id == transaction_id), None)
            if delivery is None:
                msg = "Tool job delivery transaction does not match its claim."
                raise ValueError(msg)
            delivery.acknowledged = True
            delivery.event_id = event_id
            await self._persist(entry)

    async def delivery_outcome(
        self,
        job_id: str,
        generation: int,
        transaction_id: str,
    ) -> BackgroundJob | None:
        """Validate an incoming notification against its persisted claim before admitting a response."""
        async with self._lock:
            entry = self._entries.get(job_id)
            if self._closed or entry is None:
                return None
            job = entry.job
            claim = job.delivery
            if (
                job.generation != generation
                or job.wait_acknowledged
                or job.status not in _READY
                or claim is None
                or claim.transaction_id != transaction_id
                or claim.disposition is not None
                or not self._allowed(job)
            ):
                return None
            return self._snapshot(entry)

    async def block_delivery(self, job_id: str, transaction_id: str) -> None:
        """Retain a terminal recipient mismatch disposition without consuming the result."""
        async with self._lock:
            self._ensure_open()
            entry = self._entries[job_id]
            claim = next(item for item in entry.job.deliveries if item.transaction_id == transaction_id)
            claim.disposition = "recipient_mismatch"
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
                if entry.job.status not in _READY:
                    outcome = await self._cleanup(entry)
                    self._settle_stopped(
                        entry,
                        status="interrupted",
                        reason="Tool execution was interrupted by runtime shutdown; it was not replayed.",
                        outcome=outcome,
                    )
                    await self._persist(entry)
        finally:
            self._lease.close()
