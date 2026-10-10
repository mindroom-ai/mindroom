"""The response boundary: continue a reply with ready job results, or report the work it leaves outstanding."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, cast

from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.reply_scope import current_span
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterator, Sequence

    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime


# How many times one message may continue with ready job results, counted apart from dynamic tool continuations.
_JOB_JOIN_LIMIT = 20

_DELEGATED_CHILD: ContextVar[bool] = ContextVar("delegated_child_reply", default=False)


@contextmanager
def delegated_child_context() -> Iterator[None]:
    """Run a delegated child's reply inside its caller: it has no message of its own, so it never holds work."""
    token = _DELEGATED_CHILD.set(True)
    try:
        yield
    finally:
        _DELEGATED_CHILD.reset(token)


@dataclass(frozen=True)
class _JobJoin:
    """A reply's response boundary: ready results to continue with, or whether its message holds outstanding work."""

    prompt: str | None = None
    holds: bool = False
    key: HoldKey | None = None


@dataclass(frozen=True)
class HoldKey:
    """Whose outstanding work one reply holds: one recipient's work for one requester in one conversation."""

    recipient: str
    room_id: str
    thread_id: str | None
    requester_id: str
    # Work a silent schedule started is delivered silently, so visible replies never hold it, and the reverse.
    silent: bool
    # The entities whose work the reply retrieves: the agent, or a team's members.
    participants: tuple[str, ...]

    def encode(self) -> str:
        """Return the key as the opaque JSON a waiting reply records."""
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def decode(cls, stored: str) -> HoldKey:
        """Restore a key a waiting reply recorded."""
        data = cast("dict[str, object]", json.loads(stored))
        return cls(
            recipient=str(data["recipient"]),
            room_id=str(data["room_id"]),
            thread_id=cast("str | None", data["thread_id"]),
            requester_id=str(data["requester_id"]),
            silent=data["silent"] is True,
            participants=tuple(cast("list[str]", data["participants"])),
        )


@dataclass(frozen=True)
class ConversationWork:
    """The outstanding work of one key, and the ready outcomes of it a turn may retrieve now."""

    jobs: tuple[BackgroundJob, ...]
    ready: tuple[BackgroundJob, ...]


async def conversation_work(
    runtime: ToolJobRuntime,
    key: HoldKey,
    *,
    attempted: Collection[str] = (),
) -> ConversationWork:
    """Return the outstanding work of ``key``, apart from outcomes a reply already asked for."""
    held = [
        (job, readable)
        for job, readable in await runtime.held_jobs(
            transport_agent_name=key.recipient,
            room_id=key.room_id,
            thread_id=key.thread_id,
            requester_id=key.requester_id,
            source_kind=SILENT_SCHEDULE_SOURCE_KIND if key.silent else None,
        )
        if job.owner.agent_name in key.participants and job.job_id not in attempted
    ]
    return ConversationWork(
        jobs=tuple(job for job, _readable in held),
        ready=tuple(job for job, readable in held if readable and job.status in TERMINAL_STATUSES),
    )


def _retrieval_calls(jobs: Sequence[BackgroundJob]) -> str:
    calls = []
    for job in jobs:
        member = (
            f" through member {job.owner.agent_name}"
            if job.owner.transport_agent_name not in {None, job.owner.agent_name}
            else ""
        )
        calls.append(f'job(action="wait", job_id="{job.job_id}", wait_timeout=0){member}')
    return "; ".join(calls)


def completion_prompt(jobs: Sequence[BackgroundJob]) -> str:
    """Ask once for rich native result retrieval of work that is ready."""
    return (
        "Internal runtime update, not a new human request. Background work has finished. "
        "Retrieve these stored outcomes once using the native job tool, then continue the conversation: "
        + _retrieval_calls(jobs)
    )


async def join_conversation_jobs(
    attempted: set[str],
    *,
    joins: int,
    agent_names: Sequence[str] | None = None,
) -> _JobJoin:
    """Continue this reply with ready results, or report what it leaves outstanding.

    The reply retrieves its conversation's work, including work earlier replies started. Work it already asked to
    retrieve is not asked for again. With nothing ready and work outstanding, the span's answer waits for that work,
    unless the span runs for an approval or reached the join limit; the conversation's next reply then takes it.
    """
    context = get_tool_runtime_context()
    if context is None or job_owns_execution() or _DELEGATED_CHILD.get():
        return _JobJoin()
    runtime = get_background_runtime(context.runtime_paths)
    if runtime is None:
        return _JobJoin()
    key = HoldKey(
        recipient=context.recipient,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        requester_id=context.requester_id,
        silent=context.source_kind == SILENT_SCHEDULE_SOURCE_KIND,
        participants=tuple(sorted({context.agent_name, *(agent_names or ())})),
    )
    work = await conversation_work(runtime, key, attempted=attempted)
    ready = bool(work.ready) and joins < _JOB_JOIN_LIMIT
    # At the join limit the message stops holding, and the next reply in the conversation takes the work.
    holds = not ready and bool(work.jobs) and joins < _JOB_JOIN_LIMIT
    if (handle := current_span()) is not None:
        handle.leaves_work = key.encode() if holds else None
    if ready:
        attempted.update(job.job_id for job in work.ready)
        return _JobJoin(prompt=completion_prompt(work.ready))
    return _JobJoin(holds=holds, key=key)


async def join_approval_jobs[RunT](
    response: RunT,
    *,
    is_complete: Callable[[RunT], bool],
    continue_response: Callable[[RunT, str], Awaitable[RunT]],
    agent_names: Sequence[str] | None = None,
) -> RunT:
    """Continue a reconstructed approval run with ready results, at most `_JOB_JOIN_LIMIT` times."""
    attempted: set[str] = set()
    for joins in range(_JOB_JOIN_LIMIT + 1):
        if not is_complete(response):
            break
        join = await join_conversation_jobs(attempted, joins=joins, agent_names=agent_names)
        if join.prompt is None:
            break
        response = await continue_response(response, join.prompt)
    return response
