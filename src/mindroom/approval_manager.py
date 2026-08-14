"""Approval-domain decisions backed by the shared Matrix delivery outbox."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.approval_events import PendingApproval, PendingApprovalStatus, parse_approval_datetime
from mindroom.event_journal import ApprovalCardReservation, DeliveryStage, MatrixDelivery, StoredApprovalCard
from mindroom.logging_config import get_logger
from mindroom.matrix_delivery import MatrixDeliveryWorker
from mindroom.redaction import redact_sensitive_data
from mindroom.tool_system.tool_calls import sanitize_failure_text, sanitize_failure_value

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import PrincipalStore

_ApprovalStatus = Literal["approved", "denied", "expired"]
_ResolutionStatus = Literal["approved", "denied"]
MatrixEventPreparer = Callable[[str, str | None, dict[str, Any]], Awaitable[dict[str, Any] | None]]
MatrixDeliverySender = Callable[[MatrixDelivery], Awaitable[str]]
MatrixDeliveryResolver = Callable[[MatrixDelivery], Awaitable[str | None]]
TransportSenderProvider = Callable[[], str | None]
SendingDeviceProvider = Callable[[], str | None]
ContinuationReadyHandler = Callable[[str, tuple[str, ...]], Awaitable[None] | None]

_STARTUP_RECOVERY_SCAN_PAGE = 256
_DEADLINE_SWEEP_SECONDS = 60.0
_EVENT_TYPE = "io.mindroom.tool_approval"
DEFAULT_ROUTER_MANAGED_ROOM_REASON = (
    "Tool approval requires the router to be joined to the Matrix room. "
    "In ad-hoc invited rooms accepted via accept_invites, approval only works if the router "
    "is already joined there; otherwise retry from a managed room."
)
DEFAULT_SHUTDOWN_REASON = "MindRoom shut down before approval completed."
_DEFAULT_TIMEOUT_REASON = "Tool approval request timed out."
_DEFAULT_TRUNCATED_APPROVAL_REASON = (
    "Cannot approve: the tool arguments are too large to show in full, so a human cannot review "
    "exactly what would run. Retry with a smaller payload — for example save large content to a "
    "workspace file via `mindroom_output_path` or send it as a file attachment with a short message "
    "body — or auto-approve this tool via a script-based approval rule."
)
_MAX_ARGUMENTS_PREVIEW_CHARS = 1200
_MAX_FULL_ARGUMENTS_JSON_BYTES = 2_000_000
_SANITIZER_TRUNCATION_MARKER = "... [truncated]"
_MANAGER: _ApprovalManager | None = None
logger = get_logger(__name__)


class ToolApprovalTransportError(RuntimeError):
    """One actionable reason an approval card cannot be delivered."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
    if _json_preview_length(value) <= max_length:
        return value
    return sanitize_failure_text(_compact_preview_text(value), max_length=max_length)


def _contains_sanitizer_truncation(original: object, sanitized: object) -> bool:
    if isinstance(sanitized, dict):
        if not isinstance(original, dict):
            return "__truncated__" in sanitized or any(
                _contains_sanitizer_truncation(None, item) for item in sanitized.values()
            )
        original_by_text_key = {str(key): item for key, item in original.items()}
        return (
            len(sanitized) < len(original)
            or ("__truncated__" in sanitized and "__truncated__" not in original)
            or any(
                _contains_sanitizer_truncation(original_by_text_key.get(str(key)), item)
                for key, item in sanitized.items()
                if key != "__truncated__"
            )
        )
    if isinstance(sanitized, list):
        original_items = list(original) if isinstance(original, list | tuple | set | frozenset) else []
        return (
            len(original_items) > len(sanitized)
            or (sanitized != original_items and sanitized[-1:] == [_SANITIZER_TRUNCATION_MARKER])
            or any(
                _contains_sanitizer_truncation(original_item, sanitized_item)
                for original_item, sanitized_item in zip(original_items, sanitized, strict=False)
            )
        )
    return isinstance(sanitized, str) and sanitized.endswith(_SANITIZER_TRUNCATION_MARKER) and sanitized != original


