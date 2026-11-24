"""The conversation a reply's background jobs belong to: the key a reply finds them by, and how long it waits for them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.reply_lifecycle import SOURCES_PENDING_OUTCOMES, ReplyState
from mindroom.reply_scope import current_span
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.event_journal.replies import ReplyStore
    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime


@dataclass(frozen=True)
class ConversationKey:
    """The background work of one requester in one conversation, owned by the entity that answers there.

    That entity is an agent, or a team, which owns the work its members started.
    """

    recipient: str
    room_id: str
    thread_id: str | None
    requester_id: str
    # Work a silent schedule started is reported silently, so visible replies never hear of it, and the reverse.
    silent: bool


def current_approval_wait_timeout() -> float | None:
    """Return how long the reply running now waits for a job's human decision before it goes on without it."""
    context = get_tool_runtime_context()
    return None if context is None else context.current_config.background_tool_jobs.approval_wait_timeout


def current_conversation() -> tuple[ToolJobRuntime, ConversationKey] | None:
    """Return the runtime and key of the reply running now; a job's own run has no conversation here."""
    context = get_tool_runtime_context()
    if context is None or job_owns_execution():
        return None
    runtime = get_background_runtime(context.runtime_paths)
    if runtime is None:
        return None
    return runtime, ConversationKey(
        recipient=context.recipient,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        requester_id=context.requester_id,
        silent=context.source_kind == SILENT_SCHEDULE_SOURCE_KIND,
    )


async def _reply_holds_jobs(replies: ReplyStore, reply_id: str) -> bool:
    reply = await replies.load(reply_id)
    if reply is None or reply.state is not ReplyState.ACTIVE:
        return False
    if reply.current_span_id is not None:
        return True
    last = await replies.span(reply.last_span_id)
    return last is not None and last.outcome in SOURCES_PENDING_OUTCOMES


async def unheld_jobs(
    jobs: tuple[BackgroundJob, ...],
    replies: ReplyStore | None,
    *,
    current_reply_id: str | None = None,
    holds: dict[str, bool] | None = None,
) -> tuple[BackgroundJob, ...]:
    """Leave ended jobs with their open reply, reading each parent once per pass (or conversation)."""
    if replies is None:
        return jobs
    holds = {} if holds is None else holds
    visible = []
    for job in jobs:
        if (reply_id := job.reply_id) is not None and reply_id != current_reply_id:
            if reply_id not in holds:
                holds[reply_id] = await _reply_holds_jobs(replies, reply_id)
            if holds[reply_id]:
                continue
        visible.append(job)
    return tuple(visible)


async def current_ended_jobs() -> tuple[BackgroundJob, ...]:
    """Return ended jobs this turn can announce or batch-collect, including its own."""
    context = get_tool_runtime_context()
    current = current_conversation()
    if context is None or current is None:
        return ()
    jobs = await current[0].ended_jobs(current[1])
    span = current_span()
    if span is not None:
        return await unheld_jobs(jobs, span.runtime.store.replies, current_reply_id=span.reply_id)
    return await unheld_jobs(jobs, None if context.membership is None else context.membership.replies)
