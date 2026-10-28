"""Which calls of a paused run need a person: the approval plan of a paused generation and the publication of its cards."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from mindroom.approval import manager as approval_manager
from mindroom.approval.auto_approve_windows import auto_approve_expires_at
from mindroom.approval.tool_approval import (
    POLICY_CONFIRMATION_APPROVAL_TYPE,
    evaluate_tool_approval,
    resolve_tool_approval_approver,
)
from mindroom.delegation.state import member_slot_id, pending_call_slot
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalContinuation,
    approval_arguments_digest,
)
from mindroom.event_journal import ApprovalDecision as ContinuationDecision
from mindroom.message_target import MessageTarget
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_system.events import tool_markers_match_trace
from mindroom.turn_origin import TurnIntent

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal.approval_store import ApprovalStore
    from mindroom.llm.runs import ToolExecution
    from mindroom.response_turn import PausedAttempt
    from mindroom.tool_approval_grants import ApprovalOperation


@dataclass(frozen=True)
class ApprovalPausePlan:
    """One paused generation normalized for persistence and card publication."""

    tools: tuple[ToolExecution, ...]
    calls: tuple[ApprovalCall, ...]
    waiting_text: str | None


def _team_config_names_by_provider_id(state: dict[str, object]) -> dict[str, str]:
    """Read the frozen provider-to-config identity map from a team presentation."""
    stored_members = state.get("members") if state.get("kind") == "team_stream" else None
    if not isinstance(stored_members, list):
        return {}
    config_names_by_provider_id: dict[str, str] = {}
    member_identities: set[str] = set()
    for member in stored_members:
        if not isinstance(member, dict):
            continue
        stored_member = cast("dict[str, object]", member)
        provider_id = stored_member.get("id")
        config_name = stored_member.get("config_name")
        if isinstance(provider_id, str) and provider_id and isinstance(config_name, str) and config_name:
            if {provider_id, config_name} & member_identities:
                msg = "Paused approval tool has ambiguous frozen member identity"
                raise RuntimeError(msg)
            member_identities.update((provider_id, config_name))
            config_names_by_provider_id[provider_id] = config_name
    return config_names_by_provider_id


def identify_approval_tools(
    paused: PausedAttempt,
    *,
    default_agent_name: str,
) -> tuple[tuple[ToolExecution, str], ...]:
    """Pair each paused call with the agent that invokes it, a team member by its config name."""
    # Each frozen member slot's config name, by slot id; a requirement names its member by either.
    slots = _team_config_names_by_provider_id(paused.response_presentation_state)
    owners = {
        requirement.tool_execution.tool_call_id: slots[member_slot_id(slots, requirement.member_agent_id)]
        for requirement in paused.requirements
        if requirement.member_agent_id
    }
    return tuple(
        (tool, paused.approval_agent_name or owners.get(tool.tool_call_id) or default_agent_name)
        for tool in paused.tools
    )


def require_ordered_pause_presentation(paused: PausedAttempt, *, show_tool_calls: bool) -> None:
    """Reject a visible approval handoff whose pending tools have no ordered anchors.

    The card of a team member's own delegation call, ``member_id:call_id``, is anchored by that call in the member's
    slot.
    """
    if not show_tool_calls:
        return
    unordered = "Approval suspension requires an ordered presentation for every pending tool"
    if not tool_markers_match_trace(paused.response_text, paused.tool_trace):
        raise RuntimeError(unordered)
    requirements_by_call_id = {
        requirement.tool_execution.tool_call_id: requirement for requirement in paused.requirements
    }
    is_team_presentation = paused.response_presentation_state.get("kind") == "team_stream"
    config_names_by_provider_id = _team_config_names_by_provider_id(paused.response_presentation_state)
    member_names = config_names_by_provider_id.values()
    for tool in paused.tools:
        call_id = tool.tool_call_id
        requirement = requirements_by_call_id.get(call_id)
        member_id, anchor_id = (None, call_id) if requirement is None else pending_call_slot(requirement, member_names)
        matches = [
            entry
            for entry in paused.tool_trace
            if entry.type == "tool_call_started" and entry.tool_call_id == anchor_id
        ]
        # An unanchored call is refused as such, before its member identity is resolved.
        if not matches:
            raise RuntimeError(unordered)
        if member_id is not None:
            member_id = member_slot_id(config_names_by_provider_id, member_id)
        expected_scope = f"agent:{member_id}" if member_id is not None else ("team" if is_team_presentation else None)
        # Call ids are unique only within one member's run, so another member's call may share the anchor's id.
        if sum(entry.scope_key == expected_scope for entry in matches) != 1:
            raise RuntimeError(unordered)


def continuation_target(
    continuation: ApprovalContinuation,
    *,
    reply_to_event_id: str | None = None,
) -> MessageTarget:
    """Return the canonical Matrix conversation target for one continuation."""
    if continuation.origin.intent is TurnIntent.JOB_FOLLOW_UP and reply_to_event_id in continuation.source_event_ids:
        # A follow-up is a runtime source, not a Matrix event to reply to.
        reply_to_event_id = None
    return MessageTarget(
        room_id=continuation.room_id,
        source_thread_id=continuation.thread_id,
        resolved_thread_id=continuation.thread_id,
        reply_to_event_id=reply_to_event_id,
        session_id=continuation.session_id,
    )


async def plan_approval_calls(
    identified: tuple[tuple[ToolExecution, str], ...],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    requester_id: str,
    toolkit_owners: Mapping[tuple[str, str], str],
    room_id: str | None,
    thread_id: str | None,
) -> ApprovalPausePlan:
    """Evaluate policy once and normalize exact calls with integer deadlines.

    The approver's auto-approve window in the conversation approves every call that would ask them.
    A call that would ask in a room without the router is denied with a reason, so the agent can invite the router.
    """
    approver_id = resolve_tool_approval_approver(config, runtime_paths, requester_id)
    manager = approval_manager.get_approval_store()
    decisions: dict[str, tuple[ContinuationDecision | None, float, bool, str | None]] = {}
    for tool, invoking_agent in identified:
        requires_approval, timeout_seconds = await evaluate_tool_approval(
            config,
            runtime_paths,
            tool.tool_name,
            dict(tool.tool_args or {}),
            invoking_agent,
        )
        tool_authored_confirmation = (
            tool.requires_confirmation is True and tool.approval_type != POLICY_CONFIRMATION_APPROVAL_TYPE
        )
        requires_approval = requires_approval or tool_authored_confirmation
        # Read after the awaited policy, so a window that ended meanwhile approves nothing.
        window_open = requires_approval and (
            auto_approve_expires_at(runtime_paths, room_id=room_id, thread_id=thread_id, requester_id=approver_id)
            is not None
        )
        decision, reason = None, None
        if not requires_approval or window_open:
            decision = ContinuationDecision.APPROVED
        elif approver_id is None:
            decision, reason = (
                ContinuationDecision.DENIED,
                "No approval recipient is configured; the tool was denied safely.",
            )
        elif room_id is not None and manager is not None and manager.router_missing and manager.router_missing(room_id):
            decision, reason = ContinuationDecision.DENIED, approval_manager.DEFAULT_ROUTER_MANAGED_ROOM_REASON
        decisions[tool.tool_call_id] = (decision, timeout_seconds, requires_approval, reason)
    now = datetime.now(UTC)
    calls = tuple(
        ApprovalCall(
            tool_call_id=tool.tool_call_id,
            tool_name=tool.tool_name,
            invoking_agent=invoking_agent,
            toolkit_name=toolkit_owners.get((invoking_agent, tool.tool_name)),
            arguments_digest=approval_arguments_digest(tool.tool_args),
            expires_at_ns=int(
                (now + timedelta(seconds=decisions[tool.tool_call_id][1])).timestamp() * 1_000_000_000,
            ),
            decision=decisions[tool.tool_call_id][0],
            reason=decisions[tool.tool_call_id][3],
            human_approval_required=decisions[tool.tool_call_id][2],
        )
        for tool, invoking_agent in identified
    )
    if any(call.toolkit_name is None for call in calls):
        msg = "Paused tool has no configured toolkit origin and cannot support restartable approval"
        raise RuntimeError(msg)
    gated_calls = tuple(call for call in calls if call.decision is None)
    return ApprovalPausePlan(
        tools=tuple(tool for tool, _invoking_agent in identified),
        calls=calls,
        waiting_text=(
            "Waiting for approval: " + ", ".join(f"`{call.tool_name}`" for call in gated_calls) if gated_calls else None
        ),
    )


def timed_grant_operation(config: Config, tool: ToolExecution, call: ApprovalCall) -> ApprovalOperation | None:
    """Return the operation a timed approval of this call covers; a tool's own confirmation is approved only once."""
    if tool.approval_type != POLICY_CONFIRMATION_APPROVAL_TYPE:
        return None
    return grant_operation(config, call.tool_name, dict(tool.tool_args or {}))


