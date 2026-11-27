"""Durable execution ownership, interruptible waits, and result consumption for application tool jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from operator import attrgetter
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

from mindroom.ai_runtime import queued_message_signal_context
from mindroom.approval.receipt_readers import without_approval_receipt
from mindroom.background_tasks import (
    run_blocking_until_complete,
    run_coroutine_until_complete,
    wait_for_future_until_complete,
)
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal import JOB_ENDED_REASON, REPLY_STOPPED_REASON, ToolJobSnapshotUpdate
from mindroom.failure_text import failure_text
from mindroom.logging_config import get_logger
from mindroom.shell.background_scope import bind_shell_background_scope
from mindroom.tool_jobs.background_after import validate_wait_seconds
from mindroom.tool_jobs.control import (
    JobControl,
    current_pending_human_messages,
    job_control_context,
    pending_human_messages_context,
)
from mindroom.tool_jobs.instances import tool_job_instance
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.progress import cap_progress, tool_progress_reporter
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import EventJournalStore, SavedToolJob, ToolJobStore
    from mindroom.tool_jobs.conversation import ConversationKey
    from mindroom.tool_jobs.execution_authority import JobTool

type _OutcomeStatus = Literal["completed", "failed", "cancelled", "denied", "interrupted"]
# A running job that waits for human decisions on the approval cards it posted is `awaiting_approval`.
type _BackgroundStatus = Literal["running", "awaiting_approval", "cancel_requested"] | _OutcomeStatus
# Statuses after which execution has ended for good, with an outcome a reply can retrieve.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "denied", "interrupted"})


def call_job_id(owner: ToolExecutionIdentity, run_id: str, call_id: str) -> str:
    """Return the deterministic job ID for one tool call of *owner* in *run_id*."""
    identity = {"owner": asdict(owner), "run_id": run_id, "tool_call_id": call_id}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


_RUNNING_STATUSES = frozenset({"running", "awaiting_approval"})
_UNAVAILABLE = "Tool job is not available in this conversation."
_JOB_PREFIX_PATTERN = re.compile(r"[a-f0-9]{8,63}")
_JOB_SUMMARY_MAX_CHARS = 500
# A reattaching call must name exactly the operation its job was admitted for.
_ADMITTED_OPERATION = attrgetter("tool_name", "toolkit_name", "kind", "source_event_id", "source_kind", "adapter")
# Ended jobs, read or not, are deleted this long after their last change, once their originating turn has finished.
_ENDED_JOB_RETENTION = timedelta(days=30)
# A job whose own call returned its result inline is never listed or reported once it ended, so after its turn only a
# reread by its exact ID reads it; it is deleted after this shorter margin instead.
_INLINE_JOB_RETENTION = timedelta(days=1)
logger = get_logger(__name__)
# A ToolResultPayload encoded by `tool_jobs.results`; the runtime stores it without reading it.
type EncodedResultPayload = dict[str, Any]
# An accepted job's execution, and the adapter cleanup that settles it after cancellation or interruption.
type _Operation = Callable[[], Awaitable[BackgroundOutcome]]
type _Cleanup = Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]]
# Whether a job a restart stops while it runs is kept for the runtime's creator to continue, instead of ending.
type _Continues = Callable[[BackgroundJob], bool]
# A job's current access: proven allowed, proven denied, or still resolving, such as while room membership loads.
type JobGrant = Literal["allowed", "denied", "pending"]
type FollowUpKey = tuple[str, str, str | None, str, bool]


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


@dataclass(frozen=True, slots=True, kw_only=True)
class JobSpec:
    """What one accepted job runs and how it is known.

    ``tool_name`` and ``kind`` are what people and models see: a tool's job is named by its function, a subagent's
    (``kind="delegation"``) by the agent it runs. ``adapter`` is the exact mapping the runtime admits, saves, and hands
    to cleanup. ``cancel`` settles the job's own work after a cancellation or shutdown in this process, before the
    runtime's cleanup for its kind. ``refused`` undoes what preparing the job wrote when the runtime refuses the job
    and holds none with its ID, and ``handle`` words what a call that stops waiting for the job returns, the job's
    summary when None.
    """

    tool_name: str
    kind: str = "tool"
    toolkit_name: str | None = None
    adapter: dict[str, Any]
    operation: _Operation
    cancel: _Cleanup | None = None
    refused: Callable[[], Awaitable[object]] | None = None
    handle: Callable[[BackgroundJob], str] | None = None


class ManagedJobToolkit:
    """A toolkit that prepares the job a managed call of one of its functions runs as.

    A toolkit opts in by this class, which only the framework's own toolkits have: a method that a plugin happens
    to name `managed_job` opts nothing in.
    """

    async def managed_job(
        self,
        function_name: str,
        arguments: dict[str, Any],
        *,
        job_id: str,
        runtime: ToolJobRuntime,
    ) -> JobSpec | str | None:
        """Return the job, the sentence that refuses the call, or None for a function that runs as any tool does."""
        raise NotImplementedError


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
    # The reply whose call started it, whose Stop refuses the job every later approval, and the Stop that reply had
    # recorded by then (its receipt order), so a later one is told apart; None outside a reply.
    reply_id: str | None = None
    reply_stop_order: int | None = None
    # The latest progress its running tool reported, as of this snapshot; live state that is never saved.
    progress: str | None = None


@dataclass(frozen=True)
class JobClaim:
    """One waiter's exclusive right to consume a job's outcome until it acknowledges or releases it."""

    nonce: str


def is_workflow_node_job(job: BackgroundJob) -> bool:
    """Return whether the job is one call of a workflow run, which the run reports.

    Such a job is no job of its own to anyone: it is in no list, no room state, no follow-up turn and no notice,
    also when it ended while MindRoom was down.
    """
    child = job.adapter.get("child")
    return job.kind == "delegation" and isinstance(child, dict) and bool(child.get("workflow_run_id"))


def waited_for_approval(job: BackgroundJob) -> bool:
    """Return whether the job is a workflow run's call whose agent asked for approvals and still waits for some.

    Its adapter says so (`wait`), from before the cards are posted until every one is decided, so no other wait for a
    person is taken for it. Only such a job goes on from that wait after a restart: its paused turn is resumed.
    """
    return is_workflow_node_job(job) and job.adapter.get("wait") == "approval"


def waited_for_user(job: BackgroundJob) -> bool:
    """Return whether the job is a workflow run's call waiting on its own `ask_user` question."""
    return is_workflow_node_job(job) and job.adapter.get("wait") == "user"


def _stopped_while_working(job: BackgroundJob) -> bool:
    """Return whether a job that a restart stopped was at work, or waited on a workflow approval or user question."""
    return job.status == "running" or (
        job.status == "awaiting_approval" and (waited_for_approval(job) or waited_for_user(job))
    )


@dataclass(frozen=True, slots=True)
class _ConversationWorkingState:
    """One conversation's live approval, active-job, and unconsumed ended-job state."""

    awaiting_approval: bool = False
    has_active: bool = False
    ended_jobs: tuple[BackgroundJob, ...] = ()


