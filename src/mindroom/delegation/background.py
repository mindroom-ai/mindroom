"""A subagent call's job: the child it is known by, its saved snapshot, outcome, and cleanup."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, cast

from mindroom.delegation.sessions import SubagentSessionError, subagent_recovery_lock
from mindroom.delegation.state import TERMINAL_CHILD_STATUSES, DelegationChild
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload
from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome, JobRecoveryBlockedError, JobSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationTerminalStatus


def child_snapshot(child: DelegationChild) -> dict[str, Any]:
    """Keep execution identity in metadata; the job outcome owns retained result text."""
    return asdict(replace(child, result=None))


def delegation_child(job: BackgroundJob) -> DelegationChild:
    """Reconstruct the child a subagent job saved in its adapter."""
    if job.kind != "delegation":
        msg = "Job does not represent a native delegation."
        raise SubagentSessionError(msg)
    return DelegationChild.from_dict(job.adapter["child"])


def delegation_outcome(status: DelegationTerminalStatus, text: str) -> BackgroundOutcome:
    """Keep a child's full text in the job payload; job metadata keeps only its summary."""
    return BackgroundOutcome(status, text, result_payload=encode_result_payload(ToolResultPayload(value=text)))


def _terminal(child: DelegationChild) -> BackgroundOutcome | None:
    # A child rebuilt from its saved snapshot has no result, so cleanup must settle it.
    if child.result is None or child.status not in TERMINAL_CHILD_STATUSES:
        return None
    return delegation_outcome(cast("DelegationTerminalStatus", child.status), child.result)


async def reconcile_delegation(
    job: BackgroundJob,
    *,
    cleanup: Callable[[DelegationChild], Awaitable[None]],
    runtime_paths: RuntimePaths | None = None,
    child: DelegationChild | None = None,
) -> BackgroundOutcome | None:
    """Settle a stopped subagent job's child, under its recovery lock when rebuilt from its saved snapshot.

    A live child already settled by its job needs no cleanup.
    """
    if job.kind != "delegation":
        return None
    child = child or delegation_child(job)
    if _terminal(child) is None:
        if runtime_paths is not None:
            with subagent_recovery_lock(child.subagent_id, runtime_paths) as acquired:
                if not acquired:
                    msg = "Native child is still executing; recovery cannot settle it."
                    raise JobRecoveryBlockedError(msg)
                await cleanup(child)
        else:
            await cleanup(child)
    job.adapter["child"] = child_snapshot(child)
    return _terminal(child)


def delegation_job(
    child: DelegationChild,
    operation: Callable[[], Awaitable[BackgroundOutcome]],
    *,
    cleanup: Callable[[DelegationChild], Awaitable[None]],
) -> JobSpec:
    """Return the job that runs ``operation`` for ``child``, known as that child, and settles it with ``cleanup``.

    The saved child is the one admitted until the operation saves a newer one, and the one it ended with once it ends;
    a cancellation or shutdown in this process settles the live child, which recovery after a crash rebuilds from the
    saved one.
    """
    adapter: dict[str, Any] = {"child": child_snapshot(child)}

    async def run() -> BackgroundOutcome:
        try:
            return await operation()
        finally:
            adapter["child"] = child_snapshot(child)

    async def cancel(job: BackgroundJob) -> BackgroundOutcome | None:
        return await reconcile_delegation(job, cleanup=cleanup, child=child)

    # A subagent job is known by the agent it runs.
    return JobSpec(tool_name=child.child_agent_name, kind="delegation", adapter=adapter, operation=run, cancel=cancel)
