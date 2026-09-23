"""Durable execution ownership, interruptible waits, and result consumption for application tool jobs."""

from __future__ import annotations

import asyncio
import fcntl
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
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
# Statuses after which execution has ended for good.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "denied", "interrupted"})
# Statuses whose outcome a parent can retrieve: a terminal one, or an approval that pauses execution.
READY_STATUSES = TERMINAL_STATUSES | {"awaiting_approval"}
_UNAVAILABLE = "Tool job is not available in this conversation."
_JOB_SUMMARY_MAX_CHARS = 500
_SNAPSHOT_SCHEMA_VERSION = 6
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
# The internal source that delivers one job generation's outcome; parsing accepts exactly what the builder emits.
_COMPLETION_EVENT_ID = re.compile(rf"tool-job:(?P<job_id>{_JOB_ID.pattern}):(?P<generation>0|[1-9][0-9]*)")
_PAYLOAD_SUFFIX = ".result.json"
# Consumed jobs are deleted this long after their last change, once their originating turn has finished.
CONSUMED_RESULT_RETENTION = timedelta(days=30)
logger = get_logger(__name__)
# A ToolResultPayload encoded by `tool_jobs.results`; the runtime stores it without reading it.
type EncodedResultPayload = dict[str, Any]


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
    """Serializable operation outcome; approval semantics remain owned by its adapter.

    `result` is the outcome's text, which the job keeps only as a bounded summary.
    `result_payload` is the adapter's full result, saved in a file of its own for this generation.
    """

    status: _OutcomeStatus
    result: str | None = None
    approval_state: dict[str, Any] = field(default_factory=dict)
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
    # The generation whose outcome a parent run saved as its tool result.
    consumed_generation: int | None = None
    user_stop_receipt_order: int | None = None

    @property
    def has_result_payload(self) -> bool:
        """Whether the current generation's outcome has a payload file; a newer generation has none until it settles."""
        return self.payload_generation == self.generation

    @property
    def consumed(self) -> bool:
        """Whether a parent run saved the current generation's outcome; a newer generation starts unconsumed."""
        return self.consumed_generation == self.generation


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


def _with_outcome(job: BackgroundJob, outcome: BackgroundOutcome) -> BackgroundJob:
    """Publish an outcome's status and approval state with only a bounded summary of its text."""
    text = outcome.result
    return _updated(
        job,
        status=outcome.status,
        result=text[:_JOB_SUMMARY_MAX_CHARS] if text is not None else None,
        summary_truncated=text is not None and len(text) > _JOB_SUMMARY_MAX_CHARS,
        payload_generation=job.generation if outcome.result_payload is not None else None,
        approval_state=outcome.approval_state,
    )


def _payload_name(job_id: str, generation: int) -> str:
    return f"{job_id}.g{generation}{_PAYLOAD_SUFFIX}"


def _unlink_in_order(paths: list[Path]) -> None:
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
    version = payload.pop("schema_version")
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
class JobWait:
    """A waited job, with the claim on its ready outcome that only the parent's saved tool result acknowledges."""

    job: BackgroundJob
    claim: JobClaim | None = None
    delivery_queued: bool = False


@dataclass
class _Entry:
    job: BackgroundJob
    control: JobControl = field(default_factory=JobControl)
    human_signal: HumanMessageSignal | None = None
    task: asyncio.Task[None] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    claim: JobClaim | None = None
    cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]] | None = None
    # False only while memory retains an outcome of work that already ran but could not be saved.
    saved: bool = True
    # That unsaved outcome's payload, kept only until a retried save writes its file.
    unsaved_payload: EncodedResultPayload | None = None
    stopped_outcome: BackgroundOutcome | None = None
    # The one in-flight cancellation drain: `cancel` and `cancel_owned` await it, while Stop and revocation leave it
    # running and only its logged failure reports it.
    drain: asyncio.Task[BackgroundJob] | None = None

    def notify_changed(self) -> None:
        """Wake existing state waiters while keeping the next wait fresh."""
        self.changed.set()
        self.changed = asyncio.Event()

    @property
    def live_claim(self) -> JobClaim | None:
        """The claim on the current generation; a claim on an earlier generation owns nothing."""
        claim = self.claim
        return claim if claim is not None and claim.generation == self.job.generation else None

    def mint_claim(self) -> JobClaim:
        """Claim the current generation for one waiter."""
        self.claim = JobClaim(self.job.generation, uuid4().hex)
        return self.claim

    def claim_for(self, claim: JobClaim | None) -> JobClaim | None:
        """Keep a waiter's live claim or claim an unclaimed generation for it; None while another waiter holds it."""
        live = self.live_claim
        if live is None:
            return self.mint_claim()
        return live if live == claim else None


