"""Ask about a paused background child's gated calls through approval cards its job owns."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.approval_response import identify_approval_tools, plan_approval_calls
from mindroom.tool_jobs.approvals import ask_job_approvals
from mindroom.tool_jobs.runtime import get_background_runtime

if TYPE_CHECKING:
    from agno.run.agent import RunOutput

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.event_journal import ApprovalCall
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_STOPPED_REASON = "Stopped before the approved call ran."


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
    decided = await ask_job_approvals(
        runtime,
        child.delegation_id,
        plan.tools,
        plan.calls,
        owner=owner,
        config=config,
        runtime_paths=runtime_paths,
    )
    if any(approved for approved, _reason in decided) and await runtime.stop_recorded(child.delegation_id):
        # A Stop recorded for the reply while the cards were open wins over their approvals.
        decided = tuple((False, _STOPPED_REASON) for _ in decided)
    decisions = {call.tool_call_id: approved for call, (approved, _reason) in zip(plan.calls, decided, strict=True)}
    reasons = {
        call.tool_call_id: None if approved else reason
        for call, (approved, reason) in zip(plan.calls, decided, strict=True)
    }
    return decisions, reasons, plan.calls
