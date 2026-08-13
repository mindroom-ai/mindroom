"""Response-side coordination for journal-owned native tool approvals."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mindroom.constants import STREAM_STATUS_APPROVAL_PENDING, STREAM_STATUS_COMPLETED, STREAM_STATUS_KEY
from mindroom.delivery_gateway import DeliveryStage, EditTextRequest
from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.event_journal import ApprovalDecision as ContinuationDecision
from mindroom.message_target import MessageTarget
from mindroom.tool_approval import (
    ToolApprovalCall,
    evaluate_tool_approval,
    expire_continuation_approval_cards,
    resolve_tool_approval_approver,
    send_suspended_tool_approval,
)

_USER_STOP_FAILURE_REASON = "cancelled_by_user"
_USER_STOP_VISIBLE_NOTE = "**[Response cancelled by user]**"

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.models.response import ToolExecution

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import OutboxDelivery, PrincipalStore
    from mindroom.response_turn import PausedAttempt
    from mindroom.tool_system.events import ToolTraceEntry


@dataclass(frozen=True)
class _ApprovalPausePlan:
    """One paused generation normalized for persistence and card publication."""

    tools: tuple[tuple[ToolExecution, str, str, str], ...]
    decisions: dict[str, tuple[ContinuationDecision | None, float]]
    calls: tuple[ApprovalCall, ...]
    waiting_text: str


def identify_approval_tools(
    paused: PausedAttempt,
    *,
    default_agent_name: str,
) -> tuple[tuple[ToolExecution, str, str, str], ...]:
    """Resolve exact paused call IDs, names, and invoking member ownership."""
    owners = {
        requirement.tool_execution.tool_call_id: requirement.member_agent_name
        for requirement in paused.requirements
        if requirement.tool_execution is not None and requirement.member_agent_name
    }
    identified: list[tuple[ToolExecution, str, str, str]] = []
    for tool in paused.tools:
        if not tool.tool_call_id or not tool.tool_name:
            msg = "Paused approval tool is missing its exact identity"
            raise RuntimeError(msg)
        identified.append(
            (
                tool,
                tool.tool_call_id,
                tool.tool_name,
                owners.get(tool.tool_call_id, default_agent_name),
            ),
        )
    return tuple(identified)


@dataclass
class ApprovalResponseCoordinator:
    """Own approval policy, card publication, and visible terminal settlement."""

    config: Callable[[], Config]
    runtime_paths: RuntimePaths
    store: PrincipalStore
    delivery_gateway: DeliveryGateway
    retry_sources: Callable[[tuple[str, ...]], None]

    async def create(self, continuation: ApprovalContinuation) -> ApprovalContinuation:
        """Persist one born-bound paused run against its original sources."""
        created = await self.store.create_approval_continuation(continuation)
        if created is None:
            msg = f"Could not create approval continuation {continuation.approval_id!r}"
            raise RuntimeError(msg)
        return created

    async def activate(self, continuation: ApprovalContinuation) -> ApprovalContinuation:
        """Expose one generation after every human-gated card is durable."""
        activated = await self.store.activate_approval_continuation(
            continuation.approval_id,
            expected_generation=continuation.generation,
        )
        if activated is None:
            msg = f"Could not activate approval continuation {continuation.approval_id!r}"
            raise RuntimeError(msg)
        return activated

    async def plan_pause(
        self,
        identified: tuple[tuple[ToolExecution, str, str, str], ...],
        *,
        requester_id: str,
    ) -> _ApprovalPausePlan:
        """Evaluate policy once and normalize exact calls with integer deadlines."""
        config = self.config()
        approver_id = resolve_tool_approval_approver(config, self.runtime_paths, requester_id)
        decisions: dict[str, tuple[ContinuationDecision | None, float]] = {}
        for tool, tool_call_id, tool_name, invoking_agent in identified:
            requires_approval, timeout_seconds = await evaluate_tool_approval(
                config,
                self.runtime_paths,
                tool_name,
                dict(tool.tool_args or {}),
                invoking_agent,
            )
            requires_approval = requires_approval or tool.requires_confirmation is True
            decisions[tool_call_id] = (
                None
                if requires_approval and approver_id is not None
                else ContinuationDecision.DENIED
                if requires_approval
                else ContinuationDecision.APPROVED,
                timeout_seconds,
            )
        now = datetime.now(UTC)
        calls = tuple(
            ApprovalCall(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                invoking_agent=invoking_agent,
                expires_at_ns=int((now + timedelta(seconds=decisions[tool_call_id][1])).timestamp() * 1_000_000_000),
                decision=decisions[tool_call_id][0],
                reason=(
                    "No approval recipient is configured; the tool was denied safely."
                    if decisions[tool_call_id][0] is ContinuationDecision.DENIED
                    else None
                ),
            )
            for _tool, tool_call_id, tool_name, invoking_agent in identified
        )
        return _ApprovalPausePlan(
            tools=identified,
            decisions=decisions,
            calls=calls,
            waiting_text="Waiting for approval: " + ", ".join(f"`{name}`" for _tool, _id, name, _owner in identified),
        )

    async def _publish_cards(
        self,
        continuation: ApprovalContinuation,
        plan: _ApprovalPausePlan,
        *,
        target: MessageTarget,
        failure_reason: str,
    ) -> None:
        """Publish every human-gated card already linked by durable identity."""
        config = self.config()
        calls_by_id = {call.tool_call_id: call for call in plan.calls}
        for index, (tool, tool_call_id, tool_name, invoking_agent) in enumerate(plan.tools):
            decision, _timeout_seconds = plan.decisions[tool_call_id]
            if decision is not None:
                continue
            sent = await send_suspended_tool_approval(
                ToolApprovalCall(
                    config=config,
                    runtime_paths=self.runtime_paths,
                    tool_name=tool_name,
                    arguments=dict(tool.tool_args or {}),
                    agent_name=invoking_agent,
                    room_id=target.room_id,
                    thread_id=target.resolved_thread_id,
                    requester_id=continuation.requester_id,
                ),
                approval_id=f"{continuation.approval_id}-{continuation.generation}-{index}",
                continuation_id=continuation.approval_id,
                continuation_generation=continuation.generation,
                tool_call_id=tool_call_id,
                expires_at_ns=calls_by_id[tool_call_id].expires_at_ns,
            )
            if sent is None:
                raise RuntimeError(failure_reason)

    async def publish_generation(
        self,
        continuation: ApprovalContinuation,
        plan: _ApprovalPausePlan,
        *,
        target: MessageTarget,
        failure_reason: str,
    ) -> None:
        """Publish every required card, release its lease, and wake executable work."""
        if continuation.state == "waiting":
            await self._publish_cards(
                continuation,
                plan,
                target=target,
                failure_reason=failure_reason,
            )
            continuation = await self.activate(continuation)
        if continuation.state == "ready":
            self.retry_sources(continuation.source_event_ids)

    async def advance_pause(
        self,
        current: ApprovalContinuation,
        paused: PausedAttempt,
        *,
        target: MessageTarget,
        tool_trace: list[ToolTraceEntry],
    ) -> str:
        """Replace one claim with Agno's next exact pause generation."""
        identified = identify_approval_tools(paused, default_agent_name=current.entity_name)
        plan = await self.plan_pause(identified, requester_id=current.requester_id)
        if not await self.delivery_gateway.edit_text(
            EditTextRequest(
                target=target,
                event_id=current.response_event_id,
                new_text=plan.waiting_text,
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
                tool_trace=tool_trace or None,
            ),
        ):
            msg = "Could not publish the chained approval response"
            raise RuntimeError(msg)
        publishing = await self.store.advance_approval_continuation(
            current.approval_id,
            claimant_generation=current.generation,
            run_id=paused.run_id,
            session_id=paused.session_id,
            calls=plan.calls,
        )
        if publishing is None:
            msg = "Could not persist the chained approval pause"
            raise RuntimeError(msg)
        try:
            await self.publish_generation(
                publishing,
                plan,
                target=target,
                failure_reason="Chained approval card creation failed",
            )
        except (asyncio.CancelledError, Exception):
            await self.request_failure(
                publishing,
                "Chained approval card creation failed",
            )
            raise
        return plan.waiting_text

    async def request_failure(
        self,
        continuation: ApprovalContinuation,
        reason: str,
    ) -> ApprovalContinuation | None:
        """Fence exactly the state a failed lifecycle observed and wake settlement."""
        failing = await self.store.request_approval_failure(
            continuation.approval_id,
            reason,
            expected_state=continuation.state,
            expected_generation=continuation.generation,
            expected_runtime_generation=continuation.runtime_generation,
        )
        if failing is not None:
            self.retry_sources(failing.source_event_ids)
        return failing

    async def fail_publication(self, approval_id: str, *, reason: str) -> ApprovalContinuation | None:
        """Fence a born continuation whose card publication did not finish."""
        continuation = await self.store.approval_continuation(approval_id)
        return None if continuation is None else await self.request_failure(continuation, reason)

    async def settle_failure(self, continuation: ApprovalContinuation, reason: str) -> bool:
        """Settle cards and one durable failure edit from the owning source worker."""
        current = await self.store.approval_continuation(continuation.approval_id)
        if current is None:
            return True
        if current.state == "claimed" and await self.final_delivery(current) is not None:
            return False
        if current.state != "failing":
            current = await self.request_failure(current, reason)
            if current is None:
                return False
        if not await expire_continuation_approval_cards(current.approval_id):
            return False
        visible_reason = _USER_STOP_VISIBLE_NOTE if reason == _USER_STOP_FAILURE_REASON else reason
        target = MessageTarget(
            room_id=current.room_id,
            source_thread_id=current.thread_id,
            resolved_thread_id=current.thread_id,
            reply_to_event_id=None,
            session_id=current.session_id,
        )
        delivered = await self.delivery_gateway.edit_text(
            EditTextRequest(
                target=target,
                event_id=current.response_event_id,
                new_text=visible_reason,
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
                delivery_turn_id=current.source_event_ids[0],
                defer_source_handoff=True,
            ),
        )
        return delivered and await self.store.finish_approval_continuation(current.approval_id)

    async def final_delivery(
        self,
        continuation: ApprovalContinuation,
        *,
        recover: bool = False,
    ) -> OutboxDelivery | None:
        """Return the continuation's frozen FINAL, optionally retrying its delivery."""
        outbox = self.delivery_gateway.deps.outbox
        delivery = await outbox.load_delivery(
            turn_id=continuation.source_event_ids[0],
            stage=DeliveryStage.FINAL,
        )
        if recover and delivery is not None and delivery.acknowledged_event_id is None:
            await self.delivery_gateway.recover_deliveries()
            delivery = await outbox.load_delivery(
                turn_id=continuation.source_event_ids[0],
                stage=DeliveryStage.FINAL,
            )
        return delivery
