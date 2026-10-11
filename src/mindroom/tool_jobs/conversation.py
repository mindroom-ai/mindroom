"""A conversation's background jobs: the key a reply finds them by, and those of them that ended."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
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


async def ended_jobs(runtime: ToolJobRuntime, key: ConversationKey) -> tuple[BackgroundJob, ...]:
    """Return the ended, readable, unacknowledged top-level jobs of ``key``, oldest first."""
    return await runtime.ended_jobs(
        recipient=key.recipient,
        room_id=key.room_id,
        thread_id=key.thread_id,
        requester_id=key.requester_id,
        silent=key.silent,
    )
