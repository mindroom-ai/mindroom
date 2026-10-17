"""Durable execution ownership, interruptible waits, and result consumption for application tool jobs."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from operator import attrgetter
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from mindroom.background_tasks import (
    run_blocking_until_complete,
    run_coroutine_until_complete,
    wait_for_future_until_complete,
)
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.background_after import validate_wait_seconds
from mindroom.tool_jobs.control import (
    JobControl,
    current_pending_human_messages,
    job_control_context,
    pending_human_messages_context,
)
from mindroom.tool_jobs.instances import tool_job_instance
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_system.call_record import without_tool_call_recording
from mindroom.tool_system.progress import cap_progress, tool_progress_reporter
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from agno.tools.function import Function

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import SavedToolJob, ToolJobStore
    from mindroom.tool_jobs.conversation import ConversationKey

type _OutcomeStatus = Literal["completed", "failed", "cancelled", "denied", "interrupted"]
# A running job that waits for human decisions on the approval cards it posted is `awaiting_approval`.
type _BackgroundStatus = Literal["running", "awaiting_approval", "cancel_requested"] | _OutcomeStatus
# Statuses after which execution has ended for good, with an outcome a reply can retrieve.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "denied", "interrupted"})
_RUNNING_STATUSES = frozenset({"running", "awaiting_approval"})
_UNAVAILABLE = "Tool job is not available in this conversation."
_JOB_SUMMARY_MAX_CHARS = 500
# A reattaching call must name exactly the operation its job was admitted for.
_ADMITTED_OPERATION = attrgetter("tool_name", "toolkit_name", "kind", "source_event_id", "source_kind", "adapter")
# Ended jobs, read or not, are deleted this long after their last change, once their originating turn has finished.
ENDED_JOB_RETENTION = timedelta(days=30)
logger = get_logger(__name__)
# A ToolResultPayload encoded by `tool_jobs.results`; the runtime stores it without reading it.
type EncodedResultPayload = dict[str, Any]
# An accepted job's execution, and the adapter cleanup that settles it after cancellation or interruption.
type _Operation = Callable[[], Awaitable[BackgroundOutcome]]
type _Cleanup = Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]]


class JobAccessError(ValueError):
    """The requested job is unavailable to this caller or runtime."""


class JobUnavailableError(JobAccessError):
    """No job with the requested ID is available to this caller, whether unknown, deleted, foreign, or revoked."""


class JobRecoveryBlockedError(RuntimeError):
    """Another executor still owns work that this runtime cannot safely settle."""


@dataclass
class BackgroundOutcome:
    """Serializable terminal outcome of one operation."""

    status: _OutcomeStatus
    result: str | None = None
    # The adapter's full result, saved beside the job; the job keeps only a summary of `result`.
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
    # Whether the outcome saved a payload; an outcome the runtime authored has only its summary.
    has_result_payload: bool = False
    # Whether a parent run saved the outcome as its tool result.
    consumed: bool = False
    # Whether the call that started it saved the outcome as its own result, so it never became a job to list.
    returned_inline: bool = False
    # The latest progress its running tool reported, as of this snapshot; live state that is never saved.
    progress: str | None = None


@dataclass(frozen=True)
class JobClaim:
    """One waiter's exclusive right to consume a job's outcome until it acknowledges or releases it."""

    nonce: str


def _updated(job: BackgroundJob, **changes: object) -> BackgroundJob:
    """Return the next state of a job, stamped with its transition time."""
    return replace(job, **changes, updated_at=datetime.now(UTC).isoformat())


def _parse_saved_job(saved: SavedToolJob) -> BackgroundJob:
    """Validate one saved snapshot without claiming or changing its execution."""
    msg = f"Invalid tool job snapshot {saved.job_id!r}."
    try:
        payload = json.loads(saved.job_json)
        payload["owner"] = parse_tool_execution_identity_payload(payload["owner"], strict=True)
        job = BackgroundJob(**payload)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(msg) from error
    sources = (job.source_event_id, job.source_kind)
    if job.job_id != saved.job_id or not all(isinstance(value, str | None) for value in sources):
        raise ValueError(msg)
    return job


def _encoded(job: BackgroundJob, payload: EncodedResultPayload | None) -> tuple[str, str | None]:
    return json.dumps(asdict(job)), None if payload is None else json.dumps(payload)


# People see a job by the start of its ID; `!cancel_job` accepts any prefix that names one job.
_SHORT_JOB_ID_LENGTH = 8


def describe_job(job: BackgroundJob, *, short: bool = False) -> str:
    """Name a job in text by its ID, shortened for people, and its tool or the agent a subagent job runs."""
    tool = f"{job.tool_name} subagent" if job.kind == "delegation" else job.tool_name
    return f"`{job.job_id[:_SHORT_JOB_ID_LENGTH] if short else job.job_id}` ({tool})"


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
    if job.progress is not None and job.status not in TERMINAL_STATUSES:
        summary["progress"] = job.progress
    return summary


# A cancelled call's text, so the model does not take an empty failure as a reason to run it again.
_CANCELLED_REASON = (
    "This call was cancelled before it returned a result. Its effects so far stand: do not call it again unless "
    "the user asks."
)


def _stopped_outcome(*, cancelled: bool, cause: Literal["restart", "shutdown"]) -> BackgroundOutcome:
    """Settle a job a restart or shutdown stopped: cancelled if cancelling it already began, otherwise interrupted.

    The interrupted reason tells the model what the call may have done, so it repeats only a read-only one.
    """
    if cancelled:
        return BackgroundOutcome("cancelled", _CANCELLED_REASON)
    return BackgroundOutcome(
        "interrupted",
        f"A runtime {cause} interrupted this call before it returned a result. If it has side effects, they may "
        "already have happened, so do not run it again; a read-only call can run again to get its result.",
    )


def format_job_handle(job: BackgroundJob) -> str:
    """Return the job's summary as the machine-readable handle a detached or queued call returns."""
    return json.dumps(job_summary(job))