async def _publish_cards(
    continuation: ApprovalContinuation,
    plan: ApprovalPausePlan,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    store: ApprovalStore,
    target: MessageTarget,
    failure_reason: str,
) -> None:
    """Publish every human-gated card already linked by durable identity."""
    manager = approval_manager.get_approval_store()
    approver = resolve_tool_approval_approver(config, runtime_paths, continuation.requester_id)
    cards = []
    for index, (tool, call) in enumerate(zip(plan.tools, plan.calls, strict=True)):
        if call.decision is not None:
            continue
        if manager is None or approver is None:
            raise RuntimeError(failure_reason)
        card = await manager.prepare_detached_approval(
            approval_id=f"{continuation.approval_id}-{continuation.generation}-{index}",
            continuation_id=continuation.approval_id,
            continuation_generation=continuation.generation,
            entity_name=continuation.entity_name,
            response_event_id=continuation.response_event_id,
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            arguments=deepcopy(dict(tool.tool_args or {})),
            room_id=target.room_id,
            requester_id=continuation.requester_id,
            approver_user_id=approver,
            expires_at_ns=call.expires_at_ns,
            agent_name=call.invoking_agent,
            thread_id=target.resolved_thread_id,
            grant_operation=timed_grant_operation(config, tool, call),
        )
        if card is None:
            raise RuntimeError(failure_reason)
        cards.append(card)
    if cards:
        assert manager is not None
        if not await manager.reserve_and_publish(
            continuation_principal_id=store.principal_id,
            continuation_id=continuation.approval_id,
            continuation_generation=continuation.generation,
            cards=tuple(cards),
        ):
            raise RuntimeError(failure_reason)
    elif (
        await store.activate_approval_continuation(
            continuation.approval_id,
            expected_generation=continuation.generation,
        )
        is None
    ):
        raise RuntimeError(failure_reason)


async def publish_generation(
    continuation: ApprovalContinuation,
    plan: ApprovalPausePlan,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    store: ApprovalStore,
    target: MessageTarget,
    failure_reason: str,
    retry_sources: Callable[[str, tuple[str, ...]], None],
) -> None:
    """Publish every required card of a paused generation, release its lease, and wake it when it is executable."""
    if continuation.state == "waiting":
        await _publish_cards(
            continuation,
            plan,
            config=config,
            runtime_paths=runtime_paths,
            store=store,
            target=target,
            failure_reason=failure_reason,
        )
        refreshed = await store.approval_continuation(continuation.approval_id)
        if refreshed is None:
            raise RuntimeError(failure_reason)
        continuation = refreshed
    if continuation.state == "ready":
        retry_sources(continuation.room_id, continuation.source_event_ids)
