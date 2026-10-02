"""Internal completion sources admitted under the ordinary response lifecycle lock."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from mindroom.constants import (
    STREAM_STATUS_KEY,
    STREAM_STATUS_STREAMING,
    STREAM_WARMUP_SUFFIX_KEY,
)
from mindroom.delivery_gateway import EditTextRequest
from mindroom.dynamic_tool_continuation import DYNAMIC_TOOL_CONTINUATION_LIMIT
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.hooks import MessageEnvelope
from mindroom.message_target import MessageTarget
from mindroom.tool_jobs.control import current_human_message_signal, job_owns_execution
from mindroom.tool_jobs.runtime import (
    READY_STATUSES,
    JobAccessError,
    completion_event_id,
    get_background_runtime,
    parse_completion_event_id,
)
from mindroom.tool_system.events import BackgroundWaitChunk
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.streaming import StreamingPresentation
    from mindroom.tool_jobs.runtime import BackgroundJob, JobWait, ToolJobRuntime


# The source kind of a completion whose job started outside any admitted turn.
_UNSOURCED_COMPLETION_SOURCE_KIND = "tool_job_completion"


@dataclass
class _WaitNotice:
    callback: Callable[[StreamingPresentation, str | None], Awaitable[None]]
    presentation: StreamingPresentation | None = None


_WAIT_NOTICE: ContextVar[_WaitNotice | None] = ContextVar("background_wait_notice", default=None)


@contextmanager
def background_wait_notice(callback: Callable[[StreamingPresentation, str | None], Awaitable[None]]) -> Iterator[None]:
    """Bind blocking wait progress to the current serialized response's placeholder."""
    token = _WAIT_NOTICE.set(_WaitNotice(callback))
    try:
        yield
    finally:
        _WAIT_NOTICE.reset(token)


async def report_background_wait(presentation: StreamingPresentation, notice: str | None) -> None:
    """Report blocking wait progress through its response owner when present."""
    wait = _WAIT_NOTICE.get()
    if wait is not None:
        wait.presentation = presentation
        await wait.callback(presentation, notice)


def reported_wait_presentation() -> StreamingPresentation | None:
    """Return the answer text and trace this blocking response last reported beside wait progress."""
    wait = _WAIT_NOTICE.get()
    return wait.presentation if wait is not None else None


def background_wait_edit(
    target: MessageTarget,
    event_id: str,
    presentation: StreamingPresentation,
    notice: str | None,
) -> EditTextRequest:
    """Render active wait progress separately from recoverable answer text."""
    visible = presentation.response_text.strip() or "Thinking..."
    return EditTextRequest(
        target=target,
        event_id=event_id,
        new_text=f"{visible}\n\n{notice}" if notice else visible,
        tool_trace=list(presentation.tool_trace),
        extra_content={
            "msgtype": "m.notice",
            STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
            STREAM_WARMUP_SUFFIX_KEY: notice or "",
        },
    )


def _retrieval_calls(jobs: Sequence[BackgroundJob]) -> str:
    calls = []
    for job in jobs:
        member = f" through member {job.owner.agent_name}" if job.owner.transport_agent_name else ""
        # Retrieving an approval pause presents it; once approved, the call waits for the approved work's outcome.
        budget = "" if job.status == "awaiting_approval" else ", wait_timeout=0"
        calls.append(f'job(action="wait", job_id="{job.job_id}"{budget}){member}')
    return "; ".join(calls)


def _completion_prompt(jobs: Sequence[BackgroundJob]) -> str:
    """Ask once for rich native result retrieval after the runtime has finished waiting."""
    return (
        "Internal runtime update, not a new human request. Background work has reached a result or approval boundary. "
        "Retrieve these stored outcomes once using the native job tool, then continue the conversation: "
        + _retrieval_calls(jobs)
    )


