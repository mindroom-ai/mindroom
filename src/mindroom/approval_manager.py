"""Matrix-backed tool approval runtime state."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import Future, InvalidStateError
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom.approval_events import (
    PendingApproval,
    PendingApprovalStatus,
    parse_approval_datetime,
)
from mindroom.event_journal import StoredApprovalCard
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_data
from mindroom.tool_system.tool_calls import sanitize_failure_text, sanitize_failure_value

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalView

_ApprovalStatus = Literal["approved", "denied", "expired"]
_ResolutionStatus = Literal["approved", "denied"]
MatrixEventSender = Callable[[str, str | None, dict[str, Any]], Awaitable["SentApprovalEvent | None"]]
MatrixEventEditor = Callable[[str, str, dict[str, Any]], Awaitable[bool]]
ApprovalRoomProvider = Callable[[], set[str]]
TransportSenderProvider = Callable[[], str | None]

_STARTUP_DISCARD_SCAN_LIMIT = 10_000
# The msgtype every approval card carries. Startup uses it to tell this bot's
# own cards apart from everything else it has said in an approval room.
_APPROVAL_CARD_MSGTYPE = "io.mindroom.tool_approval"
_POST_CANCEL_CLEANUP_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_DEFAULT_CANCELLED_REASON = "Tool approval request was cancelled."
_DEFAULT_MISSING_CONTEXT_REASON = "Tool approval requires a Matrix room."
_DEFAULT_MISSING_REQUESTER_REASON = "Tool approval requires a human requester."
DEFAULT_ROUTER_MANAGED_ROOM_REASON = (
    "Tool approval requires the router to be joined to the Matrix room. "
    "In ad-hoc invited rooms accepted via accept_invites, approval only works if the router "
    "is already joined there; otherwise retry from a managed room."
)
_DEFAULT_SEND_FAILURE_REASON = "Tool approval request could not be delivered to Matrix."
DEFAULT_SHUTDOWN_REASON = "MindRoom shut down before approval completed."
_DEFAULT_TIMEOUT_REASON = "Tool approval request timed out."
_DEFAULT_UNRECORDABLE_CARD_REASON = "Tool approval request could not be recorded durably, so it cannot be answered."
_DEFAULT_TRUNCATED_APPROVAL_REASON = (
    "Cannot approve: the tool arguments are too large to show in full, so a human cannot review "
    "exactly what would run. Retry with a smaller payload — for example save large content to a "
    "workspace file via `mindroom_output_path` or send it as a file attachment with a short message "
    "body — or auto-approve this tool via a script-based approval rule."
)
_STARTUP_DISCARD_REASON = "Bot restarted before approval — original request was cancelled."
_DETACHED_REQUEST_REASON = "Original tool request is no longer active."
_MAX_ARGUMENTS_PREVIEW_CHARS = 1200
_MAX_FULL_ARGUMENTS_JSON_BYTES = 2_000_000
_MAX_REMEMBERED_TERMINAL_CARD_IDS = 4096
_SANITIZER_TRUNCATION_MARKER = "... [truncated]"
_MANAGER: _ApprovalManager | None = None
logger = get_logger(__name__)


class _ResolutionOutcome(Enum):
    """How far one decision got between being committed and being shown."""

    # Nothing was written down. The card is still unanswered by every reader of
    # it, so a later click or a startup expiry remains the correct outcome.
    UNRECORDED = "unrecorded"
    # Committed, but the room has not been told. Startup redelivers it; the
    # decision itself is settled and cannot be replaced.
    RECORDED = "recorded"
    # Committed and shown. The card is finished and has been dropped.
    DELIVERED = "delivered"


class ToolApprovalTransportError(RuntimeError):
    """One actionable reason an approval card cannot be made answerable.

    Either the room cannot carry the card, or nothing durable can hold it.
    Both leave a request nobody could answer, and both are reported to the
    caller as the reason its approval was refused.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _BoundedCardEventIds:
    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
        self._ids: OrderedDict[str, None] = OrderedDict()

    def add(self, card_event_id: str) -> None:
        if card_event_id in self._ids:
            return
        self._ids[card_event_id] = None
        while len(self._ids) > self._max_size:
            self._ids.popitem(last=False)

    def discard(self, card_event_id: str) -> None:
        self._ids.pop(card_event_id, None)

    def __contains__(self, card_event_id: object) -> bool:
        return card_event_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)


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
        has_added_truncation_key = "__truncated__" in sanitized and "__truncated__" not in original
        if len(sanitized) < len(original) or has_added_truncation_key:
            return True
        original_by_text_key = {str(key): item for key, item in original.items()}
        return any(
            _contains_sanitizer_truncation(original_by_text_key.get(str(key)), item)
            for key, item in sanitized.items()
            if key != "__truncated__"
        )
    if isinstance(sanitized, list):
        original_items = list(original) if isinstance(original, list | tuple | set | frozenset) else []
        has_added_truncation_marker = sanitized != original_items and sanitized[-1:] == [_SANITIZER_TRUNCATION_MARKER]
        if len(original_items) > len(sanitized) or has_added_truncation_marker:
            return True
        return any(
            _contains_sanitizer_truncation(original_item, sanitized_item)
            for original_item, sanitized_item in zip(original_items, sanitized, strict=False)
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
        drop_key = max(preview, key=lambda k: len(_compact_preview_text(preview[k])))
        preview.pop(drop_key)
    if not preview:
        summary = {
            "_summary": sanitize_failure_text(
                f"{len(sanitized)} arguments omitted because the preview exceeded the size limit.",
                max_length=max(24, _MAX_ARGUMENTS_PREVIEW_CHARS // 2),
            ),
        }
        return summary, True
    return preview, True


def _full_arguments_json_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _build_full_event_arguments(arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Return the complete redacted arguments when they can be delivered to a human, else None."""
    if _full_arguments_json_bytes(arguments) > _MAX_FULL_ARGUMENTS_JSON_BYTES:
        return None
    sanitized = cast("dict[str, Any]", redact_sensitive_data(arguments))
    if _full_arguments_json_bytes(sanitized) > _MAX_FULL_ARGUMENTS_JSON_BYTES:
        return None
    return sanitized


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """One resolved approval outcome."""

    status: _ApprovalStatus
    reason: str | None
    resolved_by: str | None
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class SentApprovalEvent:
    """One delivered approval event."""

    event_id: str
    # Content the transport actually sent when it diverges from the requested content,
    # e.g. after offloading full arguments to an uploaded sidecar.
    sent_content: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ApprovalActionResult:
    """One approval-action outcome parsed from a Matrix control event."""

    consumed: bool
    resolved: bool
    error_reason: str | None = None
    thread_id: str | None = None
    card_event_id: str | None = None


@dataclass(slots=True)
class _LiveApprovalWaiter:
    approval_id: str
    card_event_id: str
    room_id: str
    card_event: dict[str, Any]
    future: Future[ApprovalDecision]


@dataclass(frozen=True, slots=True)
class _PostCancelCleanupTask:
    cleanup_future: Future[None]
    owner_loop: asyncio.AbstractEventLoop
    send_task: asyncio.Future[SentApprovalEvent | None]


@dataclass(slots=True, eq=False)
class _ActiveApprovalSend:
    done_future: Future[None]
    owner_loop: asyncio.AbstractEventLoop
    send_task: asyncio.Future[SentApprovalEvent | None]


class _ApprovalManager:
    """Coordinate live approval waiters against Matrix approval cards.

    Recovered approval cards support terminal cleanup only; they never make an
    approval actionable after its live waiter is gone.
    """

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        *,
        sender: MatrixEventSender | None = None,
        editor: MatrixEventEditor | None = None,
        cards: ApprovalView | None = None,
        approval_room_ids: ApprovalRoomProvider | None = None,
        transport_sender: TransportSenderProvider | None = None,
    ) -> None:
        self._runtime_storage_root = runtime_paths.storage_root
        self._send_event = sender
        self._edit_event = editor
        self._cards = cards
        self._approval_room_ids = approval_room_ids
        self._transport_sender = transport_sender
        self._live_lock = threading.RLock()
        self._pending_by_card_event: dict[str, _LiveApprovalWaiter] = {}
        self._resolving_card_event_ids: set[str] = set()
        self._resolved_card_event_ids = _BoundedCardEventIds(_MAX_REMEMBERED_TERMINAL_CARD_IDS)
        self._cancelled_card_event_ids = _BoundedCardEventIds(_MAX_REMEMBERED_TERMINAL_CARD_IDS)
        self._active_approval_sends: set[_ActiveApprovalSend] = set()
        self._post_cancel_cleanup_tasks: set[_PostCancelCleanupTask] = set()
        # Recovery that outlived the request that started it, held so it is
        # not garbage collected mid-flight.
        self._detached_card_writes: set[asyncio.Future[None]] = set()
        self._shutdown_reason: str | None = None

    async def request_approval(  # noqa: C901, PLR0911
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str | None,
        requester_id: str | None,
        approver_user_id: str | None,
        timeout_seconds: float,
        agent_name: str | None = None,
        thread_id: str | None = None,
        workflow_id: str | None = None,
        participant_id: str | None = None,
    ) -> ApprovalDecision:
        """Send one Matrix approval card and wait for the Matrix-backed resolution."""
        # Keep the send/bind/wait flow linear so cancellation cleanup remains visible.
        if room_id is None:
            return self._new_decision(status="denied", reason=_DEFAULT_MISSING_CONTEXT_REASON, resolved_by=None)
        if approver_user_id is None:
            return self._new_decision(status="denied", reason=_DEFAULT_MISSING_REQUESTER_REASON, resolved_by=None)
        if self._send_event is None:
            return self._new_decision(status="expired", reason=_DEFAULT_SEND_FAILURE_REASON, resolved_by=None)
        shutdown_reason = self._current_shutdown_reason()
        if shutdown_reason is not None:
            return self._new_decision(status="expired", reason=shutdown_reason, resolved_by=None)

        approval_id = uuid4().hex
        requested_at = _utcnow()
        expires_at = requested_at + timedelta(seconds=max(timeout_seconds, 0.0))
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
            workflow_id=workflow_id,
            participant_id=participant_id,
            thread_id=thread_id,
            requester_id=requester_id,
            approver_user_id=approver_user_id,
            requested_at=requested_at,
            expires_at=expires_at,
            status="pending",
        )

        try:
            waiter = await self._send_and_bind_waiter(
                room_id=room_id,
                thread_id=thread_id,
                content=content,
                requested_at=requested_at,
                approval_id=approval_id,
            )
        except ToolApprovalTransportError as exc:
            logger.info("Approval card could not be made answerable", room_id=room_id, reason=exc.reason)
            return self._new_decision(status="expired", reason=exc.reason, resolved_by=None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to send approval Matrix event", room_id=room_id, exc_info=True)
            return self._new_decision(status="expired", reason=_DEFAULT_SEND_FAILURE_REASON, resolved_by=None)

        if waiter is None:
            shutdown_reason = self._current_shutdown_reason()
            if shutdown_reason is not None:
                return self._new_decision(status="expired", reason=shutdown_reason, resolved_by=None)
            return self._new_decision(status="expired", reason=_DEFAULT_SEND_FAILURE_REASON, resolved_by=None)

        try:
            return await self._await_waiter(waiter, expires_at=expires_at)
        except asyncio.CancelledError:
            await self._settle_bound_waiter_as_cancelled(waiter)
            raise
        finally:
            with self._live_lock:
                self._pending_by_card_event.pop(waiter.card_event_id, None)

    async def discard_pending_on_startup(self) -> int:
        """Settle every router-authored card this bot restarted holding.

        A card whose decision was already recorded is redelivered rather than
        expired: the previous process committed to it, its tool may already
        have run, and the room may already show it. Only a card nobody ever
        answered is expired, because its requester is gone with the process
        that asked.
        """
        transport_sender = self._transport_sender_id()
        if transport_sender is None:
            return 0

        discarded = 0
        for room_id in self._configured_approval_room_ids():
            for stored in await self._recoverable_room_cards(room_id):
                pending = self._trusted_pending_from_card_event(
                    stored.card,
                    room_id=room_id,
                    transport_sender=transport_sender,
                )
                if pending is None:
                    continue
                if stored.resolution is not None:
                    if await self._redeliver_recorded_resolution(pending, stored.resolution):
                        discarded += 1
                    continue
                result = await self._discard_matrix_only_card(
                    pending=pending,
                    reason=_STARTUP_DISCARD_REASON,
                    resolved_by=transport_sender,
                )
                if result.resolved:
                    discarded += 1
        return discarded

    async def _redeliver_recorded_resolution(
        self,
        pending: PendingApproval,
        resolution: dict[str, Any],
    ) -> bool:
        """Show a decision a previous process committed to but may not have shown.

        Editing the card to the same terminal content it may already carry is
        a no-op in the room, so this converges whether or not the first attempt
        landed.
        """
        if not self._claim_matrix_cleanup(pending.card_event_id):
            return False
        with self._claimed_resolution(pending.card_event_id):
            delivered = await self._deliver_resolution(pending, resolution)
            if delivered:
                with self._live_lock:
                    self._resolved_card_event_ids.add(pending.card_event_id)
            return delivered

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
        """Resolve one approval action anchored to a Matrix approval-card event id."""
        live_waiter = self._live_waiter_for_card(card_event_id)
        if live_waiter is not None:
            return await self._handle_live_waiter_response(
                live_waiter=live_waiter,
                room_id=room_id,
                sender_id=sender_id,
                status=status,
                reason=reason,
                before_consume=before_consume,
            )

        if self.knows_in_memory_approval_card(card_event_id):
            if before_consume is not None:
                await before_consume()
            return ApprovalActionResult(consumed=True, resolved=False, card_event_id=card_event_id)

        pending = await self._recovered_pending_approval_for_card(
            room_id=room_id,
            card_event_id=card_event_id,
        )
        if pending is None or pending.approver_user_id != sender_id:
            return ApprovalActionResult(consumed=False, resolved=False, card_event_id=card_event_id)
        if before_consume is not None:
            await before_consume()
        return await self._discard_matrix_only_card(
            pending=pending,
            reason=_DETACHED_REQUEST_REASON,
            resolved_by=sender_id,
        )

    async def handle_live_approval_id_response(
        self,
        *,
        room_id: str,
        sender_id: str,
        approval_id: str,
        status: _ResolutionStatus,
        reason: str | None,
        before_consume: Callable[[], Awaitable[None]] | None = None,
    ) -> ApprovalActionResult:
        """Resolve one custom client action by in-memory approval id only."""
        live_card_event_id = self._live_card_event_id_for_approval(approval_id)
        if live_card_event_id is None:
            return ApprovalActionResult(consumed=False, resolved=False)
        live_waiter = self._live_waiter_for_card(live_card_event_id)
        if live_waiter is None:
            return ApprovalActionResult(consumed=False, resolved=False, card_event_id=live_card_event_id)
        return await self._handle_live_waiter_response(
            live_waiter=live_waiter,
            room_id=room_id,
            sender_id=sender_id,
            status=status,
            reason=reason,
            before_consume=before_consume,
        )

    async def _handle_live_waiter_response(
        self,
        *,
        live_waiter: _LiveApprovalWaiter,
        room_id: str,
        sender_id: str,
        status: _ResolutionStatus,
        reason: str | None,
        before_consume: Callable[[], Awaitable[None]] | None,
    ) -> ApprovalActionResult:
        if live_waiter.room_id != room_id:
            return ApprovalActionResult(consumed=False, resolved=False, card_event_id=live_waiter.card_event_id)
        pending = self._pending_approval_for_card(
            room_id=live_waiter.room_id,
            card_event_id=live_waiter.card_event_id,
        )
        if pending is None:
            return ApprovalActionResult(consumed=False, resolved=False)
        if pending.approver_user_id != sender_id:
            return ApprovalActionResult(
                consumed=False,
                resolved=False,
                thread_id=pending.thread_id,
                card_event_id=pending.card_event_id,
            )
        if before_consume is not None:
            await before_consume()
        return await self._resolve_live_response(
            pending=pending,
            status=status,
            reason=reason,
            resolved_by=sender_id,
        )

    def configure_transport(
        self,
        *,
        sender: MatrixEventSender | None = None,
        editor: MatrixEventEditor | None = None,
        cards: ApprovalView | None = None,
        approval_room_ids: ApprovalRoomProvider | None = None,
        transport_sender: TransportSenderProvider | None = None,
    ) -> None:
        """Update Matrix transport hooks for an existing runtime manager."""
        if sender is not None:
            self._send_event = sender
        if editor is not None:
            self._edit_event = editor
        if cards is not None:
            self._cards = cards
        if approval_room_ids is not None:
            self._approval_room_ids = approval_room_ids
        if transport_sender is not None:
            self._transport_sender = transport_sender

    def _current_shutdown_reason(self) -> str | None:
        with self._live_lock:
            return self._shutdown_reason

    async def _send_and_bind_waiter(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        content: dict[str, Any],
        requested_at: datetime,
        approval_id: str,
    ) -> _LiveApprovalWaiter | None:
        if self._send_event is None:
            return None

        send_task = asyncio.ensure_future(self._send_event(room_id, thread_id, content))
        active_send = _ActiveApprovalSend(
            done_future=Future(),
            owner_loop=asyncio.get_running_loop(),
            send_task=send_task,
        )
        with self._live_lock:
            self._active_approval_sends.add(active_send)
        try:
            try:
                sent_event = await asyncio.shield(send_task)
            except asyncio.CancelledError:
                cleanup_future = asyncio.run_coroutine_threadsafe(
                    self._cleanup_cancelled_send_when_event_arrives(
                        send_task=send_task,
                        room_id=room_id,
                        content=content,
                        requested_at=requested_at,
                        approval_id=approval_id,
                    ),
                    active_send.owner_loop,
                )
                cleanup_task = _PostCancelCleanupTask(
                    cleanup_future=cleanup_future,
                    owner_loop=active_send.owner_loop,
                    send_task=send_task,
                )
                with self._live_lock:
                    self._post_cancel_cleanup_tasks.add(cleanup_task)
                cleanup_future.add_done_callback(lambda _future: self._discard_post_cancel_cleanup_task(cleanup_task))
                raise

            if sent_event is None:
                return None
            waiter = self._bind_live_waiter(
                room_id=room_id,
                content=content,
                requested_at=requested_at,
                approval_id=approval_id,
                sent_event=sent_event,
            )
            try:
                recorded = await self._remember_card(waiter)
            except asyncio.CancelledError:
                # The card is already in the room and the caller is going away.
                # The cancelled-send path below has always handled this shape by
                # recording the card and then expiring it, detached so the
                # caller's cancellation cannot interrupt it; this path had no
                # equivalent, so a cancellation arriving after the send returned
                # left a clickable card with no row -- no restart could expire
                # it, and a click found neither a live waiter nor a stored card.
                #
                # Detached rather than shielded on purpose. Shielding lets the
                # write finish but delivers the cancellation first, so the
                # expiry runs before the row exists and a cancelled approval is
                # recorded as an approved one.
                self._schedule_bound_waiter_cancellation(waiter)
                raise
            if not recorded:
                await self._retire_unrecoverable_card(waiter)
                raise ToolApprovalTransportError(_DEFAULT_UNRECORDABLE_CARD_REASON)
            shutdown_reason = self._current_shutdown_reason()
            if shutdown_reason is not None:
                await self._settle_bound_waiter_as_expired(waiter, reason=shutdown_reason)
            return waiter
        finally:
            with self._live_lock:
                self._active_approval_sends.discard(active_send)
            with suppress(InvalidStateError):
                active_send.done_future.set_result(None)

    async def _cleanup_cancelled_send_when_event_arrives(
        self,
        *,
        send_task: asyncio.Future[SentApprovalEvent | None],
        room_id: str,
        content: dict[str, Any],
        requested_at: datetime,
        approval_id: str,
    ) -> None:
        try:
            sent_event = await asyncio.shield(send_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Cancelled approval send failed before returning an event id", exc_info=True)
            return
        if sent_event is None:
            return

        waiter = self._bind_live_waiter(
            room_id=room_id,
            content=content,
            requested_at=requested_at,
            approval_id=approval_id,
            sent_event=sent_event,
        )
        if not await self._remember_card(waiter):
            await self._retire_unrecoverable_card(waiter)
            return
        try:
            await self._settle_bound_waiter_as_cancelled(waiter)
        finally:
            with self._live_lock:
                self._pending_by_card_event.pop(waiter.card_event_id, None)

    def _schedule_bound_waiter_cancellation(self, waiter: _LiveApprovalWaiter) -> None:
        """Record and expire one already-sent card after its caller was cancelled.

        Runs detached, for the same reason the cancelled-send cleanup does: the
        work outlives the request that started it, and it must not inherit that
        request's cancellation. Recording comes first so the expiry edit and the
        durable row cannot disagree.
        """

        async def recover() -> None:
            try:
                if not await self._remember_card(waiter):
                    await self._retire_unrecoverable_card(waiter)
                    return
                await self._settle_bound_waiter_as_cancelled(waiter)
            finally:
                with self._live_lock:
                    self._pending_by_card_event.pop(waiter.card_event_id, None)

        cleanup_future = asyncio.ensure_future(recover())
        with self._live_lock:
            self._detached_card_writes.add(cleanup_future)
        cleanup_future.add_done_callback(self._discard_detached_card_write)

    def _bind_live_waiter(
        self,
        *,
        room_id: str,
        content: dict[str, Any],
        requested_at: datetime,
        approval_id: str,
        sent_event: SentApprovalEvent,
    ) -> _LiveApprovalWaiter:
        card_event = self._card_event_from_content(
            event_id=sent_event.event_id,
            content=sent_event.sent_content if sent_event.sent_content is not None else content,
            requested_at=requested_at,
        )
        waiter = _LiveApprovalWaiter(
            approval_id=approval_id,
            card_event_id=sent_event.event_id,
            room_id=room_id,
            card_event=card_event,
            future=Future(),
        )
        with self._live_lock:
            self._pending_by_card_event[sent_event.event_id] = waiter
        return waiter

    async def _settle_bound_waiter_as_cancelled(self, waiter: _LiveApprovalWaiter) -> None:
        await self._settle_bound_waiter_as_expired(
            waiter,
            reason=_DEFAULT_CANCELLED_REASON,
            mark_cancelled=True,
        )

    async def _settle_bound_waiter_as_expired(
        self,
        waiter: _LiveApprovalWaiter,
        *,
        reason: str,
        mark_cancelled: bool = False,
    ) -> None:
        decision = self._new_decision(status="expired", reason=reason, resolved_by=None)
        if mark_cancelled:
            self._remember_cancelled_card_event_id(waiter.card_event_id)
        claimed_waiter = self._claim_live_resolution(waiter.card_event_id)
        if claimed_waiter is None:
            with suppress(Exception):
                await self._wait_for_competing_terminal_decision(waiter)
            if waiter.future.done():
                completed = waiter.future.result()
                if completed.status == "expired" and completed.reason == reason:
                    self._remember_resolved_card_event_id(waiter.card_event_id)
                    if mark_cancelled:
                        self._forget_cancelled_card_event_id(waiter.card_event_id)
                    return
            pending = PendingApproval.from_card_event(waiter.card_event, room_id=waiter.room_id)
            await self._emit_resolution(
                pending,
                status=decision.status,
                reason=decision.reason,
                resolved_by=decision.resolved_by,
            )
            with self._live_lock:
                self._resolved_card_event_ids.add(waiter.card_event_id)
                if mark_cancelled:
                    self._cancelled_card_event_ids.discard(waiter.card_event_id)
            return
        with self._claimed_resolution(claimed_waiter.card_event_id):
            await self._settle_waiter_with_terminal_edit(claimed_waiter, decision)
            with self._live_lock:
                self._resolved_card_event_ids.add(claimed_waiter.card_event_id)
                if mark_cancelled:
                    self._cancelled_card_event_ids.discard(claimed_waiter.card_event_id)

    async def _await_waiter(
        self,
        waiter: _LiveApprovalWaiter,
        *,
        expires_at: datetime,
    ) -> ApprovalDecision:
        try:
            remaining_seconds = max(0.0, (expires_at - _utcnow()).total_seconds())
            if remaining_seconds <= 0:
                return await self._expire_waiter(waiter)
            wrapped_future = asyncio.wrap_future(waiter.future)
            return await asyncio.wait_for(asyncio.shield(wrapped_future), timeout=remaining_seconds)
        except TimeoutError:
            return await self._expire_waiter(waiter)

    async def _expire_waiter(self, waiter: _LiveApprovalWaiter) -> ApprovalDecision:
        decision = self._new_decision(status="expired", reason=_DEFAULT_TIMEOUT_REASON, resolved_by=None)
        claimed_waiter = self._claim_live_resolution(waiter.card_event_id)
        if claimed_waiter is None:
            return await self._wait_for_competing_terminal_decision(waiter)
        with self._claimed_resolution(claimed_waiter.card_event_id):
            await self._settle_waiter_with_terminal_edit(claimed_waiter, decision)
            with self._live_lock:
                self._resolved_card_event_ids.add(claimed_waiter.card_event_id)
            return decision

    async def _resolve_live_response(
        self,
        *,
        pending: PendingApproval,
        status: _ResolutionStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> ApprovalActionResult:
        waiter = self._claim_live_resolution(pending.card_event_id)
        if waiter is None:
            return ApprovalActionResult(
                consumed=True,
                resolved=False,
                thread_id=pending.thread_id,
                card_event_id=pending.card_event_id,
            )
        with self._claimed_resolution(pending.card_event_id):
            await self._yield_to_queued_cancellation()
            cancelled = self._cancelled_card_event_ids_contains(pending.card_event_id)
            if cancelled:
                resolved_status: _ApprovalStatus = "expired"
                resolved_reason = _DEFAULT_CANCELLED_REASON
                resolution_was_truncated = False
            else:
                resolved_status, resolved_reason, resolution_was_truncated = self._normalized_resolution_request(
                    pending,
                    status=status,
                    reason=reason,
                )
            decision = self._new_decision(status=resolved_status, reason=resolved_reason, resolved_by=resolved_by)
            delivered = await self._settle_waiter_with_terminal_edit(waiter, decision)
            with self._live_lock:
                self._resolved_card_event_ids.add(pending.card_event_id)
                self._cancelled_card_event_ids.discard(pending.card_event_id)
            return ApprovalActionResult(
                consumed=True,
                resolved=delivered,
                error_reason=_DEFAULT_TRUNCATED_APPROVAL_REASON if resolution_was_truncated else None,
                thread_id=pending.thread_id,
                card_event_id=pending.card_event_id,
            )

    @staticmethod
    async def _yield_to_queued_cancellation() -> None:
        """Let a cancellation already queued behind the resolution claim mark the waiter."""
        loop = asyncio.get_running_loop()
        checkpoint = loop.create_future()
        loop.call_soon(checkpoint.set_result, None)
        await checkpoint

    async def _discard_matrix_only_card(
        self,
        *,
        pending: PendingApproval,
        reason: str,
        resolved_by: str | None,
    ) -> ApprovalActionResult:
        if not self._claim_matrix_cleanup(pending.card_event_id):
            return ApprovalActionResult(
                consumed=True,
                resolved=False,
                thread_id=pending.thread_id,
                card_event_id=pending.card_event_id,
            )
        with self._claimed_resolution(pending.card_event_id):
            outcome = await self._emit_resolution(
                pending,
                status="expired",
                reason=reason,
                resolved_by=resolved_by,
            )
            delivered = outcome is _ResolutionOutcome.DELIVERED
            with self._live_lock:
                if delivered:
                    self._resolved_card_event_ids.add(pending.card_event_id)
            return ApprovalActionResult(
                consumed=True,
                resolved=delivered,
                thread_id=pending.thread_id,
                card_event_id=pending.card_event_id,
            )

    async def _settle_waiter_with_terminal_edit(
        self,
        waiter: _LiveApprovalWaiter,
        decision: ApprovalDecision,
    ) -> bool:
        pending = PendingApproval.from_card_event(waiter.card_event, room_id=waiter.room_id)
        outcome = await self._emit_resolution(
            pending,
            status=decision.status,
            reason=decision.reason,
            resolved_by=decision.resolved_by,
        )
        if outcome is _ResolutionOutcome.UNRECORDED:
            # Nothing was committed, so the card is still genuinely unanswered:
            # it stays clickable, and a later startup may expire it. Releasing
            # the tool on the strength of a decision no durable record agrees
            # with is the one outcome that cannot be walked back.
            self._complete_waiter(
                waiter.card_event_id,
                self._new_decision(
                    status="expired",
                    reason=_DEFAULT_SEND_FAILURE_REASON,
                    resolved_by=decision.resolved_by,
                ),
            )
            return False
        # Committed. The waiter gets exactly what was written down, whether or
        # not the room has been told yet -- a decision that ran the tool while
        # the record says otherwise is a card with two meanings, and startup
        # would go on to make the room show the one that did not happen.
        self._complete_waiter(waiter.card_event_id, decision)
        return outcome is _ResolutionOutcome.DELIVERED

    async def _emit_resolution(
        self,
        pending: PendingApproval,
        *,
        status: _ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> _ResolutionOutcome:
        if self._edit_event is None:
            return _ResolutionOutcome.UNRECORDED
        resolution = self._resolved_event_content(
            pending,
            status=status,
            reason=reason,
            resolved_by=resolved_by,
            resolved_at=_utcnow(),
        )
        # Written before the edit is attempted. A crash in between then leaves
        # a card that is answered but perhaps not shown, which startup can
        # redeliver; recording it afterwards would leave one that looks
        # unanswered, and startup would expire a decision the room already
        # shows -- possibly an approval whose tool has already run.
        if not await self._record_resolution(pending.card_event_id, resolution):
            return _ResolutionOutcome.UNRECORDED
        if await self._deliver_resolution(pending, resolution):
            return _ResolutionOutcome.DELIVERED
        return _ResolutionOutcome.RECORDED

    async def _deliver_resolution(self, pending: PendingApproval, resolution: dict[str, Any]) -> bool:
        """Show one already-recorded decision, dropping the card once it lands."""
        if self._edit_event is None:
            return False
        try:
            delivered = await self._edit_event(pending.room_id, pending.card_event_id, resolution)
        except Exception:
            logger.warning(
                "Failed to edit approval Matrix event",
                approval_id=pending.approval_id,
                room_id=pending.room_id,
                event_id=pending.card_event_id,
                exc_info=True,
            )
            return False
        # Dropped only once the room shows the decision. An edit that never
        # landed leaves a card the user can still click, and the row is the
        # only thing that brings the next startup back to it.
        if delivered:
            await self._forget_card(pending.card_event_id)
        return delivered

    async def _record_resolution(self, card_event_id: str, resolution: dict[str, Any]) -> bool:
        """Commit one decision, reporting whether the durable record now agrees.

        A store that was never configured remembers no card, so it owes no
        decision either and there is nothing left disagreeing. A configured
        store that failed is the dangerous case: its row exists and still reads
        as unanswered, so showing the decision anyway would let the next
        startup expire a card whose tool has already run.

        Failing is not only raising. The write is a guarded update that can
        match no row and say nothing about it, so what the store reports the
        row now carries -- not the absence of an exception -- is what decides
        whether this decision was recorded.
        """
        if self._cards is None:
            return True
        try:
            recorded = await self._cards.resolve_approval_card(card_event_id=card_event_id, resolution=resolution)
        except Exception:
            logger.warning(
                "Failed to record an approval decision before showing it",
                event_id=card_event_id,
                exc_info=True,
            )
            return False
        if recorded.recorded:
            return True
        # Nothing was written. Either no row exists, so no later process can
        # ever account for this decision, or the row already carries an
        # earlier one that stands. Both mean the durable record disagrees with
        # the decision offered here, and a tool released on it would be
        # released on nothing.
        logger.warning(
            "An approval decision was not recorded",
            event_id=card_event_id,
            cause="no_stored_card" if recorded.resolution is None else "already_decided",
            stored_status=None if recorded.resolution is None else recorded.resolution.get("status"),
        )
        return False

    def _pending_approval_for_card(self, *, room_id: str, card_event_id: str) -> PendingApproval | None:
        """Return the live waiter's own view of one card.

        Deliberately does not consult the durable store. A live waiter is the
        in-process authority on a card this process is still waiting on, and a
        card store write that silently failed would otherwise turn a real
        pending approval into a click that does nothing.
        """
        live_waiter = self._live_waiter_for_card(card_event_id)
        if live_waiter is None or live_waiter.room_id != room_id:
            return None
        try:
            return PendingApproval.from_card_event(live_waiter.card_event, room_id=room_id)
        except (TypeError, ValueError):
            return None

    async def _recovered_pending_approval_for_card(
        self,
        *,
        room_id: str,
        card_event_id: str,
    ) -> PendingApproval | None:
        if self._cards is None:
            return None
        stored = await self._cards.pending_approval_card(room_id=room_id, card_event_id=card_event_id)
        # A recorded decision means this card is answered; only its delivery is
        # in doubt. Treating a click on it as a fresh resolution would overwrite
        # a decision whose tool may already have run.
        if stored is None or stored.resolution is not None:
            return None
        transport_sender = self._transport_sender_id()
        if transport_sender is None:
            return None
        return self._trusted_pending_from_card_event(
            stored.card,
            room_id=room_id,
            transport_sender=transport_sender,
            expected_card_event_id=card_event_id,
        )

    def _trusted_pending_from_card_event(
        self,
        card_event: dict[str, Any],
        *,
        room_id: str,
        transport_sender: str,
        expected_card_event_id: str | None = None,
    ) -> PendingApproval | None:
        event_room_id = card_event.get("room_id")
        if event_room_id is not None and event_room_id != room_id:
            return None
        try:
            pending = PendingApproval.from_card_event(card_event, room_id=room_id)
        except (TypeError, ValueError):
            return None
        if (
            expected_card_event_id is not None and pending.card_event_id != expected_card_event_id
        ) or pending.card_sender_id != transport_sender:
            return None
        # A stored card is written pending and dropped when its decision lands,
        # so its own body should never say otherwise. Checked anyway, because
        # believing a terminal card is pending would resolve it a second time.
        return pending if pending.latest_status(None) == "pending" else None

    async def _remember_card(self, waiter: _LiveApprovalWaiter) -> bool:
        """Make one sent card recoverable by whoever restarts next.

        Recorded even for a card that is about to be cancelled, because the
        cancellation is itself a Matrix edit that can fail: without the row,
        a card left visible in the room would answer nobody forever.

        Returns whether the card is now recoverable, because a waiter bound to
        a card no row backs is a click away from releasing a tool nothing
        durable would agree had been approved.
        """
        if self._cards is None:
            return True
        try:
            await self._cards.remember_approval_card(
                room_id=waiter.room_id,
                card_event_id=waiter.card_event_id,
                card=waiter.card_event,
            )
        except Exception:
            logger.warning(
                "Failed to record approval card for recovery",
                room_id=waiter.room_id,
                event_id=waiter.card_event_id,
                exc_info=True,
            )
            return False
        return True

    async def _retire_unrecoverable_card(self, waiter: _LiveApprovalWaiter) -> None:
        """Take back one card that reached the room with no durable row behind it.

        Nothing recorded this card, so no later process can expire it, no
        later process can redeliver a decision made on it, and no decision it
        collects could ever be accounted for. The live waiter is dropped
        first, so a click can no longer release a tool, and only then is the
        room told -- the record-before-edit ordering exists to keep a row and
        the room from disagreeing, and here there is no row to disagree with.
        """
        with self._live_lock:
            self._pending_by_card_event.pop(waiter.card_event_id, None)
            self._resolved_card_event_ids.add(waiter.card_event_id)
        self._complete_waiter_direct(
            waiter,
            self._new_decision(status="expired", reason=_DEFAULT_UNRECORDABLE_CARD_REASON, resolved_by=None),
        )
        pending = PendingApproval.from_card_event(waiter.card_event, room_id=waiter.room_id)
        await self._deliver_resolution(
            pending,
            self._resolved_event_content(
                pending,
                status="expired",
                reason=_DEFAULT_UNRECORDABLE_CARD_REASON,
                resolved_by=None,
                resolved_at=_utcnow(),
            ),
        )

    async def _forget_card(self, card_event_id: str) -> None:
        if self._cards is None:
            return
        try:
            await self._cards.forget_approval_card(card_event_id=card_event_id)
        except Exception:
            logger.warning(
                "Failed to drop a resolved approval card",
                event_id=card_event_id,
                exc_info=True,
            )

    async def _recoverable_room_cards(self, room_id: str) -> tuple[StoredApprovalCard, ...]:
        """Return every card this room may still owe a decision, row or not.

        The rows are the first source and the authoritative one: they carry any
        decision already recorded. They are not the only source, because a card
        reaches the room before its row is written. A crash in that window used
        to leave a clickable card nobody could resolve -- the previous design
        caught it by scanning a general event cache, and deleting that cache
        removed the safety net without replacing it.

        The projection is where that room now lives, so the second source is
        this bot's own messages in it. A card found there with no row behind it
        is exactly the orphan: visible, unanswerable, and invisible to the row
        scan that would otherwise retire it.
        """
        if self._cards is None:
            return ()
        cards = await self._cards.pending_approval_cards(room_id=room_id, limit=_STARTUP_DISCARD_SCAN_LIMIT)
        if len(cards) >= _STARTUP_DISCARD_SCAN_LIMIT:
            logger.warning(
                "approval_startup_scan_truncated",
                room_id=room_id,
                scan_limit=_STARTUP_DISCARD_SCAN_LIMIT,
            )
        return (*cards, *await self._unrecorded_room_cards(room_id, recorded=cards))

    async def _unrecorded_room_cards(
        self,
        room_id: str,
        *,
        recorded: tuple[StoredApprovalCard, ...],
    ) -> tuple[StoredApprovalCard, ...]:
        """Return cards visible in the room that no durable row accounts for.

        Carries no resolution by construction: a decision is only ever recorded
        against a row, so a card without one has never been answered. That is
        the right shape for the caller, which expires exactly those.
        """
        cards = self._cards
        transport_sender = self._transport_sender_id()
        if cards is None or transport_sender is None:
            return ()
        # An efficiency guard, not a correctness one, and worth saying so: the
        # store's insert is `ON CONFLICT DO NOTHING` and cleanup is claimed once
        # per card event, so a card that slipped through here would be
        # deduplicated twice over downstream. What it saves is a store round
        # trip for every already-recorded card in the room, on every startup.
        recorded_event_ids = {event_id for card in recorded if isinstance(event_id := card.card.get("event_id"), str)}
        try:
            projected = await cards.room_messages_from_sender(
                room_id=room_id,
                sender=transport_sender,
                limit=_STARTUP_DISCARD_SCAN_LIMIT,
            )
        except Exception:
            logger.warning("approval_startup_projection_scan_failed", room_id=room_id, exc_info=True)
            return ()
        orphans: list[StoredApprovalCard] = []
        for message in projected:
            if message.revision_event_id in recorded_event_ids:
                continue
            if message.content.get("msgtype") != _APPROVAL_CARD_MSGTYPE:
                continue
            orphans.append(
                StoredApprovalCard(
                    # The same shape `_card_event_from_content` stores, rebuilt
                    # from the projection: the parser needs the event type and
                    # sender, and a projected message carries neither as such.
                    card={
                        "event_id": message.revision_event_id,
                        "sender": message.sender,
                        "type": _APPROVAL_CARD_MSGTYPE,
                        "origin_server_ts": message.created_ts,
                        "content": dict(message.content),
                    },
                    resolution=None,
                ),
            )
        if not orphans:
            return ()
        # Write the row the crashed process never got to. Everything downstream
        # resolves a card by recording its decision against that row first, so
        # an orphan handed on without one is refused and stays in the room --
        # finding it is only half of retiring it.
        for orphan in orphans:
            await cards.remember_approval_card(
                room_id=room_id,
                card_event_id=str(orphan.card["event_id"]),
                card=orphan.card,
            )
        logger.info(
            "approval_startup_recovered_unrecorded_cards",
            room_id=room_id,
            cards=len(orphans),
        )
        return tuple(orphans)

    async def shutdown(self, *, reason: str) -> None:
        """Expire pending approvals and drain approval cleanup tasks."""
        with self._live_lock:
            self._shutdown_reason = reason
            waiters = list(self._pending_by_card_event.values())
        for waiter in waiters:
            decision = self._new_decision(status="expired", reason=reason, resolved_by=None)
            claimed_waiter = self._claim_live_resolution(waiter.card_event_id)
            if claimed_waiter is None:
                with suppress(Exception):
                    await self._wait_for_competing_terminal_decision(waiter)
                continue
            with self._claimed_resolution(claimed_waiter.card_event_id):
                await self._settle_waiter_with_terminal_edit(claimed_waiter, decision)
                with self._live_lock:
                    self._resolved_card_event_ids.add(claimed_waiter.card_event_id)
        await self._drain_active_approval_sends()
        await self._drain_post_cancel_cleanup_tasks()
        await self._drain_detached_card_writes()

    async def _drain_active_approval_sends(self) -> None:
        while True:
            with self._live_lock:
                active_sends = tuple(self._active_approval_sends)
            if not active_sends:
                return

            wrapped_by_done = {asyncio.wrap_future(active.done_future): active for active in active_sends}
            done, pending = await asyncio.wait(
                wrapped_by_done,
                timeout=_POST_CANCEL_CLEANUP_SHUTDOWN_TIMEOUT_SECONDS,
            )
            for done_future in done:
                with suppress(asyncio.CancelledError, Exception):
                    done_future.result()

            if pending:
                pending_sends = [wrapped_by_done[wrapped_future] for wrapped_future in pending]
                for active_send in pending_sends:
                    active_send.done_future.cancel()
                    if not active_send.send_task.done() and not active_send.owner_loop.is_closed():
                        active_send.owner_loop.call_soon_threadsafe(active_send.send_task.cancel)
                with self._live_lock:
                    for active_send in pending_sends:
                        self._active_approval_sends.discard(active_send)
                logger.warning(
                    "Timed out waiting for active approval sends during shutdown",
                    active_approval_sends=len(pending_sends),
                )
                return

    async def _drain_detached_card_writes(self) -> None:
        """Wait for recovery that outlived its caller before the runtime tears down.

        These record a card and then expire it, and both halves need the
        journal store and the Matrix client that bot shutdown is about to
        close. Returning while one is still in flight lets it fail against a
        closed store and a closed client, leaving exactly the clickable card
        with no durable row that scheduling it was meant to prevent.
        """
        while True:
            with self._live_lock:
                pending = tuple(self._detached_card_writes)
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def _discard_detached_card_write(self, future: asyncio.Future[None]) -> None:
        """Drop one finished recovery from the set shutdown drains."""
        with self._live_lock:
            self._detached_card_writes.discard(future)

    async def _drain_post_cancel_cleanup_tasks(self) -> None:
        while True:
            with self._live_lock:
                futures = tuple(self._post_cancel_cleanup_tasks)
            if not futures:
                return

            wrapped_by_cleanup = {asyncio.wrap_future(cleanup.cleanup_future): cleanup for cleanup in futures}
            done, pending = await asyncio.wait(
                wrapped_by_cleanup,
                timeout=_POST_CANCEL_CLEANUP_SHUTDOWN_TIMEOUT_SECONDS,
            )
            for done_future in done:
                with suppress(asyncio.CancelledError, Exception):
                    done_future.result()

            if pending:
                pending_cleanups = [wrapped_by_cleanup[wrapped_future] for wrapped_future in pending]
                for cleanup in pending_cleanups:
                    cleanup.cleanup_future.cancel()
                    if not cleanup.send_task.done() and not cleanup.owner_loop.is_closed():
                        cleanup.owner_loop.call_soon_threadsafe(cleanup.send_task.cancel)
                with self._live_lock:
                    for cleanup in pending_cleanups:
                        self._post_cancel_cleanup_tasks.discard(cleanup)
                logger.warning(
                    "Timed out waiting for cancelled approval send cleanup during shutdown",
                    pending_cleanup_tasks=len(pending_cleanups),
                )
                return

    def _discard_post_cancel_cleanup_task(self, cleanup_task: _PostCancelCleanupTask) -> None:
        with self._live_lock:
            self._post_cancel_cleanup_tasks.discard(cleanup_task)

    def uses_storage_root(self, storage_root: Path) -> bool:
        """Return whether this manager belongs to one runtime storage root."""
        return self._runtime_storage_root == storage_root

    def has_live_work(self) -> bool:
        """Return whether live approvals or cancelled-send cleanup are still active."""
        with self._live_lock:
            has_waiters = bool(self._pending_by_card_event or self._resolving_card_event_ids)
            has_active_sends = bool(self._active_approval_sends)
            has_cleanup_tasks = bool(self._post_cancel_cleanup_tasks)
        return has_waiters or has_active_sends or has_cleanup_tasks

    def _live_waiter_for_card(self, card_event_id: str) -> _LiveApprovalWaiter | None:
        with self._live_lock:
            return self._pending_by_card_event.get(card_event_id)

    def _live_card_event_id_for_approval(self, approval_id: str) -> str | None:
        with self._live_lock:
            for card_event_id, waiter in self._pending_by_card_event.items():
                if waiter.approval_id == approval_id:
                    return card_event_id
        return None

    def _claim_live_resolution(self, card_event_id: str) -> _LiveApprovalWaiter | None:
        with self._live_lock:
            waiter = self._pending_by_card_event.get(card_event_id)
            if (
                waiter is None
                or waiter.future.done()
                or card_event_id in self._resolving_card_event_ids
                or card_event_id in self._resolved_card_event_ids
            ):
                return None
            self._resolving_card_event_ids.add(card_event_id)
            return waiter

    def _claim_matrix_cleanup(self, card_event_id: str) -> bool:
        with self._live_lock:
            if (
                card_event_id in self._pending_by_card_event
                or card_event_id in self._resolving_card_event_ids
                or card_event_id in self._resolved_card_event_ids
            ):
                return False
            self._resolving_card_event_ids.add(card_event_id)
            return True

    def _complete_waiter(self, card_event_id: str, decision: ApprovalDecision) -> None:
        with self._live_lock:
            waiter = self._pending_by_card_event.get(card_event_id)
        if waiter is None:
            return
        self._complete_waiter_direct(waiter, decision)

    @staticmethod
    def _complete_waiter_direct(waiter: _LiveApprovalWaiter, decision: ApprovalDecision) -> None:
        if waiter.future.done():
            return
        try:
            waiter.future.set_result(decision)
        except InvalidStateError:
            return

    def _remember_resolved_card_event_id(self, card_event_id: str) -> None:
        with self._live_lock:
            self._resolved_card_event_ids.add(card_event_id)

    def _remember_cancelled_card_event_id(self, card_event_id: str) -> None:
        with self._live_lock:
            self._cancelled_card_event_ids.add(card_event_id)

    def _forget_cancelled_card_event_id(self, card_event_id: str) -> None:
        with self._live_lock:
            self._cancelled_card_event_ids.discard(card_event_id)

    def _cancelled_card_event_ids_contains(self, card_event_id: str) -> bool:
        with self._live_lock:
            return card_event_id in self._cancelled_card_event_ids

    @contextmanager
    def _claimed_resolution(self, card_event_id: str) -> Iterator[None]:
        try:
            yield
        finally:
            with self._live_lock:
                self._resolving_card_event_ids.discard(card_event_id)

    def knows_in_memory_approval_card(self, card_event_id: str) -> bool:
        """Return whether this process has seen one approval card id."""
        with self._live_lock:
            return (
                card_event_id in self._pending_by_card_event
                or card_event_id in self._resolving_card_event_ids
                or card_event_id in self._resolved_card_event_ids
                or card_event_id in self._cancelled_card_event_ids
            )

    def has_active_in_memory_approval_card(self, card_event_id: str) -> bool:
        """Return whether an approval card can still consume in-process actions."""
        with self._live_lock:
            return card_event_id in self._pending_by_card_event or card_event_id in self._resolving_card_event_ids

    async def _wait_for_competing_terminal_decision(self, waiter: _LiveApprovalWaiter) -> ApprovalDecision:
        if waiter.future.done():
            return waiter.future.result()
        return await asyncio.shield(asyncio.wrap_future(waiter.future))

    def _configured_approval_room_ids(self) -> set[str]:
        if self._approval_room_ids is None:
            return set()
        return self._approval_room_ids()

    def _transport_sender_id(self) -> str | None:
        if self._transport_sender is None:
            return None
        return self._transport_sender()

    def _card_event_from_content(
        self,
        *,
        event_id: str,
        content: dict[str, Any],
        requested_at: datetime,
    ) -> dict[str, Any]:
        sender = self._transport_sender_id() or content.get("approver_user_id")
        return {
            "event_id": event_id,
            "sender": sender,
            "type": "io.mindroom.tool_approval",
            "origin_server_ts": int(requested_at.timestamp() * 1000),
            "content": content,
        }

    @staticmethod
    def _pending_event_content(
        *,
        approval_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        arguments_truncated: bool,
        agent_name: str | None,
        thread_id: str | None,
        requester_id: str | None,
        approver_user_id: str,
        requested_at: datetime,
        expires_at: datetime,
        status: PendingApprovalStatus,
        workflow_id: str | None = None,
        participant_id: str | None = None,
        full_arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content: dict[str, Any] = {
            "msgtype": "io.mindroom.tool_approval",
            "body": _ApprovalManager._event_body(
                tool_name,
                status,
                workflow_id=workflow_id,
                participant_id=participant_id,
            ),
            "tool_name": tool_name,
            "tool_call_id": approval_id,
            "arguments": arguments,
            "status": status,
            "approval_id": approval_id,
            "approver_user_id": approver_user_id,
            "requested_at": requested_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "thread_id": thread_id,
        }
        if agent_name is not None:
            content["agent_name"] = agent_name
        if workflow_id is not None:
            content["workflow_id"] = workflow_id
        if participant_id is not None:
            content["participant_id"] = participant_id
        if arguments_truncated:
            content["arguments_truncated"] = True
            if full_arguments is not None:
                content["full_arguments"] = full_arguments
            else:
                content["approvable"] = False
        if requester_id is not None:
            content["requester_id"] = requester_id
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
        requested_at = parse_approval_datetime(pending.requested_at) or datetime.fromtimestamp(
            pending.created_at_ms / 1000,
            tz=UTC,
        )
        expires_at = parse_approval_datetime(pending.expires_at) or requested_at + timedelta(
            seconds=pending.timeout_seconds,
        )
        content: dict[str, Any] = {
            "msgtype": "io.mindroom.tool_approval",
            "body": _ApprovalManager._event_body(
                pending.tool_name,
                status,
                workflow_id=pending.workflow_id,
                participant_id=pending.participant_id,
            ),
            "tool_name": pending.tool_name,
            "tool_call_id": pending.approval_id,
            "arguments": pending.arguments_preview,
            "status": status,
            "approval_id": pending.approval_id,
            "approver_user_id": pending.approver_user_id,
            "requested_at": requested_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "thread_id": pending.thread_id,
            "resolved_at": resolved_at.isoformat(),
            "resolved_by": resolved_by,
        }
        if pending.agent_name is not None:
            content["agent_name"] = pending.agent_name
        if pending.workflow_id is not None:
            content["workflow_id"] = pending.workflow_id
        if pending.participant_id is not None:
            content["participant_id"] = pending.participant_id
        if pending.arguments_preview_truncated:
            content["arguments_truncated"] = True
        if pending.requester_id:
            content["requester_id"] = pending.requester_id
        if reason:
            content["resolution_reason"] = reason
        return content

    @staticmethod
    def _event_body(
        tool_name: str,
        status: PendingApprovalStatus,
        *,
        workflow_id: str | None = None,
        participant_id: str | None = None,
    ) -> str:
        subject = tool_name
        if workflow_id is not None and participant_id is not None:
            subject = f"{tool_name} — Dynamic Workflow '{workflow_id}' participant '{participant_id}'"
        if status == "approved":
            return f"Approved: {subject}"
        if status == "denied":
            return f"Denied: {subject}"
        if status == "expired":
            return f"Expired: {subject}"
        return f"🔒 Approval required: {subject}"

    @classmethod
    def _normalized_resolution_request(
        cls,
        pending: PendingApproval,
        *,
        status: _ResolutionStatus,
        reason: str | None,
    ) -> tuple[_ApprovalStatus, str | None, bool]:
        arguments_unreviewable = pending.arguments_preview_truncated and not pending.full_arguments_available
        if status == "approved" and (not pending.approvable or arguments_unreviewable):
            return "denied", _DEFAULT_TRUNCATED_APPROVAL_REASON, True
        return status, reason, False

    @staticmethod
    def _new_decision(
        *,
        status: _ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> ApprovalDecision:
        return ApprovalDecision(
            status=status,
            reason=reason,
            resolved_by=resolved_by,
            resolved_at=_utcnow(),
        )


def get_approval_store() -> _ApprovalManager | None:
    """Return the module-level approval manager when initialized."""
    return _MANAGER


def initialize_approval_store(
    runtime_paths: RuntimePaths,
    *,
    sender: MatrixEventSender | None = None,
    editor: MatrixEventEditor | None = None,
    cards: ApprovalView | None = None,
    approval_room_ids: ApprovalRoomProvider | None = None,
    transport_sender: TransportSenderProvider | None = None,
) -> _ApprovalManager:
    """Initialize the module-level approval manager for one runtime context."""
    global _MANAGER

    if _MANAGER is not None and _MANAGER.uses_storage_root(runtime_paths.storage_root):
        _MANAGER.configure_transport(
            sender=sender,
            editor=editor,
            cards=cards,
            approval_room_ids=approval_room_ids,
            transport_sender=transport_sender,
        )
        return _MANAGER

    if _MANAGER is not None and _MANAGER.has_live_work():
        msg = "Cannot reinitialize approval store with pending live approvals; shut it down first."
        raise RuntimeError(msg)

    _MANAGER = _ApprovalManager(
        runtime_paths,
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=approval_room_ids,
        transport_sender=transport_sender,
    )
    return _MANAGER


async def shutdown_approval_manager(reason: str = DEFAULT_SHUTDOWN_REASON) -> None:
    """Expire pending approvals and drop the module-level manager."""
    global _MANAGER

    manager = _MANAGER
    if manager is not None:
        await manager.shutdown(reason=reason)
        _MANAGER = None
