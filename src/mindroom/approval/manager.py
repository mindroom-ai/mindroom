"""Approval-domain decisions backed by the shared Matrix delivery outbox."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager, suppress
from contextvars import Context
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from weakref import WeakValueDictionary

from mindroom.approval.events import card_approvable
from mindroom.event_journal import (
    SCHEDULED_APPROVAL_WINDOW_NS,
    TIMEOUT_REASON,
    ApprovalCardReservation,
    ApprovalCardView,
    ApprovalDecisionMetadata,
    BackgroundApprovalDecision,
    CallKey,
    DeliveryStage,
    ScheduledCall,
    ScheduledCallBinding,
    ScheduledCallClaim,
    ScheduledCallOutcome,
    ScheduledCallRefusal,
)
from mindroom.event_journal.matrix_delivery import MatrixDeliveryWorker
from mindroom.logging_config import get_logger
from mindroom.redaction import nests_beyond_redaction_depth, redact_sensitive_data, truncate_review_text
from mindroom.tool_approval_grants import (
    AUTO_APPROVE_OPTIONS,
    EXACT_ARGUMENTS,
    SCHEDULED_SCOPE_OPTIONS,
    ApprovalGrantScope,
    ApprovalOperation,
    approval_timestamp,
    valid_auto_approve_seconds,
)
from mindroom.tool_system.tool_calls import sanitize_failure_text, sanitize_failure_value

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from mindroom.approval.recovery import ApprovalRecovery
    from mindroom.approval.tool_approval import BackgroundScriptToolOrigin
    from mindroom.approval.transport import ApprovalMatrixTransport
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalDeliveryView, RecordedApprovalDecision, ScheduledApprovalArmState

_ApprovalStatus = Literal["approved", "denied", "expired"]
_ResolutionStatus = Literal["approved", "denied"]

_STARTUP_RECOVERY_SCAN_PAGE = 256
_DEADLINE_SWEEP_SECONDS = 60.0
_EVENT_TYPE = "io.mindroom.tool_approval"
DEFAULT_ROUTER_MANAGED_ROOM_REASON = (
    "Tool approval needs the router in this room. If `router.accept_invites` allows this Matrix transport account, "
    "call `invite_router` and wait for the router to join; otherwise enable it or add the router manually, then retry."
)
# What an approval request or settlement meets before the approval runtime and the router's card store are bound.
RUNTIME_NOT_READY_REASON = "Tool approval runtime is not ready."
_DEFAULT_TRUNCATED_APPROVAL_REASON = (
    "Cannot approve: the tool arguments cannot be shown in full, so a human cannot review "
    "exactly what would run. Retry with a smaller or simpler payload — for example save large content to a "
    "workspace file via `mindroom_output_path` or send it as a file attachment with a short message "
    "body — or auto-approve this tool via a script-based approval rule."
)
_MAX_ARGUMENTS_PREVIEW_CHARS = 1200
_MAX_FULL_ARGUMENTS_JSON_BYTES = 2_000_000
_MANAGER: ApprovalManager | None = None
logger = get_logger(__name__)


def _approval_action_authorized(card: ApprovalCardView, authorize_responder: Callable[[str], bool]) -> bool:
    """Authorize an actionable card against its trusted originating responder."""
    entity_name = card.responder_entity_name if card.owner_kind == "reply" else card.invoking_agent
    return entity_name is not None and authorize_responder(entity_name)


class ToolApprovalTransportError(RuntimeError):
    """One actionable reason an approval card cannot be transported or verified."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnverifiableApprovalCardError(ToolApprovalTransportError):
    """A legacy approval action target cannot be authenticated by Matrix."""


def _scheduled_call_target(task_id: str, execute_at_ns: int) -> dict[str, object]:
    """Name the scheduled task a scheduling card or its receipt belongs to."""
    return {
        "approval_target": "scheduled_call",
        "scheduled_task_id": task_id,
        "scheduled_for": approval_timestamp(execute_at_ns),
    }


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _now_ns() -> int:
    return int(_utcnow().timestamp() * 1_000_000_000)


