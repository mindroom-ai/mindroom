"""Response-side coordination for journal-owned native tool approvals."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from mindroom import approval_manager, interactive
from mindroom.constants import (
    DURABLE_FINAL_OUTCOME_KEY,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
)
from mindroom.delivery_gateway import DeliveryStage, EditTextRequest, SendTextRequest, TextDeliveryOutcome
from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.event_journal import ApprovalDecision as ContinuationDecision
from mindroom.message_target import MessageTarget
from mindroom.tool_approval import (
    POLICY_CONFIRMATION_APPROVAL_TYPE,
    evaluate_tool_approval,
    resolve_tool_approval_approver,
)
from mindroom.tool_system.events import format_tool_started_event, serialize_tool_trace

_USER_STOP_FAILURE_REASON = "cancelled_by_user"


_USER_STOP_VISIBLE_NOTE = "**[Response cancelled by user]**"

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.models.response import ToolExecution

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import MatrixDelivery, PrincipalStore
    from mindroom.response_turn import PausedAttempt
    from mindroom.tool_system.events import ToolTraceEntry


@dataclass(frozen=True)
class _ApprovalPausePlan:
    """One paused generation normalized for persistence and card publication."""

    tools: tuple[ToolExecution, ...]
    calls: tuple[ApprovalCall, ...]
    waiting_text: str | None


@dataclass(frozen=True)
class _ApprovalPausePresentation:
    """Visible response state published for one chained approval generation."""

    response_text: str
    tool_trace: tuple[ToolTraceEntry, ...]
    approval_pending: bool


@dataclass(frozen=True)
class _ApprovalPauseDelivery:
    """One initial pause body after transport acknowledgement."""

    event_id: str
    visible_body: str
    delivery_kind: Literal["sent", "edited"] | None


def _approval_pause_delivery_text(paused: PausedAttempt, fallback: str) -> str:
    """Render one pause without confusing raw renderer state with visible text."""
    if paused.visible_response_text:
        return paused.visible_response_text
    source_text = paused.response_text or fallback
    return interactive.parse_and_format_interactive(source_text, extract_mapping=True).formatted_text


def _require_text_delivery(outcome: TextDeliveryOutcome | None, failure_reason: str) -> TextDeliveryOutcome:
    """Return an acknowledged text delivery or raise the owning publication error."""
    if outcome is None:
        raise RuntimeError(failure_reason)
    return outcome


def _require_continuation(
    continuation: ApprovalContinuation | None,
    failure_reason: str,
) -> ApprovalContinuation:
    """Return a journal-owned continuation or raise its publication error."""
    if continuation is None:
        raise RuntimeError(failure_reason)
    return continuation


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


def durable_pause_tool_trace(
    paused: PausedAttempt,
    identified: tuple[tuple[ToolExecution, str, str, str], ...],
    *,
    entity_kind: Literal["agent", "team"],
    coordinator_name: str,
) -> tuple[ToolTraceEntry, ...]:
    """Add private pending identity when a blocking pause had no streaming tool event."""
    trace = list(paused.tool_trace)
    seen_ids = {entry.tool_call_id for entry in trace if entry.tool_call_id is not None}
    for tool, tool_call_id, _tool_name, invoking_agent in identified:
        if tool_call_id in seen_ids:
            continue
        _marker, entry = format_tool_started_event(tool, tool_index=len(trace) + 1)
        if entry is None or entry.tool_call_id != tool_call_id:
            msg = "Paused approval tool could not be assigned durable presentation identity"
            raise RuntimeError(msg)
        if entity_kind == "team":
            entry.scope_key = "team" if invoking_agent == coordinator_name else f"agent:{invoking_agent}"
        trace.append(entry)
        seen_ids.add(tool_call_id)
    return tuple(trace)


def continuation_target(
    continuation: ApprovalContinuation,
    *,
    reply_to_event_id: str | None = None,
) -> MessageTarget:
    """Return the canonical Matrix conversation target for one continuation."""
    return MessageTarget(
        room_id=continuation.room_id,
        source_thread_id=continuation.thread_id,
        resolved_thread_id=continuation.thread_id,
        reply_to_event_id=reply_to_event_id,
        session_id=continuation.session_id,
    )


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

    async def plan_pause(
        self,
        identified: tuple[tuple[ToolExecution, str, str, str], ...],
        *,
        requester_id: str,
    ) -> _ApprovalPausePlan:
        """Evaluate policy once and normalize exact calls with integer deadlines."""
        config = self.config()
        approver_id = resolve_tool_approval_approver(config, self.runtime_paths, requester_id)
        decisions: dict[str, tuple[ContinuationDecision | None, float, bool]] = {}
        for tool, tool_call_id, tool_name, invoking_agent in identified:
            requires_approval, timeout_seconds = await evaluate_tool_approval(
                config,
                self.runtime_paths,
                tool_name,
                dict(tool.tool_args or {}),
                invoking_agent,
            )
            tool_authored_confirmation = (
                tool.requires_confirmation is True and tool.approval_type != POLICY_CONFIRMATION_APPROVAL_TYPE
            )
            requires_approval = requires_approval or tool_authored_confirmation
            decisions[tool_call_id] = (
                None
                if requires_approval and approver_id is not None
                else ContinuationDecision.DENIED
                if requires_approval
                else ContinuationDecision.APPROVED,
                timeout_seconds,
                requires_approval,
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
                human_approval_required=decisions[tool_call_id][2],
            )
            for _tool, tool_call_id, tool_name, invoking_agent in identified
        )
        gated_calls = tuple(call for call in calls if call.decision is None)
        return _ApprovalPausePlan(
            tools=tuple(tool for tool, _tool_call_id, _tool_name, _invoking_agent in identified),
            calls=calls,
            waiting_text=(
                "Waiting for approval: " + ", ".join(f"`{call.tool_name}`" for call in gated_calls)
                if gated_calls
                else None
            ),
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
        manager = approval_manager.get_approval_store()
        approver = resolve_tool_approval_approver(config, self.runtime_paths, continuation.requester_id)
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
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                arguments=deepcopy(dict(tool.tool_args or {})),
                room_id=target.room_id,
                requester_id=continuation.requester_id,
                approver_user_id=approver,
                expires_at_ns=call.expires_at_ns,
                agent_name=call.invoking_agent,
                thread_id=target.resolved_thread_id,
            )
            if card is None:
                raise RuntimeError(failure_reason)
            cards.append(card)
        if cards:
            assert manager is not None
            if not await manager.reserve_and_publish(
                continuation_principal_id=self.store.principal_id,
                continuation_id=continuation.approval_id,
                continuation_generation=continuation.generation,
                cards=tuple(cards),
            ):
                raise RuntimeError(failure_reason)
        elif (
            await self.store.activate_approval_continuation(
                continuation.approval_id,
                expected_generation=continuation.generation,
            )
            is None
        ):
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
            refreshed = await self.store.approval_continuation(continuation.approval_id)
            if refreshed is None:
                raise RuntimeError(failure_reason)
            continuation = refreshed
        if continuation.state == "ready":
            self.retry_sources(continuation.source_event_ids)

    async def advance_pause(
        self,
        current: ApprovalContinuation,
        paused: PausedAttempt,
        *,
        target: MessageTarget,
        pending_text: str,
    ) -> _ApprovalPausePresentation:
        """Replace one claim with Agno's next exact pause generation."""
        identified = identify_approval_tools(paused, default_agent_name=current.entity_name)
        plan = await self.plan_pause(identified, requester_id=current.requester_id)
        durable_tool_trace = durable_pause_tool_trace(
            paused,
            identified,
            entity_kind=current.entity_kind,
            coordinator_name=current.entity_name,
        )
        approval_pending = plan.waiting_text is not None
        visible_tool_trace = tuple(paused.tool_trace) if current.show_tool_calls else ()
        delivery_text = _approval_pause_delivery_text(paused, plan.waiting_text or pending_text)
        stream_status = STREAM_STATUS_APPROVAL_PENDING if approval_pending else STREAM_STATUS_PENDING
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
        failure_reason = "Chained approval publication failed"
        try:
            edit_outcome = _require_text_delivery(
                await self.delivery_gateway.edit_text_outcome(
                    EditTextRequest(
                        target=target,
                        event_id=current.response_event_id,
                        new_text=delivery_text,
                        extra_content={STREAM_STATUS_KEY: stream_status},
                        tool_trace=list(visible_tool_trace) or None,
                    ),
                ),
                failure_reason,
            )
            committed = _require_continuation(
                await self.store.commit_approval_continuation_presentation(
                    current.approval_id,
                    expected_generation=publishing.generation,
                    response_text=paused.response_text,
                    visible_response_text=edit_outcome.visible_body,
                    response_tool_trace=serialize_tool_trace(durable_tool_trace, include_internal=True),
                    response_presentation_state=paused.response_presentation_state,
                ),
                failure_reason,
            )
            await self.publish_generation(
                committed,
                plan,
                target=target,
                failure_reason=failure_reason,
            )
        except (asyncio.CancelledError, Exception):
            await self.request_failure(publishing, failure_reason)
            raise
        return _ApprovalPausePresentation(
            response_text=edit_outcome.visible_body,
            tool_trace=visible_tool_trace,
            approval_pending=approval_pending,
        )

    async def publish_initial_pause(
        self,
        paused: PausedAttempt,
        plan: _ApprovalPausePlan,
        *,
        target: MessageTarget,
        existing_event_id: str | None,
        source_event_id: str,
        show_tool_calls: bool,
        pending_text: str,
    ) -> _ApprovalPauseDelivery:
        """Publish and acknowledge the exact visible body for a continuation's first generation."""
        approval_pending = plan.waiting_text is not None
        delivery_text = _approval_pause_delivery_text(paused, plan.waiting_text or pending_text)
        tool_trace = list(paused.tool_trace) if show_tool_calls and paused.tool_trace else None
        stream_status = STREAM_STATUS_APPROVAL_PENDING if approval_pending else STREAM_STATUS_PENDING
        extra_content = {STREAM_STATUS_KEY: stream_status}
        failure_reason = "Could not publish the suspended approval response"
        if existing_event_id is None:
            outcome = _require_text_delivery(
                await self.delivery_gateway.send_text_outcome(
                    SendTextRequest(
                        target=target,
                        response_text=delivery_text,
                        extra_content=extra_content,
                        tool_trace=tool_trace,
                        delivery_turn_id=source_event_id,
                        delivery_stage=DeliveryStage.INITIAL,
                    ),
                ),
                failure_reason,
            )
            return _ApprovalPauseDelivery(outcome.event_id, outcome.visible_body, "sent")
        if approval_pending or bool(paused.response_text):
            outcome = _require_text_delivery(
                await self.delivery_gateway.edit_text_outcome(
                    EditTextRequest(
                        target=target,
                        event_id=existing_event_id,
                        new_text=delivery_text,
                        extra_content=extra_content,
                        tool_trace=tool_trace,
                    ),
                ),
                failure_reason,
            )
            return _ApprovalPauseDelivery(existing_event_id, outcome.visible_body, "edited")
        return _ApprovalPauseDelivery(existing_event_id, delivery_text, None)

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
        if await self.successful_final_delivery(current) is not None:
            return False
        if current.state != "failing":
            current = await self.request_failure(current, reason)
            if current is None:
                return False
        manager = approval_manager.get_approval_store()
        if manager is None or not await manager.expire_continuation_cards(current.approval_id):
            return False
        visible_reason = _USER_STOP_VISIBLE_NOTE if reason == _USER_STOP_FAILURE_REASON else reason
        target = continuation_target(current)
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

    async def successful_final_delivery(
        self,
        continuation: ApprovalContinuation,
        *,
        recover: bool = False,
    ) -> MatrixDelivery | None:
        """Return FINAL debt produced by a completed Agno continuation, not failure settlement."""
        delivery = await self.final_delivery(continuation, recover=recover)
        if delivery is None:
            return None
        payload = delivery.payload
        nested = payload.get("m.new_content")
        semantic = (
            next((value for key, value in nested.items() if key == DURABLE_FINAL_OUTCOME_KEY), None)
            if isinstance(nested, dict)
            else payload.get(DURABLE_FINAL_OUTCOME_KEY)
        )
        return delivery if isinstance(semantic, dict) else None

    async def final_delivery(
        self,
        continuation: ApprovalContinuation,
        *,
        recover: bool = False,
    ) -> MatrixDelivery | None:
        """Return the continuation's frozen FINAL, optionally retrying its delivery."""
        outbox = self.delivery_gateway.deps.outbox
        delivery = await outbox.load_matrix_delivery(
            delivery_id=continuation.source_event_ids[0],
            stage=DeliveryStage.FINAL,
        )
        if recover and delivery is not None and delivery.acknowledged_event_id is None:
            await self.delivery_gateway.recover_deliveries()
            delivery = await outbox.load_matrix_delivery(
                delivery_id=continuation.source_event_ids[0],
                stage=DeliveryStage.FINAL,
            )
        return delivery
