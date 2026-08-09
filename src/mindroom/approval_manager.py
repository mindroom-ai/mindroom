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
MatrixEventSender = Callable[[str, str | None, dict[str, Any], str], Awaitable["SentApprovalEvent | None"]]
MatrixEventEditor = Callable[[str, str, dict[str, Any]], Awaitable[bool]]
ApprovalRoomProvider = Callable[[], set[str]]
TransportSenderProvider = Callable[[], str | None]
SendingDeviceProvider = Callable[[], str | None]
# Read the room for the card one approval became: (room_id, card_sender,
# approval_id). None is the room's own answer that no such card exists; raising
# says the question could not be put, which is a different fact and must not be
# mistaken for the first.
ApprovalCardLocator = Callable[[str, str, str], Awaitable[str | None]]

_STARTUP_DISCARD_SCAN_PAGE = 256
# How long shutdown waits on work it does not own. Every such wait is on a
# Matrix round trip, which is the thing most likely to never come back while
# the runtime is tearing down around it.
_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 5.0
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


def _approval_transaction_id(approval_id: str) -> str:
    """Return the Matrix transaction one approval card is sent under.

    Derived from the approval rather than generated per attempt, and stored
    with the card, so a send repeated after a crash presents the transaction
    the homeserver may already have accepted and collapses back onto the same
    event instead of posting a second card.
    """
    return f"mindroom-approval-{approval_id}"


def _sent_card_body(claimed_card: dict[str, Any], sent_event: SentApprovalEvent) -> dict[str, Any]:
    """Return the claimed card as the room now holds it.

    The transport is allowed to send something other than what it was given --
    oversized arguments become a sidecar reference -- and from here on every
    reader compares the stored card against the room, so what was actually sent
    is what has to be kept.
    """
    content = sent_event.sent_content if sent_event.sent_content is not None else claimed_card["content"]
    return {**claimed_card, "event_id": sent_event.event_id, "content": content}


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
class ApprovalStartupSweep:
    """What one startup approval sweep settled, and what it still owes.

    A card the sweep could not settle stays clickable in the room with nothing
    live behind it, so the failure count is what the caller schedules its next
    attempt on. Reporting only the settled count would make a pass that
    finished nothing look exactly like a pass with nothing to do.
    """

    discarded: int
    failed: int

    @property
    def complete(self) -> bool:
        """Return whether nothing is left for a later sweep to retry."""
        return self.failed == 0


@dataclass(frozen=True, slots=True)
class _IdentifiedCard:
    """One recovered row's card, or why this pass could not name its event."""

    card: StoredApprovalCard | None
    # Whether a missing card is finished with rather than owed. A row proven to
    # have put nothing in the room is dropped on purpose and must not keep the
    # sweep coming back; a repeat or a room lookup whose outcome is unknown
    # must.
    settled: bool = False


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
    transaction_id: str
    card_event_id: str
    room_id: str
    card_event: dict[str, Any]
    future: Future[ApprovalDecision]


@dataclass(frozen=True, slots=True)
class _PostCancelCleanupTask:
    cleanup_future: Future[None]
    owner_loop: asyncio.AbstractEventLoop
    send_task: asyncio.Future[SentApprovalEvent | None]
    # The row this send belongs to, for the same reason the active send carries
    # it. Cancelling the requester ends the request, not the send: the send is
    # shielded and still open, and this task is what will bind a waiter to
    # whatever it returns. Until then this is the only thing that says the row
    # is spoken for.
    transaction_id: str


@dataclass(frozen=True, slots=True)
class _DetachedCardWrite:
    """One cancelled request's recovery, owned by its originating loop."""

    done_future: Future[None]
    owner_loop: asyncio.AbstractEventLoop
    recovery_task: asyncio.Task[None]


