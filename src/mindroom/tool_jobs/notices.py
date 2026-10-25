"""Tell a running reply, once each, about its conversation's background jobs that ended, and ask it to collect them."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.hooks.types import EnrichmentItem
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.conversation import current_conversation
from mindroom.tool_jobs.runtime import describe_job
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from mindroom.agent_loop.model_loop import AfterToolBatchHook
    from mindroom.llm.messages import Message
    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime

logger = get_logger(__name__)


@dataclass
class JobNotices:
    """The ended jobs one reply has told its model about, across all of its attempts."""

    noticed: set[str] = field(default_factory=set)
    # The note every attempt of the reply opens with, once the first attempt has looked.
    start_note: str | None = None
    started: bool = False


_NOTICES: ContextVar[JobNotices | None] = ContextVar("tool_job_notices", default=None)


@contextmanager
def job_notice_context(notices: JobNotices) -> Iterator[None]:
    """Bind one reply's notices for one call or stream pull."""
    token = _NOTICES.set(notices)
    try:
        yield
    finally:
        _NOTICES.reset(token)


def _job_notice_text(jobs: Sequence[BackgroundJob], notice: str) -> str:
    """List each ended job on its own line, followed by what to do about them."""
    lines = []
    for job in jobs:
        # A restart stopped an interrupted job at an unknown point, and it is never run again on its own.
        partly = "; it may have partly run" if job.status == "interrupted" else ""
        lines.append(f"{describe_job(job)} {job.status}{partly}")
    return "\n".join([*lines, notice])


async def _acknowledge_cancellations(runtime: ToolJobRuntime, jobs: Sequence[BackgroundJob]) -> None:
    """Mark noticed cancelled jobs acknowledged: a cancellation has no result to collect.

    An acknowledgement can fail when the job expired or lost its access after it was listed, or when its store
    fails; the reply carries on, and after a store failure a later reply notices the unacknowledged cancellation again.
    """
    for job in jobs:
        if job.status != "cancelled":
            continue
        try:
            await _acknowledge_cancellation(runtime, job)
        except Exception:
            logger.warning("Background job cancellation was not acknowledged", job_id=job.job_id, exc_info=True)


async def _acknowledge_cancellation(runtime: ToolJobRuntime, job: BackgroundJob) -> None:
    """Claim one cancelled job's outcome and mark it consumed, releasing the claim either way."""
    waited = await runtime.wait(job.job_id, owner=job.owner, depth=job.depth, timeout=0)
    if waited.claim is None:
        return
    try:
        await runtime.acknowledge_wait(job.job_id, waited.claim)
    finally:
        await runtime.release_wait(job.job_id, waited.claim)


async def _unnoticed_notice(notices: JobNotices) -> str | None:
    """Describe this reply's ended jobs it was not told about yet, and count them as told."""
    current = current_conversation()
    context = get_tool_runtime_context()
    if current is None or context is None:
        return None
    runtime, key = current
    jobs = [job for job in await runtime.ended_jobs(key) if job.job_id not in notices.noticed]
    if not jobs:
        return None
    notices.noticed.update(job.job_id for job in jobs)
    await _acknowledge_cancellations(runtime, jobs)
    return _job_notice_text(jobs, context.current_config.get_prompt("BACKGROUND_JOBS_ENDED_NOTICE"))


async def _turn_start_job_note() -> str | None:
    """Describe the conversation's jobs that ended before this reply, cancelled ones included, once per reply.

    Every attempt of the reply asks again and gets the same note, since an attempt's prompt is not saved.
    """
    notices = _NOTICES.get()
    if notices is None:
        return None
    if not notices.started:
        notices.started = True
        notices.start_note = await _unnoticed_notice(notices)
    return notices.start_note


async def append_turn_start_job_note(items: Sequence[EnrichmentItem]) -> tuple[EnrichmentItem, ...]:
    """Add the turn-start job note to the reply's turn context, when jobs ended before it."""
    note = await _turn_start_job_note()
    if note is None:
        return tuple(items)
    return (*items, EnrichmentItem(key="background_jobs", text=note, cache_policy="volatile", persist=False))


def job_notice_hook(entity_name: str) -> AfterToolBatchHook:
    """Return the loop hook that appends, after each tool batch, the notice of jobs that ended meanwhile.

    The notice goes on the batch's last tool result, and only the answering entity ``entity_name`` hears it: an
    agent, or a configured team's leader, never a member it delegated to; an ad hoc team answers as the agent whose
    account posts its replies, so that member hears it.
    """

    async def after_tool_batch(messages: list[Message], _results: Sequence[Message]) -> None:
        notices = _NOTICES.get()
        context = get_tool_runtime_context()
        if notices is None or context is None or context.recipient != entity_name:
            return
        result = next((message for message in reversed(messages) if message.role == "tool"), None)
        if result is None or not isinstance(result.content, str | None):
            return
        text = await _unnoticed_notice(notices)
        if text is not None:
            result.content = f"{result.content}\n\n{text}" if result.content else text

    return after_tool_batch