@dataclass(frozen=True, slots=True)
class AcknowledgeWait:
    """One claimed outcome to mark consumed in a batched acknowledgement."""

    job_id: str
    claim: JobClaim | None
    returned_inline: bool = False
    adapter: Mapping[str, Any] | None = None


def _updated(job: BackgroundJob, **changes: object) -> BackgroundJob:
    """Return the next state of a job, stamped with its transition time."""
    return replace(job, **changes, updated_at=datetime.now(UTC).isoformat())


def _parse_saved_job(saved: SavedToolJob) -> BackgroundJob:
    """Validate one saved snapshot without claiming or changing its execution."""
    msg = f"Invalid tool job snapshot {saved.job_id!r}."
    try:
        payload = json.loads(saved.job_json)
        payload["owner"] = parse_tool_execution_identity_payload(payload["owner"])
        job = BackgroundJob(**payload)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(msg) from error
    sources = (job.source_event_id, job.source_kind)
    if job.job_id != saved.job_id or not all(isinstance(value, str | None) for value in sources):
        raise ValueError(msg)
    return job


def _parse_saved_jobs(saved_jobs: Sequence[SavedToolJob]) -> tuple[BackgroundJob, ...]:
    """Validate saved snapshots in order without claiming or changing their execution."""
    return tuple(_parse_saved_job(saved) for saved in saved_jobs)


def _parse_valid_saved_jobs(saved_jobs: Sequence[SavedToolJob]) -> tuple[BackgroundJob, ...]:
    """Parse saved snapshots, skipping malformed legacy rows so reads of healthy cold jobs stay available."""
    parsed: list[BackgroundJob] = []
    for saved in saved_jobs:
        with suppress(ValueError):
            parsed.append(_parse_saved_job(saved))
    return tuple(parsed)


def _encoded(job: BackgroundJob, payload: EncodedResultPayload | None) -> tuple[str, str | None]:
    return json.dumps(asdict(job)), None if payload is None else json.dumps(payload)


@dataclass(frozen=True, slots=True)
class _PendingUpdate:
    entry: _Entry
    job: BackgroundJob
    payload: EncodedResultPayload | None
    approval_end_reason: str | None


def _encode_updates(items: Sequence[_PendingUpdate]) -> tuple[ToolJobSnapshotUpdate, ...]:
    return tuple(
        ToolJobSnapshotUpdate(item.job.job_id, *_encoded(item.job, item.payload), item.approval_end_reason)
        for item in items
    )


def _acknowledged_job(
    job: BackgroundJob,
    *,
    returned_inline: bool,
    adapter: Mapping[str, Any] | None,
) -> BackgroundJob:
    # A later reread of an inline call's result does not make it a job.
    updated = _updated(job, consumed=True, returned_inline=job.returned_inline or returned_inline)
    if adapter and not job.consumed:
        # A copy: a save that fails leaves the job as it was, and a job given nothing keeps its admitted adapter.
        updated = replace(updated, adapter={**updated.adapter, **adapter})
    return updated


def _approval_end_reason(entry: _Entry, job: BackgroundJob) -> str | None:
    # A cancel request ends the job's approvals as its end does, so its save denies the pending cards.
    if job.status in TERMINAL_STATUSES or job.status == "cancel_requested":
        return REPLY_STOPPED_REASON if entry.control.reply_stopped else JOB_ENDED_REASON
    return None


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
    if job.kind == "delegation":
        summary["subagent_id"] = job.adapter["child"]["subagent_id"]
    if job.progress is not None and job.status not in TERMINAL_STATUSES:
        summary["progress"] = job.progress
    return summary


