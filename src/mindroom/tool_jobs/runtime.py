"""Durable execution ownership, interruptible waits, and result consumption for application tool jobs."""

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
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.logging_config import get_logger
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
JOB_SUMMARY_MAX_CHARS = 500
logger = get_logger(__name__)


class JobAccessError(ValueError):
    """The requested job is unavailable to this caller or runtime."""


class JobRecoveryBlockedError(RuntimeError):
    """Another executor still owns work that this runtime cannot safely settle."""


class JobContinuationError(ValueError):
    """A presented approval no longer owns the current job generation."""


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
    wait_acknowledged: bool = False
    result_expired: bool = False
    user_stop_receipt_order: int | None = None


def read_job_snapshot(path: Path) -> BackgroundJob:
    """Validate one existing snapshot without claiming or changing its execution."""
    if path.is_symlink() or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", path.stem) is None:
        raise JobAccessError(_UNAVAILABLE)
    payload = json.loads(path.read_text())
    if payload.pop("schema_version") != 1 or payload["job_id"] != path.stem:
        msg = "Invalid tool job snapshot."
        raise ValueError(msg)
    payload["owner"] = parse_tool_execution_identity_payload(payload["owner"], strict=True)
    # Retired schema-1 fields and duplicate native output carry no execution authority.
    payload.pop("human_paused", None)
    payload.pop("deliveries", None)
    if payload.get("kind") == "delegation":
        payload["adapter"]["child"]["result"] = None
    return BackgroundJob(**payload)


