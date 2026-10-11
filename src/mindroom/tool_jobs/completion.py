"""A conversation's background jobs: the key a reply finds them by, and those of them that ended."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, cast

from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime


_DELEGATED_CHILD: ContextVar[bool] = ContextVar("delegated_child_reply", default=False)


@contextmanager
def delegated_child_context() -> Iterator[None]:
    """Run a delegated child's reply inside its caller: it has no message of its own, so it reports no jobs."""
    token = _DELEGATED_CHILD.set(True)
    try:
        yield
    finally:
        _DELEGATED_CHILD.reset(token)


@dataclass(frozen=True)
class ConversationKey:
    """One recipient's background work for one requester in one conversation."""

    recipient: str
    room_id: str
    thread_id: str | None
    requester_id: str
    # Work a silent schedule started is reported silently, so visible replies never hear of it, and the reverse.
    silent: bool
    # The entities whose work the reply collects: the agent, or a team's members.
    participants: tuple[str, ...]

    def encode(self) -> str:
        """Return the key as the opaque JSON a follow-up records."""
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def decode(cls, stored: str) -> ConversationKey:
        """Restore a key a follow-up recorded."""
        data = cast("dict[str, object]", json.loads(stored))
        return cls(
            recipient=str(data["recipient"]),
            room_id=str(data["room_id"]),
            thread_id=cast("str | None", data["thread_id"]),
            requester_id=str(data["requester_id"]),
            silent=data["silent"] is True,
            participants=tuple(cast("list[str]", data["participants"])),
        )


def current_conversation(agent_names: Sequence[str] | None = None) -> tuple[ToolJobRuntime, ConversationKey] | None:
    """Return the runtime and key of the reply running now, whose participants are its agent and ``agent_names``.

    A job's own run and a delegated child's reply have no message of their own, so they have no conversation here.
    """
    context = get_tool_runtime_context()
    if context is None or job_owns_execution() or _DELEGATED_CHILD.get():
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
        participants=tuple(sorted({context.agent_name, *(agent_names or ())})),
    )


async def ended_jobs(runtime: ToolJobRuntime, key: ConversationKey) -> tuple[BackgroundJob, ...]:
    """Return the ended, readable, unacknowledged jobs of ``key``, oldest first."""
    held = await runtime.held_jobs(
        transport_agent_name=key.recipient,
        room_id=key.room_id,
        thread_id=key.thread_id,
        requester_id=key.requester_id,
        source_kind=SILENT_SCHEDULE_SOURCE_KIND if key.silent else None,
    )
    ended = (
        job
        for job, readable in held
        if readable and job.status in TERMINAL_STATUSES and job.owner.agent_name in key.participants
    )
    return tuple(sorted(ended, key=lambda job: (job.created_at, job.job_id)))