# A cancelled call's text, so the model does not take an empty failure as a reason to run it again.
_CANCELLED_REASON = (
    "This call was cancelled before it returned a result. Its effects so far stand: do not call it again unless "
    "the user asks."
)
# What a job ends with that a restart stopped and would have continued, had its user not stopped the reply that
# started it. The interruption's own reason invites a follow-up, which would run the work the user stopped.
_REPLY_STOPPED_REASON = (
    "The user stopped the reply that started this job, so it was not continued after a restart interrupted it. "
    "Do not continue it or repeat its work unless the user asks."
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
    control: JobControl = field(init=False)
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
    # Whether the job's approval cards wait for decisions (`set_awaiting_approval`), and how many other human decisions
    # its calls wait for at once (`human_decision_wait`); the job is `awaiting_approval` while either is set.
    approval_cards_open: bool = False
    human_waits: int = 0
    # How the restart that stopped the job would have ended it, set once this process kept the job running instead;
    # without a task the job waits to be continued, or to end this way.
    interruption: BackgroundOutcome | None = None

    def __post_init__(self) -> None:
        """Give the job's control its ID, which a call running in the job reads (`current_job_id`)."""
        self.control = JobControl(job_id=self.job.job_id)

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


def _ended_room_key(job: BackgroundJob | None) -> tuple[str, str] | None:
    if job is None or job.status not in TERMINAL_STATUSES or job.owner.room_id is None:
        return None
    return (job.owner.recipient, job.owner.room_id)


def _follow_up_candidate_key(job: BackgroundJob | None) -> FollowUpKey | None:
    if (
        job is None
        or job.status not in TERMINAL_STATUSES
        or job.depth != 0
        or job.consumed
        or is_workflow_node_job(job)
    ):
        return None
    owner = job.owner
    if owner.room_id is None or owner.requester_id is None:
        return None
    silent = job.source_kind == SILENT_SCHEDULE_SOURCE_KIND
    return (owner.recipient, owner.room_id, owner.resolved_thread_id, owner.requester_id, silent)


def _report_failed_drain(job_id: str, drain: asyncio.Task[BackgroundJob]) -> None:
    """Log a failed cancellation drain with its job, which stays for a later canceller, recovery, or shutdown."""
    if not drain.cancelled() and (error := drain.exception()) is not None:
        logger.error("Tool job cancellation drain failed", job_id=job_id, exc_info=error)


async def saved_jobs(
    journal: EventJournalStore,
    *,
    delegation_ids: Sequence[str] | None = None,
) -> list[BackgroundJob]:
    """Read the saved jobs that parse, owning none: an instance that runs no jobs keeps them for one that does."""
    store = journal.tool_jobs("reader")
    rows = await store.load_all() if delegation_ids is None else await store.load_by_delegation_ids(delegation_ids)
    return list(_parse_valid_saved_jobs(rows))


def workflow_job_standing(runtime_paths: RuntimePaths, job_id: str) -> str:
    """Return what the job runtime knows of *job_id*, or `unknown` while no runtime exists."""
    runtime = get_background_runtime(runtime_paths)
    return "unknown" if runtime is None else runtime._job_standing(job_id)


def workflow_job_admission(runtime_paths: RuntimePaths, job_id: str) -> asyncio.Lock:
    """Return the lock a delivery of *job_id* holds while its job is admitted."""
    runtime = get_background_runtime(runtime_paths)
    assert runtime is not None
    return runtime.admission(job_id)


def undelivered_workflow_jobs(runtime_paths: RuntimePaths) -> frozenset[str] | None:
    """Return job IDs whose terminal outcome is unsaved, or None while no runtime exists."""
    runtime = get_background_runtime(runtime_paths)
    return None if runtime is None else runtime._undelivered_jobs()


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


@dataclass
class _WorkAllowance:
    """The seconds of a job's work a wait still waits for; time the job spends awaiting approval does not count."""

    left: float
    since: float
    paused: bool = False

    def deadline(self, job: BackgroundJob, now: float) -> float | None:
        """Spend the work since the last look; return when the allowance ends, None while the job awaits approval."""
        if not self.paused:
            self.left -= now - self.since
        self.since, self.paused = now, job.status == "awaiting_approval"
        return None if self.paused else now + self.left


class _HumanWaitPause:
    """Enter a caller-supplied pause context while an awaited job is ``awaiting_approval``."""

    def __init__(self, factory: Callable[[], AbstractContextManager[None]] | None) -> None:
        self._factory = factory
        self._active: AbstractContextManager[None] | None = None

    def update(self, *, awaiting: bool) -> None:
        if awaiting and self._active is None and self._factory is not None:
            cm = self._factory()
            cm.__enter__()
            self._active = cm
        elif not awaiting and self._active is not None:
            self.close()

    def close(self) -> None:
        if self._active is not None:
            active, self._active = self._active, None
            active.__exit__(None, None, None)


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
        grant: Callable[[BackgroundJob], JobGrant],
        authorize_execution: Callable[[ToolExecutionIdentity, JobTool, Mapping[str, Any]], None],
        cancel: _Cleanup,
        continues: _Continues = lambda _job: False,
        reply_stopped: Callable[[BackgroundJob], Awaitable[bool]] | None = None,
    ) -> None:
        # Saves land only while this runtime's generation owns the jobs, which its creator takes before recovery.
        self._store = store
        self._grant = grant
        # Rechecks a retained function's current authority immediately before application entry; raises if revoked.
        self.authorize_execution = authorize_execution
        self._cancel = cancel
        self._continues = continues
        # Reads whether a job's reply recorded a Stop since the job began; the reply's record is a Stop's authority.
        self._reply_stopped = reply_stopped
        self._entries: dict[str, _Entry] = {}
        # Conversation lookups read this instead of scanning every entry; `_add_entry` and `_remove_entry` keep it.
        # Jobs are keyed by their recipient, room, thread, and requester.
        self._by_conversation: dict[tuple[str, str | None, str | None, str | None], dict[str, _Entry]] = {}
        self._live: dict[str, _Entry] = {}
        self._claimed: set[str] = set()
        self._follow_up_candidates: dict[FollowUpKey, dict[str, _Entry]] = {}
        self._follow_up_revisions: dict[FollowUpKey, int] = {}
        self._candidate_revision = 0
        self._ended_room_counts: dict[tuple[str, str], int] = {}
        self._cold_ended_rooms: frozenset[tuple[str, str]] = frozenset()
        self._lock = asyncio.Lock()
        self._admissions: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self.changed = asyncio.Event()

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

    def retained_job(self, job_id: str) -> BackgroundJob | None:
        """Return the retained job with *job_id*, or None if not retained."""
        entry = self._entries.get(job_id)
        return None if entry is None else entry.job

    def _outcome_saved(self, job_id: str) -> bool:
        """Return whether the journal holds the job as it is here; false while an outcome that failed to save waits."""
        return self._entries[job_id].saved

    def live_jobs(self) -> tuple[BackgroundJob, ...]:
        """Return every non-terminal job this runtime retains, unfiltered by access."""
        return tuple(entry.job for entry in self._live.values() if entry.job.status not in TERMINAL_STATUSES)

    def ended_rooms(self) -> frozenset[tuple[str, str]]:
        """Return every ``(recipient, room_id)`` pair that currently retains at least one terminal job."""
        return frozenset(self._ended_room_counts) | self._cold_ended_rooms

    async def has_job(self, job_id: str) -> bool:
        """Return whether this runtime or its journal holds a job with *job_id*."""
        if job_id in self._entries:
            return True
        try:
            return await self._store.load_by_id(job_id) is not None
        except Exception:
            if self._closed:
                return False
            raise

    async def delegation_jobs(self, delegation_ids: Sequence[str]) -> tuple[BackgroundJob, ...]:
        """Return hot and cold delegation jobs matching *delegation_ids*, without hydrating cold rows."""
        wanted = tuple(dict.fromkeys(delegation_ids))
        if not wanted:
            return ()
        wanted_set = frozenset(wanted)
        hot: list[BackgroundJob] = []
        found: set[str] = set()
        for entry in self._entries.values():
            if entry.job.kind != "delegation" or not isinstance(entry.job.adapter, dict):
                continue
            child = entry.job.adapter.get("child")
            if isinstance(child, dict) and isinstance(did := child.get("delegation_id"), str) and did in wanted_set:
                hot.append(entry.job)
                found.add(did)
        missing = tuple(did for did in wanted if did not in found)
        if not missing:
            return tuple(hot)
        cold = await self._parse_cold_rows(await self._store.load_by_delegation_ids(missing))
        return (*hot, *cold)

    def follow_up_candidate_revisions(self) -> dict[FollowUpKey, int]:
        """Return the current revision for every conversation with unconsumed depth-0 terminal candidates."""
        return dict(self._follow_up_revisions)

    def follow_up_candidate_jobs(self, key: FollowUpKey) -> tuple[BackgroundJob, ...]:
        """Return one conversation's unconsumed depth-0 terminal candidates."""
        return tuple(entry.job for entry in self._follow_up_candidates.get(key, {}).values())

    def is_follow_up_candidate(self, job_id: str) -> bool:
        """Return whether *job_id* is still retained as an unconsumed depth-0 terminal candidate."""
        return (entry := self._entries.get(job_id)) is not None and _follow_up_candidate_key(entry.job) is not None

    def awaited_job_ids(self) -> frozenset[str]:
        """Return the jobs whose outcome a call holds the claim on, as the call that started one does until released."""
        return frozenset(self._claimed)

    def _bump_candidate_revision(self, key: FollowUpKey) -> None:
        self._candidate_revision += 1
        self._follow_up_revisions[key] = self._candidate_revision

    def _sync_follow_up_candidate(self, entry: _Entry, prev: BackgroundJob | None, job: BackgroundJob | None) -> None:
        old, new = _follow_up_candidate_key(prev), _follow_up_candidate_key(job)
        if old == new:
            if new and prev and job and (prev.status != job.status or prev.source_event_id != job.source_event_id):
                self._bump_candidate_revision(new)
            return
        if old and (bucket := self._follow_up_candidates.get(old)) and bucket.pop(entry.job.job_id, None):
            if bucket:
                self._bump_candidate_revision(old)
            else:
                del self._follow_up_candidates[old], self._follow_up_revisions[old]
        if new:
            self._follow_up_candidates.setdefault(new, {})[entry.job.job_id] = entry
            self._bump_candidate_revision(new)

    def _sync_indexes(self, entry: _Entry, previous: BackgroundJob | None = None, *, removed: bool = False) -> None:
        job, job_id = (None if removed else entry.job), entry.job.job_id
        if job is None or job.status in TERMINAL_STATUSES:
            self._live.pop(job_id, None)
        else:
            self._live[job_id] = entry
        old_room, new_room = _ended_room_key(previous), _ended_room_key(job)
        if old_room != new_room:
            if old_room is not None:
                if (remaining := self._ended_room_counts.get(old_room, 0) - 1) > 0:
                    self._ended_room_counts[old_room] = remaining
                else:
                    self._ended_room_counts.pop(old_room, None)
            if new_room is not None:
                self._ended_room_counts[new_room] = self._ended_room_counts.get(new_room, 0) + 1
        self._sync_follow_up_candidate(entry, previous, job)

    def _apply_published(
        self,
        entry: _Entry,
        job: BackgroundJob,
        *,
        saved: bool = True,
        payload: EncodedResultPayload | None = None,
    ) -> None:
        previous = entry.job
        entry.job, entry.saved, entry.unsaved_payload = job, saved, payload
        if saved and job.status in TERMINAL_STATUSES:
            # A durable terminal outcome ends execution; drop what only running work needed.
            entry.cancel, entry.task = None, None
        if job.job_id in self._entries:
            self._sync_indexes(entry, previous)
        entry.notify_changed()

    def _claim_for(self, entry: _Entry, claim: JobClaim | None) -> JobClaim | None:
        claimed = entry.claim_for(claim)
        if entry.claim is not None and self._entries.get(entry.job.job_id) is entry:
            self._claimed.add(entry.job.job_id)
        return claimed

    def _clear_claim(self, entry: _Entry) -> None:
        entry.claim = None
        self._claimed.discard(entry.job.job_id)

    def _ensure_hot(self, entry: _Entry) -> None:
        """Hydrate a cold entry into memory before attaching an in-process claim."""
        if entry.job.job_id not in self._entries:
            self._add_entry(entry)

    def _claim_waited_entry(self, entry: _Entry, *, readable: bool, claim: JobClaim | None) -> JobClaim | None:
        """Hydrate and claim a readable entry, or release any previously held claim when unreadable."""
        if readable:
            self._ensure_hot(entry)
            return self._claim_for(entry, claim)
        if claim is not None and entry.claim == claim:
            # A wait that cannot read its job returns no claim, so it gives back the one it was handed.
            self._clear_claim(entry)
            self.changed.set()
        return None

    def _add_entry(self, entry: _Entry) -> None:
        """Make an accepted job current and findable by its conversation, which never changes."""
        job = entry.job
        self._entries[job.job_id] = entry
        self._by_conversation.setdefault(_conversation_key(job.owner), {})[job.job_id] = entry
        self._sync_indexes(entry)
        if entry.claim is not None:
            self._claimed.add(job.job_id)

    def _remove_entry(self, job_id: str) -> None:
        entry = self._entries.pop(job_id)
        key = _conversation_key(entry.job.owner)
        group = self._by_conversation[key]
        del group[job_id]
        if not group:
            del self._by_conversation[key]
        self._claimed.discard(job_id)
        self._sync_indexes(entry, entry.job, removed=True)

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
        return self._owns(entry.job, owner, depth) and self._grant(entry.job) == "allowed"

    @staticmethod
    def _is_cold_job(job: BackgroundJob) -> bool:
        """Whether *job* belongs to the cold partition omitted from startup hydration."""
        return (
            job.status in TERMINAL_STATUSES
            and job.consumed
            and job.kind != "workflow"
            and not is_workflow_node_job(job)
        )

    async def _parse_cold_rows(self, saved: Sequence[SavedToolJob]) -> tuple[BackgroundJob, ...]:
        """Parse saved rows absent from memory so hot entries always shadow DB snapshots."""
        cold_raw = [item for item in saved if item.job_id not in self._entries]
        return await asyncio.to_thread(_parse_valid_saved_jobs, cold_raw) if cold_raw else ()

    async def _find_exact_entry(self, job_id: str) -> _Entry | None:
        if (entry := self._entries.get(job_id)) is not None:
            return entry
        saved = await self._store.load_by_id(job_id)
        if saved is None:
            return None
        job = _parse_saved_job(saved)
        return _Entry(job) if self._is_cold_job(job) else None

    async def _resolve_entry(self, job_id: str, owner: ToolExecutionIdentity, depth: int) -> _Entry | None:
        if (entry := await self._find_exact_entry(job_id)) is not None or not _JOB_PREFIX_PATTERN.fullmatch(job_id):
            return entry
        group = self._by_conversation.get(_conversation_key(self._canonical_owner(owner)), {})
        matches: dict[str, _Entry] = {
            cid: item
            for cid, item in group.items()
            if cid.startswith(job_id) and not _returned_inline(item) and self._visible(item, owner, depth)
        }
        cold_jobs = await self._parse_cold_rows(await self._store.load_by_prefix(job_id))
        for job in cold_jobs:
            if not job.returned_inline:
                cold_entry = _Entry(job)
                if self._visible(cold_entry, owner, depth):
                    matches[job.job_id] = cold_entry
        if len(matches) > 1:
            ids = ", ".join(sorted(matches))
            msg = f'Job ID prefix "{job_id}" matches several jobs in this conversation: {ids}. Pass the full ID.'
            raise ValueError(msg)
        return next(iter(matches.values()), None)

    async def _entry(self, job_id: str, owner: ToolExecutionIdentity, depth: int) -> _Entry:
        self._ensure_open()
        entry = await self._resolve_entry(job_id, owner, depth)
        if entry is None or not self._visible(entry, owner, depth):
            raise JobUnavailableError(_UNAVAILABLE)
        return entry

    def _check_waited_entry(
        self,
        entry: _Entry | None,
        owner: ToolExecutionIdentity,
        depth: int,
    ) -> tuple[_Entry, bool]:
        if entry is None or not self._owns(entry.job, owner, depth):
            raise JobUnavailableError(_UNAVAILABLE)
        grant = self._grant(entry.job)
        if grant == "denied":
            raise JobUnavailableError(_UNAVAILABLE)
        return entry, grant == "allowed"

    async def _waited_entry(self, job_id: str, owner: ToolExecutionIdentity, depth: int) -> tuple[_Entry, bool]:
        """Return a job its caller may wait on, and whether its outcome is readable now.

        Only an unknown or foreign job, or a proven denial, refuses the wait; while the caller's access still
        resolves, as after a membership refresh, the job stays held, so the wait is released instead of failing a
        call that may have run.
        """
        self._ensure_open()
        return self._check_waited_entry(await self._resolve_entry(job_id, owner, depth), owner, depth)

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
                await self._store.save(
                    job.job_id,
                    job_json,
                    payload_json,
                    approval_end_reason=_approval_end_reason(entry, job),
                )
            self._apply_published(entry, job)
            self.changed.set()

        # A cancelled caller cannot separate a landed write from its in-memory publication.
        await run_coroutine_until_complete(publish())

    async def _publish_outcome(self, entry: _Entry, outcome: BackgroundOutcome) -> None:
        """Publish work that already ran with a bounded summary; a failed save keeps it for a later publish to retry.

        An interruption of a job that restarts continue, whose reply was stopped, says the user stopped it: every
        path that ends such a job instead of continuing it publishes here.
        """
        if outcome.status == "interrupted" and entry.control.reply_stopped and self._continues(entry.job):
            outcome = BackgroundOutcome("interrupted", _REPLY_STOPPED_REASON)
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
            self._apply_published(entry, job, saved=False, payload=outcome.result_payload)
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
        """Restore the jobs this runtime took over, never replaying execution.

        An unfinished job ends as the restart left it, except one kept for `continue_stopped` (`_keeps`). A runtime
        that a newer one replaced refuses, rather than adopting that runtime's jobs.
        """
        async with self._lock:
            self._ensure_open()
            await self._store.require_ownership()
            self._cold_ended_rooms = await self._store.load_ended_rooms()
            pending = [saved for saved in await self._store.load_startup() if saved.job_id not in self._entries]
            if not pending:
                return
            for job in await asyncio.to_thread(_parse_saved_jobs, pending):
                entry = _Entry(job)
                if entry.job.status not in TERMINAL_STATUSES:
                    await self._settle_stopped(entry)
                self._add_entry(entry)

    def _keeps(self, entry: _Entry, settled: BackgroundOutcome) -> bool:
        """Whether a job a restart stopped stays running, to be continued, instead of ending as ``settled``.

        Only an interruption is undecided: an outcome the job's own work or its cleanup reached stands. Only a job
        that worked, of a kind the runtime's creator continues, stays: one with a cancel request or waiting for a
        person's decision ends, as does one whose reply was stopped. The call of a workflow run that waited for an
        approval stays too (`_stopped_while_working`).
        """
        job = entry.job
        return (
            settled.status == "interrupted"
            and not (job.kind == "workflow" and settled.result_payload is not None)
            and _stopped_while_working(job)
            and not entry.control.reply_stopped
            and self._continues(job)
        )

    async def _settle_stopped(self, entry: _Entry) -> None:
        """Clean up what a restart stopped and end the job, or keep it for `continue_stopped`; the caller holds the lock.

        Only work the restart cut short is interrupted by it; a cancellation saved before it is not. A Stop the
        process that recorded it did not live to apply to the job is read from the reply's record, so the job ends as
        one whose reply was stopped (`_publish_outcome`).
        """
        cancelled = entry.job.status == "cancel_requested"
        entry.control.cancel(shutdown=not cancelled)
        if self._reply_stopped is not None and self._continues(entry.job):
            try:
                entry.control.reply_stopped = await self._reply_stopped(entry.job)
            except Exception:
                # A record that cannot be read ends its job as stopped, and the other jobs still recover.
                logger.warning("Reply of a stopped job is unreadable", job_id=entry.job.job_id, exc_info=True)
                entry.control.reply_stopped = True
        outcome = await self._cleanup(entry)
        settled = self._settled(entry, outcome, _stopped_outcome(cancelled=cancelled, cause="restart"))
        if self._keeps(entry, settled):
            # Its next cause of stopping, a cancel or another restart, is told apart from this one.
            entry.control, entry.interruption = JobControl(job_id=entry.job.job_id), settled
        else:
            await self._publish_outcome(entry, settled)

    @staticmethod
    def _awaits_continuation(entry: _Entry) -> bool:
        return entry.interruption is not None and entry.task is None and _stopped_while_working(entry.job)

    def awaiting_continuation(self) -> tuple[BackgroundJob, ...]:
        """Return the jobs a restart stopped that wait for `continue_stopped`, which continues or settles each."""
        return tuple(entry.job for entry in self._live.values() if self._awaits_continuation(entry))

    def stopped_by_restart(self, key: ConversationKey) -> tuple[BackgroundJob, ...]:
        """Return one conversation's jobs that a restart stopped and that go on: they were continued, or wait to be.

        A job being cancelled is left out, and so is one whose requester's access is unresolved or gone.
        """
        entries = self._by_conversation.get((key.recipient, key.room_id, key.thread_id, key.requester_id), {})
        return tuple(
            entry.job
            for entry in entries.values()
            if entry.interruption is not None
            and entry.job.status in _RUNNING_STATUSES
            and not is_workflow_node_job(entry.job)
            and self._grant(entry.job) == "allowed"
        )

    async def continue_stopped(self, job_id: str, spec: JobSpec | BackgroundOutcome | None) -> None:
        """Run a job a restart stopped on as ``spec``, settle a decided outcome, or end it interrupted when None.

        A job that was continued, cancelled, or settled since it was listed is left alone. A Stop of its reply that
        marked it while it waited ends it, and it stays as it is while its requester's access is no longer allowed:
        revocation cancels it, or a later pass continues it.
        """
        async with self._lock:
            self._ensure_open(accepting=True)
            entry = self._entries.get(job_id)
            if entry is None or entry.interruption is None or not self._awaits_continuation(entry):
                return
            if isinstance(spec, BackgroundOutcome):
                await self._publish_outcome(entry, spec)
            elif spec is None or entry.control.reply_stopped:
                await self._publish_outcome(entry, entry.interruption)
            elif self._grant(entry.job) == "allowed":
                await run_coroutine_until_complete(self._relaunch(entry, spec))

    async def _relaunch(self, entry: _Entry, spec: JobSpec) -> None:
        """Save a stopped job with the adapter it continues on, then launch it; callers finish this first.

        The save comes first, so a later restart finds what the job ran. The requester's access is checked again
        after it, with no await before the task's creation. A Stop of the job's reply that lands from here on has the
        effect a Stop has on a working job (`cancel_stopped_reply`): the job is refused its next approval.
        """
        job = _updated(entry.job, adapter=spec.adapter)
        # A job that goes on with the turn that waited for approvals still waits; any other works.
        await self._publish(entry, job if waited_for_approval(job) else replace(job, status="running"))
        if self._grant(entry.job) == "allowed":
            entry.cancel = spec.cancel
            entry.task = asyncio.create_task(self._run(entry, spec.operation), name=f"tool-job:{entry.job.job_id}")

    def _job_delivered(self, job: BackgroundJob) -> bool:
        return job.status in TERMINAL_STATUSES and self._outcome_saved(job.job_id)

    def _job_standing(self, job_id: str) -> str:
        """Return what this runtime knows of *job_id*: `absent`, `stopped`, `ended`, or `held`."""
        job = self.retained_job(job_id)
        if job is None:
            return "absent"
        if self._job_delivered(job):
            return "stopped" if job.status in ("cancelled", "denied") else "ended"
        return "held"

    def _undelivered_jobs(self) -> frozenset[str]:
        """Return the job IDs whose terminal outcome has not been saved yet."""
        return frozenset(job.job_id for job in self.retained_jobs() if not self._job_delivered(job))

    async def start_spec(
        self,
        job_id: str,
        spec: JobSpec,
        *,
        owner: ToolExecutionIdentity,
        depth: int,
        source_kind: str | None = None,
    ) -> tuple[BackgroundJob, JobClaim | None]:
        """Start a prepared *spec* as *job_id* for *owner* at *depth*, passing the live adapter reference."""
        return await self.start(
            job_id,
            tool_name=spec.tool_name,
            depth=depth,
            kind=spec.kind,
            toolkit_name=spec.toolkit_name,
            source_kind=source_kind,
            adapter=spec.adapter,
            owner=owner,
            operation=spec.operation,
            cancel=spec.cancel,
        )

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
        reply_id: str | None = None,
        reply_stop_order: int | None = None,
        span_stopped: Callable[[], bool] | None = None,
    ) -> tuple[BackgroundJob, JobClaim | None]:
        """Durably accept exact operation ownership before spawning execution, claiming its outcome for the caller.

        A job whose reply span ``span_stopped`` says a Stop reached is cancelled before it runs.
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
                # A subagent job updates this exact mapping; owns_execution verifies its identity.
                adapter=adapter,
                reply_id=reply_id,
                reply_stop_order=reply_stop_order,
            )
            readable = True
            existing = await self._find_exact_entry(job_id)
            if reattach and existing is not None:
                entry, readable = self._check_waited_entry(existing, owner, depth)
                if _ADMITTED_OPERATION(entry.job) != _ADMITTED_OPERATION(job):
                    raise JobAccessError(_UNAVAILABLE)
                if readable:
                    self._ensure_hot(entry)
            else:
                if existing is not None or await self._store.load_by_id(job_id) is not None:
                    msg = "Tool job already exists."
                    raise ValueError(msg)
                if not owner.session_id or depth < 0 or self._grant(job) != "allowed":
                    raise JobAccessError(_UNAVAILABLE)
                entry = _Entry(job, cancel=cancel)
                await run_coroutine_until_complete(self._admit(entry, operation, span_stopped))
            snapshot = await self._snapshot(entry)
            return snapshot, self._claim_for(entry, None) if readable else None

    async def _admit(self, entry: _Entry, operation: _Operation, span_stopped: Callable[[], bool] | None) -> None:
        """Publish accepted ownership, then launch it unless its reply span was stopped; callers finish this first."""
        await self._publish(entry, entry.job, accepted=True)
        self._add_entry(entry)
        if span_stopped is not None and span_stopped():
            await self._request_cancel(entry)
            return
        entry.task = asyncio.create_task(self._run(entry, operation), name=f"tool-job:{entry.job.job_id}")

    def admission(self, job_id: str) -> asyncio.Lock:
        """Return the lock one delivery of a call holds from preparing its job until the runtime answered its start.

        Deliveries of the same call take turns, so what a refused one takes back was prepared by no other.
        """
        return self._admissions.setdefault(job_id, asyncio.Lock())

    def owns_execution(self, job_id: str, adapter: dict[str, Any]) -> bool:
        """Recognize exact accepted adapter ownership even if its former waiter was cancelled."""
        entry = self._entries.get(job_id)
        return entry is not None and entry.job.adapter is adapter and entry.task is not None

    async def set_awaiting_approval(self, job_id: str, *, awaiting: bool) -> None:
        """Publish whether a running job waits for human decisions on the approval cards it posted."""
        async with self._lock:
            entry = self._entries[job_id]
            entry.approval_cards_open = awaiting
            await self._publish_awaiting(entry)

    @asynccontextmanager
    async def human_decision_wait(self, job_id: str) -> AsyncIterator[None]:
        """Keep a running job `awaiting_approval` while one of its calls waits for a person, as approval cards do.

        Concurrent waits are counted, so the job is `running` again only when its last wait and its approval cards
        are done, and a Stop of its reply cancels it while any wait remains. A wait that cancellation unwinds leaves
        the job saved as waiting, as an approval-card wait does: its task is being stopped, and a restart must not
        take a job that waited for a person for one that worked.
        """
        async with self._lock:
            entry = self._entries[job_id]
            entry.human_waits += 1
            await self._publish_awaiting(entry)
        cancelled = False
        try:
            yield
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            await run_coroutine_until_complete(self._end_human_wait(entry, publish=not cancelled))

    async def _end_human_wait(self, entry: _Entry, *, publish: bool) -> None:
        async with self._lock:
            entry.human_waits -= 1
            if publish:
                await self._publish_awaiting(entry)

    async def _publish_awaiting(self, entry: _Entry) -> None:
        """Publish whether a running job waits for a person; the caller holds the lock."""
        status = "awaiting_approval" if entry.approval_cards_open or entry.human_waits else "running"
        if entry.job.status in _RUNNING_STATUSES and entry.job.status != status:
            await self._publish(entry, _updated(entry.job, status=status))

    async def save_adapter(self, job_id: str, **changes: object) -> None:
        """Save a running job with ``changes`` to its adapter, as the job stands now; an ended job keeps its own."""
        async with self._lock:
            entry = self._entries[job_id]
            if entry.job.status not in TERMINAL_STATUSES:
                # The adapter is the exact mapping the job was admitted with, so its operation and cleanup see it too.
                entry.job.adapter.update(changes)
                await self._publish(entry, _updated(entry.job))

    async def _run(self, entry: _Entry, operation: _Operation) -> None:  # noqa: C901
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
                    # The job is not part of an approval continuation that starts it: a run the job resumes with
                    # approvals of its own never reads that continuation's receipt.
                    without_approval_receipt(),
                    tool_progress_reporter(report_progress, forward=True),
                    # The reply span that started the job may end first; the job owns its calls' outcome.
                    recording_tool_calls(None),
                    job_control_context(entry.control),
                    # Managed jobs outlive the reply that started them, so they never inherit its shell scope.
                    bind_shell_background_scope(None),
                    # A run the job makes is told of no newer message: the reply that waits for the job is.
                    queued_message_signal_context(None),
                ):
                    try:
                        async with execution_resources():
                            outcome = await operation()
                    finally:
                        returned = True
            except Exception as error:
                outcome = BackgroundOutcome("failed", failure_text(error))
            except asyncio.CancelledError:
                if (task := asyncio.current_task()) is not None and task.cancelling():
                    raise  # Requested cancellation: runtime-owned, or external teardown.
                if entry.control.reply_stopped:
                    await self.request_cancel(entry.job.job_id)
                    raise
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
            if not owner.session_id:
                return []
            entries = [
                entry
                for entry in self._entries.values()
                if not _returned_inline(entry)
                and not is_workflow_node_job(entry.job)
                and self._visible(entry, owner, depth)
            ]
            cold_jobs = await self._parse_cold_rows(
                await self._store.load_listable(recipient=owner.recipient, room_id=owner.room_id),
            )
            for job in cold_jobs:
                if not job.returned_inline and not is_workflow_node_job(job):
                    cold_entry = _Entry(job)
                    if self._visible(cold_entry, owner, depth):
                        entries.append(cold_entry)
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
        work_timeout: float | None = None,
        pause_on_human_wait: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> _JobWait:
        """Wait without cancelling execution, keeping or taking the outcome's claim unless another waiter has.

        ``approval_timeout`` bounds how long the wait lasts once the job awaits approval, from when this wait sees it
        ask, however long the job ran before; ``work_timeout`` bounds the time the job spends not awaiting approval
        during this wait; ``timeout`` bounds it all.
        """
        timeout = validate_wait_seconds(timeout, argument="wait_timeout")
        started = asyncio.get_running_loop().time()
        deadline = None if timeout is None else started + timeout
        approval_deadline: float | None = None
        work = None if work_timeout is None else _WorkAllowance(work_timeout, started)
        retained = False
        async with self._lock:
            entry, readable = await self._waited_entry(job_id, owner, depth)
            job_id = entry.job.job_id
            claim = self._claim_waited_entry(entry, readable=readable, claim=claim)
            if claim is None:
                return _JobWait(await self._snapshot(entry))
        # A newer human message in the waiting reply's own conversation releases the wait unless the mid-turn judge
        # lets it wait; the job runs on.
        human = current_pending_human_messages()
        # One message waiter spans the loop, so a job change never cancels the judgment it may be awaiting.
        message: asyncio.Future[None] | None = None
        released = False
        human_wait_pause = _HumanWaitPause(pause_on_human_wait)
        try:
            while True:
                async with self._lock:
                    _, readable = await self._waited_entry(job_id, owner, depth)
                    if not readable:
                        return _JobWait(await self._snapshot(entry))
                    if entry.job.status in TERMINAL_STATUSES:
                        # A released claim can be taken again unless another waiter has claimed the outcome since.
                        claim = self._claim_for(entry, claim)
                        snapshot = await self._snapshot(entry)
                        retained = claim is not None
                        return _JobWait(snapshot, claim)
                    now = asyncio.get_running_loop().time()
                    approval_deadline = _approval_deadline(entry.job, approval_timeout, approval_deadline, now)
                    work_deadline = None if work is None else work.deadline(entry.job, now)
                    ends = [end for end in (deadline, approval_deadline, work_deadline) if end is not None]
                    remaining = min(ends) - now if ends else None
                    if released or (remaining is not None and remaining <= 0):
                        return _JobWait(await self._snapshot(entry))
                    awaiting = entry.job.status == "awaiting_approval"
                    changed = entry.changed
                human_wait_pause.update(awaiting=awaiting)
                if message is None and human is not None:
                    message = asyncio.ensure_future(human.wait())
                released = await _changed_or_released(changed, message, timeout=remaining)
        finally:
            human_wait_pause.close()
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
        await self.release_waits(((job_id, claim),))

    async def release_waits(self, waits: Sequence[tuple[str, JobClaim | None]]) -> None:
        """Release every matching unpersisted claim in one lock acquisition, even if the caller is cancelled."""
        if not waits:
            return

        async def release() -> None:
            async with self._lock:
                released = False
                for job_id, claim in waits:
                    entry = self._entries.get(job_id)
                    if entry is not None and claim is not None and entry.claim == claim:
                        self._clear_claim(entry)
                        released = True
                if released:
                    self.changed.set()

        await run_coroutine_until_complete(release())

    async def acknowledge_wait(
        self,
        job_id: str,
        claim: JobClaim | None,
        *,
        returned_inline: bool = False,
        adapter: Mapping[str, Any] | None = None,
    ) -> None:
        """Mark the claimed outcome consumed, only after the exact parent tool result has been durably saved.

        ``returned_inline`` says that tool result belongs to the call that started the job, and ``adapter`` holds
        what the job's kind keeps of its first acknowledgement, which is added to the job's adapter.
        """
        errors = await self.acknowledge_waits(
            (AcknowledgeWait(job_id, claim, returned_inline=returned_inline, adapter=adapter),),
        )
        if errors:
            raise errors[0][1]

    async def acknowledge_waits(self, waits: Sequence[AcknowledgeWait]) -> tuple[tuple[str, Exception], ...]:
        """Mark every valid claimed outcome consumed in one journal write; return per-claim validation errors."""
        if not waits:
            return ()
        async with self._lock:
            self._ensure_open()
            errors: list[tuple[str, Exception]] = []
            batch: list[_PendingUpdate] = []
            seen: set[str] = set()
            for wait in waits:
                entry = self._entries.get(wait.job_id)
                if entry is None:
                    if await self._find_exact_entry(wait.job_id) is not None:
                        errors.append(
                            (wait.job_id, ValueError("Tool job wait claim no longer belongs to this waiter.")),
                        )
                    else:
                        errors.append((wait.job_id, KeyError(wait.job_id)))
                    continue
                if wait.job_id in seen or wait.claim is None or entry.claim != wait.claim:
                    errors.append((wait.job_id, ValueError("Tool job wait claim no longer belongs to this waiter.")))
                    continue
                seen.add(wait.job_id)
                job = _acknowledged_job(entry.job, returned_inline=wait.returned_inline, adapter=wait.adapter)
                payload = entry.unsaved_payload if job.has_result_payload else None
                batch.append(_PendingUpdate(entry, job, payload, _approval_end_reason(entry, job)))
            if batch:

                async def publish_batch() -> None:
                    updates = await asyncio.to_thread(_encode_updates, batch)
                    await self._store.save_many(updates)
                    for item in batch:
                        self._apply_published(item.entry, item.job)
                        self._clear_claim(item.entry)
                    self.changed.set()

                # A cancelled caller cannot separate a landed write from its in-memory publication.
                await run_coroutine_until_complete(publish_batch())
            return tuple(errors)

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
            entry = await self._entry(job_id, owner, depth)
            # A job that already ended still drains when its outcome failed to save; a durably terminal one is done.
            if entry.job.status not in TERMINAL_STATUSES or not entry.saved:
                await self._request_cancel(entry)
            return await self._snapshot(entry)

    async def cancel_revoked(self) -> None:
        """Withdraw execution whose grant is proven revoked, retaining owned cleanup; a failed job retries next pass."""
        async with self._lock:
            self._ensure_open(accepting=True)
            revoked = [
                entry
                for entry in self._live.values()
                if entry.job.status not in TERMINAL_STATUSES and self._grant(entry.job) == "denied"
            ]
            for entry in revoked:
                # One failed job must not block the rest; it retries on the next pass.
                try:
                    await self._request_cancel(entry)
                except Exception:
                    logger.exception("Tool job revocation failed", job_id=entry.job.job_id)

    async def cancel_stopped_reply(self, reply_id: str) -> None:
        """Refuse a stopped reply's jobs every later approval, and cancel those waiting on cards, which denies them.

        A job whose approved call already runs keeps running; any other is cancelled when it next asks or is answered.
        A job a restart continued is such a job again. One that still waits to be continued ends when its turn to
        continue comes (`continue_stopped`), and no later restart continues a stopped reply's job (`_keeps`).
        """
        stopped = [entry for entry in self._entries.values() if entry.job.reply_id == reply_id]
        # Marked before the lock, so a decision already waiting for the lock is refused too.
        for entry in stopped:
            entry.control.reply_stopped = True
        async with self._lock:
            for entry in stopped:
                if entry.job.status == "awaiting_approval" and not self._closed and self._shutdown_task is None:
                    await self._request_cancel(entry)

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
        """Clean up stopped execution and publish its terminal outcome, or retry saving one that already settled.

        A job a shutdown stopped that the next start may continue (`_keeps`) stays as it is saved.
        """
        settling = entry.job.status not in TERMINAL_STATUSES
        outcome = await self._cleanup(entry) if settling else None
        async with self._lock:
            if settling:
                settled = self._settled(entry, outcome, default)
                if not self._keeps(entry, settled):
                    await self._publish_outcome(entry, settled)
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
                f"Job cleanup failed; external side effects may still be active: {failure_text(error)}",
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

    async def ended_jobs(self, key: ConversationKey, *, include_cancelled: bool = True) -> tuple[BackgroundJob, ...]:
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
                    self._follow_up_candidates.get(
                        (key.recipient, key.room_id, key.thread_id, key.requester_id, key.silent),
                        {},
                    ).values(),
                )
                if entry.job.status in TERMINAL_STATUSES
                and not entry.job.consumed
                and entry.claim is None
                and (include_cancelled or entry.job.status != "cancelled")
                and self._grant(entry.job) == "allowed"
            ]
        return tuple(sorted(ended, key=lambda job: (job.created_at, job.job_id)))

    async def conversation_working_state(self, key: ConversationKey) -> _ConversationWorkingState:
        """Inspect one conversation's approval, active-job, and unconsumed ended-job state.

        Any matching job waiting on approval (including a workflow-node child) marks
        ``awaiting_approval=True`` immediately so the conversation surfaces in Needs You.
        Only non-workflow-node depth-0 jobs count toward active work or follow-ups.
        """
        async with self._lock:
            if self._closed:
                return _ConversationWorkingState()
            has_active = False
            ended: list[BackgroundJob] = []
            for entry in self._by_conversation.get(
                (key.recipient, key.room_id, key.thread_id, key.requester_id),
                {},
            ).values():
                if (entry.job.source_kind == SILENT_SCHEDULE_SOURCE_KIND) != key.silent:
                    continue
                status = entry.job.status
                if status == "awaiting_approval":
                    return _ConversationWorkingState(awaiting_approval=True)
                if entry.job.depth != 0 or is_workflow_node_job(entry.job):
                    continue
                if status not in TERMINAL_STATUSES:
                    has_active = True
                elif not entry.job.consumed and entry.claim is None and self._grant(entry.job) == "allowed":
                    ended.append(entry.job)
            return _ConversationWorkingState(has_active=has_active, ended_jobs=tuple(ended))

    async def expire_ended(
        self,
        *,
        now: datetime,
        source_finished: Callable[[BackgroundJob], Awaitable[bool]],
    ) -> list[BackgroundJob]:
        """Delete old ended jobs, read or not, whose originating turn has finished, which never lets their call run again.

        A finished turn saved the call's outcome or the job's handle as its result, only a paused run resumes and then
        runs just the calls it waits for, and `source_finished` refuses while the job's session has an open approval
        continuation. An unread result this old is not worth reporting any more.
        Returns the deleted jobs.
        """
        deleted: list[BackgroundJob] = []
        async with self._lock:
            if self._closed or self._shutdown_task is not None:
                return deleted
            candidates = [
                await self._snapshot(entry) for entry in self._entries.values() if self._expirable(entry, now)
            ]
            cold_jobs = await self._parse_cold_rows(
                await self._store.load_expirable(
                    inline_before=now - _INLINE_JOB_RETENTION,
                    ended_before=now - _ENDED_JOB_RETENTION,
                ),
            )
            candidates.extend(job for job in cold_jobs if self._job_expirable(job, now))
        try:
            for job in candidates:
                if not await source_finished(job):
                    continue
                async with self._lock:
                    if self._closed or self._shutdown_task is not None:
                        return deleted
                    if await self._delete_if_still_expirable(job, now):
                        deleted.append(job)
        finally:
            if deleted:
                async with self._lock:
                    if not self._closed and self._shutdown_task is None:
                        self._cold_ended_rooms = await self._store.load_ended_rooms()
        return deleted

    async def _delete_if_still_expirable(self, job: BackgroundJob, now: datetime) -> bool:
        """Delete one still-expirable hot or cold job under the runtime lock."""
        entry = self._entries.get(job.job_id)
        if entry is not None:
            if entry.job.updated_at != job.updated_at or not self._expirable(entry, now):
                return False
            # A cancelled caller cannot separate the landed deletion from forgetting the job.
            await run_coroutine_until_complete(self._store.delete(job.job_id))
            self._remove_entry(job.job_id)
            return True
        current_saved = await self._store.load_by_id(job.job_id)
        if current_saved is None:
            return False
        try:
            current_job = _parse_saved_job(current_saved)
        except ValueError:
            return False
        if current_job.updated_at != job.updated_at or not self._job_expirable(current_job, now):
            return False
        await run_coroutine_until_complete(self._store.delete(job.job_id))
        return True

    @staticmethod
    def _job_expirable(job: BackgroundJob, now: datetime) -> bool:
        retention = _INLINE_JOB_RETENTION if job.returned_inline else _ENDED_JOB_RETENTION
        return job.status in TERMINAL_STATUSES and datetime.fromisoformat(job.updated_at) < now - retention

    def _expirable(self, entry: _Entry, now: datetime) -> bool:
        return entry.claim is None and entry.saved and self._job_expirable(entry.job, now)

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
            if entry.saved and entry.job.status in TERMINAL_STATUSES:
                continue
            # A job cancelled before the shutdown ends cancelled, as its cleanup was told.
            stopped = entry.control.cancelled and not entry.control.shutdown
            try:
                # Work the shutdown itself cut short may stay running in the saved job, as after a crash.
                await self._settle(entry, _stopped_outcome(cancelled=stopped, cause="shutdown"))
            except Exception as error:
                # A blocked native child stays unsettled for the next recovery; other jobs still settle.
                failures.append(error)
        if failures:
            msg = "Tool job shutdown failed"
            raise ExceptionGroup(msg, failures)