# A job's recipient and the turn that started it.
type _SourceKey = tuple[str, str]
# A job's recipient, room, resolved thread, and requester.
type _ConversationKey = tuple[str, str | None, str | None, str | None]


def _conversation_key(owner: ToolExecutionIdentity) -> _ConversationKey:
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
    instance = tool_job_instance(runtime_paths)
    return instance.runtime if instance is not None else None


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
        cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]],
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
        self._authorize_execution = authorize_execution
        self._cancel = cancel
        self._entries: dict[str, _Entry] = {}
        # Per-turn lookups read these instead of scanning every entry; `_add_entry` and `_remove_entry` keep them.
        self._by_source = _EntryIndex[_SourceKey]()
        self._by_conversation = _EntryIndex[_ConversationKey]()
        self._lock = asyncio.Lock()
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self.changed = asyncio.Event()
        self._human_signals: WeakValueDictionary[tuple[str, str, str | None], HumanMessageSignal] = (
            WeakValueDictionary()
        )

    def authorize_execution(
        self,
        owner: ToolExecutionIdentity,
        function: Function,
        arguments: Mapping[str, Any],
    ) -> None:
        """Recheck a retained function's current authority immediately before application entry; raise if revoked."""
        self._authorize_execution(owner, function, arguments)

    def human_signal_for(self, transport_agent_name: str, room_id: str, thread_id: str | None) -> HumanMessageSignal:
        """Retain one conversation signal while a runner or background job uses it."""
        key = (transport_agent_name, room_id, thread_id)
        signal = self._human_signals.get(key)
        if signal is None:
            signal = HumanMessageSignal()
            self._human_signals[key] = signal
        return signal

    def _path(self, job_id: str, generation: int | None = None) -> Path:
        """Locate a job's metadata, or the payload of one of its generations."""
        if _JOB_ID.fullmatch(job_id) is None:
            raise JobAccessError(_UNAVAILABLE)
        path = self._root / (f"{job_id}.json" if generation is None else _payload_name(job_id, generation))
        if path.is_symlink():
            raise JobAccessError(_UNAVAILABLE)
        return path

    @staticmethod
    def _owner(owner: ToolExecutionIdentity) -> ToolExecutionIdentity:
        return replace(owner, thread_id=owner.resolved_thread_id)

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
        return entry.job.source_event_id if entry is not None else None

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

    def _entry(self, job_id: str, owner: ToolExecutionIdentity, depth: int) -> _Entry:
        self._ensure_open()
        entry = self._entries.get(job_id)
        if (
            entry is None
            or not owner.session_id
            or entry.job.owner != self._owner(owner)
            or entry.job.depth != depth
            or not self._authorize(entry.job)
        ):
            raise JobAccessError(_UNAVAILABLE)
        return entry

    async def _publish(self, entry: _Entry, job: BackgroundJob, payload: EncodedResultPayload | None = None) -> None:
        """Durably save the job, then make it current; a failed write leaves memory unchanged.

        A new payload, or the unsaved one this job still references, is written before the metadata that references it.
        Once the metadata lands, the payload file it replaces is deleted.
        """
        if payload is None and job.has_result_payload:
            payload = entry.unsaved_payload
        path = self._path(job.job_id)
        payload_path = self._path(job.job_id, job.generation) if payload is not None else None
        previous = entry.job
        replaced = (
            self._path(job.job_id, previous.generation)
            if previous.has_result_payload and (not job.has_result_payload or job.generation != previous.generation)
            else None
        )

        def write() -> None:
            if payload_path is not None:
                write_json_file_durable(payload_path, payload, strict_atomic_replace=True)
            write_json_file_durable(
                path,
                {"schema_version": _SNAPSHOT_SCHEMA_VERSION, **asdict(job)},
                strict_atomic_replace=True,
            )
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
        """Publish work that already ran; a failed save keeps it for acknowledgement and shutdown to retry."""
        job = _with_outcome(entry.job, outcome)
        try:
            await self._publish(entry, job, outcome.result_payload)
        except Exception:
            entry.job, entry.saved, entry.unsaved_payload = job, False, outcome.result_payload
            entry.notify_changed()
            self.changed.set()
            raise

    async def read_payload(self, job: BackgroundJob) -> EncodedResultPayload:
        """Read the payload a snapshot with `has_result_payload` references, from disk and outside the runtime lock.

        An outcome whose save failed is read from memory until a retry writes it.
        A payload deleted since the snapshot was taken, by a newer generation or by expiry, is unavailable.
        """
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
                entry = _Entry(await asyncio.to_thread(read_job_snapshot, path))
                # A Stopped approval can never continue; a Stop during shutdown left its cancellation to recovery.
                stopped_approval = (
                    entry.job.status == "awaiting_approval" and entry.job.user_stop_receipt_order is not None
                )
                if entry.job.status not in READY_STATUSES or stopped_approval:
                    outcome = await self._cleanup(entry)
                    await self._publish_outcome(
                        entry,
                        self._settled(
                            entry,
                            status="cancelled" if stopped_approval else "interrupted",
                            reason=None
                            if stopped_approval
                            else "Tool execution was interrupted by a runtime restart; it was not replayed.",
                            outcome=outcome,
                        ),
                    )
                self._add_entry(entry)
                self._restore_approval_signal(entry)
            referenced = {
                _payload_name(entry.job.job_id, entry.job.generation)
                for entry in self._entries.values()
                if entry.job.has_result_payload
            }
            # A crash can leave a payload no saved metadata references, such as one whose metadata save never landed.
            await asyncio.to_thread(self._delete_payloads_except, referenced)

    def _delete_payloads_except(self, referenced: set[str]) -> None:
        for path in self._root.glob(f"*{_PAYLOAD_SUFFIX}"):
            if path.name not in referenced:
                path.unlink()

    def _restore_approval_signal(self, entry: _Entry) -> None:
        """Observe future human ingress while a recovered approval awaits reattachment."""
        if entry.job.status != "awaiting_approval":
            return
        owner = entry.job.owner
        if owner.room_id is not None:
            entry.human_signal = self.human_signal_for(owner.recipient, owner.room_id, owner.resolved_thread_id)
            entry.human_signal.subscribe(entry.notify_changed)

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
        operation: Callable[[], Awaitable[BackgroundOutcome]],
        cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]] | None = None,
        reattach: bool = False,
    ) -> tuple[BackgroundJob, JobClaim | None]:
        """Durably accept exact operation ownership before spawning execution, claiming its outcome for the caller.

        Reattaching to a job whose current generation another waiter claims returns no claim.
        The claim is minted after the last await, so a cancelled start leaves no claim its caller never received.
        """
        async with self._lock:
            self._ensure_accepting()
            if reattach and job_id in self._entries:
                existing = self._entry(job_id, owner, depth)
                if (
                    existing.job.tool_name != tool_name
                    or existing.job.toolkit_name != toolkit_name
                    or existing.job.kind != kind
                    or existing.job.source_event_id != source_event_id
                    or existing.job.source_kind != source_kind
                    or existing.job.adapter != adapter
                ):
                    raise JobAccessError(_UNAVAILABLE)
                snapshot = await self._snapshot(existing)
                return snapshot, existing.mint_claim() if existing.live_claim is None else None
            if job_id in self._entries or self._path(job_id).exists():
                msg = "Tool job already exists."
                raise ValueError(msg)
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
            if not owner.session_id or depth < 0 or not self._authorize(job):
                raise JobAccessError(_UNAVAILABLE)
            entry = _Entry(job, cancel=cancel)
            await run_coroutine_until_complete(self._admit(entry, job, operation))
            snapshot = await self._snapshot(entry)
            return snapshot, entry.mint_claim()

    async def _admit(
        self,
        entry: _Entry,
        job: BackgroundJob,
        operation: Callable[[], Awaitable[BackgroundOutcome]],
    ) -> None:
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
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise  # Requested cancellation: runtime-owned, or external teardown.
                # The operation raised cancellation itself; settle it so waiters and delivery see an outcome.
                outcome = BackgroundOutcome("cancelled")
            async with self._lock:
                # Runtime-owned cancellation and shutdown set the control; their settlement consumes this outcome.
                if entry.control.cancelled:
                    entry.stopped_outcome = outcome
                elif entry.job.status not in TERMINAL_STATUSES:
                    try:
                        await self._publish_outcome(entry, outcome)
                    except Exception:
                        logger.exception(
                            "Tool job outcome save failed; retaining it in memory",
                            job_id=entry.job.job_id,
                        )
        except asyncio.CancelledError:
            # Only runtime-owned cancellation settles the job; external teardown leaves it for recovery.
            if entry.control.cancelled:
                entry.stopped_outcome = outcome
            raise
        finally:
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
            entries = [
                entry
                for entry in self._entries.values()
                if owner.session_id
                and entry.job.owner == self._owner(owner)
                and entry.job.depth == depth
                and self._authorize(entry.job)
            ]
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
        """Wait without cancelling execution; retain a claim on the ready outcome until acknowledgement.

        A caller holding the current claim, such as the one `start` returned, keeps it; otherwise the wait claims the
        outcome itself, unless another waiter already holds that claim.
        """
        timeout = validate_wait_timeout(timeout)
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        retained = False
        async with self._lock:
            entry = self._entry(job_id, owner, depth)
            claim = entry.claim_for(claim)
            if claim is None:
                return JobWait(await self._snapshot(entry), delivery_queued=True)
            human_notified = asyncio.Event()
            human_signal = entry.human_signal
            if human_signal is not None:
                human_signal.subscribe(human_notified.set)
        try:
            while True:
                async with self._lock:
                    self._entry(job_id, owner, depth)
                    if entry.job.status in READY_STATUSES:
                        # A continuation or cancellation can start a newer generation before this waiter sees the one
                        # it claimed; that stale claim owns nothing, so claim the ready one unless another waiter has.
                        ready = entry.claim_for(claim)
                        if ready is None:
                            return JobWait(await self._snapshot(entry), delivery_queued=True)
                        claim = ready
                        snapshot = await self._snapshot(entry)
                        retained = True
                        return JobWait(snapshot, claim)
                    if human_notified.is_set():
                        return JobWait(await self._snapshot(entry))
                    remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
                    if remaining is not None and remaining <= 0:
                        return JobWait(await self._snapshot(entry))
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
            if human_signal is not None:
                human_signal.unsubscribe(human_notified.set)
            if not retained:
                await self.release_wait(job_id, claim)

    async def release_wait(self, job_id: str, claim: JobClaim | None) -> None:
        """Release an unpersisted result claim so completion delivery remains possible.

        The release finishes even when its caller is cancelled again, so a waiter
        interrupted twice cannot leak the claim.
        """

        async def release() -> None:
            async with self._lock:
                entry = self._entries.get(job_id)
                if entry is not None and claim is not None and entry.claim == claim:
                    entry.claim = None
                    self.changed.set()

        await run_coroutine_until_complete(release())

    async def acknowledge_wait(self, job_id: str, claim: JobClaim | None) -> None:
        """Mark the claimed generation consumed, only after the exact parent tool result has been durably saved."""
        async with self._lock:
            self._ensure_open()
            entry = self._entries[job_id]
            if claim is None or entry.live_claim != claim:
                msg = "Tool job wait claim no longer belongs to this waiter."
                raise ValueError(msg)
            await self._publish(entry, _updated(entry.job, consumed_generation=claim.generation))
            entry.claim = None

    async def cancel(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
    ) -> BackgroundJob:
        """Cancel execution and return the job once its execution and cleanup have settled."""
        async with self._lock:
            self._ensure_accepting()
            entry = self._entry(job_id, owner, depth)
            drain = await self._request_cancel(entry)
        return await wait_for_future_until_complete(drain)

    async def stop_jobs(
        self,
        *,
        receipt_order: int,
        matches: Callable[[BackgroundJob], Awaitable[bool]],
    ) -> None:
        """Persist explicit Stop independently of result consumption, then request owned cleanup.

        `matches` may read the journal, so it judges snapshots outside the runtime lock.
        During shutdown the mark is saved while shutdown itself settles execution.
        """
        async with self._lock:
            self._ensure_open()
            candidates = [
                (entry, await self._snapshot(entry, include_result=False))
                for entry in self._entries.values()
                if self._stop_applies(entry.job, receipt_order)
            ]
        selected = [entry for entry, job in candidates if await matches(job)]
        async with self._lock:
            self._ensure_open()
            failures = await self._isolated(
                (entry for entry in selected if self._entries.get(entry.job.job_id) is entry),
                partial(self._mark_stopped, receipt_order=receipt_order),
                "Tool job Stop failed",
            )
        if failures:
            msg = "Tool job Stop failed"
            raise ExceptionGroup(msg, failures)

    @staticmethod
    def _stop_applies(job: BackgroundJob, receipt_order: int) -> bool:
        """Whether a Stop still changes a job: it has no mark this recent, or its execution has not ended."""
        marked = job.user_stop_receipt_order
        return marked is None or marked < receipt_order or job.status not in TERMINAL_STATUSES

    async def _mark_stopped(self, entry: _Entry, *, receipt_order: int) -> None:
        """Save a newer Stop mark, then request cleanup unless shutdown is already settling execution."""
        marked = entry.job.user_stop_receipt_order
        if marked is None or marked < receipt_order:
            await self._publish(entry, _updated(entry.job, user_stop_receipt_order=receipt_order))
        if self._shutdown_task is None and entry.job.status not in TERMINAL_STATUSES:
            await self._request_cancel(entry)

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
            entry = self._entries.get(job_id)
            return entry is not None and entry.job.user_stop_receipt_order is not None

    async def is_source_user_stopped(self, source_event_id: str, transport_agent_name: str) -> bool:
        """Recognize a stopped original response, including foreground approval recovery."""
        async with self._lock:
            return any(
                entry.job.user_stop_receipt_order is not None
                for entry in self._by_source.get((transport_agent_name, source_event_id))
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
            if entry is None or not matches(await self._snapshot(entry, include_result=False)):
                return None
            drain = await self._request_cancel(entry)
        return await wait_for_future_until_complete(drain)

    async def cancel_revoked(self) -> None:
        """Withdraw execution when current grants disappear, retaining owned cleanup; a failed job retries next pass."""
        async with self._lock:
            self._ensure_accepting()
            revoked = [
                entry
                for entry in self._entries.values()
                if entry.job.status not in TERMINAL_STATUSES and not self._authorize(entry.job)
            ]
            await self._isolated(revoked, self._request_cancel, "Tool job revocation failed")

    async def _request_cancel(self, entry: _Entry) -> asyncio.Task[BackgroundJob]:
        """Durably request cancellation and return its one drain; the caller holds the runtime lock."""

        async def request() -> asyncio.Task[BackgroundJob]:
            if entry.job.status in {"running", "awaiting_approval"}:
                requested = _updated(entry.job, status="cancel_requested")
                if entry.job.status == "awaiting_approval":
                    # Cancelling an approval publishes a fresh unconsumed generation that stale claims cannot own.
                    requested = replace(requested, generation=requested.generation + 1)
                await self._publish(entry, requested)
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
        """Await requested execution and cleanup, then publish the settled job or retry an unsaved terminal one."""
        try:
            settling = entry.job.status == "cancel_requested"
            outcome = None
            if settling:
                if entry.task is not None:
                    await asyncio.gather(entry.task, return_exceptions=True)
                outcome = await self._cleanup(entry)
            async with self._lock:
                if settling:
                    await self._publish_outcome(
                        entry,
                        self._settled(entry, status="cancelled", reason=None, outcome=outcome),
                    )
                elif not entry.saved:
                    await self._publish(entry, entry.job)
                return await self._snapshot(entry)
        finally:
            # A failed drain leaves the job for the next canceller, recovery, or shutdown to settle.
            entry.drain = None

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
    def _settled(
        entry: _Entry,
        *,
        status: Literal["cancelled", "interrupted"],
        reason: str | None,
        outcome: BackgroundOutcome | None = None,
    ) -> BackgroundOutcome:
        """Choose the terminal outcome, consuming the stopped one, after owned execution and cleanup settle."""
        if (
            entry.stopped_outcome is not None
            and entry.stopped_outcome.status in TERMINAL_STATUSES
            and (outcome is None or outcome.status not in {"completed", "failed", "denied"})
        ):
            outcome = entry.stopped_outcome
        entry.stopped_outcome = None
        if outcome is None or outcome.status not in TERMINAL_STATUSES:
            outcome = BackgroundOutcome(status, reason)
        return outcome

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
            job = _updated(
                entry.job,
                adapter=entry.job.adapter if adapter is None else adapter,
                status="running",
                generation=entry.job.generation + 1,
            )
            await run_coroutine_until_complete(self._admit(entry, job, operation))
            return await self._snapshot(entry)

    def _unconsumed(self, entry: _Entry) -> bool:
        return (
            not entry.job.consumed
            and entry.job.user_stop_receipt_order is None
            and entry.live_claim is None
            and self._authorize(entry.job)
        )

    async def pending_outcomes(self) -> list[BackgroundJob]:
        """Return authorized ready generations that no parent run consumed and no waiter claims."""
        async with self._lock:
            if self._closed:
                return []
            return [
                await self._snapshot(entry, include_result=False)
                for entry in self._entries.values()
                if entry.job.status in READY_STATUSES and self._unconsumed(entry)
            ]

    async def stoppable_jobs(self) -> list[BackgroundJob]:
        """Return jobs no Stop has marked whose execution or unconsumed outcome a saved Stop could still end."""
        async with self._lock:
            if self._closed:
                return []
            return [
                await self._snapshot(entry, include_result=False)
                for entry in self._entries.values()
                if entry.job.user_stop_receipt_order is None
                and (entry.job.status not in TERMINAL_STATUSES or not entry.job.consumed)
            ]

    async def outcome(self, job_id: str, generation: int) -> BackgroundJob | None:
        """Revalidate one unconsumed generation at its serialized response boundary."""
        async with self._lock:
            entry = self._entries.get(job_id)
            if (
                self._closed
                or entry is None
                or entry.job.generation != generation
                or entry.job.status not in READY_STATUSES
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
                for entry in self._by_source.get((transport_agent_name, source_event_id))
                if entry.job.owner.room_id == room_id
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
        silent = source_kind == SILENT_SCHEDULE_SOURCE_KIND
        async with self._lock:
            if self._closed:
                return []
            return [
                await self._snapshot(entry, include_result=False)
                for entry in self._by_conversation.get((transport_agent_name, room_id, thread_id, requester_id))
                if (entry.job.source_kind == SILENT_SCHEDULE_SOURCE_KIND) == silent and self._unconsumed(entry)
            ]

    async def expire_consumed(
        self,
        *,
        before: datetime,
        source_finished: Callable[[BackgroundJob], Awaitable[bool]],
    ) -> None:
        """Delete old consumed jobs whose originating turn has finished.

        Deleting a job cannot let its call run again, because three conditions hold together.
        A claim is acknowledged only after the parent run saved a non-`None` result for the exact tool call.
        Agno executes a tool call again only when its saved result is `None`.
        `source_finished` refuses while an approval continuation is open for the job's session.
        A job also stays while jobs started by a turn that delivered its outcome remain.
        Stop traces those jobs through it to their human turn.
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
                await run_blocking_until_complete(_unlink_in_order, files)

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
                    entry.control.cancel()
                self._release_control(entry)
                if entry.drain is not None:
                    # The cancellation request already cancelled execution; its drain settles it.
                    tasks.append(entry.drain)
                elif entry.task is not None and not entry.task.done():
                    entry.task.cancel()
                    tasks.append(entry.task)
        await asyncio.gather(*tasks, return_exceptions=True)
        for entry in self._entries.values():
            settling = entry.job.status not in READY_STATUSES
            try:
                outcome = await self._cleanup(entry) if settling else None
                async with self._lock:
                    if settling:
                        settled = self._settled(
                            entry,
                            status="interrupted",
                            reason="Tool execution was interrupted by runtime shutdown; it was not replayed.",
                            outcome=outcome,
                        )
                        await self._publish_outcome(entry, settled)
                    elif not entry.saved:
                        await self._publish(entry, entry.job)
            except Exception as error:
                # A blocked native child stays unsettled for the next recovery; other jobs still settle.
                failures.append(error)
        if failures:
            msg = "Tool job shutdown failed"
            raise ExceptionGroup(msg, failures)