def _build_event_arguments_preview(arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    sanitized = sanitize_failure_value(arguments)
    sanitizer_truncated = _contains_sanitizer_truncation(arguments, sanitized)
    if not isinstance(sanitized, dict):
        wrapped = {"value": _truncate_event_argument_value(sanitized, max_length=_MAX_ARGUMENTS_PREVIEW_CHARS // 2)}
        return wrapped, True
    if _json_preview_length(sanitized) <= _MAX_ARGUMENTS_PREVIEW_CHARS:
        return sanitized, sanitizer_truncated
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
        }, True
    return preview, True


def _full_arguments_json_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def _build_full_event_arguments(arguments: dict[str, Any]) -> dict[str, Any] | None:
    if _full_arguments_json_bytes(arguments) > _MAX_FULL_ARGUMENTS_JSON_BYTES:
        return None
    sanitized = cast("dict[str, Any]", redact_sensitive_data(arguments))
    return sanitized if _full_arguments_json_bytes(sanitized) <= _MAX_FULL_ARGUMENTS_JSON_BYTES else None


@dataclass(frozen=True, slots=True)
class ApprovalStartupSweep:
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
    resolved: bool
    error_reason: str | None = None
    thread_id: str | None = None
    card_event_id: str | None = None


@dataclass
class _ApprovalManager:
    """Own approval semantics while the generic worker owns Matrix delivery."""

    runtime_paths: RuntimePaths
    prepare_event: MatrixEventPreparer | None = None
    send_delivery: MatrixDeliverySender | None = None
    resolve_delivery: MatrixDeliveryResolver | None = None
    cards: PrincipalStore | None = None
    transport_sender: TransportSenderProvider | None = None
    sending_device: SendingDeviceProvider | None = None
    continuation_ready: ContinuationReadyHandler | None = None
    _resolving_card_event_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _live_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _deadline_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _deadline_wakeup: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def configure_transport(
        self,
        *,
        prepare_event: MatrixEventPreparer | None = None,
        send_delivery: MatrixDeliverySender | None = None,
        resolve_delivery: MatrixDeliveryResolver | None = None,
        cards: PrincipalStore | None = None,
        transport_sender: TransportSenderProvider | None = None,
        sending_device: SendingDeviceProvider | None = None,
        continuation_ready: ContinuationReadyHandler | None = None,
    ) -> None:
        """Rebind transport collaborators after runtime reload."""
        if prepare_event is not None:
            self.prepare_event = prepare_event
        if send_delivery is not None:
            self.send_delivery = send_delivery
        if resolve_delivery is not None:
            self.resolve_delivery = resolve_delivery
        if cards is not None:
            self.cards = cards
        if transport_sender is not None:
            self.transport_sender = transport_sender
        if sending_device is not None:
            self.sending_device = sending_device
        if continuation_ready is not None:
            self.continuation_ready = continuation_ready

    async def prepare_detached_approval(
        self,
        *,
        approval_id: str,
        continuation_id: str,
        continuation_generation: int,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        requester_id: str,
        approver_user_id: str,
        expires_at_ns: int,
        agent_name: str | None = None,
        thread_id: str | None = None,
    ) -> ApprovalCardReservation | None:
        """Prepare one exact frozen payload without creating delivery debt."""
        if self.prepare_event is None:
            return None
        requested_at = _utcnow()
        expires_at = datetime.fromtimestamp(expires_at_ns / 1_000_000_000, tz=UTC)
        event_arguments, arguments_truncated = _build_event_arguments_preview(arguments)
        full_arguments = (
            await asyncio.to_thread(_build_full_event_arguments, arguments) if arguments_truncated else None
        )
        content = self._pending_event_content(
            approval_id=approval_id,
            tool_name=tool_name,
            arguments=event_arguments,
            arguments_truncated=arguments_truncated,
            full_arguments=full_arguments,
            agent_name=agent_name,
            thread_id=thread_id,
            requester_id=requester_id,
            approver_user_id=approver_user_id,
            requested_at=requested_at,
            expires_at=expires_at,
            status="pending",
        )
        content.update(
            continuation_id=continuation_id,
            continuation_generation=continuation_generation,
            tool_call_id=tool_call_id,
        )
        prepared = await self.prepare_event(room_id, thread_id, content)
        if prepared is None:
            return None
        return ApprovalCardReservation(
            delivery_id=approval_id,
            tool_call_id=tool_call_id,
            event_type=_EVENT_TYPE,
            payload=prepared,
        )

    async def reserve_and_publish(
        self,
        *,
        continuation_principal_id: str,
        continuation_id: str,
        continuation_generation: int,
        cards: tuple[ApprovalCardReservation, ...],
    ) -> bool:
        """Reserve a complete generation atomically, then best-effort flush its cards."""
        if self.cards is None or self.send_delivery is None:
            return False
        reserved = await self.cards.reserve_approval_card_deliveries(
            continuation_principal_id=continuation_principal_id,
            continuation_id=continuation_id,
            expected_generation=continuation_generation,
            cards=cards,
        )
        if not reserved:
            return False
        worker = self._worker()
        for card in cards:
            try:
                await worker.flush(delivery_id=card.delivery_id, stage=DeliveryStage.INITIAL)
            except Exception:
                logger.warning(
                    "approval_card_initial_delivery_deferred",
                    delivery_id=card.delivery_id,
                    exc_info=True,
                )
        self._ensure_deadline_sweep()
        return True

    def _worker(self) -> MatrixDeliveryWorker:
        if self.cards is None or self.send_delivery is None:
            msg = "Approval Matrix delivery is not configured"
            raise ToolApprovalTransportError(msg)
        return MatrixDeliveryWorker(
            store=self.cards,
            send=self.send_delivery,
            event_type=_EVENT_TYPE,
            resend_after_reconciliation_miss=False,
            sending_device_id=None if self.sending_device is None else self.sending_device(),
            resolve_delivered=self.resolve_delivery,
        )

    async def handle_card_response(
        self,
        *,
        room_id: str,
        sender_id: str,
        card_event_id: str,
        status: _ResolutionStatus,
        reason: str | None,
        before_consume: Callable[[], Awaitable[None]] | None = None,
    ) -> ApprovalActionResult:
        """Atomically choose the exact-call winner and enqueue its terminal edit."""
        if self.has_active_in_memory_approval_card(card_event_id):
            if before_consume is not None:
                await before_consume()
            return ApprovalActionResult(consumed=True, resolved=False, card_event_id=card_event_id)
        cards = self.cards
        stored = (
            None
            if cards is None
            else await cards.pending_approval_card(
                room_id=room_id,
                card_event_id=card_event_id,
            )
        )
        if stored is None:
            terminal = cards is not None and await cards.is_terminal_approval_card(
                room_id=room_id,
                card_event_id=card_event_id,
            )
            if terminal and before_consume is not None:
                await before_consume()
            if terminal or cards is None or self.send_delivery is None:
                return ApprovalActionResult(consumed=terminal, resolved=False, card_event_id=card_event_id)
            # Matrix can expose a card whose send response died with its
            # process. Let the shared delivery owner recover that event ID
            # before deciding this reaction targets nothing durable.
            await self._worker().recover()
            stored = await cards.pending_approval_card(
                room_id=room_id,
                card_event_id=card_event_id,
            )
            if stored is None:
                return ApprovalActionResult(consumed=False, resolved=False, card_event_id=card_event_id)
        transport_sender = None if self.transport_sender is None else self.transport_sender()
        pending = (
            None
            if transport_sender is None
            else self._trusted_pending_from_card_event(
                stored.card,
                room_id=room_id,
                transport_sender=transport_sender,
                expected_card_event_id=card_event_id,
            )
        )
        if pending is None or pending.approver_user_id != sender_id:
            return ApprovalActionResult(consumed=False, resolved=False, card_event_id=card_event_id)
        if before_consume is not None:
            await before_consume()
        resolved_status, resolved_reason, resolution_was_truncated = self._normalized_resolution_request(
            pending,
            status=status,
            reason=reason,
        )
        with self._claimed_resolution(card_event_id):
            delivered = await self._record_and_flush_resolution(
                pending,
                stored,
                status=resolved_status,
                reason=resolved_reason,
                resolved_by=sender_id if resolved_status == status else None,
            )
        return ApprovalActionResult(
            consumed=True,
            resolved=delivered,
            error_reason=_DEFAULT_TRUNCATED_APPROVAL_REASON if resolution_was_truncated else None,
            thread_id=pending.thread_id,
            card_event_id=card_event_id,
        )

    async def _record_and_flush_resolution(
        self,
        pending: PendingApproval,
        stored: StoredApprovalCard,
        *,
        status: _ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> bool:
        if self.cards is None:
            return False
        offered = self._resolved_event_content(
            pending,
            status=status,
            reason=reason,
            resolved_by=resolved_by,
            resolved_at=_utcnow(),
        )
        recorded = await self.cards.resolve_continuation_approval_card(
            card_event_id=pending.card_event_id,
            requested_status=status,
            reason=reason,
            resolution=offered,
        )
        if recorded.resolution is None:
            return False
        if (
            recorded.recorded
            and recorded.continuation_ready
            and recorded.continuation_entity_name is not None
            and self.continuation_ready is not None
        ):
            wake = self.continuation_ready(recorded.continuation_entity_name, recorded.source_event_ids)
            if wake is not None:
                await wake
        try:
            edit_event_id = await self._worker().flush(
                delivery_id=stored.delivery_id,
                stage=DeliveryStage.FINAL,
            )
        except Exception:
            logger.warning("approval_terminal_delivery_deferred", delivery_id=stored.delivery_id, exc_info=True)
            return False
        if edit_event_id is None:
            return False
        return await self.cards.retire_approval_card(
            delivery_id=stored.delivery_id,
            card_event_id=pending.card_event_id,
        )

    async def expire_continuation_cards(self, continuation_id: str) -> bool:
        """Enqueue and flush expiry for every card owned by one failed continuation."""
        if self.cards is None:
            return False
        complete = True
        for room_id in await self.cards.pending_approval_room_ids():
            cursor: tuple[int, str] | None = None
            while True:
                page = await self.cards.pending_approval_cards(
                    room_id=room_id,
                    limit=_STARTUP_RECOVERY_SCAN_PAGE,
                    after=cursor,
                )
                if not page:
                    break
                cursor = (page[-1].created_at_ns, page[-1].delivery_id)
                for stored in page:
                    if stored.continuation_id == continuation_id:
                        complete = await self._expire_stored(room_id, stored) and complete
                if len(page) < _STARTUP_RECOVERY_SCAN_PAGE:
                    break
        return complete

    async def _expire_stored(self, room_id: str, stored: StoredApprovalCard) -> bool:
        if self.cards is None or stored.card_event_id is None:
            return False
        transport_sender = None if self.transport_sender is None else self.transport_sender()
        pending = (
            None
            if transport_sender is None
            else self._trusted_pending_from_card_event(
                stored.card,
                room_id=room_id,
                transport_sender=transport_sender,
                expected_card_event_id=stored.card_event_id,
            )
        )
        if pending is None:
            return False
        if stored.resolution is not None:
            try:
                edit_event_id = await self._worker().flush(delivery_id=stored.delivery_id, stage=DeliveryStage.FINAL)
            except Exception:
                return False
            return edit_event_id is not None and await self.cards.retire_approval_card(
                delivery_id=stored.delivery_id,
                card_event_id=stored.card_event_id,
            )
        return await self._record_and_flush_resolution(
            pending,
            stored,
            status="expired",
            reason=_DEFAULT_TIMEOUT_REASON,
            resolved_by=None,
        )

    async def recover_cards_on_startup(self) -> ApprovalStartupSweep:
        """Run generic delivery recovery, deadline decisions, and domain retirement."""
        if self.cards is None or self.send_delivery is None:
            return ApprovalStartupSweep(discarded=0, failed=1)
        outcome = await self._worker().recover()
        scanned = 0
        retired = 0
        failed = outcome.failed
        for room_id in await self.cards.pending_approval_room_ids():
            cursor: tuple[int, str] | None = None
            while True:
                page = await self.cards.pending_approval_cards(
                    room_id=room_id,
                    limit=_STARTUP_RECOVERY_SCAN_PAGE,
                    after=cursor,
                )
                if not page:
                    break
                cursor = (page[-1].created_at_ns, page[-1].delivery_id)
                for stored in page:
                    scanned += 1
                    if stored.resolution is not None:
                        settled = await self._expire_stored(room_id, stored)
                        retired += int(settled)
                        failed += int(not settled)
                        continue
                    try:
                        expires_at = parse_approval_datetime(
                            cast("str | None", stored.card["content"].get("expires_at")),
                        )
                    except (TypeError, ValueError):
                        logger.warning(
                            "approval_card_expiry_unreadable",
                            delivery_id=stored.delivery_id,
                        )
                        continue
                    if expires_at is not None and expires_at <= _utcnow():
                        settled = await self._expire_stored(room_id, stored)
                        retired += int(settled)
                        failed += int(not settled)
                if len(page) < _STARTUP_RECOVERY_SCAN_PAGE:
                    break
        self._ensure_deadline_sweep()
        return ApprovalStartupSweep(discarded=retired, failed=failed, scanned=scanned)

    def _ensure_deadline_sweep(self) -> None:
        if self._deadline_task is None or self._deadline_task.done():
            self._deadline_task = asyncio.create_task(self._run_deadline_sweep(), name="approval_deadline_sweep")
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

    async def shutdown(self, *, reason: str) -> None:
        """Stop the domain deadline scanner; durable delivery debt remains in the outbox."""
        del reason
        task = self._deadline_task
        self._deadline_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def uses_storage_root(self, storage_root: Path) -> bool:
        return self.runtime_paths.storage_root == storage_root

    def has_live_work(self) -> bool:
        with self._live_lock:
            return bool(self._resolving_card_event_ids)

    def has_active_in_memory_approval_card(self, card_event_id: str) -> bool:
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

    @staticmethod
    def _trusted_pending_from_card_event(
        card_event: dict[str, Any],
        *,
        room_id: str,
        transport_sender: str,
        expected_card_event_id: str,
    ) -> PendingApproval | None:
        event = dict(card_event)
        event.setdefault("sender", transport_sender)
        try:
            pending = PendingApproval.from_card_event(event, room_id=room_id)
        except (TypeError, ValueError):
            return None
        if pending.card_event_id != expected_card_event_id or pending.card_sender_id != transport_sender:
            return None
        return pending if pending.latest_status(None) == "pending" else None

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
        status: PendingApprovalStatus,
        full_arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content: dict[str, Any] = {
            "msgtype": _EVENT_TYPE,
            "body": _ApprovalManager._event_body(tool_name, status),
            "tool_name": tool_name,
            "arguments": arguments,
            "status": status,
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
    def _resolved_event_content(
        pending: PendingApproval,
        *,
        status: _ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
        resolved_at: datetime,
    ) -> dict[str, Any]:
        content = dict(pending.arguments_preview)
        result: dict[str, Any] = {
            "msgtype": _EVENT_TYPE,
            "body": _ApprovalManager._event_body(pending.tool_name, status),
            "tool_name": pending.tool_name,
            "arguments": content,
            "status": status,
            "approval_id": pending.approval_id,
            "approver_user_id": pending.approver_user_id,
            "requester_id": pending.requester_id,
            "requested_at": pending.requested_at,
            "expires_at": pending.expires_at,
            "thread_id": pending.thread_id,
            "resolved_at": resolved_at.isoformat(),
            "resolved_by": resolved_by,
        }
        if pending.agent_name is not None:
            result["agent_name"] = pending.agent_name
        if pending.arguments_preview_truncated:
            result["arguments_truncated"] = True
        if reason:
            result["resolution_reason"] = reason
        return result

    @staticmethod
    def _event_body(tool_name: str, status: PendingApprovalStatus) -> str:
        if status == "approved":
            return f"Approved: {tool_name}"
        if status == "denied":
            return f"Denied: {tool_name}"
        if status == "expired":
            return f"Expired: {tool_name}"
        return f"🔒 Approval required: {tool_name}"

    @staticmethod
    def _normalized_resolution_request(
        pending: PendingApproval,
        *,
        status: _ResolutionStatus,
        reason: str | None,
    ) -> tuple[_ApprovalStatus, str | None, bool]:
        expires_at = parse_approval_datetime(pending.expires_at)
        if expires_at is not None and expires_at <= _utcnow():
            return "expired", _DEFAULT_TIMEOUT_REASON, False
        arguments_unreviewable = pending.arguments_preview_truncated and not pending.full_arguments_available
        if status == "approved" and (not pending.approvable or arguments_unreviewable):
            return "denied", _DEFAULT_TRUNCATED_APPROVAL_REASON, True
        return status, reason, False


def get_approval_store() -> _ApprovalManager | None:
    """Return the configured approval domain, if the runtime is ready."""
    return _MANAGER


def initialize_approval_store(
    runtime_paths: RuntimePaths,
    *,
    prepare_event: MatrixEventPreparer | None = None,
    send_delivery: MatrixDeliverySender | None = None,
    resolve_delivery: MatrixDeliveryResolver | None = None,
    cards: PrincipalStore | None = None,
    transport_sender: TransportSenderProvider | None = None,
    sending_device: SendingDeviceProvider | None = None,
    continuation_ready: ContinuationReadyHandler | None = None,
) -> _ApprovalManager:
    """Initialize the module-level approval domain for one runtime context."""
    global _MANAGER
    if _MANAGER is not None and _MANAGER.uses_storage_root(runtime_paths.storage_root):
        _MANAGER.configure_transport(
            prepare_event=prepare_event,
            send_delivery=send_delivery,
            resolve_delivery=resolve_delivery,
            cards=cards,
            transport_sender=transport_sender,
            sending_device=sending_device,
            continuation_ready=continuation_ready,
        )
        return _MANAGER
    if _MANAGER is not None and _MANAGER.has_live_work():
        msg = "Cannot reinitialize approval store while a decision is committing"
        raise RuntimeError(msg)
    _MANAGER = _ApprovalManager(
        runtime_paths,
        prepare_event=prepare_event,
        send_delivery=send_delivery,
        resolve_delivery=resolve_delivery,
        cards=cards,
        transport_sender=transport_sender,
        sending_device=sending_device,
        continuation_ready=continuation_ready,
    )
    return _MANAGER


async def shutdown_approval_manager(reason: str = DEFAULT_SHUTDOWN_REASON) -> None:
    """Stop deadline scanning and release the process-local domain facade."""
    global _MANAGER
    manager = _MANAGER
    if manager is not None:
        await manager.shutdown(reason=reason)
        _MANAGER = None
