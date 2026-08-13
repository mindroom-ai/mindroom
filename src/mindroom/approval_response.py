"""Response-side coordination for persisted native tool approvals."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from mindroom.approval_continuation import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalContinuationStore,
)
from mindroom.approval_continuation import (
    ApprovalDecision as ContinuationDecision,
)
from mindroom.constants import STREAM_STATUS_APPROVAL_PENDING, STREAM_STATUS_COMPLETED, STREAM_STATUS_KEY
from mindroom.delivery_gateway import DeliveryStage, EditTextRequest, SendTextRequest
from mindroom.message_target import MessageTarget
from mindroom.tool_approval import (
    ToolApprovalCall,
    ToolApprovalTransportError,
    evaluate_tool_approval,
    expire_suspended_tool_approval,
    resolve_tool_approval_approver,
    send_suspended_tool_approval,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    import structlog
    from agno.models.response import ToolExecution

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delivery_gateway import DeliveryGateway
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
    """Own response-side continuation persistence, cards, and terminal settlement."""

    config: Callable[[], Config]
    runtime_paths: RuntimePaths
    agent_name: str
    delivery_gateway: DeliveryGateway
    logger: structlog.stdlib.BoundLogger
    runtime_generation: Callable[[], str]
    _store: ApprovalContinuationStore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Open the response coordinator's continuation-store handle."""
        self._store = ApprovalContinuationStore(self.runtime_paths.storage_root)

    async def _write[WriteResult](
        self,
        operation: Callable[..., WriteResult],
        /,
        *args: object,
        **kwargs: object,
    ) -> WriteResult:
        """Finish one durable mutation before propagating caller cancellation."""
        write = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(write)
        except asyncio.CancelledError:
            while not write.done():
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if write.done() and not write.cancelled() and (error := write.exception()) is not None:
                self.logger.warning(
                    "approval_continuation_cancelled_write_failed",
                    operation_type=type(operation).__qualname__,
                    error=str(error),
                    exc_info=(type(error), error, error.__traceback__),
                )
            raise

    async def close(self) -> None:
        """Close the response coordinator's continuation-store handle."""
        await asyncio.to_thread(self._store.close)

    async def create(self, continuation: ApprovalContinuation) -> ApprovalContinuation:
        """Persist a newly publishing continuation."""
        owned = replace(continuation, runtime_generation=self.runtime_generation())
        return await self._write(self._store.create, owned)

    async def get(self, approval_id: str) -> ApprovalContinuation | None:
        """Read one continuation without blocking the event loop."""
        return await asyncio.to_thread(self._store.get, approval_id)

    async def for_source_event(self, source_event_id: str) -> ApprovalContinuation | None:
        """Read continuation ownership for one inbound source event."""
        return await asyncio.to_thread(self._store.for_source_event, source_event_id)

    async def bind_response_event(
        self,
        approval_id: str,
        response_event_id: str,
        *,
        state: Literal["publishing", "pending", "ready", "settling"],
        calls: tuple[ApprovalCall, ...] | None = None,
    ) -> ApprovalContinuation | None:
        """Atomically bind the waiting event and optional normalized calls."""
        return await self._write(
            self._store.bind_response_event,
            approval_id,
            response_event_id,
            state=state,
            calls=calls,
        )

    async def claim(self, approval_id: str, claimant_id: str) -> ApprovalContinuation | None:
        """Claim one ready continuation exactly once."""
        return await self._write(
            self._store.claim,
            approval_id,
            claimant_id,
            runtime_generation=self.runtime_generation(),
        )

    async def complete(self, approval_id: str, claimant_id: str) -> ApprovalContinuation | None:
        """Complete one claimed continuation exactly once."""
        return await self._write(self._store.complete, approval_id, claimant_id)

    async def fail_claimed(
        self,
        approval_id: str,
        claimant_id: str,
        reason: str,
    ) -> ApprovalContinuation | None:
        """Fail a claimed continuation whose visible lifecycle already settled."""
        return await self._write(self._store.fail_claimed, approval_id, claimant_id, reason)

    async def _begin_failure(
        self,
        approval_id: str,
        reason: str,
        *,
        claimant_id: str | None,
        settlement_id: str,
    ) -> ApprovalContinuation | None:
        """Fence continuation execution before visible terminal settlement."""
        return await self._write(
            self._store.begin_failure,
            approval_id,
            reason,
            claimant_id=claimant_id,
            settlement_id=settlement_id,
            runtime_generation=self.runtime_generation(),
        )

    async def _release_failure(self, approval_id: str, settlement_id: str) -> ApprovalContinuation | None:
        """Release an incomplete settlement for a later retry."""
        return await self._write(self._store.release_failure, approval_id, settlement_id)

    async def _finish_failure(
        self,
        approval_id: str,
        settlement_id: str,
        reason: str,
    ) -> ApprovalContinuation | None:
        """Commit terminal failure for the exclusive settlement owner."""
        return await self._write(self._store.finish_failure, approval_id, settlement_id, reason)

    async def plan_pause(
        self,
        identified: tuple[tuple[ToolExecution, str, str, str], ...],
        *,
        requester_id: str,
    ) -> _ApprovalPausePlan:
        """Evaluate approval policy once and build the durable call generation."""
        config = self.config()
        approver_id = resolve_tool_approval_approver(
            config,
            self.runtime_paths,
            requester_id,
        )
        decisions: dict[str, tuple[ContinuationDecision | None, float]] = {}
        for tool, tool_call_id, tool_name, invoking_agent in identified:
            requires_approval, timeout_seconds = await evaluate_tool_approval(
                config,
                self.runtime_paths,
                tool_name,
                dict(tool.tool_args or {}),
                invoking_agent,
            )
            decisions[tool_call_id] = (
                (
                    None
                    if requires_approval and approver_id is not None
                    else ContinuationDecision.DENIED
                    if requires_approval
                    else ContinuationDecision.APPROVED
                ),
                timeout_seconds,
            )
        now = datetime.now(UTC)
        return _ApprovalPausePlan(
            tools=identified,
            decisions=decisions,
            calls=tuple(
                ApprovalCall(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    invoking_agent=invoking_agent,
                    expires_at=(now + timedelta(seconds=decisions[tool_call_id][1])).isoformat(),
                    decision=decisions[tool_call_id][0],
                    reason=(
                        "No approval recipient is configured; the tool was denied safely."
                        if decisions[tool_call_id][0] is ContinuationDecision.DENIED
                        else None
                    ),
                    decision_recorded=decisions[tool_call_id][0] is not None,
                )
                for _tool, tool_call_id, tool_name, invoking_agent in identified
            ),
            waiting_text="Waiting for approval: " + ", ".join(f"`{name}`" for _tool, _id, name, _owner in identified),
        )

    async def publish_cards(
        self,
        continuation: ApprovalContinuation,
        plan: _ApprovalPausePlan,
        *,
        target: MessageTarget,
        failure_reason: str,
    ) -> ApprovalContinuation:
        """Publish and attach every human-gated card for one persisted generation."""
        current = continuation
        config = self.config()
        for index, (tool, tool_call_id, tool_name, invoking_agent) in enumerate(plan.tools):
            decision, timeout_seconds = plan.decisions[tool_call_id]
            if decision is not None:
                continue
            try:
                sent = await send_suspended_tool_approval(
                    ToolApprovalCall(
                        config=config,
                        runtime_paths=self.runtime_paths,
                        tool_name=tool_name,
                        arguments=dict(tool.tool_args or {}),
                        agent_name=invoking_agent,
                        room_id=target.room_id,
                        thread_id=target.resolved_thread_id,
                        requester_id=current.requester_id,
                    ),
                    approval_id=f"{current.approval_id}-{current.generation}-{index}",
                    continuation_id=current.approval_id,
                    tool_call_id=tool_call_id,
                    timeout_seconds=timeout_seconds,
                )
            except ToolApprovalTransportError as error:
                await self.fail_publication(current.approval_id, reason=error.reason)
                raise
            if sent is None:
                await self.fail_publication(current.approval_id, reason=failure_reason)
                raise RuntimeError(failure_reason)
            attached = await self._write(
                self._store.attach_card,
                current.approval_id,
                tool_call_id,
                sent.event_id,
            )
            if attached is not None:
                current = attached
        return current

    async def advance_pause(
        self,
        current: ApprovalContinuation,
        paused: PausedAttempt,
        *,
        target: MessageTarget,
        claimant_id: str,
        tool_trace: list[ToolTraceEntry],
    ) -> tuple[ApprovalContinuation, str]:
        """Atomically replace a claimed continuation with Agno's next valid pause."""
        identified = identify_approval_tools(paused, default_agent_name=current.entity_name)
        plan = await self.plan_pause(identified, requester_id=current.requester_id)
        if current.response_event_id is None:
            msg = "Claimed approval continuation has no visible response"
            raise RuntimeError(msg)
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
        advanced = await self._write(
            self._store.advance_pause,
            current.approval_id,
            claimant_id,
            run_id=paused.run_id,
            session_id=paused.session_id,
            calls=plan.calls,
        )
        if advanced is None:
            msg = "Could not persist the chained approval pause"
            raise RuntimeError(msg)
        advanced = await self.publish_cards(
            advanced,
            plan,
            target=target,
            failure_reason="Chained approval card creation failed",
        )
        return advanced, plan.waiting_text

    async def fail_publication(self, approval_id: str, *, reason: str) -> None:
        """Fail a partially published approval set without losing recovery ownership."""
        continuation = await self.get(approval_id)
        if continuation is not None:
            await self.fail_continuation(continuation, reason)

    async def fail_continuation(self, continuation: ApprovalContinuation, reason: str) -> None:
        """Make an unrecoverable continuation visibly terminal."""
        current = await self.get(continuation.approval_id)
        if current is None or current.state in {"completed", "failed"}:
            return
        if await self._reconcile_claimed_delivery(current):
            return
        settlement_id = uuid4().hex
        current = await self._begin_failure(
            current.approval_id,
            reason,
            claimant_id=continuation.claimant_id,
            settlement_id=settlement_id,
        )
        if current is None or current.state != "settling" or current.settlement_id != settlement_id:
            return
        settlement_finished = False
        try:
            current = await self._adopt_waiting_delivery(current)
            reason = current.failure_reason or reason
            terminalized = await asyncio.gather(
                *(
                    expire_suspended_tool_approval(current.room_id, call.card_event_id)
                    for call in current.calls
                    if call.card_event_id is not None and not call.decision_recorded
                ),
                return_exceptions=True,
            )
            incomplete = sum(result is not True for result in terminalized)
            if incomplete:
                self.logger.warning(
                    "approval_continuation_card_settlement_incomplete",
                    approval_id=current.approval_id,
                    incomplete_cards=incomplete,
                )
                return
            target = MessageTarget(
                room_id=current.room_id,
                source_thread_id=current.thread_id,
                resolved_thread_id=current.thread_id,
                reply_to_event_id=None,
                session_id=current.session_id,
            )
            if current.response_event_id is not None and self.agent_name == current.entity_name:
                delivered = await self._edit_response(current, target=target, text=reason)
                response_event_id = None
            else:
                response_event_id = await self.delivery_gateway.send_text(
                    SendTextRequest(
                        target=target,
                        response_text=reason,
                        delivery_turn_id=current.source_event_ids[0],
                        delivery_stage=DeliveryStage.FINAL,
                    ),
                )
                delivered = response_event_id is not None
            if delivered and current.response_event_id is None:
                assert response_event_id is not None
                current = (
                    await self.bind_response_event(
                        current.approval_id,
                        response_event_id,
                        state="settling",
                    )
                    or current
                )
            if delivered:
                failed = await self._finish_failure(current.approval_id, settlement_id, reason)
                settlement_finished = failed is not None and failed.state == "failed"
            else:
                self.logger.warning(
                    "approval_continuation_failure_not_delivered",
                    approval_id=current.approval_id,
                    response_event_id=current.response_event_id,
                )
        finally:
            if not settlement_finished:
                await finish_cancelled_approval_settlement(
                    self._release_failure(current.approval_id, settlement_id),
                )

    async def _adopt_waiting_delivery(self, continuation: ApprovalContinuation) -> ApprovalContinuation:
        """Bind an acknowledged waiting event after a crash or cancellation gap."""
        if continuation.state not in {"publishing", "settling"} or continuation.response_event_id is not None:
            return continuation
        binding_state: Literal["publishing", "settling"] = (
            "publishing" if continuation.state == "publishing" else "settling"
        )
        delivery = await self.delivery_gateway.deps.outbox.load_delivery(
            turn_id=continuation.source_event_ids[0],
            stage=DeliveryStage.INITIAL,
        )
        if delivery is None or delivery.acknowledged_event_id is None:
            return continuation
        bound = await self.bind_response_event(
            continuation.approval_id,
            delivery.acknowledged_event_id,
            state=binding_state,
        )
        return continuation if bound is None else bound

    async def _reconcile_claimed_delivery(self, continuation: ApprovalContinuation) -> bool:
        """Honor a frozen FINAL outbox payload before recovered claim failure."""
        if continuation.state != "claimed":
            return False
        turn_id = continuation.source_event_ids[0]
        delivery = await self.delivery_gateway.deps.outbox.load_delivery(
            turn_id=turn_id,
            stage=DeliveryStage.FINAL,
        )
        if delivery is None:
            return False
        if delivery.acknowledged_event_id is None:
            await self.delivery_gateway.recover_deliveries()
            delivery = await self.delivery_gateway.deps.outbox.load_delivery(
                turn_id=turn_id,
                stage=DeliveryStage.FINAL,
            )
        if delivery is None or delivery.acknowledged_event_id is None:
            return False
        if continuation.claimant_id is not None:
            await self.complete(continuation.approval_id, continuation.claimant_id)
        return True

    async def _edit_response(
        self,
        continuation: ApprovalContinuation,
        *,
        target: MessageTarget,
        text: str,
    ) -> bool:
        """Durably replace a continuation's visible waiting response."""
        if continuation.response_event_id is None:
            return False
        return await self.delivery_gateway.edit_text(
            EditTextRequest(
                target=target,
                event_id=continuation.response_event_id,
                new_text=text,
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
                delivery_turn_id=continuation.source_event_ids[0],
            ),
        )


async def finish_cancelled_approval_settlement(operation: Coroutine[Any, Any, object]) -> None:
    """Finish durable approval settlement despite repeated task cancellation."""
    settlement = asyncio.create_task(operation)
    while not settlement.done():
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError:
            continue
    await settlement
