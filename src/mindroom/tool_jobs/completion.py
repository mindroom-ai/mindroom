"""The holding reply's join of its conversation's background jobs and its transient waiting presentation."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import (
    STREAM_STATUS_KEY,
    STREAM_STATUS_STREAMING,
    STREAM_WARMUP_SUFFIX_KEY,
)
from mindroom.delivery_gateway import EditTextRequest
from mindroom.tool_jobs.control import current_human_message_signal, job_owns_execution
from mindroom.tool_jobs.runtime import READY_STATUSES, JobAccessError, get_background_runtime
from mindroom.tool_system.events import BackgroundWaitChunk
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence

    from mindroom.message_target import MessageTarget
    from mindroom.streaming import StreamingPresentation
    from mindroom.tool_jobs.runtime import BackgroundJob, JobWait, ToolJobRuntime


# How many times one reply may continue with ready job results, counted apart from dynamic tool continuations.
JOB_JOIN_LIMIT = 20


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
    agent_names: Sequence[str] | None = None,
) -> RunT:
    """Join ready jobs after a reconstructed approval, at most `JOB_JOIN_LIMIT` times."""
    attempted: set[tuple[str, int]] = set()
    for _ in range(JOB_JOIN_LIMIT):
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
    return response
