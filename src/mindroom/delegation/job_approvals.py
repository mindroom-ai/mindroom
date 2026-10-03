"""Approval cards that a background child's job posts and owns while the child's paused run waits for decisions."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from mindroom import approval_manager
from mindroom.approval_response import identify_approval_tools, plan_approval_calls
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.event_journal import ApprovalDecision
from mindroom.logging_config import get_logger
from mindroom.tool_approval import BackgroundScriptToolOrigin, resolve_tool_approval_approver
from mindroom.tool_jobs.runtime import get_background_runtime

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agno.models.response import ToolExecution
    from agno.run.agent import RunOutput

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.event_journal import ApprovalCall
    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)
_CANCELLED_REASON = "The background job asking for this approval ended before a decision."


def _approval_run_id(job_id: str) -> str:
    """Name a job's approval cards in the exact-call approval domain that background work shares."""
    return f"tool-job:{job_id}"


async def request_child_approvals(
    child: DelegationChild,
    response: RunOutput,
    toolkit_owners: dict[tuple[str, str], str | None],
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[dict[str, bool], dict[str, str | None], tuple[ApprovalCall, ...]]:
    """Ask the requester about each gated call of a paused child through cards its job owns, and wait for them.

    Returns each call's decision and denial reason, and the planned calls the child resumes with.
    """
    from mindroom.response_turn import paused_attempt_from_response  # noqa: PLC0415 - response_turn imports delegation

    paused = paused_attempt_from_response(
        response,
        fallback_session_id=child.session_id,
        fallback_run_id=child.run_id,
        toolkit_owners=toolkit_owners,
    )
    if paused is None or owner.requester_id is None:
        msg = "Delegated child paused without supported exact approval requirements"
        raise RuntimeError(msg)
    plan = await plan_approval_calls(
        identify_approval_tools(paused, default_agent_name=child.child_agent_name),
        config=config,
        runtime_paths=runtime_paths,
        requester_id=owner.requester_id,
        toolkit_owners=paused.toolkit_owners,
    )
    runtime = get_background_runtime(runtime_paths)
    if runtime is None:
        msg = "Background tool jobs are not running."
        raise RuntimeError(msg)
    gated = any(call.decision is None for call in plan.calls)
    if gated:
        await runtime.set_awaiting_approval(child.delegation_id, awaiting=True)
    decisions = [
        asyncio.ensure_future(
            _decide(child.delegation_id, tool, call, owner=owner, config=config, runtime_paths=runtime_paths),
        )
        for tool, call in zip(plan.tools, plan.calls, strict=True)
    ]
    try:
        decided = await asyncio.gather(*decisions)
    except BaseException:
        # Cancellation or a failed card ends this pause; no card of it may stay answerable.
        await run_coroutine_until_complete(_end_pause(runtime, child.delegation_id, decisions))
        raise
    if gated:
        await runtime.set_awaiting_approval(child.delegation_id, awaiting=False)
    decisions = {call.tool_call_id: approved for call, (approved, _reason) in zip(plan.calls, decided, strict=True)}
    reasons = {
        call.tool_call_id: None if approved else reason
        for call, (approved, reason) in zip(plan.calls, decided, strict=True)
    }
    return decisions, reasons, plan.calls


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
    )
    return decision.status == "approved", decision.reason


async def _end_pause(
    runtime: ToolJobRuntime,
    job_id: str,
    decisions: Sequence[asyncio.Future[tuple[bool, str | None]]],
) -> None:
    """Stop every card request of an ended pause, so none posts later, then deny the cards already posted."""
    for decision in decisions:
        decision.cancel()
    await asyncio.gather(*decisions, return_exceptions=True)
    await settle_child_approvals(runtime, job_id)


async def settle_child_approvals(runtime: ToolJobRuntime, job_id: str) -> None:
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


async def prune_child_approvals(jobs: Iterable[BackgroundJob]) -> None:
    """Forget the settled approval targets of the delegation jobs retention deleted; their cards retired long before."""
    manager = approval_manager.get_approval_store()
    if manager is None:
        return
    for job in jobs:
        if job.kind == "delegation":
            await manager.prune_background_approvals(_approval_run_id(job.job_id))
