"""Durable execution ownership, interruptible waits, and result consumption for application tool jobs."""

from __future__ import annotations

import asyncio
import fcntl
import json
import re
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from operator import attrgetter
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
from mindroom.tool_jobs.instances import tool_job_instance
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.wait_timeout import validate_wait_timeout
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping
    from pathlib import Path

    from agno.tools.function import Function

    from mindroom.constants import RuntimePaths

type _OutcomeStatus = Literal["awaiting_approval", "completed", "failed", "cancelled", "denied", "interrupted"]
type _BackgroundStatus = Literal["running", "cancel_requested"] | _OutcomeStatus
# Statuses after which execution has ended for good.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "denied", "interrupted"})
# Statuses whose outcome a parent can retrieve: a terminal one, or an approval that pauses execution.
READY_STATUSES = TERMINAL_STATUSES | {"awaiting_approval"}
_UNAVAILABLE = "Tool job is not available in this conversation."
_JOB_SUMMARY_MAX_CHARS = 500
_SNAPSHOT_SCHEMA_VERSION = 7
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
# The internal source that delivers one job generation's outcome; parsing accepts exactly what the builder emits.
_COMPLETION_EVENT_ID = re.compile(rf"tool-job:(?P<job_id>{_JOB_ID.pattern}):(?P<generation>0|[1-9][0-9]*)")
_PAYLOAD_SUFFIX = ".result.json"
# A reattaching call must name exactly the operation its job was admitted for.
_ADMITTED_OPERATION = attrgetter("tool_name", "toolkit_name", "kind", "source_event_id", "source_kind", "adapter")
# Consumed jobs are deleted this long after their last change, once their originating turn has finished.
CONSUMED_RESULT_RETENTION = timedelta(days=30)
logger = get_logger(__name__)
# A ToolResultPayload encoded by `tool_jobs.results`; the runtime stores it without reading it.
type EncodedResultPayload = dict[str, Any]
# An accepted job's execution, and the adapter cleanup that settles it after cancellation or interruption.
type _Operation = Callable[[], Awaitable[BackgroundOutcome]]
type _Cleanup = Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]]


class JobAccessError(ValueError):
    """The requested job is unavailable to this caller or runtime."""


class UnsupportedToolJobSnapshotError(ValueError):
    """A saved job uses a snapshot schema this runtime does not read."""


class JobRecoveryBlockedError(RuntimeError):
    """Another executor still owns work that this runtime cannot safely settle."""


class JobContinuationError(ValueError):
    """A presented approval no longer owns the current job generation."""


@dataclass
class BackgroundOutcome:
    """Serializable operation outcome; approval semantics remain owned by its adapter."""

    status: _OutcomeStatus
    result: str | None = None
    approval_state: dict[str, Any] = field(default_factory=dict)
    # The adapter's full result, saved in this generation's own file; the job keeps only a summary of `result`.
    result_payload: EncodedResultPayload | None = None


@dataclass(frozen=True)
class BackgroundJob:
    """Durable execution and result, scoped to one conversation and requester."""

    job_id: str
    owner: ToolExecutionIdentity
    tool_name: str
    depth: int
    kind: str = "tool"
    toolkit_name: str | None = None
    # The admitted turn whose run started this job, and that turn's ingress source kind; None outside a turn.
    source_event_id: str | None = None
    source_kind: str | None = None
    adapter: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: _BackgroundStatus = "running"
    # At most _JOB_SUMMARY_MAX_CHARS of the outcome text; an adapter's payload keeps its full result.
    result: str | None = None
    summary_truncated: bool = False
    # The generation whose outcome saved a payload file; an outcome the runtime authored has only its summary.
    payload_generation: int | None = None
    approval_state: dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    # The generation whose outcome a parent run saved as its tool result, and the admitted turn that first saved it.
    consumed_generation: int | None = None
    consumed_by_source: str | None = None
    user_stop_receipt_order: int | None = None

    @property
    def has_result_payload(self) -> bool:
        """Whether the current generation's outcome has a payload file; a newer generation has none until it settles."""
        return self.payload_generation == self.generation

    @property
    def consumed(self) -> bool:
        """Whether a parent run saved the current generation's outcome; a newer generation starts unconsumed."""
        return self.consumed_generation == self.generation

    @property
    def consuming_source(self) -> str | None:
        """The admitted turn whose saved run first consumed the current generation's outcome."""
        return self.consumed_by_source if self.consumed else None


@dataclass(frozen=True)
class JobClaim:
    """One waiter's exclusive right to consume a generation's outcome until it acknowledges or releases it."""

    generation: int
    nonce: str


def _completion_source(job_id: str, generation: int) -> str:
    return f"tool-job:{job_id}:{generation}"


def completion_event_id(job: BackgroundJob) -> str:
    """Name the internal source that delivers one job generation's outcome, stable across worker and process retries."""
    return _completion_source(job.job_id, job.generation)


