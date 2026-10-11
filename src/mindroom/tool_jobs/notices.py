"""Tell a running reply, once each, about its conversation's background jobs that ended, and ask it to collect them."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.agno_compat_model_hooks import install_tool_result_callback
from mindroom.hooks.types import EnrichmentItem
from mindroom.tool_jobs.completion import current_conversation, ended_jobs
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from agno.models.base import Model
    from agno.models.message import Message
    from agno.models.response import ModelResponse

    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime

_HOOK_ATTR = "_mindroom_job_notice_hook_installed"


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
    """List each ended job on its own line, followed by the request to collect them."""
    lines = []
    for job in jobs:
        member = (
            f" by member {job.owner.agent_name}"
            if job.owner.transport_agent_name not in {None, job.owner.agent_name}
            else ""
        )
        lines.append(f"`{job.job_id}` ({job.tool_name}) {job.status}{member}")
    return "\n".join([*lines, notice])


async def _acknowledge_cancellations(runtime: ToolJobRuntime, jobs: Sequence[BackgroundJob]) -> None:
    """Mark noticed cancelled jobs acknowledged: a cancellation has no result to collect."""
    for job in jobs:
        if job.status != "cancelled":
            continue
        waited = await runtime.wait(job.job_id, owner=job.owner, depth=job.depth, timeout=0)
        if waited.claim is not None:
            await runtime.acknowledge_wait(job.job_id, waited.claim)


async def _unnoticed_notice(notices: JobNotices, agent_names: Sequence[str]) -> str | None:
    """Describe this reply's ended jobs it was not told about yet, and count them as told."""
    current = current_conversation(agent_names)
    context = get_tool_runtime_context()
    if current is None or context is None:
        return None
    runtime, key = current
    jobs = [job for job in await ended_jobs(runtime, key) if job.job_id not in notices.noticed]
    if not jobs:
        return None
    notices.noticed.update(job.job_id for job in jobs)
    await _acknowledge_cancellations(runtime, jobs)
    return _job_notice_text(jobs, context.current_config.get_prompt("BACKGROUND_JOBS_ENDED_NOTICE"))


async def _turn_start_job_note(agent_names: Sequence[str] = ()) -> str | None:
    """Describe the conversation's jobs that ended before this reply, cancelled ones included, once per reply.

    Every attempt of the reply asks again and gets the same note, since an attempt's prompt is not saved.
    """
    notices = _NOTICES.get()
    if notices is None:
        return None
    if not notices.started:
        notices.started = True
        notices.start_note = await _unnoticed_notice(notices, agent_names)
    return notices.start_note


async def append_turn_start_job_note(
    items: Sequence[EnrichmentItem],
    agent_names: Sequence[str] = (),
) -> tuple[EnrichmentItem, ...]:
    """Open the reply's prompt with its turn-start job note, when jobs ended before it."""
    note = await _turn_start_job_note(agent_names)
    if note is None:
        return tuple(items)
    return (*items, EnrichmentItem(key="background_jobs", text=note, cache_policy="volatile", persist=False))


async def _append_job_notice(messages: list[Message], *, agent_name: str) -> None:
    """Append the notice for jobs that ended during the tool batch to the batch's last result."""
    notices = _NOTICES.get()
    if notices is None:
        return
    result = next((message for message in reversed(messages) if message.role == "tool"), None)
    if result is None or not isinstance(result.content, str | None):
        return
    text = await _unnoticed_notice(notices, (agent_name,))
    if text is None:
        return
    result.content = f"{result.content}\n\n{text}" if result.content else text
    if result.compressed_content is not None:
        result.compressed_content = f"{result.compressed_content}\n\n{text}"


def install_job_notice_hook(model: Model, *, agent_name: str) -> None:
    """Tell the model after each tool batch about the agent's jobs that ended since it last heard."""

    async def after_tools(messages: list[Message], _result: ModelResponse) -> None:
        await _append_job_notice(messages, agent_name=agent_name)

    async def no_entry_notice(_messages: list[Message]) -> None:
        return None

    install_tool_result_callback(
        model,
        marker=_HOOK_ATTR,
        callback=lambda _messages, _results: None,
        before_response=lambda _messages: None,
        before_response_async=no_entry_notice,
        after_tools_async=after_tools,
    )