def format_job_handle(
    job: BackgroundJob,
    *,
    subagent_id: str | None = None,
    delivery_queued: bool = False,
) -> str:
    """Return a stable machine-readable handle without resolving the job."""
    handle: dict[str, Any] = {"job_id": job.job_id, "tool": job.tool_name, "status": job.status}
    if subagent_id is not None:
        handle["subagent_id"] = subagent_id
    if delivery_queued:
        handle["delivery_queued"] = True
    return json.dumps(handle)


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
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    wait_token: str | None = None
    cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]] | None = None
    stopping: bool = False
    saved: bool = False
    cold: bool = False
    stopped_outcome: BackgroundOutcome | None = None
    cancel_task: asyncio.Task[BackgroundJob] | None = None
    cancel_settlement_pending: bool = False
    cancel_ready: asyncio.Event = field(default_factory=asyncio.Event)

    def notify_changed(self) -> None:
        """Wake existing state waiters while keeping the next wait fresh."""
        self.changed.set()
        self.changed = asyncio.Event()


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
        self._unacknowledged: set[str] = set()
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

    def _ensure_accepting(self) -> None:
        self._ensure_open()
        if self._shutdown_task is not None:
            msg = "Tool job runtime is shutting down."
            raise JobAccessError(msg)

    def has_job(self, job_id: str) -> bool:
        """Recognize accepted ownership; access still requires an authorized lookup."""
        return job_id in self._entries

    def source_event_id(self, job_id: str) -> str | None:
        """Read accepted provenance for internal Stop ancestry without acquiring the admission lock."""
        entry = self._entries.get(job_id)
        source = entry.job.adapter.get("source_event_id") if entry is not None else None
        return source if isinstance(source, str) else None

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

    async def _persist(self, entry: _Entry, *, update_timestamp: bool = True) -> None:
        if entry.cold:
            msg = "Cannot persist a job without its saved result"
            raise RuntimeError(msg)
        job = entry.job
        if update_timestamp:
            job.updated_at = datetime.now(UTC).isoformat()
        entry.saved = False
        self._index_consumption(entry)
        path = self._path(job.job_id)

        def write() -> None:
            write_json_file_durable(path, {"schema_version": 1, **asdict(job)}, strict_atomic_replace=True)

        try:
            await run_blocking_until_complete(write)
            entry.saved = True
            entry.cancel_settlement_pending = False
        finally:
            # A failed save still leaves a new outcome for current waiters to observe.
            entry.notify_changed()
            self.changed.set()

    async def recover(self) -> list[BackgroundJob]:
        """Restore outcomes and approval snapshots, never automatically replay execution."""
        async with self._lock:
            self._ensure_open()
            if self._recovered:
                return [await self._snapshot(entry, include_result=False) for entry in self._entries.values()]
            for path in sorted(self._root.glob("*.json")):
                if path.stem in self._entries:
                    continue
                # LEGACY_COMPAT: Discard retired metadata and duplicate native results in job snapshots.
                # Legacy format: Schema 1 included human_paused, deliveries, and a redundant adapter.child.result.
                # Last legacy release: Unreleased; neither the original nor restored writer is in a release tag.
                # Replacement: Current schema 1 stores outcome text once and durable wait acknowledgement.
                # Handling: Drop obsolete metadata; retain result, approval, generation, and consumption evidence.
                # Coverage: tests/test_tool_jobs.py::test_legacy_job_snapshot_preserves_outcome_and_consumption
                # Coverage: tests/test_tool_jobs.py::test_legacy_paused_execution_is_interrupted_without_replay
                # Coverage: tests/test_background_subagents.py::test_native_result_expires_to_a_compact_receipt_after_restart
                job = await asyncio.to_thread(read_job_snapshot, path)
                entry = _Entry(job, saved=True)
                interrupted = job.status not in _READY
                if interrupted:
                    outcome = await self._cleanup(entry)
                    self._settle_stopped(
                        entry,
                        status="interrupted",
                        reason="Tool execution was interrupted by a runtime restart; it was not replayed.",
                        outcome=outcome,
                    )
                    await self._persist(entry)
                self._index_consumption(entry)
                self._cool(entry)
                self._entries[job.job_id] = entry
                self._restore_approval_signal(entry)
            self._recovered = True
            return [await self._snapshot(entry, include_result=False) for entry in self._entries.values()]

    def _restore_approval_signal(self, entry: _Entry) -> None:
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
            entry.human_signal.subscribe(entry.notify_changed)

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
            self._ensure_accepting()
            if reattach and spec.job_id in self._entries:
                existing = self._entry(spec.job_id, owner, spec.depth)
                if existing.job.result_expired:
                    raise JobAccessError(existing.job.result)
                if (
                    existing.job.tool_name != spec.tool_name
                    or existing.job.toolkit_name != spec.toolkit_name
                    or existing.job.kind != spec.kind
                    or existing.job.adapter != spec.adapter
                ):
                    raise JobAccessError(_UNAVAILABLE)
                if existing.wait_token is None:
                    existing.wait_token = initial_wait_token
                return await self._snapshot(existing)
            if spec.job_id in self._entries or self._path(spec.job_id).exists():
                msg = "Tool job already exists."
                raise ValueError(msg)
            job = BackgroundJob(owner=self._owner(owner), **asdict(spec))
            # Native adapters mutate this exact mapping; owns_execution verifies its identity.
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
            self._index_consumption(entry)
            if entry.human_signal is not None:
                entry.human_signal.subscribe(entry.notify_changed)
            await run_coroutine_until_complete(self._persist_and_launch(entry, operation))
            return await self._snapshot(entry)

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
                    self._index_consumption(entry)
                    raise
            self._release_control(entry)
            if not self._path(entry.job.job_id).exists():
                self._entries.pop(entry.job.job_id)
                self._unacknowledged.discard(entry.job.job_id)
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

    async def _run(self, entry: _Entry, operation: Callable[[], Awaitable[BackgroundOutcome]]) -> None:
        # The notice implementation imports Agno/storage; keep the job/control import surface light.
        from mindroom.ai_runtime import (  # noqa: PLC0415
            finalize_queued_notice_response_turn_async,
            queued_message_signal_context,
        )

        outcome = None
        try:
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
            except Exception as error:
                outcome = BackgroundOutcome("failed", str(error))
            async with self._lock:
                if entry.stopping:
                    entry.stopped_outcome = outcome
                elif entry.job.status not in _TERMINAL:
                    entry.job.status = outcome.status
                    entry.job.result = outcome.result
                    entry.job.approval_state = outcome.approval_state
                    entry.job.result_payload = outcome.result_payload
                    try:
                        await self._persist(entry)
                    except Exception:
                        # Consumption acknowledgement and shutdown retry this exact snapshot.
                        logger.exception(
                            "Tool job outcome save failed; retaining it in memory",
                            job_id=entry.job.job_id,
                        )
        except asyncio.CancelledError:
            async with self._lock:
                if entry.stopping:
                    entry.stopped_outcome = outcome
                elif entry.job.status not in _TERMINAL:
                    entry.job.status = "interrupted" if self._shutdown_task is not None else "cancelled"
                    await self._persist(entry)
            raise
        finally:
            if entry.job.status in _TERMINAL:
                self._release_control(entry)
                self._cool(entry)

    @staticmethod
    def _release_control(entry: _Entry) -> None:
        if entry.human_signal is not None:
            entry.human_signal.unsubscribe(entry.notify_changed)

    async def lookup(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        include_result: bool = True,
    ) -> BackgroundJob:
        """Inspect an exact job after validating its current caller and authorization."""
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            return await self._snapshot(entry, include_result=include_result)

    def _index_consumption(self, entry: _Entry) -> None:
        if entry.job.wait_acknowledged or entry.job.user_stop_receipt_order is not None:
            self._unacknowledged.discard(entry.job.job_id)
        else:
            self._unacknowledged.add(entry.job.job_id)

    def _cool(self, entry: _Entry) -> None:
        """Keep only discovery metadata in memory after durable terminal publication."""
        if entry.saved and entry.job.status in _TERMINAL:
            entry.job = replace(
                entry.job,
                result=entry.job.result[: JOB_SUMMARY_MAX_CHARS + 1] if entry.job.result is not None else None,
                result_payload=None,
                approval_state={},
            )
            entry.cold = not entry.job.result_expired
            self._release_control(entry)
            entry.human_signal = None
            entry.cancel = None
            entry.task = None

    async def _snapshot(self, entry: _Entry, *, include_result: bool = True) -> BackgroundJob:
        if include_result and entry.cold:
            return await run_blocking_until_complete(read_job_snapshot, self._path(entry.job.job_id))
        job = entry.job if include_result else replace(entry.job, result_payload=None, approval_state={})
        return await run_blocking_until_complete(deepcopy, job)

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
            return [await self._snapshot(entry, include_result=False) for entry in entries[offset : offset + limit]]

    async def wait(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        timeout: float | None = None,  # noqa: ASYNC109
        reserved_token: str | None = None,
    ) -> _BackgroundWait:
        """Wait without cancelling execution; retain ready-result ownership until acknowledgement."""
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 0 <= timeout < float("inf")
        ):
            msg = "Tool job wait timeout must be finite and non-negative."
            raise ValueError(msg)
        token = reserved_token or uuid4().hex
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        retained = False
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            if entry.wait_token is not None and entry.wait_token != reserved_token:
                return _BackgroundWait(await self._snapshot(entry), delivery_queued=True)
            entry.wait_token = token
            human_notified = asyncio.Event()
            if entry.human_signal is not None:
                entry.human_signal.subscribe(human_notified.set)
        try:
            while True:
                async with self._lock:
                    self._entry(job_id, owner, depth)
                    if entry.job.status in _READY:
                        snapshot = await self._snapshot(entry)
                        retained = True
                        return _BackgroundWait(snapshot, token)
                    if human_notified.is_set():
                        return _BackgroundWait(await self._snapshot(entry))
                    remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
                    if remaining is not None and remaining <= 0:
                        return _BackgroundWait(await self._snapshot(entry))
                    changed = entry.changed
                changed_wait = asyncio.create_task(changed.wait())
                human_wait = asyncio.create_task(human_notified.wait())
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
            if entry.human_signal is not None:
                entry.human_signal.unsubscribe(human_notified.set)
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
            if entry.cold:
                entry.job = await self._snapshot(entry)
                entry.cold = False
            entry.job.wait_acknowledged = True
            await self._persist(entry)
            entry.wait_token = None
            self._cool(entry)

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
            self._ensure_accepting()
            entry = self._entry(job_id, owner, depth)
            task = self._cancellation_task(entry)
        if await_completion:
            return await wait_for_future_until_complete(task)
        return await self._cancel_admitted(entry, task)

    async def _cancel_admitted(self, entry: _Entry, task: asyncio.Task[BackgroundJob]) -> BackgroundJob:
        """Wait for durable admission without waiting for uncooperative execution to drain."""
        admitted = asyncio.create_task(entry.cancel_ready.wait())
        try:
            await asyncio.wait({task, admitted}, return_when=asyncio.FIRST_COMPLETED)
            async with self._lock:
                if task.done():
                    return task.result()
                return await self._snapshot(entry)
        finally:
            admitted.cancel()
            await asyncio.gather(admitted, return_exceptions=True)

    async def stop_jobs(
        self,
        *,
        receipt_order: int,
        matches: Callable[[BackgroundJob], Awaitable[bool]],
    ) -> None:
        """Persist explicit Stop independently of result consumption, then request owned cleanup."""
        cancellations = []
        async with self._lock:
            self._ensure_accepting()
            for entry in self._entries.values():
                if not await matches(await self._snapshot(entry, include_result=False)):
                    continue
                if entry.cold:
                    entry.job = await self._snapshot(entry)
                    entry.cold = False
                entry.job.user_stop_receipt_order = max(entry.job.user_stop_receipt_order or 0, receipt_order)
                await self._persist(entry)
                if entry.job.status not in _TERMINAL:
                    cancellations.append((entry, self._cancellation_task(entry)))
                else:
                    self._cool(entry)
        for entry, task in cancellations:
            await self._cancel_admitted(entry, task)

    async def is_user_stopped(self, job_id: str) -> bool:
        """Check suppression without confusing a read receipt with explicit user intent."""
        async with self._lock:
            entry = self._entries.get(job_id)
            return entry is not None and entry.job.user_stop_receipt_order is not None

    async def is_source_user_stopped(self, source_event_id: str, transport_agent_name: str) -> bool:
        """Recognize a stopped original response, including foreground approval recovery."""
        async with self._lock:
            return any(
                entry.job.user_stop_receipt_order is not None
                and entry.job.adapter.get("source_event_id") == source_event_id
                and (entry.job.owner.transport_agent_name or entry.job.owner.agent_name) == transport_agent_name
                for entry in self._entries.values()
            )

    async def cancel_owned(
        self,
        job_id: str,
        *,
        matches: Callable[[BackgroundJob], bool],
    ) -> BackgroundJob | None:
        """Settle retained adapter ownership during internal cleanup after authority revocation."""
        async with self._lock:
            if self._closed or self._shutdown_task is not None:
                return None
            entry = self._entries.get(job_id)
            if entry is None or not matches(await self._snapshot(entry)):
                return None
            task = self._cancellation_task(entry)
        return await wait_for_future_until_complete(task)

    async def cancel_revoked(self) -> None:
        """Withdraw execution when current grants disappear, retaining owned cleanup."""
        async with self._lock:
            self._ensure_accepting()
            cancellations = [
                (entry, self._cancellation_task(entry))
                for entry in self._entries.values()
                if entry.job.status not in _TERMINAL and not self._allowed(entry.job)
            ]
        for entry, task in cancellations:
            await self._cancel_admitted(entry, task)

    def _cancellation_task(self, entry: _Entry) -> asyncio.Task[BackgroundJob]:
        """Accept exactly one cleanup while the caller holds the runtime admission lock."""
        task = entry.cancel_task
        failed = task is not None and task.done() and (task.cancelled() or task.exception() is not None)
        if task is None or failed:
            entry.cancel_ready = asyncio.Event()
            task = asyncio.create_task(self._cancel_entry(entry))
            entry.cancel_task = task
        return task

    async def _cancel_entry(self, entry: _Entry) -> BackgroundJob:
        async with self._lock:
            if entry.job.status in _TERMINAL:
                if entry.cancel_settlement_pending:
                    await self._persist(entry)
                    self._release_control(entry)
                snapshot = await self._snapshot(entry)
                entry.cancel_task = None
                return snapshot
            previous = replace(entry.job)
            previous_token = entry.wait_token
            if entry.job.status == "awaiting_approval":
                entry.job.generation += 1
                entry.job.wait_acknowledged = False
                entry.wait_token = None
            entry.job.status = "cancel_requested"
            try:
                await self._persist(entry)
            except BaseException:
                saved = await asyncio.to_thread(read_job_snapshot, self._path(entry.job.job_id))
                if (saved.status, saved.generation) == (previous.status, previous.generation):
                    entry.job = previous
                    entry.wait_token = previous_token
                    self._index_consumption(entry)
                raise
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
            entry.cancel_settlement_pending = True
            await self._persist(entry)
            self._release_control(entry)
            snapshot = await self._snapshot(entry)
            entry.cancel_task = None
            self._cool(entry)
            return snapshot

    async def _cleanup(self, entry: _Entry) -> BackgroundOutcome | None:
        try:
            outcome = await entry.cancel(entry.job) if entry.cancel is not None else None
            if outcome is None and self._cancel is not None:
                outcome = await self._cancel(entry.job)
        except JobRecoveryBlockedError:
            raise
        except Exception as error:
            return BackgroundOutcome(
                "failed",
                f"Job cleanup failed; external side effects may still be active: {error}",
            )
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
        expected_generation: int | None,
        operation: Callable[[], Awaitable[BackgroundOutcome]],
        adapter: dict[str, Any] | None = None,
    ) -> BackgroundJob:
        """Continue the same job after its native approval has been resolved."""
        async with self._lock:
            self._ensure_accepting()
            entry = self._entry(job_id, owner, depth)
            if (
                entry.job.status != "awaiting_approval"
                or entry.job.generation != expected_generation
                or entry.job.user_stop_receipt_order is not None
            ):
                msg = "Approval no longer applies: tool job is not awaiting this approval generation."
                raise JobContinuationError(msg)
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
                    entry.human_signal.subscribe(entry.notify_changed)
            await run_coroutine_until_complete(
                self._persist_and_launch(entry, operation, previous=(previous, previous_token)),
            )
            return await self._snapshot(entry)

    def _unconsumed(self, entry: _Entry) -> bool:
        return (
            not entry.job.wait_acknowledged
            and entry.job.user_stop_receipt_order is None
            and entry.wait_token is None
            and self._allowed(entry.job)
        )

    async def pending_outcomes(self) -> list[BackgroundJob]:
        """Return authorized ready generations that have no durable consumer receipt."""
        async with self._lock:
            if self._closed:
                return []
            return [
                await self._snapshot(entry, include_result=False)
                for job_id in tuple(self._unacknowledged)
                if (entry := self._entries[job_id]).job.status in _READY and self._unconsumed(entry)
            ]

    async def outcome(self, job_id: str, generation: int) -> BackgroundJob | None:
        """Revalidate one unconsumed generation at its serialized response boundary."""
        async with self._lock:
            entry = self._entries.get(job_id)
            if (
                self._closed
                or entry is None
                or entry.job.generation != generation
                or entry.job.status not in _READY
                or not self._unconsumed(entry)
            ):
                return None
            return await self._snapshot(entry, include_result=False)

    async def source_jobs(
        self,
        source_event_id: str,
        *,
        transport_agent_name: str,
        room_id: str,
        thread_id: str | None,
        session_id: str,
        requester_id: str,
    ) -> list[BackgroundJob]:
        """Recognize exact accepted source ownership even after result access is revoked.

        This internal recovery lookup prevents side-effect replay; callers must
        retrieve result data through the authorized native wait boundary.
        """
        async with self._lock:
            if self._closed:
                return []
            return [
                await self._snapshot(entry, include_result=False)
                for entry in self._entries.values()
                if entry.job.adapter.get("source_event_id") == source_event_id
                and (entry.job.owner.transport_agent_name or entry.job.owner.agent_name) == transport_agent_name
                and entry.job.owner.room_id == room_id
                and entry.job.owner.resolved_thread_id == thread_id
                and entry.job.owner.session_id == session_id
                and entry.job.owner.requester_id == requester_id
            ]

    async def conversation_jobs(
        self,
        *,
        transport_agent_name: str,
        room_id: str,
        thread_id: str | None,
        requester_id: str,
        source_kind: str | None = None,
    ) -> list[BackgroundJob]:
        """Discover outstanding work for one requester, conversation, and delivery policy."""
        async with self._lock:
            if self._closed:
                return []
            return [
                await self._snapshot(entry, include_result=False)
                for entry in self._entries.values()
                if (entry.job.owner.transport_agent_name or entry.job.owner.agent_name) == transport_agent_name
                and entry.job.owner.room_id == room_id
                and entry.job.owner.resolved_thread_id == thread_id
                and entry.job.owner.requester_id == requester_id
                and (entry.job.adapter.get("source_kind") == SILENT_SCHEDULE_SOURCE_KIND)
                == (source_kind == SILENT_SCHEDULE_SOURCE_KIND)
                and self._unconsumed(entry)
            ]

    async def expire_consumed(
        self,
        *,
        before: datetime,
        source_finished: Callable[[BackgroundJob], Awaitable[bool]],
    ) -> None:
        """Replace old consumed results with receipts after their response ownership settles."""
        async with self._lock:
            if self._closed:
                return
            candidates = [
                await self._snapshot(entry, include_result=False)
                for entry in self._entries.values()
                if entry.job.status in _TERMINAL
                and entry.job.wait_acknowledged
                and not entry.job.result_expired
                and entry.wait_token is None
                and entry.saved
                and datetime.fromisoformat(entry.job.updated_at) < before
            ]
        for job in candidates:
            if not await source_finished(job):
                continue
            async with self._lock:
                if self._closed:
                    return
                entry = self._entries[job.job_id]
                if entry.wait_token is not None or entry.job.updated_at != job.updated_at or not entry.saved:
                    continue
                entry.job = replace(
                    entry.job,
                    adapter=self._compact_adapter(entry.job.adapter),
                    result="Tool result expired after 30 days; its execution receipt prevents replay.",
                    result_payload=None,
                    approval_state={},
                    result_expired=True,
                )
                entry.cold = False
                await self._persist(entry, update_timestamp=False)

    @staticmethod
    def _compact_adapter(adapter: dict[str, Any]) -> dict[str, Any]:
        """Retain replay and access evidence without the original operation's input."""
        compact = {key: value for key, value in adapter.items() if key != "arguments"}
        if "child" in compact:
            compact["child"] = {**compact["child"], "task": ""}
        return compact

    async def quiesce(self) -> None:
        """Drain owned execution while response finalizers retain result receipt access."""
        if self._shutdown_task is None:
            self.changed.set()
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await wait_for_future_until_complete(self._shutdown_task)

    async def shutdown(self) -> None:
        """Close storage after execution and all remaining response owners have drained."""
        try:
            await self.quiesce()
        finally:
            await run_coroutine_until_complete(self._close())

    async def _close(self) -> None:
        """Serialize lease release after every admitted receipt write."""
        async with self._lock:
            self._closed = True
            self.changed.set()
            self._lease.close()

    async def _shutdown(self) -> None:  # noqa: C901 - Drain execution and retry unsaved outcomes before releasing storage.
        tasks = []
        cancellations = []
        failures = []
        async with self._lock:
            for entry in self._entries.values():
                if entry.job.status not in _READY:
                    entry.stopping = True
                    entry.control.cancel()
                self._release_control(entry)
                cancellation = entry.cancel_task
                cancellation_is_live = cancellation is not None and not cancellation.done()
                if cancellation_is_live:
                    cancellations.append(cancellation)
                task = entry.task
                if task is not None and not task.done():
                    if not cancellation_is_live:
                        task.cancel()
                    tasks.append(task)
        await asyncio.gather(*tasks, *cancellations, return_exceptions=True)
        for entry in self._entries.values():
            settling = entry.job.status not in _READY
            outcome = await self._cleanup(entry) if settling else None
            async with self._lock:
                if settling:
                    self._settle_stopped(
                        entry,
                        status="interrupted",
                        reason="Tool execution was interrupted by runtime shutdown; it was not replayed.",
                        outcome=outcome,
                    )
                try:
                    if settling or not entry.saved:
                        await self._persist(entry, update_timestamp=settling)
                except Exception as error:
                    failures.append(error)
        if failures:
            msg = "Tool job shutdown persistence failed"
            raise ExceptionGroup(msg, failures)
