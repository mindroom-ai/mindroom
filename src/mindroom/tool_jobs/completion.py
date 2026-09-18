"""Internal completion sources admitted under the ordinary response lifecycle lock."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.dynamic_tool_continuation import DYNAMIC_TOOL_CONTINUATION_LIMIT
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.hooks import MessageEnvelope
from mindroom.message_target import MessageTarget
from mindroom.tool_job_completion import ToolJobCompletion
from mindroom.tool_jobs.control import current_human_message_signal, job_owns_execution
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.streaming import StreamingPresentation
    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime


_WAIT_NOTICE: ContextVar[Callable[[StreamingPresentation], Awaitable[None]] | None] = ContextVar(
    "background_wait_notice",
    default=None,
)


@contextmanager
def background_wait_notice(callback: Callable[[StreamingPresentation], Awaitable[None]]) -> Iterator[None]:
    """Bind blocking wait progress to the current serialized response's placeholder."""
    token = _WAIT_NOTICE.set(callback)
    try:
        yield
    finally:
        _WAIT_NOTICE.reset(token)


async def report_background_wait(presentation: StreamingPresentation) -> None:
    """Report blocking wait progress through its response owner when present."""
    callback = _WAIT_NOTICE.get()
    if callback is not None:
        await callback(presentation)


def completion_source_id(job_id: str, generation: int) -> str:
    """Return an internal source identity stable across worker and process retries."""
    return f"tool-job:{job_id}:{generation}"


def completion_prompt(jobs: Sequence[BackgroundJob]) -> str:
    """Ask once for rich native result retrieval after the runtime has finished waiting."""
    calls = []
    for job in jobs:
        member = f" through member {job.owner.agent_name}" if job.owner.transport_agent_name else ""
        calls.append(f'job(action="wait", job_id="{job.job_id}", wait_timeout=0){member}')
    return (
        "Internal runtime update, not a new human request. Background work has reached a result or approval boundary. "
        "Retrieve these stored outcomes once using the native job tool, then continue the conversation: "
        + "; ".join(calls)
    )


def completion_event(job: BackgroundJob, *, sender_id: str) -> InboundEvent:
    """Admit response ownership without manufacturing a Matrix timeline event."""
    assert job.owner.room_id is not None
    return InboundEvent(
        event_id=completion_source_id(job.job_id, job.generation),
        room_id=job.owner.room_id,
        thread_id=job.owner.resolved_thread_id,
        kind=EventKind.TOOL_JOB_COMPLETION,
        event_class=EventClass.ACTIONABLE,
        sender=sender_id,
        origin_server_ts=int(datetime.fromisoformat(job.created_at).timestamp() * 1000),
        source={"job_id": job.job_id, "generation": job.generation},
    )


def completion_envelope(job: BackgroundJob, *, sender_id: str) -> MessageEnvelope:
    """Retain requester authority while identifying input as runtime-owned work."""
    owner = job.owner
    assert owner.room_id is not None
    assert owner.requester_id is not None
    assert owner.session_id is not None
    recipient = owner.transport_agent_name or owner.agent_name
    return MessageEnvelope(
        source_event_id=completion_source_id(job.job_id, job.generation),
        target=MessageTarget(owner.room_id, owner.resolved_thread_id, owner.resolved_thread_id, None, owner.session_id),
        body=completion_prompt([job]),
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=recipient,
        origin=TurnOrigin(
            transport_sender_id=sender_id,
            requester_id=owner.requester_id,
            sender_entity_name=recipient,
            requester_entity_name=None,
            sender_kind=SenderKind.MANAGED_ENTITY,
            requester_kind=SenderKind.USER,
            intent=TurnIntent.TOOL_JOB_COMPLETION,
            source_kind="tool_job_completion",
            trust=TurnTrust.TRUSTED_INTERNAL,
        ),
        hook_source="tool_job_completion",
        tool_job_completion=ToolJobCompletion(job.job_id, job.generation),
    )


async def admit_job_completion(
    envelope: MessageEnvelope,
    *,
    target: MessageTarget,
    runtime_paths: RuntimePaths,
) -> bool:
    """Recheck current outcome and exact requester under the conversation lock."""
    if envelope.hook_source != "tool_job_completion":
        return True
    reference = envelope.tool_job_completion
    if reference is None or envelope.origin.intent is not TurnIntent.TOOL_JOB_COMPLETION:
        return False
    runtime = get_background_runtime(runtime_paths)
    if runtime is None:
        msg = "Tool job runtime is not ready for completion admission"
        raise RuntimeError(msg)
    job = await runtime.outcome(reference.job_id, reference.generation)
    if job is None:
        return False
    owner = job.owner
    return (
        envelope.source_event_id == completion_source_id(job.job_id, job.generation)
        and envelope.requester_id == owner.requester_id
        and envelope.agent_name == (owner.transport_agent_name or owner.agent_name)
        and target.room_id == owner.room_id
        and target.resolved_thread_id == owner.resolved_thread_id
        and target.session_id == owner.session_id
    )


@dataclass(frozen=True)
class _ReadyJobContinuation:
    """One internal result-retrieval prompt, emitted only after work is ready."""

    prompt: str


async def join_conversation_jobs(
    attempted: set[tuple[str, int]],
    *,
    agent_names: Sequence[str] | None = None,
) -> AsyncIterator[str | _ReadyJobContinuation]:
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
            transport_agent_name=context.transport_agent_name or context.agent_name,
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
            requester_id=context.requester_id,
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
        ready = [job for job in jobs if job.status not in {"running", "cancel_requested"}]
        if not ready:
            yield "\n\n⏳ Waiting for background work…\n\n"
            await _wait_for_ready_jobs(runtime, jobs, human)
            if human.is_set():
                return
            ready = [job for job in await pending() if job.status not in {"running", "cancel_requested"}]
        if ready and not human.is_set():
            attempted.update((job.job_id, job.generation) for job in ready)
            yield _ReadyJobContinuation(completion_prompt(ready))
    finally:
        if signal is not None:
            signal.unsubscribe(human.set)


async def _wait_for_job(runtime: ToolJobRuntime, job: BackgroundJob) -> None:
    waited = None
    try:
        waited = await runtime.wait(job.job_id, owner=job.owner, depth=job.depth)
    finally:
        if waited is not None:
            await run_coroutine_until_complete(runtime.release_wait(job.job_id, waited.token))


async def _wait_for_ready_jobs(runtime: ToolJobRuntime, jobs: Sequence[BackgroundJob], human: asyncio.Event) -> None:
    """Release all transient wait claims before handing outcomes back to the runner."""
    waiters = [asyncio.create_task(_wait_for_job(runtime, job)) for job in jobs]
    human_wait = asyncio.create_task(human.wait())
    tasks = [*waiters, human_wait]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
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
    """Keep reconstructed agent/team approvals at the ordinary bounded join boundary."""
    attempted: set[tuple[str, int]] = set()
    for _ in range(DYNAMIC_TOOL_CONTINUATION_LIMIT):
        if not is_complete(response):
            break
        prompt = None
        async for joined in join_conversation_jobs(attempted, agent_names=agent_names):
            if isinstance(joined, str):
                current = presentation()
                await report_background_wait(replace(current, response_text=current.response_text + joined))
            else:
                prompt = joined.prompt
        if prompt is None:
            break
        response = await continue_response(response, prompt)
    return response