def recovered_jobs_note(jobs: Sequence[BackgroundJob]) -> str:
    """Tell the re-run of an interrupted request to read, not repeat, the tool calls it already made."""
    return (
        "This request was interrupted before its reply finished. This reply replaces the interrupted one, so answer "
        "the request in full. The tool calls the interrupted attempt made became background jobs that were not "
        "replayed: do not repeat them, and retrieve their stored outcomes once using the native job tool: "
        + _retrieval_calls(jobs)
    )


def completion_event(job: BackgroundJob, *, sender_id: str) -> InboundEvent:
    """Admit response ownership without manufacturing a Matrix timeline event."""
    assert job.owner.room_id is not None
    return InboundEvent(
        event_id=completion_event_id(job),
        room_id=job.owner.room_id,
        thread_id=job.owner.resolved_thread_id,
        kind=EventKind.TOOL_JOB_COMPLETION,
        event_class=EventClass.ACTIONABLE,
        sender=sender_id,
        origin_server_ts=int(datetime.fromisoformat(job.created_at).timestamp() * 1000),
        source={},
    )


def completion_origin(job: BackgroundJob, *, sender_id: str) -> TurnOrigin:
    """Identify input as runtime-owned work while retaining its requester's authority."""
    owner = job.owner
    assert owner.requester_id is not None
    return TurnOrigin(
        transport_sender_id=sender_id,
        requester_id=owner.requester_id,
        sender_entity_name=owner.recipient,
        requester_entity_name=None,
        sender_kind=SenderKind.MANAGED_ENTITY,
        requester_kind=SenderKind.USER,
        intent=TurnIntent.TOOL_JOB_COMPLETION,
        source_kind=job.source_kind or _UNSOURCED_COMPLETION_SOURCE_KIND,
        trust=TurnTrust.TRUSTED_INTERNAL,
    )


def completion_envelope(job: BackgroundJob, *, sender_id: str) -> MessageEnvelope:
    """Address one job's completion to its exact owner conversation."""
    owner = job.owner
    assert owner.room_id is not None
    assert owner.session_id is not None
    return MessageEnvelope(
        source_event_id=completion_event_id(job),
        target=MessageTarget(owner.room_id, owner.resolved_thread_id, owner.resolved_thread_id, None, owner.session_id),
        body=_completion_prompt([job]),
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=owner.recipient,
        origin=completion_origin(job, sender_id=sender_id),
    )


async def admit_job_completion(envelope: MessageEnvelope, runtime_paths: RuntimePaths) -> bool:
    """Under the conversation lock, admit an internal completion only while the runtime still offers its generation.

    A completion envelope is built from that same job's immutable owner, so its current outcome is the only recheck.
    A turn without completion intent is admitted whatever its source event ID looks like, and so is a recovered turn
    with completion intent whose source keeps its human event ID, because that ID names no completion.
    """
    if (
        envelope.origin.intent is not TurnIntent.TOOL_JOB_COMPLETION
        or (completion := parse_completion_event_id(envelope.source_event_id)) is None
    ):
        return True
    runtime = get_background_runtime(runtime_paths)
    if runtime is None:
        msg = "Tool job runtime is not ready for completion admission"
        raise RuntimeError(msg)
    job_id, generation = completion
    return await runtime.outcome(job_id, generation, source_event_id=envelope.source_event_id) is not None


@dataclass(frozen=True)
class _ReadyJobContinuation:
    """One internal result-retrieval prompt, emitted only after work is ready."""

    prompt: str


