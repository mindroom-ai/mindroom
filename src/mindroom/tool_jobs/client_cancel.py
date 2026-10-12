"""A person's cancel of background jobs from a client, and the jobs a bot leaves behind in a room it left."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.requester_identity import is_managed_entity_id
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, describe_job

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime

BACKGROUND_JOB_CANCEL_EVENT_TYPE = "io.mindroom.background_job_cancel"


@dataclass(frozen=True)
class JobCancelRequest:
    """One job to cancel, or, with no ``job_id``, every job of the thread it was sent in, or of the room outside any."""

    job_id: str | None
    thread_id: str | None


def _parse_job_cancel(content: Mapping[str, object]) -> JobCancelRequest | None:
    """Read a cancel event's content: ``{"job_id": ...}`` or ``{"all": true}``, in the thread it relates to."""
    relation = content.get("m.relates_to")
    thread_id = None
    if isinstance(relation, Mapping) and cast("Mapping[str, object]", relation).get("rel_type") == "m.thread":
        event_id = cast("Mapping[str, object]", relation).get("event_id")
        thread_id = event_id if isinstance(event_id, str) else None
    job_id = content.get("job_id")
    if isinstance(job_id, str) and job_id:
        return JobCancelRequest(job_id=job_id, thread_id=thread_id)
    if content.get("all") is True:
        return JobCancelRequest(job_id=None, thread_id=thread_id)
    return None


def person_may_cancel(
    sender_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    may_talk: Callable[[str], bool],
) -> bool:
    """Return whether a sender may stop an entity's work here: a person who may talk to it, as with 🛑, never an agent."""
    return not is_managed_entity_id(sender_id, config, runtime_paths) and may_talk(sender_id)


def _live_jobs(runtime: ToolJobRuntime, *, agent_name: str, room_id: str) -> list[BackgroundJob]:
    return [
        job
        for job in runtime.retained_jobs()
        if job.status not in TERMINAL_STATUSES and job.owner.recipient == agent_name and job.owner.room_id == room_id
    ]


async def _cancel_jobs(
    runtime: ToolJobRuntime,
    *,
    agent_name: str,
    room_id: str,
    request: JobCancelRequest,
) -> tuple[BackgroundJob, ...]:
    """Cancel the agent's live jobs a request names in a room, and return them."""
    live = _live_jobs(runtime, agent_name=agent_name, room_id=room_id)
    if request.job_id is None:
        targets = [job for job in live if request.thread_id in {None, job.owner.resolved_thread_id}]
    else:
        targets = [job for job in live if job.job_id == request.job_id]
    for job in targets:
        await runtime.request_cancel(job.job_id)
    return tuple(targets)


async def handle_job_cancel_event(
    runtime: ToolJobRuntime,
    *,
    agent_name: str,
    room_id: str,
    content: Mapping[str, object],
    sender_may_cancel: bool,
) -> tuple[BackgroundJob, ...]:
    """Apply a client's cancel event to this agent's jobs, when its sender may talk to the agent in the room."""
    request = _parse_job_cancel(content)
    if request is None or not sender_may_cancel:
        return ()
    return await _cancel_jobs(runtime, agent_name=agent_name, room_id=room_id, request=request)


async def cancel_room_jobs(runtime: ToolJobRuntime, *, agent_name: str, room_id: str) -> None:
    """Cancel every live job of an agent in a room it left, where nobody can see or cancel them any more."""
    for job in _live_jobs(runtime, agent_name=agent_name, room_id=room_id):
        await runtime.request_cancel(job.job_id)


def conversation_jobs(runtime: ToolJobRuntime, *, room_id: str, thread_id: str | None) -> list[BackgroundJob]:
    """Return the live jobs of every entity in one thread, or in the whole room for ``None``, oldest first."""
    return sorted(
        (
            job
            for job in runtime.retained_jobs()
            if job.status not in TERMINAL_STATUSES
            and job.owner.room_id == room_id
            and thread_id in {None, job.owner.resolved_thread_id}
        ),
        key=lambda job: (job.created_at, job.job_id),
    )


def jobs_listing(jobs: list[BackgroundJob]) -> str:
    """Describe a conversation's live jobs for a chat reply."""
    if not jobs:
        return "No background jobs are running here."
    lines = [f"- {describe_job(job, short=True)} {job.status}, by {job.owner.recipient}" for job in jobs]
    return "Background jobs running here:\n" + "\n".join(lines)


async def cancel_jobs_command(
    runtime: ToolJobRuntime,
    *,
    room_id: str,
    thread_id: str | None,
    request: JobCancelRequest,
    may_cancel: Callable[[BackgroundJob], bool],
) -> str:
    """Cancel the jobs a chat command names that its sender may stop, and describe what it cancelled."""
    prefix = request.job_id
    if prefix is None:
        targets = conversation_jobs(runtime, room_id=room_id, thread_id=thread_id)
    else:
        # A job ID, or any start of one, names one job, wherever in the room it runs.
        targets = [
            job for job in conversation_jobs(runtime, room_id=room_id, thread_id=None) if job.job_id.startswith(prefix)
        ]
        if len(targets) > 1:
            return "That ID matches several background jobs here; use more of it."
    allowed = [job for job in targets if may_cancel(job)]
    if not allowed:
        if prefix is None:
            return "No background jobs to cancel here."
        return "No running background job here has that ID; `!jobs` lists them."
    for job in allowed:
        await runtime.request_cancel(job.job_id)
    return "Cancelling " + ", ".join(describe_job(job, short=True) for job in allowed) + "."
