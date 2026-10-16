"""The conversation a reply's background jobs belong to: the key a reply finds them by, and how long it waits for them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.tool_jobs.runtime import ToolJobRuntime


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


# Usually nobody sends the newer message that would release an unattended turn's wait, so a wait without a budget ends
# after this long there and the work continues in the background, its result reported by a follow-up.
_UNATTENDED_WAIT_SECONDS = 120.0


def current_wait_budget(requested: float | None) -> float | None:
    """Return how long the reply running now waits for a job: as requested, or a bounded wait in an unattended turn.

    A silent schedule's jobs get no follow-up, so its run keeps waiting to report what they found.
    """
    if requested is not None:
        return requested
    context = get_tool_runtime_context()
    if context is None or not context.unattended or context.source_kind == SILENT_SCHEDULE_SOURCE_KIND:
        return None
    return _UNATTENDED_WAIT_SECONDS


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