@dataclass(slots=True, eq=False)
class _ActiveApprovalSend:
    done_future: Future[None]
    owner_loop: asyncio.AbstractEventLoop
    send_task: asyncio.Future[SentApprovalEvent | None]
    # The row this send belongs to. The claim is durable before the send
    # starts and the live waiter is built out of what the send returns, so for
    # the length of the send this is the only thing that says the row is
    # spoken for.
    transaction_id: str


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
        sending_device: SendingDeviceProvider | None = None,
        locate_card: ApprovalCardLocator | None = None,
    ) -> None:
        self._runtime_storage_root = runtime_paths.storage_root
        self._send_event = sender
        self._edit_event = editor
        self._cards = cards
        self._approval_room_ids = approval_room_ids
        self._transport_sender = transport_sender
        self._sending_device = sending_device
        self._locate_card = locate_card
        self._live_lock = threading.RLock()
        self._pending_by_card_event: dict[str, _LiveApprovalWaiter] = {}
        self._resolving_card_event_ids: set[str] = set()
        self._resolved_card_event_ids = _BoundedCardEventIds(_MAX_REMEMBERED_TERMINAL_CARD_IDS)
        self._cancelled_card_event_ids = _BoundedCardEventIds(_MAX_REMEMBERED_TERMINAL_CARD_IDS)
        self._active_approval_sends: set[_ActiveApprovalSend] = set()
        self._post_cancel_cleanup_tasks: set[_PostCancelCleanupTask] = set()
        # Recovery that outlived the request that started it, held so it is
        # not garbage collected mid-flight.
        self._detached_card_writes: set[_DetachedCardWrite] = set()
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

        transaction_id = _approval_transaction_id(approval_id)
        claimed_card = self._claimed_card_body(content=content, requested_at=requested_at)
        # Before the send, so the window a crash can land in holds a row for a
        # card that may not exist rather than a card that no row explains. The
        # first is settled by presenting the transaction again; the second used
        # to be settled by nothing at all.
        if not await self._claim_card(room_id=room_id, transaction_id=transaction_id, card=claimed_card):
            return self._new_decision(status="expired", reason=_DEFAULT_UNRECORDABLE_CARD_REASON, resolved_by=None)

        try:
            waiter = await self._send_and_bind_waiter(
                room_id=room_id,
                thread_id=thread_id,
                content=content,
                claimed_card=claimed_card,
                transaction_id=transaction_id,
                approval_id=approval_id,
            )
        except ToolApprovalTransportError as exc:
            # Raised by the transport's own preconditions, so the card never
            # reached the homeserver and the claim can be taken back.
            await self._forget_card(transaction_id)
            logger.info("Approval card could not be made answerable", room_id=room_id, reason=exc.reason)
            return self._new_decision(status="expired", reason=exc.reason, resolved_by=None)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Deliberately keeps the claim. An exception out of the send says
            # the outcome is unknown, not that nothing was sent, and abandoning
            # the row would strand whatever did reach the room.
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

    async def discard_pending_on_startup(self) -> ApprovalStartupSweep:
        """Settle every router-authored card this bot restarted holding.

        A card whose decision was already recorded is redelivered rather than
        expired: the previous process committed to it, its tool may already
        have run, and the room may already show it. Only a card nobody ever
        answered is expired, because its requester is gone with the process
        that asked.

        Every card is walked, not one page of them, and what could not be
        settled is counted rather than swallowed. Both matter for the same
        reason: a card left behind here is one the user can still click, whose
        answer no live waiter and no later pass would otherwise come back for.
        The count is what the caller schedules that later pass on.
        """
        transport_sender = self._transport_sender_id()
        if transport_sender is None:
            return ApprovalStartupSweep(discarded=0, failed=0)

        discarded = 0
        failed = 0
        for room_id in self._configured_approval_room_ids():
            # A card whose settlement failed keeps its row deliberately, so it
            # stays inside the scan's window. Skipping it in memory is not
            # enough -- a whole page of failures would be read again on every
            # turn of this loop and everything behind it would never be seen.
            cursor: tuple[int, str] | None = None
            while True:
                page = await self._recoverable_room_cards(room_id, after=cursor)
                if not page:
                    break
                cursor = (page[-1].created_at_ns, page[-1].transaction_id)
                for claimed in page:
                    settled = await self._settle_recovered_card(
                        room_id=room_id,
                        claimed=claimed,
                        transport_sender=transport_sender,
                    )
                    if settled is None:
                        continue
                    if settled:
                        discarded += 1
                    else:
                        failed += 1
        return ApprovalStartupSweep(discarded=discarded, failed=failed)

    async def _settle_recovered_card(
        self,
        *,
        room_id: str,
        claimed: StoredApprovalCard,
        transport_sender: str,
    ) -> bool | None:
        """Settle one recovered card, or report that a later pass should try again.

        None is neither: the row is finished with as far as this pass is
        concerned and retrying would reach the same answer, so counting it as
        owed would keep the sweep asking forever.
        """
        if self._send_is_in_flight(claimed.transaction_id):
            # A row whose send has not come back is indistinguishable, from
            # here, from one a dead process abandoned: claimed, no event id,
            # and claimed by a device this process can still present from. The
            # live waiter that would say otherwise is built out of the send's
            # own return value, so it does not exist yet.
            #
            # Acting on it presents the transaction a second time -- which is
            # a second card in the room whenever the homeserver has not yet
            # seen the first, exactly the case a send still in flight
            # describes -- and then expires the answer its caller is waiting
            # for. Nothing is owed here, so this is not a failure either;
            # counting it would have the sweep return for a card that is
            # already in hand.
            logger.debug(
                "approval_startup_card_skipped_send_in_flight",
                room_id=room_id,
                transaction_id=claimed.transaction_id,
            )
            return None
        identified = await self._identified_card(room_id, claimed)
        if identified.card is None:
            return None if identified.settled else False
        pending = self._trusted_pending_from_card_event(
            identified.card.card,
            room_id=room_id,
            transport_sender=transport_sender,
        )
        if pending is None:
            # The stored body is not one this bot may act on -- it does not
            # parse as a card, or it names a sender this transport is not. No
            # later pass reads it differently, so it is logged rather than
            # retried; the row stays as the only remaining trace of it.
            logger.warning(
                "approval_startup_card_unusable",
                room_id=room_id,
                transaction_id=identified.card.transaction_id,
                card_event_id=identified.card.card_event_id,
            )
            return None
        if identified.card.resolution is not None:
            return await self._redeliver_recorded_resolution(pending, identified.card)
        result = await self._discard_matrix_only_card(
            pending=pending,
            transaction_id=identified.card.transaction_id,
            reason=_STARTUP_DISCARD_REASON,
            resolved_by=transport_sender,
        )
        return result.resolved

    async def _redeliver_recorded_resolution(self, pending: PendingApproval, stored: StoredApprovalCard) -> bool:
        """Show a decision a previous process committed to but may not have shown.

        Editing the card to the same terminal content it may already carry is
        a no-op in the room, so this converges whether or not the first attempt
        landed.
        """
        if not self._claim_matrix_cleanup(pending.card_event_id):
            return False
        with self._claimed_resolution(pending.card_event_id):
            delivered = await self._deliver_resolution(pending, stored.resolution or {}, stored.transaction_id)
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

        recovered = await self._recovered_pending_approval_for_card(room_id=room_id, card_event_id=card_event_id)
        if recovered is None or recovered[0].approver_user_id != sender_id:
            return ApprovalActionResult(consumed=False, resolved=False, card_event_id=card_event_id)
        if before_consume is not None:
            await before_consume()
        pending, transaction_id = recovered
        return await self._discard_matrix_only_card(
            pending=pending,
            transaction_id=transaction_id,
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
        sending_device: SendingDeviceProvider | None = None,
        locate_card: ApprovalCardLocator | None = None,
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
        if sending_device is not None:
            self._sending_device = sending_device
        if locate_card is not None:
            self._locate_card = locate_card

    def _current_shutdown_reason(self) -> str | None:
        with self._live_lock:
            return self._shutdown_reason

    async def _send_and_bind_waiter(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        content: dict[str, Any],
        claimed_card: dict[str, Any],
        transaction_id: str,
        approval_id: str,
    ) -> _LiveApprovalWaiter | None:
        if self._send_event is None:
            return None

        send_task = asyncio.ensure_future(
            self._mark_attempted_then_send(
                room_id=room_id,
                thread_id=thread_id,
                content=content,
                transaction_id=transaction_id,
            ),
        )
        active_send = _ActiveApprovalSend(
            done_future=Future(),
            owner_loop=asyncio.get_running_loop(),
            send_task=send_task,
            transaction_id=transaction_id,
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
                        claimed_card=claimed_card,
                        transaction_id=transaction_id,
                        approval_id=approval_id,
                    ),
                    active_send.owner_loop,
                )
                cleanup_task = _PostCancelCleanupTask(
                    cleanup_future=cleanup_future,
                    owner_loop=active_send.owner_loop,
                    send_task=send_task,
                    transaction_id=transaction_id,
                )
                # Registered before the raise, so the ``finally`` below hands
                # the row over rather than dropping it: there is no instant at
                # which neither owner claims this transaction.
                with self._live_lock:
                    self._post_cancel_cleanup_tasks.add(cleanup_task)
                cleanup_future.add_done_callback(lambda _future: self._discard_post_cancel_cleanup_task(cleanup_task))
                raise

            if sent_event is None:
                # Either the transport reports a send it knows did not happen,
                # or the attempt could not be committed before one was made.
                # Both say the claim describes a card that will never exist,
                # and it would only be resurrected by a later recovery send.
                await self._forget_card(transaction_id)
                return None
            waiter = self._bind_live_waiter(
                room_id=room_id,
                claimed_card=claimed_card,
                transaction_id=transaction_id,
                approval_id=approval_id,
                sent_event=sent_event,
            )
            try:
                await self._acknowledge_card(waiter)
            except asyncio.CancelledError:
                # The card is in the room and the caller is going away, so the
                # expiry edit still has to happen and must not inherit the
                # cancellation. Detached rather than shielded: shielding lets
                # the write finish but delivers the cancellation first, so the
                # expiry would run against a waiter nobody settles.
                self._schedule_bound_waiter_cancellation(waiter)
                raise
            shutdown_reason = self._current_shutdown_reason()
            if shutdown_reason is not None:
                await self._settle_bound_waiter_as_expired(waiter, reason=shutdown_reason)
            return waiter
        finally:
            # Retires the request's hold on the send, not the send. On the
            # cancelled path the send is still open and now belongs to the
            # cleanup task registered above, which is what keeps its row
            # spoken for; ``done_future`` has to be released either way, or the
            # shutdown drain would wait on a request that has already left and
            # then cancel a send its new owner is still shielding.
            with self._live_lock:
                self._active_approval_sends.discard(active_send)
            with suppress(InvalidStateError):
                active_send.done_future.set_result(None)

    async def _cleanup_cancelled_send_when_event_arrives(
        self,
        *,
        send_task: asyncio.Future[SentApprovalEvent | None],
        room_id: str,
        claimed_card: dict[str, Any],
        transaction_id: str,
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
            await self._forget_card(transaction_id)
            return

        waiter = self._bind_live_waiter(
            room_id=room_id,
            claimed_card=claimed_card,
            transaction_id=transaction_id,
            approval_id=approval_id,
            sent_event=sent_event,
        )
        await self._acknowledge_card(waiter)
        try:
            await self._settle_bound_waiter_as_cancelled(waiter)
        finally:
            with self._live_lock:
                self._pending_by_card_event.pop(waiter.card_event_id, None)

    def _schedule_bound_waiter_cancellation(self, waiter: _LiveApprovalWaiter) -> None:
        """Expire one already-sent card after its caller was cancelled.

        Runs detached, for the same reason the cancelled-send cleanup does: the
        work outlives the request that started it, and it must not inherit that
        request's cancellation.
        """
        done_future: Future[None] = Future()
        owner_loop = asyncio.get_running_loop()

        async def recover() -> None:
            try:
                await self._acknowledge_card(waiter)
                await self._settle_bound_waiter_as_cancelled(waiter)
            finally:
                with self._live_lock:
                    self._pending_by_card_event.pop(waiter.card_event_id, None)

        recovery_task = asyncio.create_task(recover())
        detached_write = _DetachedCardWrite(
            done_future=done_future,
            owner_loop=owner_loop,
            recovery_task=recovery_task,
        )
        with self._live_lock:
            self._detached_card_writes.add(detached_write)
        recovery_task.add_done_callback(lambda _future: self._discard_detached_card_write(detached_write))

    def _bind_live_waiter(
        self,
        *,
        room_id: str,
        claimed_card: dict[str, Any],
        transaction_id: str,
        approval_id: str,
        sent_event: SentApprovalEvent,
    ) -> _LiveApprovalWaiter:
        waiter = _LiveApprovalWaiter(
            approval_id=approval_id,
            transaction_id=transaction_id,
            card_event_id=sent_event.event_id,
            room_id=room_id,
            card_event=_sent_card_body(claimed_card, sent_event),
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
                transaction_id=waiter.transaction_id,
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
        transaction_id: str,
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
                transaction_id=transaction_id,
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
            transaction_id=waiter.transaction_id,
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
        transaction_id: str,
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
        if await self._deliver_resolution(pending, resolution, transaction_id):
            return _ResolutionOutcome.DELIVERED
        return _ResolutionOutcome.RECORDED

    async def _deliver_resolution(
        self,
        pending: PendingApproval,
        resolution: dict[str, Any],
        transaction_id: str,
    ) -> bool:
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
            await self._forget_card(transaction_id)
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
    ) -> tuple[PendingApproval, str] | None:
        """Return one clicked card and the row key that will retire it.

        The transaction comes back with the card because that is what the row
        is keyed on, and the caller's next move is to drop it.
        """
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
        pending = self._trusted_pending_from_card_event(
            stored.card,
            room_id=room_id,
            transport_sender=transport_sender,
            expected_card_event_id=card_event_id,
        )
        return None if pending is None else (pending, stored.transaction_id)

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

    async def _claim_card(self, *, room_id: str, transaction_id: str, card: dict[str, Any]) -> bool:
        """Make one card recoverable before anyone can see it.

        Returns whether the claim holds, because the card must not be sent
        otherwise: a card in the room that nothing accounts for is exactly the
        state this ordering exists to make impossible, and refusing the
        approval outright costs only a tool call that fails closed.
        """
        if self._cards is None:
            return True
        try:
            await self._cards.claim_approval_card(
                room_id=room_id,
                transaction_id=transaction_id,
                card=card,
            )
        except Exception:
            logger.warning(
                "Failed to claim an approval card before sending it",
                room_id=room_id,
                transaction_id=transaction_id,
                exc_info=True,
            )
            return False
        return True

    async def _mark_card_attempted(self, transaction_id: str) -> bool:
        """Record that this device is about to use one card's frozen transaction.

        Returns whether the send may go ahead. Refusing on a failed write is
        the only safe answer: an unmarked row reads as "nothing ever left this
        process", and recovery drops such a row without looking at the room --
        so sending under one would strand the card it created. A refused
        approval costs a tool call that fails closed.
        """
        if self._cards is None:
            return True
        try:
            return await self._cards.mark_approval_card_attempted(
                transaction_id=transaction_id,
                sending_device_id=self._sending_device_id(),
            )
        except Exception:
            logger.warning(
                "Failed to record an approval card send before making it",
                transaction_id=transaction_id,
                exc_info=True,
            )
            return False

    async def _mark_attempted_then_send(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        content: dict[str, Any],
        transaction_id: str,
    ) -> SentApprovalEvent | None:
        """Commit the attempt, then make it.

        One task rather than two awaited steps, because the caller registers
        this send as in flight straight after creating it and nothing may run
        in between. A bare ``await`` on the marking would open a window in
        which the startup sweep sees a row no live send is registered against
        and acts on it, which is the duplicate card that registration exists
        to prevent.

        Nothing means the attempt could not be committed, which is the same
        answer the transport gives for a send it knows did not happen, and the
        caller retires the claim on either.
        """
        if not await self._mark_card_attempted(transaction_id):
            return None
        if self._send_event is None:
            return None
        return await self._send_event(room_id, thread_id, content, transaction_id)

    async def _acknowledge_card(self, waiter: _LiveApprovalWaiter) -> None:
        """Point one claimed row at the Matrix event the send produced.

        Best effort on purpose, and the reason it can be is the claim: the row
        already exists, so a failure here leaves a card whose event id is
        merely unknown rather than one nothing accounts for. The next startup
        presents the same transaction, gets the same event back, and expires
        it. Until then the card fails closed, because every decision is
        recorded against the event id this write was meant to store.
        """
        if self._cards is None:
            return
        try:
            await self._cards.acknowledge_approval_card(
                transaction_id=waiter.transaction_id,
                card_event_id=waiter.card_event_id,
                card=waiter.card_event,
            )
        except Exception:
            logger.warning(
                "Failed to record the Matrix event an approval card became",
                room_id=waiter.room_id,
                event_id=waiter.card_event_id,
                exc_info=True,
            )

    async def _forget_card(self, transaction_id: str) -> None:
        """Drop one card that is finished, whether it was shown or never sent."""
        if self._cards is None:
            return
        try:
            await self._cards.forget_approval_card(transaction_id=transaction_id)
        except Exception:
            logger.warning("Failed to drop an approval card", transaction_id=transaction_id, exc_info=True)

    async def _recoverable_room_cards(
        self,
        room_id: str,
        *,
        after: tuple[int, str] | None = None,
    ) -> tuple[StoredApprovalCard, ...]:
        """Return one page of the cards this room may still owe a decision on.

        One source, because there is only one: a card is claimed before it is
        sent, so nothing can be in the room without a row here. Reading the
        room itself to look for strays would be looking for a state the claim
        ordering does not produce.

        A page rather than the room, because the caller walks all of them. The
        bound exists so one enormous room cannot hold the whole sweep in
        memory, not to decide how much of that room gets recovered.
        """
        if self._cards is None:
            return ()
        return await self._cards.pending_approval_cards(
            room_id=room_id,
            limit=_STARTUP_DISCARD_SCAN_PAGE,
            after=after,
        )

    def _repeat_would_deduplicate(self, stored: StoredApprovalCard) -> bool:
        """Return whether presenting this row's transaction again is safe.

        A Matrix transaction ID is scoped to the device that used it, so the
        homeserver only collapses a repeat onto the original event when the
        same device asks. From any other device the repeat is simply a new
        event, which for a card means a second one in the room.

        An unrecorded device on either side is not "unchanged", it is a device
        nobody can name, and a repeat cannot be proven safe against a device
        that cannot be named. Only attempted rows are asked, so a null device
        here means the sending process could not name its own.
        """
        current = self._sending_device_id()
        if stored.sending_device_id is None or current is None:
            return False
        return stored.sending_device_id == current

    async def _identified_card(self, room_id: str, stored: StoredApprovalCard) -> _IdentifiedCard:
        """Establish which Matrix event one claimed card became, by whichever means is sound.

        A row with no event id is the crash window claiming turns from
        unrecoverable into merely unknown, and the attempt marker is what
        narrows "unknown" down. An unattempted row is proof: the send was never
        reached, so the room holds nothing and the claim can simply go, with no
        homeserver asked and no card resent.

        An attempted row really may be clickable somewhere, and there are two
        ways to find out which event it is. Presenting the frozen transaction
        again is the cheap one, and it works because the homeserver collapses a
        repeat onto the event it already accepted -- but only for the device
        that used the transaction. From any other device the "repeat" is a
        second card, so that route is closed by a re-login and the room has to
        be read instead, the way the response outbox reads it.

        Where this parts company with the outbox is what happens with what it
        finds. A lost answer is unacceptable, so the outbox reconciles and then
        sends anyway. A card is a prompt for a human decision: two of them ask
        a question that has one answer, and answering the copy resolves
        nothing. So a card found in the room is adopted and expired where it
        stands, and never resent.

        Only after that does a row become safe to forget. Forgetting one whose
        card is still in the room retires the only thing that could expire it
        or honour a click on it, and nothing comes back for it -- which is why
        an absence has to be established rather than assumed, and why a lookup
        that could not run leaves the row owed for the next sweep.

        A repeat that fails leaves the row alone for the same reason. Hence the
        two empty answers here are not the same answer: a row proven to have no
        card is finished with, while one whose outcome is still unestablished
        is owed.
        """
        if stored.card_event_id is not None:
            return _IdentifiedCard(card=stored)
        if not stored.attempted:
            logger.info(
                "approval_startup_card_dropped_never_attempted",
                room_id=room_id,
                transaction_id=stored.transaction_id,
            )
            await self._forget_card(stored.transaction_id)
            return _IdentifiedCard(card=None, settled=True)
        if not self._repeat_would_deduplicate(stored):
            return await self._card_a_previous_device_left(room_id, stored)
        return await self._card_the_frozen_transaction_recovers(room_id, stored)

    async def _card_the_frozen_transaction_recovers(
        self,
        room_id: str,
        stored: StoredApprovalCard,
    ) -> _IdentifiedCard:
        """Present one attempted card's transaction again, from the device that used it.

        The homeserver collapses the repeat onto the event it already accepted,
        or accepts the card now if the first attempt never landed, so this ends
        up holding an event either way without reading the room. A repeat that
        does not come back leaves the row owed, because the outcome is still
        unknown and dropping the claim would abandon whatever did arrive.
        """
        cards = self._cards
        content = stored.card.get("content")
        if cards is None or self._send_event is None or not isinstance(content, dict):
            # No transport to ask with, or a body that is not a card. Neither
            # is something a later sweep resolves differently.
            return _IdentifiedCard(card=None, settled=True)
        try:
            sent_event = await self._send_event(room_id, content.get("thread_id"), content, stored.transaction_id)
        except Exception:
            logger.warning("approval_startup_resend_failed", room_id=room_id, exc_info=True)
            return _IdentifiedCard(card=None)
        if sent_event is None:
            return _IdentifiedCard(card=None)
        card = _sent_card_body(stored.card, sent_event)
        await cards.acknowledge_approval_card(
            transaction_id=stored.transaction_id,
            card_event_id=sent_event.event_id,
            card=card,
        )
        return _IdentifiedCard(card=self._adopted_card(stored, card_event_id=sent_event.event_id, card=card))

    async def _card_a_previous_device_left(self, room_id: str, stored: StoredApprovalCard) -> _IdentifiedCard:
        """Read the room for one attempted card whose transaction can no longer be repeated.

        Located by the approval id rather than the transaction: the transaction
        belongs to the device that used it, which is the whole reason this
        lookup is happening. The approval id is a per-request ``uuid4`` frozen
        into the card body before the send, so at most one original card in the
        room carries it.

        Three outcomes, and they are three because a missing card and an
        unanswered question are not the same thing. Found is adopted, so the
        caller expires it where it stands. Established as absent retires the
        row. Anything else -- no way to ask, or an ask that failed -- keeps the
        row and reports it owed, because dropping it on a guess is precisely
        how a clickable card ends up with nothing behind it.
        """
        approval_id = self._stored_card_approval_id(stored)
        card_sender = stored.card.get("sender")
        locate_card = self._locate_card
        if approval_id is None or not isinstance(card_sender, str) or not card_sender:
            # A body naming neither an approval nor a sender describes nothing
            # this pass could look for, and no later sweep reads it
            # differently, so it is retired rather than retried.
            logger.warning(
                "approval_startup_card_unidentifiable",
                room_id=room_id,
                transaction_id=stored.transaction_id,
                claimed_by_device=stored.sending_device_id,
                sending_device=self._sending_device_id(),
            )
            await self._forget_card(stored.transaction_id)
            return _IdentifiedCard(card=None, settled=True)
        if locate_card is None:
            # The question is answerable, just not by this process yet. Owed,
            # never guessed: a wrong absence here retires a clickable card's
            # only owner.
            logger.warning(
                "approval_startup_card_lookup_unavailable",
                room_id=room_id,
                transaction_id=stored.transaction_id,
                claimed_by_device=stored.sending_device_id,
                sending_device=self._sending_device_id(),
            )
            return _IdentifiedCard(card=None)
        try:
            card_event_id = await locate_card(room_id, card_sender, approval_id)
        except Exception:
            logger.warning(
                "approval_startup_card_lookup_failed",
                room_id=room_id,
                transaction_id=stored.transaction_id,
                exc_info=True,
            )
            return _IdentifiedCard(card=None)
        if card_event_id is None:
            logger.info(
                "approval_startup_card_absent_after_device_change",
                room_id=room_id,
                transaction_id=stored.transaction_id,
                claimed_by_device=stored.sending_device_id,
                sending_device=self._sending_device_id(),
            )
            await self._forget_card(stored.transaction_id)
            return _IdentifiedCard(card=None, settled=True)
        logger.info(
            "approval_startup_card_adopted_after_device_change",
            room_id=room_id,
            transaction_id=stored.transaction_id,
            card_event_id=card_event_id,
            claimed_by_device=stored.sending_device_id,
            sending_device=self._sending_device_id(),
        )
        # The claimed body is the best this pass has: the transport may have
        # diverged from it when it sent, but nothing here can read the room's
        # copy, and an expiry needs only the event id and the approval it
        # names.
        card = {**stored.card, "event_id": card_event_id}
        if self._cards is not None:
            await self._cards.acknowledge_approval_card(
                transaction_id=stored.transaction_id,
                card_event_id=card_event_id,
                card=card,
            )
        return _IdentifiedCard(card=self._adopted_card(stored, card_event_id=card_event_id, card=card))

    @staticmethod
    def _stored_card_approval_id(stored: StoredApprovalCard) -> str | None:
        """Return the per-request id frozen into one stored card's body."""
        content = stored.card.get("content")
        if not isinstance(content, dict):
            return None
        approval_id = content.get("approval_id")
        return approval_id if isinstance(approval_id, str) and approval_id else None

    @staticmethod
    def _adopted_card(
        stored: StoredApprovalCard,
        *,
        card_event_id: str,
        card: dict[str, Any],
    ) -> StoredApprovalCard:
        """Return one recovered row as it now stands, with its event established."""
        return StoredApprovalCard(
            card=card,
            resolution=stored.resolution,
            transaction_id=stored.transaction_id,
            card_event_id=card_event_id,
            attempted=stored.attempted,
            sending_device_id=stored.sending_device_id,
            created_at_ns=stored.created_at_ns,
        )

    async def shutdown(self, *, reason: str) -> None:
        """Expire pending approvals and drain approval cleanup tasks."""
        with self._live_lock:
            self._shutdown_reason = reason
            waiters = list(self._pending_by_card_event.values())
        for waiter in waiters:
            decision = self._new_decision(status="expired", reason=reason, resolved_by=None)
            claimed_waiter = self._claim_live_resolution(waiter.card_event_id)
            if claimed_waiter is None:
                await self._stand_down_for_the_claim_holder(waiter)
                continue
            with self._claimed_resolution(claimed_waiter.card_event_id):
                await self._settle_waiter_with_terminal_edit(claimed_waiter, decision)
                with self._live_lock:
                    self._resolved_card_event_ids.add(claimed_waiter.card_event_id)
        await self._drain_active_approval_sends()
        await self._drain_post_cancel_cleanup_tasks()
        await self._drain_detached_card_writes()

    async def _stand_down_for_the_claim_holder(self, waiter: _LiveApprovalWaiter) -> None:
        """Leave one card to whoever already claimed it, for as long as shutdown can afford.

        Waiting is right and racing would be wrong: the holder is partway
        through committing a decision, and publishing a second one for the same
        card is the outcome that cannot be walked back -- a tool released on an
        answer the record disagrees with.

        What the holder is waiting on is a Matrix round trip, which is the one
        thing here that may simply never come back while the runtime tears down
        around it. That is why the drains below this loop are bounded, and this
        wait, which every one of them is queued behind, was not.

        Bounded and reported, never bounded and quiet. Standing down leaves a
        clickable card and the row that accounts for it, which is work the next
        startup sweep is owed -- not work that disappeared.
        """
        try:
            await asyncio.wait_for(
                self._wait_for_competing_terminal_decision(waiter),
                timeout=_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Timed out waiting for an approval decision claimed elsewhere during shutdown",
                room_id=waiter.room_id,
                event_id=waiter.card_event_id,
            )
        except Exception:
            logger.warning(
                "An approval decision claimed elsewhere failed during shutdown",
                room_id=waiter.room_id,
                event_id=waiter.card_event_id,
                exc_info=True,
            )

    async def _drain_active_approval_sends(self) -> None:
        while True:
            with self._live_lock:
                active_sends = tuple(self._active_approval_sends)
            if not active_sends:
                return

            wrapped_by_done = {asyncio.wrap_future(active.done_future): active for active in active_sends}
            done, pending = await asyncio.wait(
                wrapped_by_done,
                timeout=_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
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
        """Give detached recovery a bounded chance before runtime teardown.

        These record a card and then expire it, and both halves need the
        journal store and the Matrix client that bot shutdown is about to
        close. Once the bound expires, cancellation is safe because the card
        row precedes its send and the terminal decision precedes its edit; the
        next startup sweep resumes whichever durable half was reached.
        """
        while True:
            with self._live_lock:
                pending = tuple(self._detached_card_writes)
            if not pending:
                return
            wrapped_by_write = {
                asyncio.wrap_future(detached_write.done_future): detached_write for detached_write in pending
            }
            done, unfinished = await asyncio.wait(
                wrapped_by_write,
                timeout=_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
            )
            for done_future in done:
                with suppress(asyncio.CancelledError, Exception):
                    done_future.result()
            if unfinished:
                unfinished_writes = [wrapped_by_write[wrapped] for wrapped in unfinished]
                for detached_write in unfinished_writes:
                    if not detached_write.recovery_task.done() and not detached_write.owner_loop.is_closed():
                        detached_write.owner_loop.call_soon_threadsafe(detached_write.recovery_task.cancel)
                cancelled, still_running = await asyncio.wait(
                    unfinished,
                    timeout=_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
                )
                for cancelled_future in cancelled:
                    with suppress(asyncio.CancelledError, Exception):
                        cancelled_future.result()
                with self._live_lock:
                    self._detached_card_writes.difference_update(unfinished_writes)
                logger.warning(
                    "Timed out waiting for detached approval card recovery during shutdown",
                    pending_card_writes=len(unfinished_writes),
                    cancellation_incomplete=len(still_running),
                )
                return

    def _discard_detached_card_write(self, detached_write: _DetachedCardWrite) -> None:
        """Drop one finished recovery from the set shutdown drains."""
        with suppress(asyncio.CancelledError, Exception):
            detached_write.recovery_task.result()
        with self._live_lock:
            self._detached_card_writes.discard(detached_write)
        with suppress(InvalidStateError):
            detached_write.done_future.set_result(None)

    async def _drain_post_cancel_cleanup_tasks(self) -> None:
        while True:
            with self._live_lock:
                futures = tuple(self._post_cancel_cleanup_tasks)
            if not futures:
                return

            wrapped_by_cleanup = {asyncio.wrap_future(cleanup.cleanup_future): cleanup for cleanup in futures}
            done, pending = await asyncio.wait(
                wrapped_by_cleanup,
                timeout=_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
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

    def _send_is_in_flight(self, transaction_id: str) -> bool:
        """Return whether this row is still owned by a send that has not come back.

        Both owners count, because both end the same way: whoever holds the
        send binds a waiter to what it returns and settles the card exactly
        once. A cancelled requester changes which owner that is -- the send is
        shielded and outlives the request -- and nothing else about the row.
        Answering on the request alone would call a handed-over send finished
        and let the sweep present its transaction a second time. This is the
        same pair ``has_live_work`` already counts as live.
        """
        with self._live_lock:
            return any(send.transaction_id == transaction_id for send in self._active_approval_sends) or any(
                cleanup.transaction_id == transaction_id for cleanup in self._post_cancel_cleanup_tasks
            )

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

    def _sending_device_id(self) -> str | None:
        if self._sending_device is None:
            return None
        return self._sending_device()

    def _claimed_card_body(self, *, content: dict[str, Any], requested_at: datetime) -> dict[str, Any]:
        """Return the card as it is recorded before the homeserver has seen it.

        Carries no event id, because none exists yet and inventing a
        placeholder would let a reader mistake an unsent card for a sent one.
        The event id lives in its own column until the send comes back, and
        ``_sent_card_body`` is what folds it in.
        """
        sender = self._transport_sender_id() or content.get("approver_user_id")
        return {
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
    sending_device: SendingDeviceProvider | None = None,
    locate_card: ApprovalCardLocator | None = None,
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
            sending_device=sending_device,
            locate_card=locate_card,
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
        sending_device=sending_device,
        locate_card=locate_card,
    )
    return _MANAGER


async def shutdown_approval_manager(reason: str = DEFAULT_SHUTDOWN_REASON) -> None:
    """Expire pending approvals and drop the module-level manager."""
    global _MANAGER

    manager = _MANAGER
    if manager is not None:
        await manager.shutdown(reason=reason)
        _MANAGER = None