async def join_conversation_jobs(
    attempted: set[tuple[str, int]],
    *,
    agent_names: Sequence[str] | None = None,
) -> AsyncIterator[BackgroundWaitChunk | _ReadyJobContinuation]:
    """Wait outside the model, yielding visible progress and at most one ready prompt."""
    context = get_tool_runtime_context()
    if context is None or job_owns_execution():
        return
    runtime = get_background_runtime(context.runtime_paths)
    if runtime is None:
        return
    participants = {context.agent_name, *(agent_names or ())}
    signal = current_human_message_signal()
    human = asyncio.Event()
    if signal is not None:
        signal.subscribe(human.set)

    async def pending() -> list[BackgroundJob]:
        jobs = await runtime.conversation_jobs(
            transport_agent_name=context.recipient,
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
            requester_id=context.requester_id,
            source_kind=context.source_kind,
        )
        return [
            job
            for job in jobs
            if job.owner.agent_name in participants and (job.job_id, job.generation) not in attempted
        ]

    try:
        jobs = await pending()
        if human.is_set() or not jobs:
            return
        ready = [job for job in jobs if job.status in READY_STATUSES]
        if not ready:
            yield BackgroundWaitChunk("⏳ Waiting for background work…")
            ready = await _wait_until_ready(runtime, jobs, human, pending)
            yield BackgroundWaitChunk(None)
        if ready and not human.is_set():
            attempted.update((job.job_id, job.generation) for job in ready)
            yield _ReadyJobContinuation(_completion_prompt(ready))
    finally:
        if signal is not None:
            signal.unsubscribe(human.set)


async def _wait_until_ready(
    runtime: ToolJobRuntime,
    jobs: list[BackgroundJob],
    human: asyncio.Event,
    pending: Callable[[], Awaitable[list[BackgroundJob]]],
) -> list[BackgroundJob]:
    """Wait for a ready job, a human message, or no remaining jobs this reply can still access."""
    # Jobs this reply lost access to are gone for it; the others keep the reply waiting.
    unavailable: set[str] = set()
    ready: list[BackgroundJob] = []
    while jobs and not ready and not human.is_set():
        unavailable |= await _wait_for_ready_jobs(runtime, jobs, human)
        jobs = [job for job in await pending() if job.job_id not in unavailable]
        ready = [job for job in jobs if job.status in READY_STATUSES]
    return ready


async def _wait_for_job(runtime: ToolJobRuntime, job: BackgroundJob) -> str | None:
    """Wait until one job is ready, returning its ID when this reply lost access to it."""
    waited: JobWait | None = None
    try:
        waited = await runtime.wait(job.job_id, owner=job.owner, depth=job.depth)
    except JobAccessError:
        return job.job_id
    finally:
        if waited is not None:
            await runtime.release_wait(job.job_id, waited.claim)
    return None


async def _wait_for_ready_jobs(
    runtime: ToolJobRuntime,
    jobs: Sequence[BackgroundJob],
    human: asyncio.Event,
) -> set[str]:
    """Wait for the first ready job or human message, returning the jobs this reply lost access to."""
    waiters = [asyncio.create_task(_wait_for_job(runtime, job)) for job in jobs]
    human_wait = asyncio.create_task(human.wait())
    tasks = [*waiters, human_wait]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return {lost for task in waiters if task in done and (lost := task.result()) is not None}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def join_approval_jobs[RunT](
    response: RunT,
    *,
    is_complete: Callable[[RunT], bool],
    continue_response: Callable[[RunT, str], Awaitable[RunT]],
    presentation: Callable[[], StreamingPresentation],
    continuation_count: int,
    agent_names: Sequence[str] | None = None,
) -> tuple[RunT, int]:
    """Join ready jobs after a reconstructed approval, within the continuation budget its turn has left.

    Returns the final response and how many continuations the joins spent.
    """
    attempted: set[tuple[str, int]] = set()
    joins = 0
    for _ in range(DYNAMIC_TOOL_CONTINUATION_LIMIT - continuation_count):
        if not is_complete(response):
            break
        prompt = None
        async for joined in join_conversation_jobs(attempted, agent_names=agent_names):
            if isinstance(joined, BackgroundWaitChunk):
                await report_background_wait(presentation(), joined.content)
            else:
                prompt = joined.prompt
        if prompt is None:
            break
        response = await continue_response(response, prompt)
        joins += 1
    return response, joins