@dataclass(frozen=True)
class _JobWait:
    """A waited job, with the claim on its ready outcome that only the parent's saved tool result acknowledges."""

    job: BackgroundJob
    claim: JobClaim | None = None


@dataclass
class _Entry:
    job: BackgroundJob
    control: JobControl = field(default_factory=JobControl)
    task: asyncio.Task[None] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    claim: JobClaim | None = None
    # The claim of the call that started the job and returned its outcome inline, until its reply is saved.
    inline_claim: JobClaim | None = None
    cancel: _Cleanup | None = None
    # False only while memory retains an outcome of work that already ran but could not be saved.
    saved: bool = True
    # That unsaved outcome's payload, kept only until a retried save lands it.
    unsaved_payload: EncodedResultPayload | None = None
    stopped_outcome: BackgroundOutcome | None = None
    # The one in-flight cancellation drain; a cancel and revocation do not await it, so a done callback logs its failure.
    drain: asyncio.Task[BackgroundJob] | None = None
    # The latest progress its running tool reported, which snapshots of the running job carry.
    progress: str | None = None

    def notify_changed(self) -> None:
        """Wake existing state waiters while keeping the next wait fresh."""
        self.changed.set()
        self.changed = asyncio.Event()

    def claim_for(self, claim: JobClaim | None) -> JobClaim | None:
        """Keep a waiter's claim or claim an unclaimed outcome for it; None while another waiter holds it."""
        if self.claim is None:
            self.claim = JobClaim(uuid4().hex)
            return self.claim
        return self.claim if self.claim == claim else None


def _returned_inline(entry: _Entry) -> bool:
    """Whether the call that started a job got its outcome as its own result, so it never became a job."""
    return entry.job.returned_inline or (entry.claim is not None and entry.claim == entry.inline_claim)


