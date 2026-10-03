"""Native delegation adapter for the generic durable tool-job owner."""

from __future__ import annotations

from dataclasses import asdict, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from mindroom.delegation.sessions import SubagentSessionError, subagent_recovery_lock
from mindroom.delegation.state import DelegationChild
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload, read_result_payload
from mindroom.tool_jobs.runtime import (
    BackgroundJob,
    BackgroundOutcome,
    JobClaim,
    JobRecoveryBlockedError,
    ToolJobRuntime,
)
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _child_snapshot(child: DelegationChild) -> dict[str, Any]:
    """Keep execution identity in metadata; the job outcome owns retained result text."""
    return asdict(replace(child, result=None))


def delegation_child(job: BackgroundJob) -> DelegationChild:
    """Reconstruct a native child from an opaque durable adapter snapshot."""
    if job.kind != "delegation":
        msg = "Job does not represent a native delegation."
        raise SubagentSessionError(msg)
    return DelegationChild(**job.adapter["child"])


def delegation_outcome(status: Literal["completed", "failed", "cancelled", "denied"], text: str) -> BackgroundOutcome:
    """Keep a child's full text in the job payload; job metadata keeps only its summary."""
    return BackgroundOutcome(status, text, result_payload=encode_result_payload(ToolResultPayload(value=text)))


async def delegation_result(runtime: ToolJobRuntime, job: BackgroundJob) -> str | None:
    """Read a child's full saved text."""
    return (await read_result_payload(runtime, job)).value


def _terminal(child: DelegationChild) -> BackgroundOutcome | None:
    # Metadata-only reconstruction still needs the durable native outcome.
    if child.result is None:
        return None
    match child.status:
        case "completed" | "failed" | "cancelled" | "denied" as status:
            return delegation_outcome(status, child.result)
        case _:
            return None


async def reconcile_delegation(
    job: BackgroundJob,
    *,
    cleanup: Callable[[DelegationChild], Awaitable[None]],
    runtime_paths: RuntimePaths | None = None,
    child: DelegationChild | None = None,
) -> BackgroundOutcome | None:
    """Reconcile native durable evidence only while no native executor owns its handle."""
    if job.kind != "delegation":
        return None
    child = child or delegation_child(job)
    if _terminal(child) is None:
        if runtime_paths is not None and child.subagent_id is not None:
            with subagent_recovery_lock(child.subagent_id, runtime_paths) as acquired:
                if not acquired:
                    msg = "Native child is still executing; recovery cannot settle it."
                    raise JobRecoveryBlockedError(msg)
                await cleanup(child)
        else:
            await cleanup(child)
    job.adapter["child"] = _child_snapshot(child)
    return _terminal(child)


async def _run_refreshing_snapshot(
    operation: Callable[[], Awaitable[BackgroundOutcome]],
    child: DelegationChild,
    adapter: dict[str, Any],
) -> BackgroundOutcome:
    """Run the child, then snapshot its native state into the job adapter however the run ends."""
    try:
        return await operation()
    finally:
        adapter["child"] = _child_snapshot(child)


async def start_delegation(
    runtime: ToolJobRuntime,
    child: DelegationChild,
    *,
    owner: ToolExecutionIdentity,
    operation: Callable[[], Awaitable[BackgroundOutcome]],
    cancel: Callable[[DelegationChild], Awaitable[None]],
) -> tuple[BackgroundJob, JobClaim | None]:
    """Accept native child ownership without exposing its live object in a generic record, claiming its outcome."""
    if child.caller_agent_name != owner.agent_name:
        msg = "Child caller does not match its job owner."
        raise SubagentSessionError(msg)
    context = get_tool_runtime_context()
    adapter = {"child": _child_snapshot(child)}

    async def cleanup(job: BackgroundJob) -> BackgroundOutcome | None:
        return await reconcile_delegation(job, cleanup=cancel, child=child)

    return await runtime.start(
        child.delegation_id,
        tool_name="delegate",
        depth=child.depth - 1,
        kind="delegation",
        source_event_id=context.membership_turn_id if context is not None else None,
        source_kind=context.source_kind if context is not None else None,
        adapter=adapter,
        owner=owner,
        operation=partial(_run_refreshing_snapshot, operation, child, adapter),
        cancel=cleanup,
    )