def _compact_preview_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _json_preview_length(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _truncate_event_argument_value(value: object, *, max_length: int) -> object:
    # The value is already redacted; redacting its JSON text again could hide text the first pass kept.
    if _json_preview_length(value) <= max_length:
        return value
    return truncate_review_text(_compact_preview_text(value), max_length)


def _build_event_arguments_preview(arguments: dict[str, Any], placeholders: dict[str, str]) -> dict[str, Any]:
    sanitized = sanitize_failure_value(arguments, token_placeholders=placeholders)
    if not isinstance(sanitized, dict):
        return {"value": _truncate_event_argument_value(sanitized, max_length=_MAX_ARGUMENTS_PREVIEW_CHARS // 2)}
    if _json_preview_length(sanitized) <= _MAX_ARGUMENTS_PREVIEW_CHARS:
        return sanitized
    per_value_budget = max(24, _MAX_ARGUMENTS_PREVIEW_CHARS // max(len(sanitized), 1))
    preview = {
        key: _truncate_event_argument_value(value, max_length=per_value_budget) for key, value in sanitized.items()
    }
    while _json_preview_length(preview) > _MAX_ARGUMENTS_PREVIEW_CHARS and preview:
        drop_key = max(preview, key=lambda key: len(_compact_preview_text(preview[key])))
        preview.pop(drop_key)
    if not preview:
        return {
            "_summary": sanitize_failure_text(
                f"{len(sanitized)} arguments omitted because the preview exceeded the size limit.",
                max_length=max(24, _MAX_ARGUMENTS_PREVIEW_CHARS // 2),
            ),
        }
    return preview


def _raw_arguments_json_bytes(arguments: dict[str, Any]) -> int:
    # Raw tool arguments may carry lone surrogates or values JSON cannot encode; only their size matters here.
    return len(json.dumps(arguments, ensure_ascii=False, default=repr).encode("utf-8", "surrogatepass"))


def _build_full_event_arguments(arguments: dict[str, Any], placeholders: dict[str, str]) -> dict[str, Any] | None:
    """Return the complete redacted arguments, or ``None`` when a reviewer could not see all of them.

    The size cap applies to the raw arguments, so redaction itself never makes a card unapprovable.
    """
    if nests_beyond_redaction_depth(arguments) or _raw_arguments_json_bytes(arguments) > _MAX_FULL_ARGUMENTS_JSON_BYTES:
        return None
    return cast("dict[str, Any]", redact_sensitive_data(arguments, token_placeholders=placeholders))


@dataclass(frozen=True, slots=True)
class _EventArguments:
    """Redacted tool arguments for one approval card."""

    preview: dict[str, Any]
    full: dict[str, Any] | None
    """The complete redacted arguments, or ``None`` when a reviewer could not see all of them."""

    @property
    def truncated(self) -> bool:
        """Whether the preview shows less than the complete redacted arguments."""
        return self.preview != self.full


def _build_event_arguments(arguments: dict[str, Any]) -> _EventArguments:
    # One placeholder mapping per card, so a token has the same number in the preview and the full copy.
    placeholders: dict[str, str] = {}
    full = _build_full_event_arguments(arguments, placeholders)
    return _EventArguments(preview=_build_event_arguments_preview(arguments, placeholders), full=full)


@dataclass(frozen=True, slots=True)
class _ApprovalStartupSweep:
    """What one generic recovery pass settled and still owes."""

    discarded: int
    failed: int
    scanned: int = field(default=0, compare=False)

    @property
    def complete(self) -> bool:
        """Return whether the pass left no delivery debt."""
        return self.failed == 0


@dataclass(frozen=True, slots=True)
class ApprovalActionResult:
    """One approval-action outcome parsed from a Matrix control event."""

    consumed: bool
    error_reason: str | None = None
    thread_id: str | None = None
    card_event_id: str | None = None


@dataclass
class ApprovalManager:
    """Own approval semantics while the generic worker owns Matrix delivery."""

    runtime_paths: RuntimePaths
    # Prepares, sends, and verifies cards through the router, and wakes the replies their decisions unblock.
    transport: ApprovalMatrixTransport
    cards: ApprovalDeliveryView | None = None
    recovery: ApprovalRecovery | None = None
    _resolving_card_event_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _live_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _deadline_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _deadline_wakeup: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    # Shared by every worker this manager builds, so the deadline sweep and another pass never send one delivery at once.
    _delivery_locks: WeakValueDictionary[str, asyncio.Lock] = field(
        default_factory=WeakValueDictionary,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Attach recovery to its owning runtime after construction."""
        if self.recovery is not None:
            self.recovery.bind(self)

    async def prepare_detached_approval(
        self,
        *,
        approval_id: str,
        continuation_id: str,
        continuation_generation: int,
        entity_name: str,
        response_event_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        requester_id: str,
        approver_user_id: str,
        expires_at_ns: int,
        agent_name: str,
        thread_id: str | None,
        grant_operation: ApprovalOperation | None,
    ) -> ApprovalCardReservation | None:
        """Prepare one exact frozen payload without creating delivery debt."""
        return await self._prepare_approval_card(
            approval_id=approval_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            raw_arguments=arguments,
            agent_name=agent_name,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            approver_user_id=approver_user_id,
            expires_at_ns=expires_at_ns,
            grant_operation=grant_operation,
            target_fields={
                "continuation_id": continuation_id,
                "continuation_generation": continuation_generation,
                "tool_call_id": tool_call_id,
                "response_event_id": response_event_id,
                **(
                    {"approval_scope": grant_operation.scope_wire("0" * 64, entity_name, agent_name or entity_name)}
                    if grant_operation is not None
                    else {}
                ),
            },
        )

    async def request_background_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        owner_kind: Literal["job", "script"],
        room_id: str,
        thread_id: str | None,
        agent_name: str,
        requester_id: str,
        approver_user_id: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: float,
        entity_name: str | None = None,
        grant_operation: ApprovalOperation | None = None,
    ) -> BackgroundApprovalDecision:
        """Publish and await one durable exact-call background approval.

        With a grant operation, the card offers timed approvals scoped like a paused reply's to the conversation
        of ``entity_name``, and an active grant of that scope approves the call without a card.
        """
        cards = self.cards
        if cards is None:
            return BackgroundApprovalDecision(status="denied", reason=RUNTIME_NOT_READY_REASON)
        delivery_id = f"script-approval:{origin.run_id}:{origin.call_id}"
        expires_at_ns = time.time_ns() + max(0, round(timeout_seconds * 1_000_000_000))
        grant_scope = (
            None
            if grant_operation is None or entity_name is None
            else self._grant_scope(
                room_id=room_id,
                thread_id=thread_id,
                requester_id=requester_id,
                entity_name=entity_name,
                invoking_agent=agent_name,
            )
        )
        if grant_scope is None:
            grant_operation = None
        reservation = await self._prepare_approval_card(
            approval_id=delivery_id,
            tool_call_id=origin.call_id,
            tool_name=tool_name,
            raw_arguments=arguments,
            agent_name=agent_name,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            approver_user_id=approver_user_id,
            expires_at_ns=expires_at_ns,
            grant_operation=grant_operation,
            target_fields={
                "approval_target": "background_script",
                "background_run_id": origin.run_id,
                "background_call_id": origin.call_id,
                **(
                    {}
                    if grant_operation is None or grant_scope is None
                    else {"approval_scope": grant_operation.scope_wire("0" * 64, grant_scope.entity_name, agent_name)}
                ),
            },
        )
        if reservation is None:
            return BackgroundApprovalDecision(status="denied", reason="Tool approval card could not be prepared.")
        call = CallKey.detached(origin.run_id, origin.call_id)
        # Registered before the card is reserved, so a decision that commits at any point after it, an active grant's
        # included, ends the wait.
        with cards.approvals.signals.decision_wakeup(call) as decided:
            reserved = await cards.approvals.reserve_detached_card(
                call=call,
                owner_kind=owner_kind,
                tool_name=tool_name,
                toolkit_name=origin.toolkit_name,
                invoking_agent=agent_name,
                room_id=room_id,
                thread_id=thread_id,
                expires_at_ns=expires_at_ns,
                card=reservation,
                grant_scope=grant_scope,
            )
            if not reserved:
                return BackgroundApprovalDecision(
                    status="denied",
                    reason="Approval card could not be published in this room.",
                )
            await self._publish_reserved_cards([delivery_id])
            while True:
                # Cleared before each read, so a decision that commits on either side of the read is seen.
                decided.clear()
                decision = await cards.approvals.detached_decision(call)
                if decision is not None:
                    return decision
                remaining = (expires_at_ns - time.time_ns()) / 1_000_000_000
                if remaining <= 0:
                    expired = await cards.approvals.resolve_approval_call(
                        call,
                        requested_status="expired",
                        reason=TIMEOUT_REASON,
                    )
                    await self._flush_decided(expired)
                    decision = await cards.approvals.detached_decision(call)
                    if decision is not None:
                        return decision
                    return BackgroundApprovalDecision(status="denied", reason=TIMEOUT_REASON)
                with suppress(TimeoutError):
                    async with asyncio.timeout(remaining):
                        await decided.wait()

    def _grant_scope(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        requester_id: str,
        entity_name: str,
        invoking_agent: str,
    ) -> ApprovalGrantScope | None:
        """Bind a detached call's timed approvals to its conversation's responder, or None while that is unknown."""
        responder_principal_id = self.transport.responder_principal_id(entity_name)
        if responder_principal_id is None:
            return None
        return ApprovalGrantScope(
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            entity_name=entity_name,
            responder_principal_id=responder_principal_id,
            invoking_agent=invoking_agent,
        )

    async def request_scheduled_call_approval(
        self,
        binding: ScheduledCallBinding,
        *,
        approver_user_id: str,
        scheduled_for_text: str,
        any_arguments_offered: bool,
    ) -> bool:
        """Publish one card approving the call a scheduled task stores for later."""
        cards = self.cards
        if cards is None:
            return False
        delivery_id = f"scheduled-approval:{binding.task_id}"
        reservation = await self._prepare_approval_card(
            approval_id=delivery_id,
            tool_call_id=binding.task_id,
            tool_name=binding.tool_name,
            raw_arguments=json.loads(binding.arguments_json),
            agent_name=binding.agent_name,
            room_id=binding.room_id,
            thread_id=binding.thread_id,
            requester_id=binding.requester_id,
            approver_user_id=approver_user_id,
            expires_at_ns=binding.execute_at_ns,
            target_fields={
                **_scheduled_call_target(binding.task_id, binding.execute_at_ns),
                "scheduled_window_seconds": SCHEDULED_APPROVAL_WINDOW_NS // 1_000_000_000,
                **({"scheduled_scope_options": list(SCHEDULED_SCOPE_OPTIONS)} if any_arguments_offered else {}),
                "body": f"🔒 Approval required: {binding.tool_name} (scheduled for {scheduled_for_text})",
            },
        )
        if reservation is None or not reservation.approvable:
            return False
        if not await cards.approvals.reserve_scheduled_call_approval(
            binding=binding,
            card=reservation,
            any_arguments_offered=any_arguments_offered,
        ):
            return False
        await self._publish_reserved_cards([delivery_id])
        return True

    async def scheduled_call(self, task_id: str) -> ScheduledCall | None:
        """Read the call one scheduled task stored."""
        return None if self.cards is None else await self.cards.approvals.scheduled_call(task_id=task_id)

    async def claim_scheduled_call(
        self,
        call: ScheduledCall,
        *,
        arguments_json: str,
        approver_user_id: str,
    ) -> ScheduledCallClaim | ScheduledCallRefusal | None:
        """Spend a scheduled approval for the arguments about to run and publish its receipt.

        Returns None when the approval runtime cannot prepare the receipt, which claims nothing.
        """
        cards = self.cards
        if cards is None:
            return None
        delivery_id = f"scheduled-receipt:{call.task_id}"
        receipt = await self._prepare_approval_card(
            approval_id=delivery_id,
            tool_call_id=call.task_id,
            tool_name=call.tool_name,
            raw_arguments=json.loads(arguments_json),
            agent_name=call.agent_name,
            room_id=call.room_id,
            thread_id=call.thread_id,
            requester_id=call.requester_id,
            approver_user_id=approver_user_id,
            expires_at_ns=call.execute_at_ns + SCHEDULED_APPROVAL_WINDOW_NS,
            target_fields=_scheduled_call_target(call.task_id, call.execute_at_ns),
        )
        if receipt is None:
            return None
        claimed = await cards.approvals.claim_scheduled_call(call=call, arguments_json=arguments_json, receipt=receipt)
        if isinstance(claimed, ScheduledCallClaim):
            await self._publish_reserved_cards([delivery_id])
        return claimed

    async def record_scheduled_call_outcome(self, task_id: str, outcome: ScheduledCallOutcome) -> None:
        """Record how one claimed scheduled call ended."""
        if self.cards is not None:
            await self.cards.approvals.record_scheduled_call_outcome(task_id=task_id, outcome=outcome)

    async def arm_scheduled_call_approval(
        self,
        task_id: str,
        workflow_digest: str,
        *,
        any_arguments_allowed: bool,
    ) -> ScheduledApprovalArmState:
        """Arm one approved scheduled call as its unchanged task fires."""
        if self.cards is None:
            return "none"
        return await self.cards.approvals.arm_scheduled_call_approval(
            task_id=task_id,
            workflow_digest=workflow_digest,
            any_arguments_allowed=any_arguments_allowed,
        )

    async def withdraw_scheduled_call_approval(self, task_id: str, *, reason: str) -> None:
        """Withdraw a cancelled task's approval and deny its card if it is still pending, or raise while it cannot."""
        if self.cards is None:
            raise RuntimeError(RUNTIME_NOT_READY_REASON)
        await self._flush_decided(
            await self.cards.approvals.withdraw_scheduled_call_approval(task_id=task_id, reason=reason),
        )

    async def _prepare_approval_card(
        self,
        *,
        approval_id: str,
        tool_call_id: str,
        tool_name: str,
        raw_arguments: dict[str, Any],
        agent_name: str | None,
        room_id: str,
        thread_id: str | None,
        requester_id: str,
        approver_user_id: str,
        expires_at_ns: int,
        target_fields: dict[str, object],
        grant_operation: ApprovalOperation | None = None,
    ) -> ApprovalCardReservation | None:
        """Prepare one shared pending-card payload for a typed exact-call target."""
        event_arguments = await asyncio.to_thread(_build_event_arguments, raw_arguments)
        content = self._pending_event_content(
            approval_id=approval_id,
            tool_name=tool_name,
            arguments=event_arguments.preview,
            arguments_truncated=event_arguments.truncated,
            full_arguments=event_arguments.full if event_arguments.truncated else None,
            agent_name=agent_name,
            thread_id=thread_id,
            requester_id=requester_id,
            approver_user_id=approver_user_id,
            requested_at=_utcnow(),
            expires_at=datetime.fromtimestamp(expires_at_ns / 1_000_000_000, tz=UTC),
        )
        if (
            grant_operation is not None
            and thread_id
            and agent_name
            and requester_id == approver_user_id
            and content.get("approvable", True)
        ):
            content["auto_approve_options"] = list(AUTO_APPROVE_OPTIONS)
        else:
            grant_operation = None
        content.update(target_fields)
        if grant_operation is None:
            content.pop("approval_scope", None)
        prepared = await self.transport.prepare_approval_event(room_id, thread_id, content)
        if prepared is None:
            return None
        if prepared.get("approvable", True) is not True:
            grant_operation = None
            prepared.pop("auto_approve_options", None)
            prepared.pop("approval_scope", None)
        return ApprovalCardReservation(
            delivery_id=approval_id,
            tool_call_id=tool_call_id,
            event_type=_EVENT_TYPE,
            payload=prepared,
            approver_user_id=approver_user_id,
            approvable=card_approvable(prepared),
            grant_operation=grant_operation,
        )

    async def settle_background_approval(
        self,
        origin: BackgroundScriptToolOrigin,
        *,
        reason: str,
    ) -> bool:
        """Deny one cancelled exact call and drive its shared terminal delivery."""
        if self.cards is None:
            return False
        recorded = await self.cards.approvals.resolve_approval_call(
            CallKey.detached(origin.run_id, origin.call_id),
            requested_status="denied",
            reason=reason,
        )
        if recorded.resolution is None:
            return False
        await self._flush_decided(recorded)
        return recorded.recorded

    async def settle_pending_background_approvals(self, run_id: str, *, reason: str) -> int:
        """Deny only the still-pending approval targets owned by one run, and deliver their cards' terminal edits."""
        if self.cards is None:
            return 0
        decided = await self.cards.approvals.deny_pending_detached_calls(owner_id=run_id, reason=reason)
        for recorded in decided:
            await self._flush_decided(recorded)
        return len(decided)

    async def prune_background_approvals(self, run_id: str) -> bool:
        """Prune settled background targets only after terminal card retirement."""
        if self.cards is None:
            return False
        return await self.cards.approvals.prune_detached_calls(owner_id=run_id)

    async def reserve_and_publish(
        self,
        *,
        continuation_principal_id: str,
        continuation_id: str,
        continuation_generation: int,
        cards: tuple[ApprovalCardReservation, ...],
    ) -> bool:
        """Reserve a complete generation atomically, then best-effort flush its cards."""
        if self.cards is None:
            return False
        reserved = await self.cards.approvals.reserve_reply_cards(
            continuation_principal_id=continuation_principal_id,
            continuation_id=continuation_id,
            expected_generation=continuation_generation,
            cards=cards,
        )
        if not reserved:
            return False
        await self._publish_reserved_cards([card.delivery_id for card in cards])
        return True

    async def _publish_reserved_cards(self, delivery_ids: list[str]) -> None:
        """Send reserved cards now, leaving any failed send to the delivery worker's retries."""
        worker = self._worker()
        for delivery_id in delivery_ids:
            try:
                await worker.flush(delivery_id=delivery_id, stage=DeliveryStage.INITIAL)
            except Exception:
                logger.warning(
                    "approval_card_initial_delivery_deferred",
                    delivery_id=delivery_id,
                    exc_info=True,
                )
        self._ensure_deadline_sweep()

    def _worker(self) -> MatrixDeliveryWorker:
        if self.cards is None:
            raise ToolApprovalTransportError(RUNTIME_NOT_READY_REASON)
        return MatrixDeliveryWorker(
            store=self.cards,
            send=self.transport.send_approval_delivery,
            event_type=_EVENT_TYPE,
            resend_after_reconciliation_miss=lambda delivery: (
                delivery.payload.get("status") == "approved" and delivery.payload.get("approvable") is False
            ),
            sending_device_id=self.transport.transport_device_id(),
            resolve_delivered=self.transport.resolve_approval_delivery,
            delivery_locks=self._delivery_locks,
        )

    async def handle_card_response(  # noqa: C901, PLR0911, PLR0912 - transport recovery and domain terminal states meet here
        self,
        *,
        room_id: str,
        sender_id: str,
        card_event_id: str,
        status: _ResolutionStatus,
        reason: str | None,
        authorize_responder: Callable[[str], bool],
        before_consume: Callable[[], Awaitable[None]] | None,
        auto_approve_seconds: int | None,
        current_binding: str | None,
        scheduled_scope: str | None,
    ) -> ApprovalActionResult:
        """Atomically choose the exact-call winner and enqueue its terminal edit."""
        if self.has_active_in_memory_approval_card(card_event_id):
            if before_consume is not None:
                await before_consume()
            return ApprovalActionResult(consumed=True, card_event_id=card_event_id)
        cards = self.cards
        if cards is None:
            return ApprovalActionResult(consumed=False, card_event_id=card_event_id)
        card = await cards.approvals.approval_card(room_id=room_id, card_event_id=card_event_id)
        if card is None:
            try:
                card = await self._bind_action_delivery(cards, room_id=room_id, card_event_id=card_event_id)
            except UnverifiableApprovalCardError as exc:
                if before_consume is not None:
                    await before_consume()
                logger.warning(
                    "unverifiable_legacy_approval_action_ignored",
                    room_id=room_id,
                    card_event_id=card_event_id,
                    transport_reason=exc.reason,
                )
                return ApprovalActionResult(consumed=True, card_event_id=card_event_id)
        if card is None:
            return ApprovalActionResult(consumed=False, card_event_id=card_event_id)
        if card == "terminal" or card.terminal:
            if before_consume is not None:
                await before_consume()
            return ApprovalActionResult(consumed=True, card_event_id=card_event_id)
        if not card.current:
            return ApprovalActionResult(consumed=False, card_event_id=card_event_id)
        if card.decided:
            if before_consume is not None:
                await before_consume()
            self._ensure_deadline_sweep()
            return ApprovalActionResult(consumed=True, card_event_id=card_event_id)
        if (
            not self._transport_ready()
            or card.approver_user_id != sender_id
            or not _approval_action_authorized(card, authorize_responder)
            or (
                auto_approve_seconds is not None
                and (
                    status != "approved"
                    or not valid_auto_approve_seconds(auto_approve_seconds)
                    or not card.offers_timed_approval
                    or auto_approve_seconds not in AUTO_APPROVE_OPTIONS
                    or not card.approvable
                )
            )
            or (
                scheduled_scope is not None
                and (
                    status != "approved"
                    or not card.any_arguments_offered
                    or scheduled_scope not in SCHEDULED_SCOPE_OPTIONS
                )
            )
        ):
            return ApprovalActionResult(consumed=False, card_event_id=card_event_id)
        if before_consume is not None:
            await before_consume()
        resolved_status, resolved_reason, resolution_was_truncated = self._normalized_resolution_request(
            card,
            status=status,
            reason=reason,
        )
        if auto_approve_seconds is not None:
            return await self._approve_with_grant(
                card,
                room_id=room_id,
                card_event_id=card_event_id,
                sender_id=sender_id,
                seconds=auto_approve_seconds,
                status=resolved_status,
                reason=reason,
                current_binding=current_binding,
            )
        with self._claimed_resolution(card_event_id):
            await self._record_and_flush_resolution(
                card,
                status=resolved_status,
                reason=resolved_reason,
                resolved_by=sender_id if resolved_status == status else None,
                scheduled_scope=(scheduled_scope or EXACT_ARGUMENTS)
                if card.owner_kind == "scheduled" and resolved_status == "approved"
                else None,
            )
        return ApprovalActionResult(
            consumed=True,
            error_reason=_DEFAULT_TRUNCATED_APPROVAL_REASON if resolution_was_truncated else None,
            thread_id=card.thread_id,
            card_event_id=card_event_id,
        )

    async def _approve_with_grant(
        self,
        card: ApprovalCardView,
        *,
        room_id: str,
        card_event_id: str,
        sender_id: str,
        seconds: int,
        status: _ApprovalStatus,
        reason: str | None,
        current_binding: str | None,
    ) -> ApprovalActionResult:
        """Commit the grant batch and wake every newly executable continuation."""
        if status != "approved" or self.cards is None:
            return ApprovalActionResult(consumed=True, card_event_id=card_event_id)
        decisions = await self.cards.approvals.create_approval_grant(
            room_id=room_id,
            card_event_id=card_event_id,
            sender_id=sender_id,
            seconds=seconds,
            metadata=ApprovalDecisionMetadata(resolved_by=sender_id, resolved_at=_utcnow().isoformat()),
            reason=reason,
            current_binding=current_binding,
        )
        for decision in decisions:
            if decision.recorded and decision.continuation_ready:
                await self._wake_continuation(decision)
        for decision in decisions:
            if decision.delivery_id is not None:
                await self._flush_card_delivery(decision.delivery_id, decision.card_event_id, room_id=room_id)
        self._ensure_deadline_sweep()
        return ApprovalActionResult(
            consumed=True,
            thread_id=card.thread_id,
            card_event_id=card_event_id,
        )

    async def handle_grant_revocation(
        self,
        *,
        room_id: str,
        sender_id: str,
        card_event_id: str,
        grant_id: str,
        authorize_responder: Callable[[str], bool],
        before_consume: Callable[[], Awaitable[None]] | None,
    ) -> ApprovalActionResult:
        """Authenticate a retained grant and deliver its durable revocation edit."""
        if self.cards is None:
            return ApprovalActionResult(consumed=False)
        grant = await self.cards.approvals.approval_grant_for_card(room_id=room_id, card_event_id=card_event_id)
        if (
            grant is None
            or grant.grant_id != grant_id
            or grant.requester_id != sender_id
            or not authorize_responder(grant.entity_name)
        ):
            return ApprovalActionResult(consumed=False)
        if before_consume is not None:
            await before_consume()
        revocation = await self.cards.approvals.revoke_approval_grant(
            room_id=room_id,
            card_event_id=card_event_id,
            sender_id=sender_id,
            grant_id=grant_id,
        )
        if revocation is None:
            return ApprovalActionResult(consumed=True)
        await self._flush_card_delivery(revocation.original_delivery_id, card_event_id, room_id=room_id)
        await self._maintain_automatic_approvals(grant_id=grant_id)
        self._ensure_deadline_sweep()
        return ApprovalActionResult(
            consumed=True,
            card_event_id=card_event_id,
            thread_id=grant.thread_id,
        )

    async def _bind_action_delivery(
        self,
        cards: ApprovalDeliveryView,
        *,
        room_id: str,
        card_event_id: str,
    ) -> ApprovalCardView | Literal["terminal"] | None:
        """Bind the exact visible action target to generic delivery debt."""
        delivery_id = await self.transport.resolve_approval_action_delivery(room_id, card_event_id)
        if delivery_id is None:
            return None
        delivery = await cards.load_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.INITIAL,
        )
        if delivery is not None and (
            delivery.room_id != room_id
            or delivery.event_type != _EVENT_TYPE
            or delivery.payload.get("approval_id") != delivery_id
        ):
            return None
        if delivery is not None and delivery.acknowledged_event_id in {None, card_event_id}:
            acknowledgement = await cards.acknowledge_matrix_delivery(
                delivery_id=delivery_id,
                stage=DeliveryStage.INITIAL,
                event_id=card_event_id,
                delivered_projections=(),
            )
            if acknowledgement.settled_event_id == card_event_id:
                return await cards.approvals.approval_card(room_id=room_id, card_event_id=card_event_id)
        await cards.approvals.remember_terminal_approval_alias(
            room_id=room_id,
            card_event_id=card_event_id,
            delivery_id=delivery_id,
        )
        return await cards.approvals.approval_card(room_id=room_id, card_event_id=card_event_id)

    async def _record_and_flush_resolution(
        self,
        card: ApprovalCardView,
        *,
        status: _ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
        scheduled_scope: str | None = None,
    ) -> bool:
        if self.cards is None:
            return False
        recorded = await self.cards.approvals.resolve_approval_call(
            card.call,
            requested_status=status,
            reason=reason,
            metadata=ApprovalDecisionMetadata(
                resolved_by=resolved_by,
                resolved_at=_utcnow().isoformat(),
                scheduled_scope=scheduled_scope,
            ),
        )
        if recorded.resolution is None:
            return False
        if recorded.recorded and recorded.continuation_ready:
            await self._wake_continuation(recorded)
        return await self._flush_card_delivery(card.delivery_id, recorded.card_event_id, room_id=card.room_id)

    async def _flush_decided(self, recorded: RecordedApprovalDecision) -> None:
        """Deliver the terminal edit of the card that shows one decided call; the deadline sweep retries what fails."""
        if recorded.delivery_id is not None:
            await self._flush_card_delivery(recorded.delivery_id, recorded.card_event_id)

    async def _flush_card_delivery(
        self,
        delivery_id: str,
        card_event_id: str | None,
        *,
        room_id: str | None = None,
    ) -> bool:
        """Deliver one committed card decision, preserving INITIAL before FINAL."""
        assert self.cards is not None
        try:
            if card_event_id is None:
                card_event_id = await self._worker().flush(delivery_id=delivery_id, stage=DeliveryStage.INITIAL)
                if card_event_id is None:
                    return False
            edit_event_id = await self._worker().flush(delivery_id=delivery_id, stage=DeliveryStage.FINAL)
        except Exception:
            logger.warning(
                "approval_terminal_delivery_deferred",
                delivery_id=delivery_id,
                room_id=room_id,
                exc_info=True,
            )
            return False
        return edit_event_id is not None and await self.cards.approvals.retire_approval_card(delivery_id=delivery_id)

    async def expire_continuation_cards(self, continuation_id: str, reason: str) -> bool:
        """Enqueue and flush expiry for every card owned by one failed continuation, with its failure's reason."""
        if self.cards is None:
            return False
        complete = True
        for room_id in await self.cards.approvals.pending_approval_room_ids():
            async for card in self._pending_cards(self.cards, room_id):
                if card.owner_kind == "reply" and card.call.owner_id == continuation_id:
                    complete = await self._expire_stored(card, reason=reason) and complete
        return complete

    @staticmethod
    async def _pending_cards(cards: ApprovalDeliveryView, room_id: str) -> AsyncIterator[ApprovalCardView]:
        """Yield one room's live request cards oldest first, one page at a time."""
        cursor: tuple[int, str] | None = None
        while True:
            page = await cards.approvals.pending_approval_cards(
                room_id=room_id,
                limit=_STARTUP_RECOVERY_SCAN_PAGE,
                after=cursor,
            )
            if not page:
                return
            cursor = (page[-1].created_at_ns, page[-1].delivery_id)
            for card in page:
                yield card
            if len(page) < _STARTUP_RECOVERY_SCAN_PAGE:
                return

    async def pending_card_event_ids(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        approver_user_id: str,
    ) -> tuple[str, ...]:
        """Return the unexpired, unanswered, approvable cards one approver has in one conversation, scheduled calls aside."""
        if self.cards is None:
            return ()
        now_ns = _now_ns()
        # A card past its deadline expires when answered, until the deadline sweep retires it.
        return tuple(
            [
                card.card_event_id
                async for card in self._pending_cards(self.cards, room_id)
                if card.card_event_id is not None
                and not card.decided
                and card.owner_kind not in {None, "scheduled"}
                and card.thread_id == thread_id
                and card.approver_user_id == approver_user_id
                and card.approvable
                and card.expires_at_ns is not None
                and card.expires_at_ns > now_ns
            ],
        )

    async def _expire_stored(self, card: ApprovalCardView, *, reason: str = TIMEOUT_REASON) -> bool:
        if self.cards is None or (card.card_event_id is None and card.decided):
            return False
        if card.card_event_id is None:
            recorded = await self.cards.approvals.resolve_approval_call(
                card.call,
                requested_status="expired",
                reason=reason,
            )
            if recorded.recorded and recorded.continuation_ready:
                await self._wake_continuation(recorded)
            return False
        if not self._transport_ready():
            return False
        if card.decided:
            return await self._flush_card_delivery(card.delivery_id, card.card_event_id, room_id=card.room_id)
        return await self._record_and_flush_resolution(
            card,
            status="expired",
            reason=reason,
            resolved_by=None,
        )

    async def _wake_continuation(self, recorded: RecordedApprovalDecision) -> None:
        if recorded.continuation_entity_name is None or recorded.continuation_room_id is None:
            return
        await self.transport.wake_continuation_sources(
            recorded.continuation_entity_name,
            recorded.continuation_room_id,
            recorded.source_event_ids,
        )

    async def _settle_startup_card(self, card: ApprovalCardView) -> bool | None:
        """Settle due recovery work, or return None when none is currently due."""
        if card.decided or card.expires_at_ns is None or card.expires_at_ns <= _now_ns():
            return await self._expire_stored(card)
        return None

    async def _maintain_automatic_approvals(self, *, grant_id: str | None = None) -> set[tuple[str, DeliveryStage]]:
        """Retire spent payloads and flush newly unblocked acknowledgements."""
        assert self.cards is not None
        failed: set[tuple[str, DeliveryStage]] = set()
        for delivery_id in await self.cards.approvals.maintain_automatic_approvals(grant_id=grant_id):
            try:
                acknowledged = await self._worker().flush(delivery_id=delivery_id, stage=DeliveryStage.FINAL)
            except Exception:
                logger.warning("approval_grant_revocation_delivery_deferred", delivery_id=delivery_id, exc_info=True)
                acknowledged = None
            if acknowledged is None:
                failed.add((delivery_id, DeliveryStage.FINAL))
        return failed

    async def recover_cards_on_startup(self) -> _ApprovalStartupSweep:
        """Run generic delivery recovery, deadline decisions, and domain retirement."""
        if self.cards is None:
            return _ApprovalStartupSweep(discarded=0, failed=1)
        outcome = await self._worker().recover()
        transport_failures = set(outcome.failed_deliveries)
        failed = outcome.failed - len(transport_failures)
        transport_failures.update(await self._maintain_automatic_approvals())
        scanned = 0
        retired = 0
        for room_id in await self.cards.approvals.pending_approval_room_ids():
            async for card in self._pending_cards(self.cards, room_id):
                scanned += 1
                settled = await self._settle_startup_card(card)
                had_transport_failure = any(
                    delivery_id == card.delivery_id for delivery_id, _stage in transport_failures
                )
                if settled is True:
                    transport_failures = {
                        delivery for delivery in transport_failures if delivery[0] != card.delivery_id
                    }
                    retired += 1
                elif settled is False and not had_transport_failure:
                    failed += 1
        self._ensure_deadline_sweep()
        return _ApprovalStartupSweep(
            discarded=retired,
            failed=failed + len(transport_failures),
            scanned=scanned,
        )

    def _ensure_deadline_sweep(self) -> None:
        if self._deadline_task is None or self._deadline_task.done():
            # A turn's approval request can start the never-ending sweep, which must not keep that turn's Agent.
            self._deadline_task = asyncio.create_task(
                self._run_deadline_sweep(),
                name="approval_deadline_sweep",
                context=Context(),
            )
        self._deadline_wakeup.set()

    async def _run_deadline_sweep(self) -> None:
        while True:
            self._deadline_wakeup.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._deadline_wakeup.wait(), timeout=_DEADLINE_SWEEP_SECONDS)
            try:
                await self.recover_cards_on_startup()
            except Exception:
                logger.warning("approval_deadline_sweep_failed", exc_info=True)

    async def shutdown(self) -> None:
        """Stop the domain deadline scanner; durable delivery debt remains in the outbox."""
        if self.recovery is not None:
            await self.recovery.close()
        task = self._deadline_task
        self._deadline_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _has_live_work(self) -> bool:
        """Report decisions currently committing in this process."""
        with self._live_lock:
            return bool(self._resolving_card_event_ids)

    def has_active_in_memory_approval_card(self, card_event_id: str) -> bool:
        """Prevent a concurrent callback from consuming an active decision."""
        with self._live_lock:
            return card_event_id in self._resolving_card_event_ids

    @contextmanager
    def _claimed_resolution(self, card_event_id: str) -> Iterator[None]:
        with self._live_lock:
            self._resolving_card_event_ids.add(card_event_id)
        try:
            yield
        finally:
            with self._live_lock:
                self._resolving_card_event_ids.discard(card_event_id)

    def _transport_ready(self) -> bool:
        """Return whether this process's transport account is known, without which no card is acted on."""
        return self.transport.transport_sender_id() is not None

    @staticmethod
    def _pending_event_content(
        *,
        approval_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        arguments_truncated: bool,
        agent_name: str | None,
        thread_id: str | None,
        requester_id: str,
        approver_user_id: str,
        requested_at: datetime,
        expires_at: datetime,
        full_arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content: dict[str, Any] = {
            "msgtype": _EVENT_TYPE,
            "body": f"🔒 Approval required: {tool_name}",
            "tool_name": tool_name,
            "arguments": arguments,
            "status": "pending",
            "approval_id": approval_id,
            "approver_user_id": approver_user_id,
            "requester_id": requester_id,
            "requested_at": requested_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "thread_id": thread_id,
        }
        if agent_name is not None:
            content["agent_name"] = agent_name
        if arguments_truncated:
            content["arguments_truncated"] = True
            if full_arguments is None:
                content["approvable"] = False
            else:
                content["full_arguments"] = full_arguments
        return content

    @staticmethod
    def _normalized_resolution_request(
        card: ApprovalCardView,
        *,
        status: _ResolutionStatus,
        reason: str | None,
    ) -> tuple[_ApprovalStatus, str | None, bool]:
        """Deny an approval no reviewer could see in full; the journal applies the deadline when it decides."""
        if status == "approved" and not card.approvable:
            return "denied", _DEFAULT_TRUNCATED_APPROVAL_REASON, True
        return status, reason, False


def get_approval_store() -> ApprovalManager | None:
    """Return the configured approval domain, if the runtime is ready."""
    return _MANAGER


def initialize_approval_store(
    runtime_paths: RuntimePaths,
    *,
    transport: ApprovalMatrixTransport,
    cards: ApprovalDeliveryView | None,
    recovery: ApprovalRecovery | None,
) -> ApprovalManager:
    """Initialize the module-level approval domain for one runtime context, or rebind it after a reload.

    A rebind keeps the bound card store and recovery when it is given none, as before the router bot is built.
    """
    global _MANAGER
    if _MANAGER is not None and _MANAGER.runtime_paths.storage_root == runtime_paths.storage_root:
        _MANAGER.transport = transport
        if cards is not None:
            _MANAGER.cards = cards
        if recovery is not None:
            _MANAGER.recovery = recovery
            recovery.bind(_MANAGER)
        return _MANAGER
    if _MANAGER is not None and _MANAGER._has_live_work():
        msg = "Cannot reinitialize approval store while a decision is committing"
        raise RuntimeError(msg)
    _MANAGER = ApprovalManager(runtime_paths, transport=transport, cards=cards, recovery=recovery)
    return _MANAGER


async def shutdown_approval_manager() -> None:
    """Stop deadline scanning and release the process-local domain facade."""
    global _MANAGER
    manager = _MANAGER
    if manager is not None:
        await manager.shutdown()
        _MANAGER = None