def _conversation_key(owner: ToolExecutionIdentity) -> tuple[str, str | None, str | None, str | None]:
    """A job's recipient, room, resolved thread, and requester."""
    return (owner.recipient, owner.room_id, owner.resolved_thread_id, owner.requester_id)


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


def _approval_deadline(
    job: BackgroundJob,
    approval_timeout: float | None,
    current: float | None,
    now: float,
) -> float | None:
    """Return when a wait stops waiting for the job's approval: the whole allowance from each ask it sees."""
    if job.status != "awaiting_approval" or approval_timeout is None:
        return None
    return now + approval_timeout if current is None else current


async def _changed_or_released(
    changed: asyncio.Event,
    message: asyncio.Future[None] | None,
    *,
    timeout: float | None,  # noqa: ASYNC109
) -> bool:
    """Wait until the job changes, ``message`` returns, or ``timeout`` passes; return whether the message came."""
    change = asyncio.ensure_future(changed.wait())
    try:
        await asyncio.wait(
            [change] if message is None else [change, message],
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        return message is not None and message.done()
    finally:
        change.cancel()
        await asyncio.gather(change, return_exceptions=True)


class ToolJobRuntime:
    """Keep tool execution alive while individual foreground waiters come and go."""

    def __init__(
        self,
        store: ToolJobStore,
        *,
        authorize: Callable[[BackgroundJob], bool],
        authorize_execution: Callable[[ToolExecutionIdentity, Function, Mapping[str, Any]], None],
        cancel: _Cleanup,
        denied: Callable[[BackgroundJob], bool],
    ) -> None:
        # Saves land only while this runtime's generation owns the jobs, which its creator takes before recovery.
        self._store = store
        self._authorize = authorize
        # Access proven gone, as opposed to access merely unresolved, such as while room membership resolves.
        self._denied = denied
        # Rechecks a retained function's current authority immediately before application entry; raises if revoked.
        self.authorize_execution = authorize_execution
        self._cancel = cancel
        self._entries: dict[str, _Entry] = {}
        # Conversation lookups read this instead of scanning every entry; `_add_entry` and `_remove_entry` keep it.
        # Jobs are keyed by their recipient, room, thread, and requester.
        self._by_conversation: dict[tuple[str, str | None, str | None, str | None], dict[str, _Entry]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self.changed = asyncio.Event()
        # Jobs that ended while their approval cards could not be denied yet; the coordinator retries them every pass.
        self.unsettled_approvals: set[str] = set()

    @staticmethod
    def _canonical_owner(owner: ToolExecutionIdentity) -> ToolExecutionIdentity:
        return replace(owner, thread_id=owner.resolved_thread_id)

    def _ensure_open(self, *, accepting: bool = False) -> None:
        """Refuse calls once closed, and calls that start or cancel execution (`accepting`) once shutdown began."""
        if self._closed or (accepting and self._shutdown_task is not None):
            msg = "Tool job runtime is closed." if self._closed else "Tool job runtime is shutting down."
            raise JobAccessError(msg)

    def retained_jobs(self) -> tuple[BackgroundJob, ...]:
        """Return every job this runtime retains, unfiltered by access, for the coordinator's own passes."""
        return tuple(entry.job for entry in self._entries.values())

    def has_job(self, job_id: str) -> bool:
        """Recognize accepted ownership; access still requires an authorized lookup."""
        return job_id in self._entries

    def _add_entry(self, entry: _Entry) -> None:
        """Make an accepted job current and findable by its conversation, which never changes."""
        job = entry.job
        self._entries[job.job_id] = entry
        self._by_conversation.setdefault(_conversation_key(job.owner), {})[job.job_id] = entry

    def _remove_entry(self, job_id: str) -> None:
        key = _conversation_key(self._entries.pop(job_id).job.owner)
        group = self._by_conversation[key]
        del group[job_id]
        if not group:
            del self._by_conversation[key]

    def _owns(self, job: BackgroundJob, owner: ToolExecutionIdentity, depth: int) -> bool:
        """Whether a caller owns a job: it made the call, or it is the agent or team that answers in its conversation.

        A team owns the jobs its members start for its replies, so the team that answers collects their outcomes.
        """
        caller = self._canonical_owner(owner)
        if not owner.session_id or job.depth != depth:
            return False
        answers = (
            caller.agent_name == caller.recipient
            and _conversation_key(job.owner) == _conversation_key(caller)
            and job.owner.session_id == caller.session_id
        )
        return job.owner == caller or answers

    def _visible(self, entry: _Entry, owner: ToolExecutionIdentity, depth: int) -> bool:
        """Whether a caller owns a job it may still access."""
        return self._owns(entry.job, owner, depth) and self._authorize(entry.job)

    def _entry(self, job_id: str, owner: ToolExecutionIdentity, depth: int) -> _Entry:
        self._ensure_open()
        entry = self._entries.get(job_id)
        if entry is None or not self._visible(entry, owner, depth):
            raise JobUnavailableError(_UNAVAILABLE)
        return entry

    def _waited_entry(self, job_id: str, owner: ToolExecutionIdentity, depth: int) -> tuple[_Entry, bool]:
        """Return a job its caller may wait on, and whether its outcome is readable now.

        Only an unknown or foreign job, or a proven denial, refuses the wait; while the caller's access still
        resolves, as after a membership refresh, the job stays held, so the wait is released instead of failing a
        call that may have run.
        """
        self._ensure_open()
        entry = self._entries.get(job_id)
        if entry is None or not self._owns(entry.job, owner, depth) or self._denied(entry.job):
            raise JobUnavailableError(_UNAVAILABLE)
        return entry, self._authorize(entry.job)

    async def _publish(
        self,
        entry: _Entry,
        job: BackgroundJob,
        payload: EncodedResultPayload | None = None,
        *,
        accepted: bool = False,
    ) -> None:
        """Durably save the job, with its new or still unsaved payload, then make it current.

        `accepted` saves a newly accepted job, which the journal refuses while a job with its ID is still saved.
        A failed write leaves memory unchanged.
        """
        if payload is None and job.has_result_payload:
            payload = entry.unsaved_payload

        async def publish() -> None:
            job_json, payload_json = await asyncio.to_thread(_encoded, job, payload)
            if accepted:
                await self._store.accept(job.job_id, job_json)
            else:
                await self._store.save(job.job_id, job_json, payload_json)
            entry.job, entry.saved, entry.unsaved_payload = job, True, None
            if job.status in TERMINAL_STATUSES:
                # A durable terminal outcome ends execution; drop what only running work needed.
                entry.cancel, entry.task = None, None
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
            has_result_payload=outcome.result_payload is not None,
        )
        try:
            await self._publish(entry, job, outcome.result_payload)
        except Exception:
            entry.job, entry.saved, entry.unsaved_payload = job, False, outcome.result_payload
            entry.notify_changed()
            self.changed.set()
            raise

    async def read_payload(self, job: BackgroundJob) -> EncodedResultPayload:
        """Read a snapshot's payload outside the lock; one expiry deleted since is unavailable."""
        self._ensure_open()
        entry = self._entries.get(job.job_id)
        unsaved = entry.unsaved_payload if entry is not None else None
        if unsaved is not None:
            return await asyncio.to_thread(lambda: deepcopy(unsaved))
        payload_json = await self._store.load_payload(job.job_id)
        if payload_json is None:
            raise JobUnavailableError(_UNAVAILABLE)
        return await asyncio.to_thread(json.loads, payload_json)

    async def recover(self) -> None:
        """Restore the outcomes of the jobs this runtime took over, never replaying execution.

        A runtime that a newer one replaced refuses, rather than adopting that runtime's jobs.
        """
        async with self._lock:
            self._ensure_open()
            await self._store.require_ownership()
            for saved in await self._store.load_all():
                if saved.job_id in self._entries:
                    continue
                entry = _Entry(await asyncio.to_thread(_parse_saved_job, saved))
                if entry.job.status not in TERMINAL_STATUSES:
                    # Only work the restart cut short is interrupted by it; a cancellation saved before it is not.
                    cancelled = entry.job.status == "cancel_requested"
                    entry.control.cancel(shutdown=not cancelled)
                    outcome = await self._cleanup(entry)
                    default = _stopped_outcome(cancelled=cancelled, cause="restart")
                    await self._publish_outcome(entry, self._settled(entry, outcome, default))
                self._add_entry(entry)

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
                owner=self._canonical_owner(owner),
                tool_name=tool_name,
                depth=depth,
                kind=kind,
                toolkit_name=toolkit_name,
                source_event_id=source_event_id,
                source_kind=source_kind,
                # Native adapters mutate this exact mapping; owns_execution verifies its identity.
                adapter=adapter,
            )
            readable = True
            if reattach and job_id in self._entries:
                entry, readable = self._waited_entry(job_id, owner, depth)
                if _ADMITTED_OPERATION(entry.job) != _ADMITTED_OPERATION(job):
                    raise JobAccessError(_UNAVAILABLE)
            else:
                if job_id in self._entries:
                    msg = "Tool job already exists."
                    raise ValueError(msg)
                if not owner.session_id or depth < 0 or not self._authorize(job):
                    raise JobAccessError(_UNAVAILABLE)
                entry = _Entry(job, cancel=cancel)
                await run_coroutine_until_complete(self._admit(entry, job, operation))
            snapshot = await self._snapshot(entry)
            return snapshot, entry.claim_for(None) if readable else None

    async def _admit(self, entry: _Entry, job: BackgroundJob, operation: _Operation) -> None:
        """Publish accepted ownership, then launch it; callers finish this before propagating cancellation."""
        await self._publish(entry, job, accepted=True)
        self._add_entry(entry)
        entry.task = asyncio.create_task(self._run(entry, operation), name=f"tool-job:{job.job_id}")

    def owns_execution(self, job_id: str, adapter: dict[str, Any]) -> bool:
        """Recognize exact accepted adapter ownership even if its former waiter was cancelled."""
        entry = self._entries.get(job_id)
        return entry is not None and entry.job.adapter is adapter and entry.task is not None

    async def set_awaiting_approval(self, job_id: str, *, awaiting: bool) -> None:
        """Publish whether a running job waits for human decisions on the approval cards it posted."""
        async with self._lock:
            entry = self._entries[job_id]
            status = "awaiting_approval" if awaiting else "running"
            if entry.job.status in _RUNNING_STATUSES and entry.job.status != status:
                await self._publish(entry, _updated(entry.job, status=status))

    async def _run(self, entry: _Entry, operation: _Operation) -> None:
        # The notice implementation imports Agno/storage; keep the job/control import surface light.
        from mindroom.ai_runtime import (  # noqa: PLC0415
            finalize_queued_notice_response_turn_async,
            queued_message_signal_context,
        )

        outcome = None
        returned = False
        loop = asyncio.get_running_loop()

        def record_progress(text: str) -> None:
            # A thread that outlives the call cannot report for it.
            if not returned:
                entry.progress = cap_progress(text)

        def report_progress(text: str) -> None:
            loop.call_soon_threadsafe(record_progress, text)

        try:
            try:
                with (
                    # A newer human message releases replies, never the background work they wait for.
                    pending_human_messages_context(None),
                    tool_progress_reporter(report_progress),
                    # The reply span that started the job may end first; the job owns its calls' outcome.
                    without_tool_call_recording(),
                    job_control_context(entry.control),
                    queued_message_signal_context(None) as notice,
                ):
                    try:
                        async with execution_resources():
                            outcome = await operation()
                    finally:
                        returned = True
                        await finalize_queued_notice_response_turn_async(notice)
            except Exception as error:
                outcome = BackgroundOutcome("failed", str(error))
            except asyncio.CancelledError:
                if (task := asyncio.current_task()) is not None and task.cancelling():
                    raise  # Requested cancellation: runtime-owned, or external teardown.
                # The operation raised cancellation itself; settle it so waiters and delivery see an outcome.
                outcome = BackgroundOutcome("cancelled", _CANCELLED_REASON)
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

    @staticmethod
    async def _snapshot(entry: _Entry) -> BackgroundJob:
        """Copy a job's metadata, with the latest progress while it runs."""
        job = await run_blocking_until_complete(deepcopy, entry.job)
        if entry.progress is None or job.status in TERMINAL_STATUSES:
            return job
        return replace(job, progress=entry.progress)

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
                if not _returned_inline(entry) and self._visible(entry, owner, depth)
            ]
            entries.sort(key=lambda entry: entry.job.updated_at, reverse=True)
            entries.sort(key=lambda entry: entry.job.status in TERMINAL_STATUSES)
            return [await self._snapshot(entry) for entry in entries[offset : offset + limit]]

    async def wait(
        self,
        job_id: str,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        timeout: float | None = None,  # noqa: ASYNC109
        claim: JobClaim | None = None,
        approval_timeout: float | None = None,
    ) -> _JobWait:
        """Wait without cancelling execution, keeping or taking the outcome's claim unless another waiter has.

        ``approval_timeout`` bounds how long the wait lasts once the job awaits approval, from when this wait sees it
        ask, however long the job ran before; ``timeout`` bounds it all.
        """
        timeout = validate_wait_seconds(timeout, argument="wait_timeout")
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        approval_deadline: float | None = None
        retained = False
        async with self._lock:
            entry, readable = self._waited_entry(job_id, owner, depth)
            claim = entry.claim_for(claim) if readable else None
            if claim is None:
                return _JobWait(await self._snapshot(entry))
        # A newer human message in the waiting reply's own conversation releases the wait unless the mid-turn judge
        # lets it wait; the job runs on.
        human = current_pending_human_messages()
        # One message waiter spans the loop, so a job change never cancels the judgment it may be awaiting.
        message: asyncio.Future[None] | None = None
        released = False
        try:
            while True:
                async with self._lock:
                    _, readable = self._waited_entry(job_id, owner, depth)
                    if not readable:
                        return _JobWait(await self._snapshot(entry))
                    if entry.job.status in TERMINAL_STATUSES:
                        # A released claim can be taken again unless another waiter has claimed the outcome since.
                        claim = entry.claim_for(claim)
                        snapshot = await self._snapshot(entry)
                        retained = claim is not None
                        return _JobWait(snapshot, claim)
                    now = asyncio.get_running_loop().time()
                    approval_deadline = _approval_deadline(entry.job, approval_timeout, approval_deadline, now)
                    ends = [end for end in (deadline, approval_deadline) if end is not None]
                    remaining = min(ends) - now if ends else None
                    if released or (remaining is not None and remaining <= 0):
                        return _JobWait(await self._snapshot(entry))
                    changed = entry.changed
                if message is None and human is not None:
                    message = asyncio.ensure_future(human.wait())
                released = await _changed_or_released(changed, message, timeout=remaining)
        finally:
            if message is not None:
                message.cancel()
            if not retained:
                await self.release_wait(job_id, claim)

    def hold_inline(self, job_id: str, claim: JobClaim) -> None:
        """Treat a claimed outcome as the result of the call that started the job while that call's reply runs."""
        entry = self._entries.get(job_id)
        if entry is not None and entry.claim == claim:
            entry.inline_claim = claim

    async def release_wait(self, job_id: str, claim: JobClaim | None) -> None:
        """Release an unpersisted claim so completion can still be delivered, even if the caller is cancelled again."""

        async def release() -> None:
            async with self._lock:
                entry = self._entries.get(job_id)
                if entry is not None and claim is not None and entry.claim == claim:
                    entry.claim = None
                    self.changed.set()

        await run_coroutine_until_complete(release())

    async def acknowledge_wait(self, job_id: str, claim: JobClaim | None, *, returned_inline: bool = False) -> None:
        """Mark the claimed outcome consumed, only after the exact parent tool result has been durably saved.

        ``returned_inline`` says that tool result belongs to the call that started the job.
        """
        async with self._lock:
            self._ensure_open()
            entry = self._entries[job_id]
            if claim is None or entry.claim != claim:
                msg = "Tool job wait claim no longer belongs to this waiter."
                raise ValueError(msg)
            # A later reread of an inline call's result does not make it a job.
            returned_inline = entry.job.returned_inline or returned_inline
            await self._publish(entry, _updated(entry.job, consumed=True, returned_inline=returned_inline))
            entry.claim = None

    async def request_cancel(self, job_id: str) -> None:
        """Ask a live job to stop without waiting for it to settle, for a cancel its caller already authorized."""
        async with self._lock:
            self._ensure_open()
            entry = self._entries.get(job_id)
            if entry is not None and entry.job.status not in TERMINAL_STATUSES:
                await self._request_cancel(entry)

    async def cancel(self, job_id: str, *, owner: ToolExecutionIdentity, depth: int) -> BackgroundJob:
        """Ask a job its caller may see to stop, and return it as requested; the runtime drains it to its outcome."""
        async with self._lock:
            self._ensure_open(accepting=True)
            entry = self._entry(job_id, owner, depth)
            # A job that already ended still drains, which retries saving an outcome that failed to save.
            await self._request_cancel(entry)
            return await self._snapshot(entry)

    async def cancel_revoked(self, *, denied: Callable[[BackgroundJob], bool]) -> None:
        """Withdraw execution whose grant is proven revoked, retaining owned cleanup; a failed job retries next pass."""
        async with self._lock:
            self._ensure_open(accepting=True)
            revoked = [
                entry
                for entry in self._entries.values()
                if entry.job.status not in TERMINAL_STATUSES and denied(entry.job)
            ]
            for entry in revoked:
                # One failed job must not block the rest; it retries on the next pass.
                try:
                    await self._request_cancel(entry)
                except Exception:
                    logger.exception("Tool job revocation failed", job_id=entry.job.job_id)

    async def _request_cancel(self, entry: _Entry) -> asyncio.Task[BackgroundJob]:
        """Durably request cancellation and return its one drain; the caller holds the runtime lock."""

        async def request() -> asyncio.Task[BackgroundJob]:
            if entry.job.status in _RUNNING_STATUSES:
                await self._publish(entry, _updated(entry.job, status="cancel_requested"))
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
            await self._settle(entry, BackgroundOutcome("cancelled", _CANCELLED_REASON))
            async with self._lock:
                return await self._snapshot(entry)
        finally:
            # A failed drain leaves the job for the next canceller, recovery, or shutdown to settle.
            entry.drain = None

    async def _settle(self, entry: _Entry, default: BackgroundOutcome) -> None:
        """Clean up stopped execution and publish its terminal outcome, or retry saving one that already settled."""
        settling = entry.job.status not in TERMINAL_STATUSES
        outcome = await self._cleanup(entry) if settling else None
        async with self._lock:
            if settling:
                await self._publish_outcome(entry, self._settled(entry, outcome, default))
            elif not entry.saved:
                await self._publish(entry, entry.job)

    async def _cleanup(self, entry: _Entry) -> BackgroundOutcome | None:
        try:
            # Cleanup runs under the job's control, so its adapter can tell a shutdown or restart from a cancellation.
            with job_control_context(entry.control):
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

    async def ended_jobs(self, key: ConversationKey) -> tuple[BackgroundJob, ...]:
        """Return one conversation's ended top-level jobs that nobody acknowledged or reads now, oldest first.

        A job stays out while a waiter claims it, while its requester's access is unresolved or gone, and once its
        outcome was acknowledged; a silent schedule's jobs and visible ones are listed apart.
        """
        async with self._lock:
            if self._closed:
                return ()
            ended = [
                await self._snapshot(entry)
                for entry in list(
                    self._by_conversation.get(
                        (key.recipient, key.room_id, key.thread_id, key.requester_id),
                        {},
                    ).values(),
                )
                if entry.job.status in TERMINAL_STATUSES
                and entry.job.depth == 0
                and not entry.job.consumed
                and entry.claim is None
                and (entry.job.source_kind == SILENT_SCHEDULE_SOURCE_KIND) == key.silent
                and self._authorize(entry.job)
            ]
        return tuple(sorted(ended, key=lambda job: (job.created_at, job.job_id)))

    async def expire_ended(
        self,
        *,
        before: datetime,
        source_finished: Callable[[BackgroundJob], Awaitable[bool]],
    ) -> list[BackgroundJob]:
        """Delete old ended jobs, read or not, whose originating turn has finished, which never lets their call run again.

        A finished turn saved a non-`None` result for the call, its outcome or the job's handle, Agno runs a call again
        only when its saved result is `None`, and `source_finished` refuses while the job's session has an open approval
        continuation. An unread result this old is not worth reporting any more.
        Returns the deleted jobs.
        """
        deleted: list[BackgroundJob] = []
        async with self._lock:
            if self._closed or self._shutdown_task is not None:
                return deleted
            candidates = [
                await self._snapshot(entry) for entry in self._entries.values() if self._expirable(entry, before)
            ]
        for job in candidates:
            if not await source_finished(job):
                continue
            async with self._lock:
                if self._closed or self._shutdown_task is not None:
                    return deleted
                entry = self._entries.get(job.job_id)
                if entry is None or entry.job.updated_at != job.updated_at or not self._expirable(entry, before):
                    continue
                # A cancelled caller cannot separate the landed deletion from forgetting the job.
                await run_coroutine_until_complete(self._store.delete(job.job_id))
                self._remove_entry(job.job_id)
                deleted.append(job)
        return deleted

    def _expirable(self, entry: _Entry, before: datetime) -> bool:
        job = entry.job
        return (
            job.status in TERMINAL_STATUSES
            and entry.claim is None
            and entry.saved
            and datetime.fromisoformat(job.updated_at) < before
        )

    async def quiesce(self) -> None:
        """Drain owned execution while response finalizers can still acknowledge consumed results."""
        if self._shutdown_task is None:
            self.changed.set()
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await wait_for_future_until_complete(self._shutdown_task)

    async def shutdown(self) -> None:
        """Refuse further calls once execution and all remaining response owners have drained."""
        try:
            await self.quiesce()
        finally:
            await run_coroutine_until_complete(self._close())

    async def _close(self) -> None:
        """Refuse further calls once every admitted write finished."""
        async with self._lock:
            self._closed = True
            self.changed.set()

    async def _shutdown(self) -> None:
        """Drain execution and retry unsaved outcomes before the runtime closes."""
        tasks = []
        failures = []
        async with self._lock:
            for entry in self._entries.values():
                if entry.job.status not in TERMINAL_STATUSES:
                    # A cancellation requested before shutdown remains a cancellation.
                    entry.control.cancel(shutdown=entry.job.status != "cancel_requested")
                if entry.drain is not None:
                    # The cancellation request already cancelled execution; its drain settles it.
                    tasks.append(entry.drain)
                elif entry.task is not None and not entry.task.done():
                    entry.task.cancel()
                    tasks.append(entry.task)
        await asyncio.gather(*tasks, return_exceptions=True)
        for entry in self._entries.values():
            # A job cancelled before the shutdown ends cancelled, as its cleanup was told.
            stopped = entry.control.cancelled and not entry.control.shutdown
            try:
                await self._settle(entry, _stopped_outcome(cancelled=stopped, cause="shutdown"))
            except Exception as error:
                # A blocked native child stays unsettled for the next recovery; other jobs still settle.
                failures.append(error)
        if failures:
            msg = "Tool job shutdown failed"
            raise ExceptionGroup(msg, failures)
