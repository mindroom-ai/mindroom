"""The response boundary: continue a reply with ready job results, or end it holding its outstanding work."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_jobs.held_replies import HoldKey, conversation_work, waiting_notice
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence

    from mindroom.streaming import StreamingPresentation
    from mindroom.tool_jobs.runtime import BackgroundJob


# How many times one message may continue with ready job results, counted apart from dynamic tool continuations.
JOB_JOIN_LIMIT = 20

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
class ReplyBoundary:
    """What a reply's last response boundary left of the work its message can hold."""

    key: HoldKey
    # The notice the message shows while it holds outstanding work, or None when it holds nothing.
    notice: str | None
    joins: int
    # Outcomes the reply already asked for, which turns continuing its message do not ask for again.
    offered: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _JobJoin:
    """A reply's response boundary: ready results to continue with, or whether its message holds outstanding work."""

    prompt: str | None = None
    holds: bool = False


@dataclass(frozen=True)
class HeldContinuation:
    """A turn continuing a held message: what the message already shows and the ready work it retrieves first."""

    presentation: StreamingPresentation
    # Outcomes this turn asks for first, with those the message already asked for, so neither is asked for again.
    attempted_job_ids: frozenset[str]
    # Ready results the message continued with before this turn; this turn's first retrieval adds one more.
    joins: int


@dataclass
class ReplyBoundaryReport:
    """One response's boundary outcome, read by its owner once the reply finished."""

    boundary: ReplyBoundary | None = None


_REPORT: ContextVar[ReplyBoundaryReport | None] = ContextVar("reply_boundary_report", default=None)


@contextmanager
def reply_boundary_report(report: ReplyBoundaryReport) -> Iterator[None]:
    """Collect the boundary outcome of the response run inside this scope into ``report``."""
    token = _REPORT.set(report)
    try:
        yield
    finally:
        _REPORT.reset(token)


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


def recovered_jobs_note(jobs: Sequence[BackgroundJob]) -> str:
    """Tell the re-run of an interrupted request to read, not repeat, the tool calls it already made."""
    return (
        "This request was interrupted before its reply finished. This reply replaces the interrupted one, so answer "
        "the request in full. The tool calls the interrupted attempt made became background jobs that were not "
        "replayed: do not repeat them, and retrieve their stored outcomes once using the native job tool: "
        + _retrieval_calls(jobs)
    )


async def join_conversation_jobs(
    attempted: set[str],
    *,
    joins: int,
    agent_names: Sequence[str] | None = None,
) -> _JobJoin:
    """Continue this reply with ready results, or record what it leaves outstanding for its message to hold.

    The reply retrieves its conversation's work, including work earlier replies started. Work it already asked to
    retrieve is not asked for again. With nothing ready, the reply ends, and its owner lets the message hold whatever
    is still outstanding.
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
    if work.ready and joins < JOB_JOIN_LIMIT:
        attempted.update(job.job_id for job in work.ready)
        return _JobJoin(prompt=completion_prompt(work.ready))
    # At the join limit the message stops holding, and the next reply in the conversation takes the work.
    holds = bool(work.jobs) and joins < JOB_JOIN_LIMIT
    report = _REPORT.get()
    if report is not None:
        notice = waiting_notice(work.jobs) if holds else None
        report.boundary = ReplyBoundary(key, notice, joins, offered=frozenset(attempted))
    return _JobJoin(holds=holds)


async def join_approval_jobs[RunT](
    response: RunT,
    *,
    is_complete: Callable[[RunT], bool],
    continue_response: Callable[[RunT, str], Awaitable[RunT]],
    agent_names: Sequence[str] | None = None,
) -> RunT:
    """Continue a reconstructed approval run with ready results, at most `JOB_JOIN_LIMIT` times."""
    attempted: set[str] = set()
    for joins in range(JOB_JOIN_LIMIT + 1):
        if not is_complete(response):
            break
        join = await join_conversation_jobs(attempted, joins=joins, agent_names=agent_names)
        if join.prompt is None:
            break
        response = await continue_response(response, join.prompt)
    return response
