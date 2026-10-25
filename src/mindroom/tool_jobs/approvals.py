"""Approval cards that a background job posts and owns while it waits for their decisions.

A background subagent's gated calls ask here, and so does a gated tool call that runs as a job. The job stays
`awaiting_approval` while its cards are open; an exit before every decision arrives denies the cards it posted.
Timed approvals work as they do for a paused reply: the cards offer them, and an active one approves a call
without a card.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from mindroom.approval import manager as approval_manager
from mindroom.approval.response import plan_approval_calls, timed_grant_operation
from mindroom.approval.tool_approval import (
    POLICY_CONFIRMATION_APPROVAL_TYPE,
    BackgroundScriptToolOrigin,
    resolve_tool_approval_approver,
)
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.event_journal import ApprovalDecision
from mindroom.llm.runs import ToolExecution
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.control import job_stopped_by_shutdown
from mindroom.tool_jobs.runtime import BackgroundOutcome

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.tool_jobs.execution_authority import JobCall
    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)
_CANCELLED_REASON = "The background job asking for this approval ended before a decision."
_INTERRUPTED_APPROVAL_REASON = "The runtime stopped while this call waited for its approval, so it did not run."


def _approval_run_id(job_id: str) -> str:
    """Name a job's approval cards in the exact-call approval domain that background work shares."""
    return f"tool-job:{job_id}"


async def ask_job_approvals(
    runtime: ToolJobRuntime,
    job_id: str,
    tools: Sequence[ToolExecution],
    calls: Sequence[ApprovalCall],
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[tuple[bool, str | None], ...]:
    """Ask the requester about each planned call through cards the job owns, and wait for every decision.

    Returns each call's decision and denial reason, in order. Any exit before then, a cancellation or a failure,
    stops the card requests still open and denies the cards already posted.
    """
    gated = any(call.decision is None for call in calls)
    if gated:
        await runtime.set_awaiting_approval(job_id, awaiting=True)
    decisions = [
        asyncio.ensure_future(_decide(job_id, tool, call, owner=owner, config=config, runtime_paths=runtime_paths))
        for tool, call in zip(tools, calls, strict=True)
    ]
    try:
        decided = await asyncio.gather(*decisions)
    except BaseException:
        # Cancellation or a failed card ends this wait; no card of it may stay answerable.
        await run_coroutine_until_complete(_end_wait(runtime, job_id, decisions))
        raise
    if gated:
        await runtime.set_awaiting_approval(job_id, awaiting=False)
    return tuple(decided)


async def ask_tool_call_approval(
    runtime: ToolJobRuntime,
    job_id: str,
    call: JobCall,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[bool, str | None]:
    """Decide one gated tool call that runs as a job, from policy or the requester's answer on the job's card.

    The policy, the card, and the execution all read the same frozen arguments.
    """
    call_id = call.context.call_id
    assert call_id is not None, "a managed call has its exact call identity"
    name = call.tool.function.name
    # Only the approval policy gates a call that asks as its job; a tool's own confirmation keeps pausing the reply.
    tool = ToolExecution(
        tool_call_id=call_id,
        tool_name=name,
        tool_args=dict(call.arguments),
        approval_type=POLICY_CONFIRMATION_APPROVAL_TYPE,
    )
    plan = await plan_approval_calls(
        ((tool, call_id, name, owner.agent_name),),
        config=config,
        runtime_paths=runtime_paths,
        requester_id=owner.requester_id or "",
        toolkit_owners={(owner.agent_name, name): call.tool.toolkit_name},
        room_id=owner.room_id,
        thread_id=owner.resolved_thread_id,
    )
    [decision] = await ask_job_approvals(
        runtime,
        job_id,
        plan.tools,
        plan.calls,
        owner=owner,
        config=config,
        runtime_paths=runtime_paths,
    )
    return decision


async def _decide(
    job_id: str,
    tool: ToolExecution,
    call: ApprovalCall,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[bool, str | None]:
    """Return a policy decision as is, or post one card into the thread and wait for the requester's decision."""
    if call.decision is not None:
        return call.decision is ApprovalDecision.APPROVED, call.reason
    manager = approval_manager.get_approval_store()
    approver = resolve_tool_approval_approver(config, runtime_paths, owner.requester_id)
    if manager is None or approver is None or owner.room_id is None or owner.requester_id is None:
        return False, "Tool approval runtime is not ready."
    assert call.toolkit_name is not None
    decision = await manager.request_background_approval(
        origin=BackgroundScriptToolOrigin(
            run_id=_approval_run_id(job_id),
            call_id=call.tool_call_id,
            requester_id=owner.requester_id,
            toolkit_name=call.toolkit_name,
            function_name=call.tool_name,
        ),
        room_id=owner.room_id,
        thread_id=owner.resolved_thread_id,
        agent_name=call.invoking_agent,
        requester_id=owner.requester_id,
        approver_user_id=approver,
        tool_name=call.tool_name,
        arguments=dict(tool.tool_args or {}),
        timeout_seconds=max(0.0, (call.expires_at_ns - time.time_ns()) / 1_000_000_000),
        # Timed approvals bind to the conversation the job's reply answered, exactly as a paused reply's do.
        entity_name=owner.recipient,
        grant_operation=timed_grant_operation(config, tool, call),
    )
    return decision.status == "approved", decision.reason


async def _end_wait(
    runtime: ToolJobRuntime,
    job_id: str,
    decisions: Sequence[asyncio.Future[tuple[bool, str | None]]],
) -> None:
    """Stop every card request of an ended wait, so none posts later, then deny the cards already posted."""
    for decision in decisions:
        decision.cancel()
    await asyncio.gather(*decisions, return_exceptions=True)
    await settle_job_approvals(runtime, job_id)


async def end_tool_call_approval(runtime: ToolJobRuntime, job: BackgroundJob) -> BackgroundOutcome | None:
    """Deny the card a stopped tool call's job waits on; a shutdown or restart during that wait leaves the call unrun."""
    await settle_job_approvals(runtime, job.job_id)
    if job.status == "awaiting_approval" and job_stopped_by_shutdown():
        # The approval is the job's first phase, so unlike other interrupted work its call has no effects to check.
        return BackgroundOutcome("interrupted", _INTERRUPTED_APPROVAL_REASON)
    return None


async def settle_job_approvals(runtime: ToolJobRuntime, job_id: str) -> None:
    """Deny every card a job still waits on, or leave the job to the coordinator's retries while that cannot happen."""
    manager = approval_manager.get_approval_store()
    if manager is not None and manager.cards is not None and manager.send_delivery is not None:
        try:
            await manager.settle_pending_background_approvals(_approval_run_id(job_id), reason=_CANCELLED_REASON)
        except Exception:
            # A settlement failure must not replace how the job ended.
            logger.exception("Denying a background job's approval cards failed; retrying", job_id=job_id)
        else:
            runtime.unsettled_approvals.discard(job_id)
            return
    runtime.unsettled_approvals.add(job_id)


async def prune_job_approvals(jobs: Iterable[BackgroundJob]) -> None:
    """Forget the settled approval targets of the jobs retention deleted; their cards retired long before."""
    manager = approval_manager.get_approval_store()
    if manager is None:
        return
    for job in jobs:
        await manager.prune_background_approvals(_approval_run_id(job.job_id))
