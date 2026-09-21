"""Native delegation adapter for the generic durable tool-job owner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary, ref

from mindroom.delegation.sessions import SubagentSessionError, subagent_recovery_lock
from mindroom.delegation.state import DelegationChild
from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome, JobSpec, ToolJobRuntime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from weakref import ReferenceType

    from mindroom.constants import RuntimePaths
    from mindroom.tool_jobs.control import HumanMessageSignal
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass
class _RetainedDelegation:
    child: ReferenceType[DelegationChild]
    adapter: dict[str, Any]


# Native live objects remain adapter-owned; durable generic records contain only JSON snapshots.
_retained: WeakKeyDictionary[ToolJobRuntime, dict[str, _RetainedDelegation]] = WeakKeyDictionary()


def _child_snapshot(child: DelegationChild) -> dict[str, Any]:
    """Keep execution identity in metadata; the job outcome owns retained result text."""
    return asdict(replace(child, result=None))


def delegation_child(job: BackgroundJob) -> DelegationChild:
    """Reconstruct a native child from an opaque durable adapter snapshot."""
    if job.kind != "delegation":
        msg = "Job does not represent a native delegation."
        raise SubagentSessionError(msg)
    return DelegationChild(**job.adapter["child"])


def _retained_delegation(runtime: ToolJobRuntime, job: BackgroundJob) -> tuple[DelegationChild, dict[str, Any]]:
    """Recover or reuse the exact live child and adapter owned by one operation."""
    retained = _retained.setdefault(runtime, {})
    binding = retained.get(job.job_id)
    child = binding.child() if binding is not None else None
    if child is None:
        child = delegation_child(job)
        binding = retained[job.job_id] = _RetainedDelegation(ref(child), job.adapter)
    assert binding is not None
    return child, binding.adapter


def retained_child(runtime: ToolJobRuntime, job: BackgroundJob) -> DelegationChild:
    """Recover or reuse the exact mutable native object retained by an active operation."""
    return _retained_delegation(runtime, job)[0]


def _terminal(child: DelegationChild) -> BackgroundOutcome | None:
    # Metadata-only reconstruction still needs the durable native outcome.
    if child.result is None:
        return None
    match child.status:
        case "completed" | "failed" | "cancelled" | "denied" as status:
            return BackgroundOutcome(status, child.result)
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
                    raise SubagentSessionError(msg)
                await cleanup(child)
        else:
            await cleanup(child)
    job.adapter["child"] = _child_snapshot(child)
    return _terminal(child)


async def start_delegation(
    runtime: ToolJobRuntime,
    child: DelegationChild,
    *,
    owner: ToolExecutionIdentity,
    operation: Callable[[], Awaitable[BackgroundOutcome]],
    human_signal: HumanMessageSignal | None = None,
    initial_wait_token: str | None = None,
    cancel: Callable[[DelegationChild], Awaitable[None]] | None = None,
    output_path: str | None = None,
) -> BackgroundJob:
    """Accept native child ownership without exposing its live object in a generic record."""
    if child.caller_agent_name != owner.agent_name:
        msg = "Child caller does not match its job owner."
        raise SubagentSessionError(msg)
    context = get_tool_runtime_context()
    adapter = {
        "child": _child_snapshot(child),
        "source_event_id": context.membership_turn_id if context is not None else None,
        "source_kind": context.source_kind if context is not None else None,
        "output_path": output_path,
    }

    async def run() -> BackgroundOutcome:
        try:
            return await operation()
        finally:
            adapter["child"] = _child_snapshot(child)

    async def cleanup(job: BackgroundJob) -> BackgroundOutcome | None:
        if cancel is not None:
            return await reconcile_delegation(job, cleanup=cancel, child=child)
        job.adapter["child"] = _child_snapshot(child)
        return _terminal(child)

    try:
        return await runtime.start(
            JobSpec(child.delegation_id, "delegate", child.depth - 1, kind="delegation", adapter=adapter),
            owner=owner,
            operation=run,
            human_signal=human_signal,
            initial_wait_token=initial_wait_token,
            cancel=cleanup,
        )
    finally:
        if runtime.owns_execution(child.delegation_id, adapter):
            _retained.setdefault(runtime, {})[child.delegation_id] = _RetainedDelegation(ref(child), adapter)


async def continue_delegation(
    runtime: ToolJobRuntime,
    job_id: str,
    *,
    owner: ToolExecutionIdentity,
    depth: int,
    operation: Callable[[], Awaitable[BackgroundOutcome]],
) -> BackgroundJob:
    """Continue native approval work under the existing generic job."""
    job = await runtime.lookup(job_id, owner=owner, depth=depth)
    child, adapter = _retained_delegation(runtime, job)

    async def run() -> BackgroundOutcome:
        try:
            return await operation()
        finally:
            # Continuation metadata is applied atomically with its outcome by the runtime.
            adapter["child"] = _child_snapshot(child)

    return await runtime.continue_job(job_id, owner=owner, depth=depth, operation=run, adapter=adapter)


def owns_delegation(runtime: ToolJobRuntime, child: DelegationChild) -> bool:
    """Recognize cancellation handoff only for the exact admitted live native child."""
    retained = _retained.get(runtime, {}).get(child.delegation_id)
    return (
        retained is not None
        and retained.child() is child
        and runtime.owns_execution(child.delegation_id, retained.adapter)
    )


async def cancel_retained_delegation(runtime: ToolJobRuntime, child: DelegationChild) -> bool:
    """Cancel trusted parent ownership even after public delegation authority changes."""

    def matches(job: BackgroundJob) -> bool:
        if job.kind != "delegation":
            return False
        retained = delegation_child(job)
        return (
            retained.caller_agent_name,
            retained.child_agent_name,
            retained.session_id,
            retained.run_id,
            retained.depth,
            retained.execution_identity,
        ) == (
            child.caller_agent_name,
            child.child_agent_name,
            child.session_id,
            child.run_id,
            child.depth,
            child.execution_identity,
        )

    job = await runtime.cancel_owned(child.delegation_id, matches=matches)
    if job is None:
        return False
    retained = delegation_child(job)
    child.status = retained.status
    child.result = job.result
    child.run_id = retained.run_id
    child.model_name = retained.model_name
    return True