def parse_completion_event_id(event_id: str) -> tuple[str, int] | None:
    """Return the job ID and generation an internal completion source names, or None for any other event."""
    match = _COMPLETION_EVENT_ID.fullmatch(event_id)
    return None if match is None else (match["job_id"], int(match["generation"]))


def _updated(job: BackgroundJob, **changes: object) -> BackgroundJob:
    """Return the next state of a job, stamped with its transition time."""
    return replace(job, **changes, updated_at=datetime.now(UTC).isoformat())


def _cancel_requested(job: BackgroundJob) -> BackgroundJob:
    """Request cancellation; a cancelled approval gets a fresh unconsumed generation that stale claims cannot own."""
    paused = job.status == "awaiting_approval"
    return _updated(job, status="cancel_requested", generation=job.generation + 1 if paused else job.generation)


def _payload_name(job_id: str, generation: int) -> str:
    return f"{job_id}.g{generation}{_PAYLOAD_SUFFIX}"


def _unlink(paths: Iterable[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def saved_job_paths(root: Path) -> list[Path]:
    """List saved job metadata, leaving out the payload files it references."""
    return sorted(path for path in root.glob("*.json") if not path.name.endswith(_PAYLOAD_SUFFIX))


def read_job_snapshot(path: Path) -> BackgroundJob:
    """Validate one existing snapshot without claiming or changing its execution."""
    if path.is_symlink() or _JOB_ID.fullmatch(path.stem) is None:
        raise JobAccessError(_UNAVAILABLE)
    payload = json.loads(path.read_text())
    version = payload.pop("schema_version", None) if isinstance(payload, dict) else None
    if version != _SNAPSHOT_SCHEMA_VERSION:
        msg = f"Unsupported tool job snapshot {path} (schema_version={version}); remove it to continue."
        raise UnsupportedToolJobSnapshotError(msg)
    if payload["job_id"] != path.stem or not all(
        isinstance(payload.get(key), str | None) for key in ("source_event_id", "source_kind")
    ):
        msg = "Invalid tool job snapshot."
        raise ValueError(msg)
    payload["owner"] = parse_tool_execution_identity_payload(payload["owner"], strict=True)
    return BackgroundJob(**payload)


def job_summary(job: BackgroundJob) -> dict[str, Any]:
    """Describe a job for the model with its bounded outcome summary and, for a subagent, its reusable ID."""
    summary: dict[str, Any] = {
        "job_id": job.job_id,
        "tool": job.tool_name,
        "status": job.status,
        "summary": job.result,
        "summary_truncated": job.summary_truncated,
    }
    subagent_id = job.adapter.get("child", {}).get("subagent_id") if job.kind == "delegation" else None
    if subagent_id:
        summary["subagent_id"] = subagent_id
    return summary


def format_job_handle(job: BackgroundJob) -> str:
    """Return the job's summary as the machine-readable handle a detached or queued call returns."""
    return json.dumps(job_summary(job))


@dataclass(frozen=True)
class JobWait:
    """A waited job, with the claim on its ready outcome that only the parent's saved tool result acknowledges."""

    job: BackgroundJob
    claim: JobClaim | None = None


@dataclass
class _Entry:
    job: BackgroundJob
    control: JobControl = field(default_factory=JobControl)
    human_signal: HumanMessageSignal | None = None
    task: asyncio.Task[None] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    claim: JobClaim | None = None
    cancel: _Cleanup | None = None
    # False only while memory retains an outcome of work that already ran but could not be saved.
    saved: bool = True
    # That unsaved outcome's payload, kept only until a retried save writes its file.
    unsaved_payload: EncodedResultPayload | None = None
    stopped_outcome: BackgroundOutcome | None = None
    # The one in-flight cancellation drain; Stop and revocation do not await it, so a done callback logs its failure.
    drain: asyncio.Task[BackgroundJob] | None = None

    def notify_changed(self) -> None:
        """Wake existing state waiters while keeping the next wait fresh."""
        self.changed.set()
        self.changed = asyncio.Event()

    @property
    def live_claim(self) -> JobClaim | None:
        """The claim on the current generation; a claim on an earlier generation owns nothing."""
        return self.claim if self.claim is not None and self.claim.generation == self.job.generation else None

    def claim_for(self, claim: JobClaim | None) -> JobClaim | None:
        """Keep a waiter's live claim or claim an unclaimed generation for it; None while another waiter holds it."""
        live = self.live_claim
        if live is None:
            self.claim = JobClaim(self.job.generation, uuid4().hex)
            return self.claim
        return live if live == claim else None


def _conversation_key(owner: ToolExecutionIdentity) -> tuple[str, str | None, str | None, str | None]:
    """A job's recipient, room, resolved thread, and requester."""
    return (owner.recipient, owner.room_id, owner.resolved_thread_id, owner.requester_id)


@dataclass
class _EntryIndex[Key]:
    """Accepted jobs grouped by an identity that stays fixed for each job's life, in admission order."""

    groups: dict[Key, dict[str, _Entry]] = field(default_factory=dict)

    def add(self, key: Key, entry: _Entry) -> None:
        self.groups.setdefault(key, {})[entry.job.job_id] = entry

    def remove(self, key: Key, job_id: str) -> None:
        group = self.groups[key]
        del group[job_id]
        if not group:
            del self.groups[key]

    def get(self, key: Key) -> list[_Entry]:
        return list(self.groups.get(key, {}).values())


def _report_failed_drain(job_id: str, drain: asyncio.Task[BackgroundJob]) -> None:
    """Log a failed cancellation drain with its job, which stays for a later canceller, recovery, or shutdown."""
    if not drain.cancelled() and (error := drain.exception()) is not None:
        logger.error("Tool job cancellation drain failed", job_id=job_id, exc_info=error)


def get_background_runtime(runtime_paths: RuntimePaths) -> ToolJobRuntime | None:
    """Find the managed Matrix runtime for one storage root, if present."""
    return instance.runtime if (instance := tool_job_instance(runtime_paths)) is not None else None


def register_background_runtime(runtime_paths: RuntimePaths, runtime: ToolJobRuntime) -> None:
    """Publish the recovered runtime of the instance pinned for its storage root; releasing the instance withdraws it."""
    instance = tool_job_instance(runtime_paths)
    if instance is None:
        msg = "Pin background tool jobs for this storage root before publishing its runtime."
        raise RuntimeError(msg)
    instance.runtime = runtime


class ToolJobRuntime:
    """Keep tool execution alive while individual foreground waiters come and go."""

    def __init__(
        self,
        storage_root: Path,
        *,
        authorize: Callable[[BackgroundJob], bool],
        authorize_execution: Callable[[ToolExecutionIdentity, Function, Mapping[str, Any]], None],
        cancel: _Cleanup,
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
        # Rechecks a retained function's current authority immediately before application entry; raises if revoked.
        self.authorize_execution = authorize_execution
        self._cancel = cancel
        self._entries: dict[str, _Entry] = {}
        # Per-turn lookups read these instead of scanning every entry; `_add_entry` and `_remove_entry` keep them.
        # Sources are keyed by a job's recipient and the turn that started it.
        self._by_source = _EntryIndex[tuple[str, str]]()
        self._by_conversation = _EntryIndex[tuple[str, str | None, str | None, str | None]]()
        self._lock = asyncio.Lock()
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self.changed = asyncio.Event()
        self._human_signals = WeakValueDictionary[tuple[str, str, str | None], HumanMessageSignal]()

    def human_signal_for(self, transport_agent_name: str, room_id: str, thread_id: str | None) -> HumanMessageSignal:
        """Retain one conversation signal while a runner or background job uses it."""
        return self._human_signals.setdefault((transport_agent_name, room_id, thread_id), HumanMessageSignal())

    def _path(self, job_id: str, generation: int | None = None) -> Path:
        """Locate a job's metadata, or the payload of one of its generations."""
        path = self._root / (f"{job_id}.json" if generation is None else _payload_name(job_id, generation))
        if _JOB_ID.fullmatch(job_id) is None or path.is_symlink():
            raise JobAccessError(_UNAVAILABLE)
        return path

    @staticmethod
    def _owner(owner: ToolExecutionIdentity) -> ToolExecutionIdentity:
        return replace(owner, thread_id=owner.resolved_thread_id)

    def _ensure_open(self, *, accepting: bool = False) -> None:
        """Refuse calls once closed, and calls that start or cancel execution (`accepting`) once shutdown began."""
        if self._closed or (accepting and self._shutdown_task is not None):
            msg = "Tool job runtime is closed." if self._closed else "Tool job runtime is shutting down."
            raise JobAccessError(msg)

    def has_job(self, job_id: str) -> bool:
        """Recognize accepted ownership; access still requires an authorized lookup."""
        return job_id in self._entries

    def source_event_id(self, job_id: str) -> str | None:
        """Read accepted provenance for internal Stop ancestry without acquiring the admission lock."""
        return entry.job.source_event_id if (entry := self._entries.get(job_id)) is not None else None

    def _add_entry(self, entry: _Entry) -> None:
        """Make an accepted job current and findable by its source and conversation, which never change."""
        job = entry.job
        self._entries[job.job_id] = entry
        if job.source_event_id is not None:
            self._by_source.add((job.owner.recipient, job.source_event_id), entry)
        self._by_conversation.add(_conversation_key(job.owner), entry)

    def _remove_entry(self, job_id: str) -> None:
        job = self._entries.pop(job_id).job
        if job.source_event_id is not None:
            self._by_source.remove((job.owner.recipient, job.source_event_id), job_id)
        self._by_conversation.remove(_conversation_key(job.owner), job_id)

    def _visible(self, entry: _Entry, owner: ToolExecutionIdentity, depth: int) -> bool:
        """Whether a caller's conversation, requester, and depth own a job it may still access."""
        job = entry.job
        return bool(owner.session_id) and (job.owner, job.depth) == (self._owner(owner), depth) and self._authorize(job)

    def _entry(self, job_id: str, owner: ToolExecutionIdentity, depth: int) -> _Entry:
        self._ensure_open()
        entry = self._entries.get(job_id)
        if entry is None or not self._visible(entry, owner, depth):
            raise JobAccessError(_UNAVAILABLE)
        return entry

    async def _publish(self, entry: _Entry, job: BackgroundJob, payload: EncodedResultPayload | None = None) -> None:
        """Durably save the job, then make it current; a failed write leaves memory unchanged.

        Its payload, new or still unsaved, lands before the metadata referencing it; the replaced payload goes after.
        """
        if payload is None and job.has_result_payload:
            payload = entry.unsaved_payload
        path = self._path(job.job_id)
        payload_path = self._path(job.job_id, job.generation) if payload is not None else None
        previous = entry.job
        stale = previous.has_result_payload and not (job.has_result_payload and job.generation == previous.generation)
        replaced = self._path(job.job_id, previous.generation) if stale else None

        def write() -> None:
            if payload_path is not None:
                write_json_file_durable(payload_path, payload, strict_atomic_replace=True)
            snapshot = {"schema_version": _SNAPSHOT_SCHEMA_VERSION, **asdict(job)}
            write_json_file_durable(path, snapshot, strict_atomic_replace=True)
            if replaced is not None:
                replaced.unlink(missing_ok=True)

        async def publish() -> None:
            await asyncio.to_thread(write)
            entry.job, entry.saved, entry.unsaved_payload = job, True, None
            if job.status in TERMINAL_STATUSES:
                # A durable terminal outcome ends execution; drop what only running work needed.
                self._release_control(entry)
                entry.human_signal, entry.cancel, entry.task = None, None, None
            entry.notify_changed()
            self.changed.set()

        # A cancelled caller cannot separate a landed write from its in-memory publication.
        await run_coroutine_until_complete(publish())

    async def _publish_outcome(self, entry: _Entry, outcome: BackgroundOutcome) -> None:
        """Publish work that already ran with a bounded summary; a failed save keeps it for a later publish to retry."""
        text = outcome.result
        job = _updated(
            entry.job,
            status=outcome.status,
            result=text[:_JOB_SUMMARY_MAX_CHARS] if text is not None else None,
            summary_truncated=text is not None and len(text) > _JOB_SUMMARY_MAX_CHARS,
            payload_generation=entry.job.generation if outcome.result_payload is not None else None,
            approval_state=outcome.approval_state,
        )
        try:
            await self._publish(entry, job, outcome.result_payload)
        except Exception:
            entry.job, entry.saved, entry.unsaved_payload = job, False, outcome.result_payload
            entry.notify_changed()
            self.changed.set()
            raise

    async def read_payload(self, job: BackgroundJob) -> EncodedResultPayload:
        """Read a snapshot's payload outside the lock; one a newer generation or expiry deleted since is unavailable."""
        self._ensure_open()
        entry = self._entries.get(job.job_id)
        unsaved = entry.unsaved_payload if entry is not None and entry.job.generation == job.generation else None
        if unsaved is not None:
            return await asyncio.to_thread(lambda: deepcopy(unsaved))
        path = self._path(job.job_id, job.generation)
        try:
            return await asyncio.to_thread(lambda: json.loads(path.read_text()))
        except FileNotFoundError:
            raise JobAccessError(_UNAVAILABLE) from None

    async def recover(self) -> None:
        """Restore outcomes and approval snapshots, never automatically replay execution."""
        async with self._lock:
            self._ensure_open()
            for path in await asyncio.to_thread(saved_job_paths, self._root):
                if path.stem in self._entries:
                    continue
                job = await asyncio.to_thread(read_job_snapshot, path)
                # A Stopped approval can never continue; a Stop during shutdown left its cancellation to recovery.
                stopped = job.status == "awaiting_approval" and job.user_stop_receipt_order is not None
                entry = _Entry(_cancel_requested(job) if stopped else job)
                if entry.job.status not in READY_STATUSES:
                    reason = "Tool execution was interrupted by a runtime restart; it was not replayed."
                    default = BackgroundOutcome("cancelled") if stopped else BackgroundOutcome("interrupted", reason)
                    await self._publish_outcome(entry, self._settled(entry, await self._cleanup(entry), default))
                self._add_entry(entry)
                owner = entry.job.owner
                if entry.job.status == "awaiting_approval" and owner.room_id is not None:
                    # Observe future human ingress while the recovered approval awaits reattachment.
                    entry.human_signal = self.human_signal_for(owner.recipient, owner.room_id, owner.resolved_thread_id)
                    entry.human_signal.subscribe(entry.notify_changed)
            # A crash can leave a payload no saved metadata references, such as one whose metadata save never landed.
            jobs = [entry.job for entry in self._entries.values() if entry.job.has_result_payload]
            referenced = {self._root / _payload_name(job.job_id, job.generation) for job in jobs}
            await asyncio.to_thread(lambda: _unlink(set(self._root.glob(f"*{_PAYLOAD_SUFFIX}")) - referenced))

    async def start(
        self,
        job_id: str,
        *,
        tool_name: str,
        depth: int,
        kind: str = "tool",
        toolkit_name: str | None = None,
        source_event_id: str | None = None,
        source_kind: str | None = None,
        adapter: dict[str, Any],
        owner: ToolExecutionIdentity,
        operation: _Operation,
        cancel: _Cleanup | None = None,
        reattach: bool = False,
    ) -> tuple[BackgroundJob, JobClaim | None]:
        """Durably accept exact operation ownership before spawning execution, claiming its outcome for the caller.

        The claim is taken after the last await, so a cancelled start leaves no claim its caller never received.
        """
        async with self._lock:
            self._ensure_open(accepting=True)
            job = BackgroundJob(
                job_id=job_id,
                owner=self._owner(owner),
                tool_name=tool_name,
                depth=depth,
                kind=kind,
                toolkit_name=toolkit_name,
                source_event_id=source_event_id,
                source_kind=source_kind,
                # Native adapters mutate this exact mapping; owns_execution verifies its identity.
                adapter=adapter,
            )
            if reattach and job_id in self._entries:
                entry = self._entry(job_id, owner, depth)
                if _ADMITTED_OPERATION(entry.job) != _ADMITTED_OPERATION(job):
                    raise JobAccessError(_UNAVAILABLE)
            else:
                if job_id in self._entries or self._path(job_id).exists():
                    msg = "Tool job already exists."
                    raise ValueError(msg)
                if not owner.session_id or depth < 0 or not self._authorize(job):
                    raise JobAccessError(_UNAVAILABLE)
                entry = _Entry(job, cancel=cancel)
                await run_coroutine_until_complete(self._admit(entry, job, operation))
            snapshot = await self._snapshot(entry)
            return snapshot, entry.claim_for(None)

    async def _admit(self, entry: _Entry, job: BackgroundJob, operation: _Operation) -> None:
        """Publish accepted ownership, then launch it; callers finish this before propagating cancellation."""
        await self._publish(entry, job)
        self._add_entry(entry)
        signal = current_human_message_signal()
        if entry.human_signal is None and signal is not None:
            entry.human_signal = signal
            signal.subscribe(entry.notify_changed)
        entry.task = asyncio.create_task(self._run(entry, operation), name=f"tool-job:{job.job_id}")

    def owns_execution(self, job_id: str, adapter: dict[str, Any]) -> bool:
        """Recognize exact accepted adapter ownership even if its former waiter was cancelled."""
        entry = self._entries.get(job_id)
        return entry is not None and entry.job.adapter is adapter and entry.task is not None

    async def _run(self, entry: _Entry, operation: _Operation) -> None:
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
            except asyncio.CancelledError:
                if (task := asyncio.current_task()) is not None and task.cancelling():
                    raise  # Requested cancellation: runtime-owned, or external teardown.
                # The operation raised cancellation itself; settle it so waiters and delivery see an outcome.
                outcome = BackgroundOutcome("cancelled")
            async with self._lock:
                if not entry.control.cancelled and entry.job.status not in TERMINAL_STATUSES:
                    try:
                        await self._publish_outcome(entry, outcome)
                    except Exception:
                        logger.exception(
                            "Tool job outcome save failed; retaining it in memory",
                            job_id=entry.job.job_id,
                        )
        finally:
            # Runtime-owned cancellation and shutdown settle with this outcome; external teardown leaves it to recovery.
            if entry.control.cancelled:
                entry.stopped_outcome = outcome
            if entry.job.status in TERMINAL_STATUSES:
                self._release_control(entry)

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

    async def _snapshot(self, entry: _Entry, *, include_result: bool = True) -> BackgroundJob:
        """Copy a job's metadata; without its result, the copy also omits a paused generation's approval state."""
        job = entry.job if include_result else replace(entry.job, approval_state={})
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
            entries = [entry for entry in self._entries.values() if self._visible(entry, owner, depth)]
            entries.sort(key=lambda entry: entry.job.updated_at, reverse=True)
            entries.sort(key=lambda entry: entry.job.status in TERMINAL_STATUSES)
            return [await self._snapshot(entry, include_result=False) for entry in entries[offset : offset + limit]]

    async def wait(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        timeout: float | None = None,  # noqa: ASYNC109
        claim: JobClaim | None = None,
    ) -> JobWait:
        """Wait without cancelling execution, keeping or taking the ready outcome's claim unless another waiter has."""
        timeout = validate_wait_timeout(timeout)
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        retained = False
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            claim = entry.claim_for(claim)
            if claim is None:
                return JobWait(await self._snapshot(entry))
            human_notified = asyncio.Event()

            def notify_human() -> None:
                human_notified.set()
                entry.notify_changed()

            human_signal = entry.human_signal
            if human_signal is not None:
                human_signal.subscribe(notify_human)
        try:
            while True:
                async with self._lock:
                    self._entry(job_id, owner, depth)
                    if entry.job.status in READY_STATUSES:
                        # A continuation or cancellation can start a newer generation before this waiter sees the one
                        # it claimed; that stale claim owns nothing, so claim the ready one unless another waiter has.
                        claim = entry.claim_for(claim)
                        snapshot = await self._snapshot(entry)
                        retained = claim is not None
                        return JobWait(snapshot, claim)
                    remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
                    if human_notified.is_set() or (remaining is not None and remaining <= 0):
                        return JobWait(await self._snapshot(entry))
                    changed = entry.changed
                with suppress(TimeoutError):
                    await asyncio.wait_for(changed.wait(), remaining)
        finally:
            if human_signal is not None:
                human_signal.unsubscribe(notify_human)
            if not retained:
                await self.release_wait(job_id, claim)

    async def release_wait(self, job_id: str, claim: JobClaim | None) -> None:
        """Release an unpersisted claim so completion can still be delivered, even if the caller is cancelled again."""

        async def release() -> None:
            async with self._lock:
                entry = self._entries.get(job_id)
                if entry is not None and claim is not None and entry.claim == claim:
                    entry.claim = None
                    self.changed.set()

        await run_coroutine_until_complete(release())

    async def acknowledge_wait(
        self,
        job_id: str,
        claim: JobClaim | None,
        *,
        source_event_id: str | None = None,
    ) -> None:
        """Mark the claimed generation consumed, only after the exact parent tool result has been durably saved."""
        async with self._lock:
            self._ensure_open()
            entry = self._entries[job_id]
            if claim is None or entry.live_claim != claim:
                msg = "Tool job wait claim no longer belongs to this waiter."
                raise ValueError(msg)
            # A reread keeps the first consumer, whose unfinished reply still owns recovering this outcome.
            source = entry.job.consumed_by_source if entry.job.consumed else source_event_id
            await self._publish(
                entry,
                _updated(entry.job, consumed_generation=claim.generation, consumed_by_source=source),
            )
            entry.claim = None

    async def cancel(self, job_id: str, *, owner: ToolExecutionIdentity, depth: int) -> BackgroundJob:
        """Cancel execution and return the job once its execution and cleanup have settled."""
        async with self._lock:
            self._ensure_open(accepting=True)
            entry = self._entry(job_id, owner, depth)
            drain = await self._request_cancel(entry)
        return await wait_for_future_until_complete(drain)

    async def stop_jobs(self, *, receipt_order: int, matches: Callable[[BackgroundJob], Awaitable[bool]]) -> None:
        """Persist explicit Stop apart from result consumption, then request cleanup unless shutdown settles it."""

        def newer(job: BackgroundJob) -> bool:
            return job.user_stop_receipt_order is None or job.user_stop_receipt_order < receipt_order

        async def stop(entry: _Entry) -> None:
            if newer(entry.job):
                await self._publish(entry, _updated(entry.job, user_stop_receipt_order=receipt_order))
            if self._shutdown_task is None and entry.job.status not in TERMINAL_STATUSES:
                await self._request_cancel(entry)

        async with self._lock:
            self._ensure_open()
            candidates = [
                (entry, await self._snapshot(entry, include_result=False))
                for entry in self._entries.values()
                if newer(entry.job) or entry.job.status not in TERMINAL_STATUSES
            ]
        # `matches` may read the journal, so it judges snapshots outside the runtime lock.
        selected = [entry for entry, job in candidates if await matches(job)]
        async with self._lock:
            self._ensure_open()
            current = (entry for entry in selected if self._entries.get(entry.job.job_id) is entry)
            failures = await self._isolated(current, stop, "Tool job Stop failed")
        if failures:
            msg = "Tool job Stop failed"
            raise ExceptionGroup(msg, failures)

    @staticmethod
    async def _isolated(
        entries: Iterable[_Entry],
        action: Callable[[_Entry], Awaitable[object]],
        failure: str,
    ) -> list[Exception]:
        """Apply an action to each job, logging and returning failures so one job cannot block the rest."""
        failures = []
        for entry in entries:
            try:
                await action(entry)
            except Exception as error:
                logger.exception(failure, job_id=entry.job.job_id)
                failures.append(error)
        return failures

    async def is_user_stopped(self, job_id: str) -> bool:
        """Check suppression without confusing a read receipt with explicit user intent."""
        async with self._lock:
            return (entry := self._entries.get(job_id)) is not None and entry.job.user_stop_receipt_order is not None

    async def is_source_user_stopped(self, source_event_id: str, transport_agent_name: str) -> bool:
        """Recognize a stopped original response, including foreground approval recovery."""
        async with self._lock:
            return any(
                entry.job.user_stop_receipt_order is not None
                for entry in self._by_source.get((transport_agent_name, source_event_id))
            )

    async def cancel_owned(self, job_id: str, *, matches: Callable[[BackgroundJob], bool]) -> BackgroundJob | None:
        """Settle retained adapter ownership during internal cleanup after authority revocation."""
        async with self._lock:
            if self._closed or self._shutdown_task is not None:
                return None
            entry = self._entries.get(job_id)
            if entry is None or not matches(await self._snapshot(entry, include_result=False)):
                return None
            drain = await self._request_cancel(entry)
        return await wait_for_future_until_complete(drain)

    async def cancel_revoked(self, *, denied: Callable[[BackgroundJob], bool]) -> None:
        """Withdraw execution whose grant is proven revoked, retaining owned cleanup; a failed job retries next pass."""
        async with self._lock:
            self._ensure_open(accepting=True)
            revoked = [
                entry
                for entry in self._entries.values()
                if entry.job.status not in TERMINAL_STATUSES and denied(entry.job)
            ]
            await self._isolated(revoked, self._request_cancel, "Tool job revocation failed")

    async def _request_cancel(self, entry: _Entry) -> asyncio.Task[BackgroundJob]:
        """Durably request cancellation and return its one drain; the caller holds the runtime lock."""

        async def request() -> asyncio.Task[BackgroundJob]:
            if entry.job.status in {"running", "awaiting_approval"}:
                await self._publish(entry, _cancel_requested(entry.job))
                entry.control.cancel()
                if entry.task is not None:
                    entry.task.cancel()
            if entry.drain is None:
                entry.drain = asyncio.create_task(self._drain_cancel(entry), name=f"tool-job-cancel:{entry.job.job_id}")
                entry.drain.add_done_callback(partial(_report_failed_drain, entry.job.job_id))
            return entry.drain

        # A cancelled caller cannot separate a durable request from stopping execution and starting its drain.
        return await run_coroutine_until_complete(request())

    async def _drain_cancel(self, entry: _Entry) -> BackgroundJob:
        """Await requested execution, settle it, and return the settled job."""
        try:
            if entry.task is not None:
                await asyncio.gather(entry.task, return_exceptions=True)
            await self._settle(entry, BackgroundOutcome("cancelled"))
            async with self._lock:
                return await self._snapshot(entry)
        finally:
            # A failed drain leaves the job for the next canceller, recovery, or shutdown to settle.
            entry.drain = None

    async def _settle(self, entry: _Entry, default: BackgroundOutcome) -> None:
        """Clean up stopped execution and publish its terminal outcome, or retry saving one that already settled."""
        settling = entry.job.status not in READY_STATUSES
        outcome = await self._cleanup(entry) if settling else None
        async with self._lock:
            if settling:
                await self._publish_outcome(entry, self._settled(entry, outcome, default))
            elif not entry.saved:
                await self._publish(entry, entry.job)

    async def _cleanup(self, entry: _Entry) -> BackgroundOutcome | None:
        try:
            outcome = await entry.cancel(entry.job) if entry.cancel is not None else None
            if outcome is None:
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
    def _settled(entry: _Entry, outcome: BackgroundOutcome | None, default: BackgroundOutcome) -> BackgroundOutcome:
        """Choose the terminal outcome, consuming the stopped one, after owned execution and cleanup settle."""
        stopped, entry.stopped_outcome = entry.stopped_outcome, None
        # A cleanup that completed, failed, or was denied outranks the terminal outcome execution stopped with.
        decided = outcome is not None and outcome.status in {"completed", "failed", "denied"}
        if not decided and stopped is not None and stopped.status in TERMINAL_STATUSES:
            outcome = stopped
        return outcome if outcome is not None and outcome.status in TERMINAL_STATUSES else default

    async def continue_job(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        expected_generation: int | None,
        operation: _Operation,
        adapter: dict[str, Any],
    ) -> BackgroundJob:
        """Continue the same job after its native approval has been resolved, with its updated adapter state."""
        async with self._lock:
            self._ensure_open(accepting=True)
            entry = self._entry(job_id, owner, depth)
            if (
                entry.job.status != "awaiting_approval"
                or entry.job.generation != expected_generation
                or entry.job.user_stop_receipt_order is not None
            ):
                msg = "Approval no longer applies: tool job is not awaiting this approval generation."
                raise JobContinuationError(msg)
            job = _updated(
                entry.job,
                adapter=adapter,
                status="running",
                generation=entry.job.generation + 1,
            )
            await run_coroutine_until_complete(self._admit(entry, job, operation))
            return await self._snapshot(entry)

    def _unconsumed(self, entry: _Entry, source_event_id: str | None = None) -> bool:
        """Whether no parent run consumed this generation, or only the reply of `source_event_id` did."""
        job = entry.job
        return (
            (not job.consumed or (source_event_id is not None and job.consuming_source == source_event_id))
            and job.user_stop_receipt_order is None
            and entry.live_claim is None
            and self._authorize(entry.job)
        )

    def _pending(self, entry: _Entry, source_event_id: str | None = None) -> bool:
        return entry.job.status in READY_STATUSES and self._unconsumed(entry, source_event_id)

    async def _find(
        self,
        entries: Callable[[], Iterable[_Entry]],
        matches: Callable[[_Entry], bool],
    ) -> list[BackgroundJob]:
        """Copy matching jobs without approval state, reading `entries` under the lock; a closed runtime has none."""
        async with self._lock:
            if self._closed:
                return []
            return [await self._snapshot(entry, include_result=False) for entry in entries() if matches(entry)]

    async def pending_outcomes(self) -> list[BackgroundJob]:
        """Return authorized ready generations that no parent run consumed and no waiter claims."""
        return await self._find(self._entries.values, self._pending)

    async def stoppable_jobs(self) -> list[BackgroundJob]:
        """Return jobs no Stop has marked whose execution or unconsumed outcome a saved Stop could still end."""
        return await self._find(
            self._entries.values,
            lambda entry: (
                entry.job.user_stop_receipt_order is None
                and (entry.job.status not in TERMINAL_STATUSES or not entry.job.consumed)
            ),
        )

    async def outcome(
        self,
        job_id: str,
        generation: int,
        *,
        source_event_id: str | None = None,
    ) -> BackgroundJob | None:
        """Revalidate an unread generation, or one the reply of `source_event_id` already consumed, at its boundary."""
        async with self._lock:
            entry = self._entries.get(job_id)
            if (
                self._closed
                or entry is None
                or entry.job.generation != generation
                or not self._pending(entry, source_event_id)
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
        """Recognize work a source started or consumed, even after result access is revoked, so recovery never replays it."""
        return await self._find(
            partial(self._by_conversation.get, (transport_agent_name, room_id, thread_id, requester_id)),
            lambda entry: (
                entry.job.owner.session_id == session_id
                and source_event_id in {entry.job.source_event_id, entry.job.consuming_source}
            ),
        )

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
        silent = source_kind == SILENT_SCHEDULE_SOURCE_KIND
        return await self._find(
            partial(self._by_conversation.get, (transport_agent_name, room_id, thread_id, requester_id)),
            lambda entry: (entry.job.source_kind == SILENT_SCHEDULE_SOURCE_KIND) == silent and self._unconsumed(entry),
        )

    async def expire_consumed(
        self,
        *,
        before: datetime,
        source_finished: Callable[[BackgroundJob], Awaitable[bool]],
    ) -> None:
        """Delete old consumed jobs whose originating turn has finished, which never lets their call run again.

        A claim is acknowledged only after the parent run saved a non-`None` result for the exact tool call, Agno runs
        a call again only when its saved result is `None`, and `source_finished` refuses while the job's session has an
        open approval continuation.
        A job also stays while jobs its completion turn started remain, because Stop traces them through it.
        """
        async with self._lock:
            if self._closed or self._shutdown_task is not None:
                return
            candidates = [
                await self._snapshot(entry, include_result=False)
                for entry in self._entries.values()
                if self._expirable(entry, before)
            ]
        for job in candidates:
            if not await source_finished(job):
                continue
            async with self._lock:
                if self._closed or self._shutdown_task is not None:
                    return
                entry = self._entries.get(job.job_id)
                if entry is None or entry.job.updated_at != job.updated_at or not self._expirable(entry, before):
                    continue
                files = [self._path(job.job_id)]
                if entry.job.has_result_payload:
                    files.append(self._path(job.job_id, entry.job.generation))
                self._remove_entry(job.job_id)
                # Metadata goes first, so a crash can leave only a payload, which recovery deletes.
                await run_blocking_until_complete(_unlink, files)

    def _expirable(self, entry: _Entry, before: datetime) -> bool:
        job = entry.job
        return (
            job.status in TERMINAL_STATUSES
            and job.consumed
            and entry.live_claim is None
            and entry.saved
            and datetime.fromisoformat(job.updated_at) < before
            and not any(
                (job.owner.recipient, _completion_source(job.job_id, generation)) in self._by_source.groups
                for generation in range(job.generation + 1)
            )
        )

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

    async def _shutdown(self) -> None:
        """Drain execution and retry unsaved outcomes before releasing storage."""
        tasks = []
        failures = []
        async with self._lock:
            for entry in self._entries.values():
                if entry.job.status not in READY_STATUSES:
                    entry.control.cancel(shutdown=True)
                self._release_control(entry)
                if entry.drain is not None:
                    # The cancellation request already cancelled execution; its drain settles it.
                    tasks.append(entry.drain)
                elif entry.task is not None and not entry.task.done():
                    entry.task.cancel()
                    tasks.append(entry.task)
        await asyncio.gather(*tasks, return_exceptions=True)
        reason = "Tool execution was interrupted by runtime shutdown; it was not replayed."
        for entry in self._entries.values():
            try:
                await self._settle(entry, BackgroundOutcome("interrupted", reason))
            except Exception as error:
                # A blocked native child stays unsettled for the next recovery; other jobs still settle.
                failures.append(error)
        if failures:
            msg = "Tool job shutdown failed"
            raise ExceptionGroup(msg, failures)
