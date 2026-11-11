"""Ask about a paused background child's gated calls through approval cards its job owns."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.approval.response import identify_approval_tools, plan_approval_calls
from mindroom.delegation.lifecycle import live_delegation_config, report_child_approval_wait
from mindroom.event_journal import ApprovalDecision
from mindroom.response_turn import paused_attempt_from_response
from mindroom.tool_jobs.approvals import ask_job_approvals

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.event_journal import ApprovalCall
    from mindroom.llm.runs import RunRecord
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


async def request_child_approvals(
    child: DelegationChild,
    response: RunRecord,
    toolkit_owners: Mapping[tuple[str, str], str],
    *,
    runtime: ToolJobRuntime,
    job_id: str,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[ApprovalCall, ...]:
    """Ask the requester about each gated call of a paused child through cards ``runtime``'s job ``job_id`` owns, and wait.

    Returns the planned calls the child resumes with, each approved, or denied with its reason.
    """
    paused = paused_attempt_from_response(
        response,
        toolkit_owners=toolkit_owners,
    )
    if paused is None or owner.requester_id is None:
        msg = "Delegated child paused without supported exact approval requirements"
        raise RuntimeError(msg)
    # A child can pause long after its job started: the policy and approver current when it asks decide, as for a
    # paused reply.
    config = live_delegation_config(config)
    plan = await plan_approval_calls(
        identify_approval_tools(paused, default_agent_name=child.child_agent_name),
        config=config,
        runtime_paths=runtime_paths,
        requester_id=owner.requester_id,
        toolkit_owners=paused.toolkit_owners,
        room_id=owner.room_id,
        thread_id=owner.resolved_thread_id,
    )
    # The child waits for the requester only while a card is open; a call policy decided needs none.
    gated = any(call.decision is None for call in plan.calls)
    if gated:
        report_child_approval_wait(child, awaiting=True)
    decided = await ask_job_approvals(
        runtime,
        job_id,
        plan.tools,
        plan.calls,
        owner=owner,
        config=config,
        runtime_paths=runtime_paths,
        # The child's calls are not in the reply that started the job, so the cards name no reply event.
        response_event_id=None,
    )
    if gated:
        report_child_approval_wait(child, awaiting=False)
    return tuple(
        replace(
            call,
            decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.DENIED,
            reason=None if approved else reason,
        )
        for call, (approved, reason) in zip(plan.calls, decided, strict=True)
    )
