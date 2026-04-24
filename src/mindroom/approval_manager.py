"""Matrix-backed tool approval runtime state."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, InvalidStateError
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom.logging_config import get_logger
from mindroom.tool_system.tool_failures import sanitize_failure_text, sanitize_failure_value

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache.event_cache import ConversationEventCache

ApprovalStatus = Literal["approved", "denied", "expired"]
PendingApprovalStatus = Literal["pending", "approved", "denied", "expired"]
ResolutionStatus = Literal["approved", "denied"]
MatrixEventSender = Callable[[str, str | None, dict[str, Any]], Awaitable["SentApprovalEvent | None"]]
MatrixEventEditor = Callable[[str, str, dict[str, Any]], Awaitable[bool]]
MatrixEventFetcher = Callable[[str, str], Awaitable[dict[str, Any] | None]]
MatrixRoomEventScanner = Callable[[str, int, int], Awaitable[list[dict[str, Any]]]]
ApprovalRoomProvider = Callable[[], set[str]]
TransportSenderProvider = Callable[[], str | None]

_APPROVALS_DIRNAME = "approvals"
_DEFAULT_CANCELLED_REASON = "Tool approval request was cancelled."
_DEFAULT_MISSING_CONTEXT_REASON = "Tool approval requires a Matrix room."
_DEFAULT_MISSING_REQUESTER_REASON = "Tool approval requires a human requester."
_DEFAULT_REINITIALIZE_REASON = "MindRoom reinitialized before approval completed."
_DEFAULT_ROUTER_MANAGED_ROOM_REASON = (
    "Tool approval requires the router to be joined to the Matrix room. "
    "In ad-hoc invited rooms accepted via accept_invites, approval only works if the router "
    "is already joined there; otherwise retry from a managed room."
)
_DEFAULT_SEND_FAILURE_REASON = "Tool approval request could not be delivered to Matrix."
_DEFAULT_SHUTDOWN_REASON = "MindRoom shut down before approval completed."
_DEFAULT_TIMEOUT_REASON = "Tool approval request timed out."
_DEFAULT_TRUNCATED_APPROVAL_REASON = (
    "Cannot approve: the displayed arguments are truncated. "
    "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
)
_STARTUP_AUTO_DENY_REASON = "Bot restarted before approval — original request was cancelled."
_CANCEL_RESOLUTION_RACE_GRACE_SECONDS = 0.01
_MAX_ARGUMENTS_PREVIEW_CHARS = 1200
_MANAGER: ApprovalManager | None = None
logger = get_logger(__name__)


class ToolApprovalTransportError(RuntimeError):
    """One actionable approval transport limitation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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


def _build_event_arguments_preview(arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    sanitized = sanitize_failure_value(arguments)
    if not isinstance(sanitized, dict):
        wrapped = {"value": _truncate_event_argument_value(sanitized, max_length=_MAX_ARGUMENTS_PREVIEW_CHARS // 2)}
        return wrapped, True
    if _json_preview_length(sanitized) <= _MAX_ARGUMENTS_PREVIEW_CHARS:
        return sanitized, False

    per_value_budget = max(24, _MAX_ARGUMENTS_PREVIEW_CHARS // max(len(sanitized), 1))
    preview = {
        key: _truncate_event_argument_value(value, max_length=per_value_budget) for key, value in sanitized.items()
    }

    while _json_preview_length(preview) > _MAX_ARGUMENTS_PREVIEW_CHARS:
        shrink_key = max(preview, key=lambda candidate: len(_compact_preview_text(preview[candidate])))
        current_value = preview[shrink_key]
        current_text = _compact_preview_text(current_value)
        if current_value is None or current_text == "[truncated]":
            preview[shrink_key] = None
        else:
            overflow = _json_preview_length(preview) - _MAX_ARGUMENTS_PREVIEW_CHARS
            next_max_length = max(len(current_text) - overflow - 8, len("[truncated]"))
            next_value = sanitize_failure_text(current_text, max_length=next_max_length)
            preview[shrink_key] = next_value if next_value != current_text else "[truncated]"
        if all(value is None for value in preview.values()):
            break

    while _json_preview_length(preview) > _MAX_ARGUMENTS_PREVIEW_CHARS and preview:
        drop_key = max(preview, key=len)
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


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """One resolved approval outcome."""

    status: ApprovalStatus
    reason: str | None
    resolved_by: str | None
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class SentApprovalEvent:
    """One delivered approval event."""

    event_id: str


@dataclass(frozen=True, slots=True)
class ApprovalActionResult:
    """One approval-action outcome parsed from a Matrix control event."""

    consumed: bool
    resolved: bool
    error_reason: str | None = None
    thread_id: str | None = None
    card_event_id: str | None = None


AnchoredApprovalActionResult = ApprovalActionResult


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """Typed projection of one Matrix `io.mindroom.tool_approval` card."""

    approval_id: str
    card_event_id: str
    room_id: str
    card_sender_id: str
    requester_id: str
    approver_user_id: str
    tool_name: str
    arguments_preview: dict[str, Any]
    arguments_preview_truncated: bool
    timeout_seconds: int
    created_at_ms: int
    thread_id: str | None = None
    agent_name: str | None = None
    requested_at: str | None = None
    expires_at: str | None = None

    @classmethod
    def from_card_event(cls, event: dict[str, Any], *, room_id: str) -> PendingApproval:
        """Parse one Matrix approval card event into a typed read-only view."""
        if event.get("type") != "io.mindroom.tool_approval":
            msg = "Approval card event has the wrong event type."
            raise ValueError(msg)
        content = event.get("content")
        if not isinstance(content, dict):
            msg = "Approval card event is missing content."
            raise TypeError(msg)
        if _is_replace_content(content):
            msg = "Approval card event is a replacement edit, not an original card."
            raise ValueError(msg)

        event_id = _required_str(event, "event_id")
        sender = _required_str(event, "sender")
        approval_id = _content_str(content, "approval_id") or _content_str(content, "tool_call_id")
        tool_name = _content_str(content, "tool_name")
        approver_user_id = _content_str(content, "approver_user_id")
        if approval_id is None or tool_name is None or approver_user_id is None:
            msg = "Approval card event is missing required approval fields."
            raise ValueError(msg)

        arguments = content.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}

        requested_at = _content_str(content, "requested_at")
        expires_at = _content_str(content, "expires_at")
        created_at_ms = _created_at_ms(event, requested_at)
        timeout_seconds = _timeout_seconds(requested_at, expires_at)
        requester_id = _content_str(content, "requester_id") or ""
        thread_id = _content_str(content, "thread_id")
        agent_name = _content_str(content, "agent_name")

        return cls(
            approval_id=approval_id,
            card_event_id=event_id,
            room_id=room_id,
            card_sender_id=sender,
            requester_id=requester_id,
            approver_user_id=approver_user_id,
            tool_name=tool_name,
            arguments_preview=cast("dict[str, Any]", arguments),
            arguments_preview_truncated=bool(content.get("arguments_truncated")),
            timeout_seconds=timeout_seconds,
            created_at_ms=created_at_ms,
            thread_id=thread_id,
            agent_name=agent_name,
            requested_at=requested_at,
            expires_at=expires_at,
        )

    def latest_status(self, latest_edit: dict[str, Any] | None) -> PendingApprovalStatus:
        """Return the visible approval status after applying the latest cached edit."""
        if latest_edit is None:
            return "pending"
        content = latest_edit.get("content")
        if not isinstance(content, dict):
            return "pending"
        new_content = content.get("m.new_content")
        if not isinstance(new_content, dict):
            return "pending"
        status = new_content.get("status")
        if status in {"pending", "approved", "denied", "expired"}:
            return cast("PendingApprovalStatus", status)
        return "pending"


@dataclass(slots=True)
class _LiveApprovalWaiter:
    approval_id: str
    card_event_id: str
    room_id: str
    card_event: dict[str, Any]
    future: Future[ApprovalDecision]


class _StartupTerminalState(Enum):
    RESOLVED = "resolved"
    PENDING_CONFIRMED = "pending_confirmed"
    UNKNOWN = "unknown"


class _CardOrigin(Enum):
    CACHE_HIT = "cache_hit"
    FETCHED = "fetched"
    HISTORY_SCAN = "history_scan"


class ApprovalManager:
    """Coordinate live approval waiters against Matrix approval cards."""

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        *,
        sender: MatrixEventSender | None = None,
        editor: MatrixEventEditor | None = None,
        event_cache: ConversationEventCache | None = None,
        event_fetcher: MatrixEventFetcher | None = None,
        room_event_scanner: MatrixRoomEventScanner | None = None,
        approval_room_ids: ApprovalRoomProvider | None = None,
        transport_sender: TransportSenderProvider | None = None,
    ) -> None:
        self._runtime_storage_root = runtime_paths.storage_root
        self._send_event = sender
        self._edit_event = editor
        self._event_cache = event_cache
        self._event_fetcher = event_fetcher
        self._room_event_scanner = room_event_scanner
        self._approval_room_ids = approval_room_ids
        self._transport_sender = transport_sender
        self._live_lock = threading.Lock()
        self._pending_by_card_event: dict[str, _LiveApprovalWaiter] = {}
        self._resolving_card_event_ids: set[str] = set()
        self._resolved_card_event_ids: set[str] = set()
        self._cancelled_card_event_ids: set[str] = set()
        self._post_cancel_cleanup_tasks: set[asyncio.Task[None]] = set()
        _purge_legacy_approval_files(runtime_paths.storage_root)

    async def request_approval(  # noqa: PLR0911
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
    ) -> ApprovalDecision:
        """Send one Matrix approval card and wait for the Matrix-backed resolution."""
        if room_id is None:
            return self._new_decision(status="denied", reason=_DEFAULT_MISSING_CONTEXT_REASON, resolved_by=None)
        if approver_user_id is None:
            return self._new_decision(status="denied", reason=_DEFAULT_MISSING_REQUESTER_REASON, resolved_by=None)
        if self._send_event is None:
            return self._new_decision(status="expired", reason=_DEFAULT_SEND_FAILURE_REASON, resolved_by=None)

        approval_id = uuid4().hex
        requested_at = _utcnow()
        expires_at = requested_at + timedelta(seconds=max(timeout_seconds, 0.0))
        event_arguments, arguments_truncated = _build_event_arguments_preview(arguments)
        content = self._pending_event_content(
            approval_id=approval_id,
            tool_name=tool_name,
            arguments=event_arguments,
            arguments_truncated=arguments_truncated,
            agent_name=agent_name,
            room_id=room_id,
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
            logger.info("Approval Matrix transport unavailable", room_id=room_id, reason=exc.reason)
            return self._new_decision(status="expired", reason=exc.reason, resolved_by=None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to send approval Matrix event", room_id=room_id, exc_info=True)
            return self._new_decision(status="expired", reason=_DEFAULT_SEND_FAILURE_REASON, resolved_by=None)

        if waiter is None:
            return self._new_decision(status="expired", reason=_DEFAULT_SEND_FAILURE_REASON, resolved_by=None)

        try:
            return await self._await_waiter(waiter, expires_at=expires_at)
        except asyncio.CancelledError:
            await self._settle_bound_waiter_as_cancelled(waiter)
            raise
        finally:
            with self._live_lock:
                self._pending_by_card_event.pop(waiter.card_event_id, None)

    async def resolve_approval(
        self,
        *,
        card_event_id: str,
        room_id: str,
        status: ResolutionStatus,
        reason: str | None = None,
        resolved_by: str | None = None,
    ) -> ApprovalActionResult:
        """Emit a terminal edit for one approval card and then release any live waiter."""
        pending = await self._pending_approval_for_card(room_id=room_id, card_event_id=card_event_id)
        if pending is None:
            return ApprovalActionResult(consumed=False, resolved=False)
        if self._live_waiter_for_card(card_event_id) is not None:
            return await self._resolve_live_response(
                pending=pending,
                status=status,
                reason=reason,
                resolved_by=resolved_by,
            )
        if status == "denied":
            return await self._deny_matrix_only_card(
                pending=pending,
                reason=reason or _STARTUP_AUTO_DENY_REASON,
                resolved_by=resolved_by,
            )
        return ApprovalActionResult(consumed=False, resolved=False, thread_id=pending.thread_id)

    async def get_pending_approval(
        self,
        room_id: str,
        approval_id: str,
    ) -> PendingApproval | None:
        """Return a pending approval by id from cache, live memory, or bounded Matrix history."""
        card_event_id = self._live_card_event_id_for_approval(approval_id)
        if card_event_id is not None:
            pending = await self._pending_approval_for_card(room_id=room_id, card_event_id=card_event_id)
            if pending is not None:
                return pending

        cached_pending: PendingApproval | None = None
        cached_pending_is_same_router = False
        for event in await self._scan_cached_room_cards(room_id, since_ts_ms=_lookback_cutoff_ms(24), limit=500):
            event_id = event.get("event_id")
            latest_edit = (
                await self._latest_edit(room_id=room_id, card_event_id=event_id) if isinstance(event_id, str) else None
            )
            pending = await self._pending_from_event_if_matching(
                event,
                room_id=room_id,
                approval_id=approval_id,
                latest_edit=latest_edit,
                latest_edit_known=True,
                origin=_CardOrigin.CACHE_HIT,
            )
            if pending is not None:
                cached_pending = pending
                cached_pending_is_same_router = pending.card_sender_id == self._transport_sender_id()
                break

        try:
            history_events = await self._scan_room_messages_for_approval_events(
                room_id,
                since_ts_ms=_lookback_cutoff_ms(24),
                limit=500,
            )
        except Exception as exc:
            logger.warning("approval.history_scan_failed", room_id=room_id, error=str(exc))
            return cached_pending if cached_pending_is_same_router else None
        history_latest_edits = _latest_replacement_edits_by_card_event_id(
            history_events,
            card_senders=_card_senders_by_event_id(history_events),
        )
        history_matched = False
        for event in history_events:
            if not _is_original_approval_card(event):
                continue
            card_event_id = str(event.get("event_id", ""))
            history_pending = await self._pending_from_event_if_matching(
                event,
                room_id=room_id,
                approval_id=approval_id,
                latest_edit=history_latest_edits.get(card_event_id),
                latest_edit_known=card_event_id in history_latest_edits,
                origin=_CardOrigin.HISTORY_SCAN,
            )
            if history_pending is not None:
                return history_pending
            with suppress(ValueError, TypeError):
                parsed = PendingApproval.from_card_event(event, room_id=room_id)
                history_matched = parsed.approval_id == approval_id or history_matched
        if history_matched:
            return None
        return cached_pending

    async def auto_deny_pending_on_startup(self, *, lookback_hours: int = 24) -> int:
        """Auto-deny unresolved approval cards after startup using Matrix as source of truth."""
        # AUTO-DENY MUST emit `m.replace` denied for same-router cards whose terminal state is
        # PENDING_CONFIRMED: no cached terminal edit exists, because this router write-throughs every
        # terminal edit it sends.
        # AUTO-DENY MUST NOT emit anything for other routers' cards or RESOLVED cards with a
        # cached or history-observed approved/denied/expired edit.
        # UNKNOWN means cross-router or history-discovered state cannot prove whether Matrix already
        # has a terminal edit. UNKNOWN is skipped: missing a cleanup is safer than false-denying a
        # card a human already approved.
        transport_sender = self._transport_sender_id()
        if transport_sender is None:
            return 0

        cutoff_ts_ms = _lookback_cutoff_ms(lookback_hours)
        denied = 0
        for room_id in self._configured_approval_room_ids():
            (
                candidates_by_event_id,
                cached_event_ids,
                history_latest_edits,
                history_scan_succeeded,
            ) = await self._startup_card_candidates(room_id=room_id, since_ts_ms=cutoff_ts_ms)
            for event_id, card_event in candidates_by_event_id.items():
                try:
                    pending = PendingApproval.from_card_event(card_event, room_id=room_id)
                except (TypeError, ValueError):
                    continue
                if pending.card_sender_id != transport_sender:
                    continue
                state = await self._classify_startup_card(
                    pending,
                    origin=_CardOrigin.CACHE_HIT if event_id in cached_event_ids else _CardOrigin.HISTORY_SCAN,
                    history_latest_edit=history_latest_edits.get(event_id),
                    history_scan_succeeded=history_scan_succeeded,
                )
                if state != _StartupTerminalState.PENDING_CONFIRMED:
                    continue
                result = await self._deny_matrix_only_card(
                    pending=pending,
                    reason=_STARTUP_AUTO_DENY_REASON,
                    resolved_by=transport_sender,
                )
                if result.resolved:
                    denied += 1
        return denied

    async def _startup_card_candidates(
        self,
        *,
        room_id: str,
        since_ts_ms: int,
    ) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, dict[str, Any]], bool]:
        cached_candidates = await self._scan_cached_room_cards(room_id, since_ts_ms=since_ts_ms, limit=500)
        history_scan_succeeded = True
        try:
            history_events = await self._scan_room_messages_for_approval_events(
                room_id,
                since_ts_ms=since_ts_ms,
                limit=500,
            )
        except Exception as exc:
            logger.warning("approval.startup_scan_failed", room_id=room_id, error=str(exc))
            history_scan_succeeded = False
            history_events = []
        history_candidates = [event for event in history_events if _is_original_approval_card(event)]
        cached_event_ids = {
            event_id
            for event_id in (card_event.get("event_id") for card_event in cached_candidates)
            if isinstance(event_id, str) and event_id
        }
        candidates_by_event_id: dict[str, dict[str, Any]] = {}
        for card_event in [*cached_candidates, *history_candidates]:
            event_id = card_event.get("event_id")
            if isinstance(event_id, str) and event_id:
                candidates_by_event_id.setdefault(event_id, card_event)
        card_senders = {
            event_id: str(card_event["sender"])
            for event_id, card_event in candidates_by_event_id.items()
            if isinstance(card_event.get("sender"), str)
        }
        return (
            candidates_by_event_id,
            cached_event_ids,
            _latest_replacement_edits_by_card_event_id(history_events, card_senders=card_senders),
            history_scan_succeeded,
        )

    async def _classify_startup_card(
        self,
        pending: PendingApproval,
        *,
        origin: _CardOrigin,
        history_latest_edit: dict[str, Any] | None,
        history_scan_succeeded: bool,
    ) -> _StartupTerminalState:
        if origin == _CardOrigin.CACHE_HIT:
            cached_latest_edit = await self._latest_edit(
                room_id=pending.room_id,
                card_event_id=pending.card_event_id,
            )
            if not _terminal_edit_matches_card_sender(cached_latest_edit, pending.card_sender_id):
                cached_latest_edit = None
            if cached_latest_edit is not None and pending.latest_status(cached_latest_edit) != "pending":
                return _StartupTerminalState.RESOLVED
        if history_scan_succeeded and pending.latest_status(history_latest_edit) != "pending":
            return _StartupTerminalState.RESOLVED
        if origin == _CardOrigin.CACHE_HIT and pending.card_sender_id == self._transport_sender_id():
            return _StartupTerminalState.PENDING_CONFIRMED
        if not history_scan_succeeded:
            return _StartupTerminalState.UNKNOWN
        return _StartupTerminalState.PENDING_CONFIRMED

    async def handle_card_response(
        self,
        *,
        room_id: str,
        sender_id: str,
        card_event_id: str,
        status: ResolutionStatus,
        reason: str | None,
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
            )

        consumed = self._known_in_memory_approval_card(card_event_id)
        return ApprovalActionResult(consumed=consumed, resolved=False, card_event_id=card_event_id)

    async def handle_live_approval_id_response(
        self,
        *,
        room_id: str,
        sender_id: str,
        approval_id: str,
        status: ResolutionStatus,
        reason: str | None,
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
        )

    async def _handle_live_waiter_response(
        self,
        *,
        live_waiter: _LiveApprovalWaiter,
        room_id: str,
        sender_id: str,
        status: ResolutionStatus,
        reason: str | None,
    ) -> ApprovalActionResult:
        if live_waiter.room_id != room_id:
            return ApprovalActionResult(consumed=False, resolved=False, card_event_id=live_waiter.card_event_id)
        pending = await self._pending_approval_for_card(
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
        return await self._resolve_live_response(
            pending=pending,
            status=status,
            reason=reason,
            resolved_by=sender_id,
        )

    def _configure_transport(
        self,
        *,
        sender: MatrixEventSender | None = None,
        editor: MatrixEventEditor | None = None,
        event_cache: ConversationEventCache | None = None,
        event_fetcher: MatrixEventFetcher | None = None,
        room_event_scanner: MatrixRoomEventScanner | None = None,
        approval_room_ids: ApprovalRoomProvider | None = None,
        transport_sender: TransportSenderProvider | None = None,
    ) -> None:
        if sender is not None:
            self._send_event = sender
        if editor is not None:
            self._edit_event = editor
        if event_cache is not None:
            self._event_cache = event_cache
        if event_fetcher is not None:
            self._event_fetcher = event_fetcher
        if room_event_scanner is not None:
            self._room_event_scanner = room_event_scanner
        if approval_room_ids is not None:
            self._approval_room_ids = approval_room_ids
        if transport_sender is not None:
            self._transport_sender = transport_sender

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
        try:
            sent_event = await asyncio.shield(send_task)
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(
                self._cleanup_cancelled_send_when_event_arrives(
                    send_task=send_task,
                    room_id=room_id,
                    content=content,
                    requested_at=requested_at,
                    approval_id=approval_id,
                ),
            )
            self._post_cancel_cleanup_tasks.add(cleanup_task)
            cleanup_task.add_done_callback(self._post_cancel_cleanup_tasks.discard)
            raise

        if sent_event is None:
            return None
        return self._bind_live_waiter(
            room_id=room_id,
            content=content,
            requested_at=requested_at,
            approval_id=approval_id,
            sent_event=sent_event,
        )

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
        try:
            await self._settle_bound_waiter_as_cancelled(waiter)
        finally:
            with self._live_lock:
                self._pending_by_card_event.pop(waiter.card_event_id, None)

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
            room_id=room_id,
            content=content,
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
        decision = self._new_decision(status="expired", reason=_DEFAULT_CANCELLED_REASON, resolved_by=None)
        with self._live_lock:
            self._cancelled_card_event_ids.add(waiter.card_event_id)
        claimed_waiter = self._claim_live_resolution(waiter.card_event_id)
        if claimed_waiter is None:
            with suppress(Exception):
                await self._wait_for_competing_terminal_decision(waiter)
            if waiter.future.done():
                completed = waiter.future.result()
                if completed.status == "expired" and completed.reason == _DEFAULT_CANCELLED_REASON:
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
                self._cancelled_card_event_ids.discard(waiter.card_event_id)
            return
        claim_released = False
        try:
            await self._settle_waiter_with_terminal_edit(claimed_waiter, decision)
            with self._live_lock:
                self._resolving_card_event_ids.discard(claimed_waiter.card_event_id)
                self._resolved_card_event_ids.add(claimed_waiter.card_event_id)
                self._cancelled_card_event_ids.discard(claimed_waiter.card_event_id)
            claim_released = True
        finally:
            if not claim_released:
                with self._live_lock:
                    self._resolving_card_event_ids.discard(claimed_waiter.card_event_id)

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
        claim_released = False
        try:
            await self._settle_waiter_with_terminal_edit(claimed_waiter, decision)
            with self._live_lock:
                self._resolving_card_event_ids.discard(claimed_waiter.card_event_id)
                self._resolved_card_event_ids.add(claimed_waiter.card_event_id)
            claim_released = True
            return decision
        finally:
            if not claim_released:
                with self._live_lock:
                    self._resolving_card_event_ids.discard(claimed_waiter.card_event_id)

    async def _resolve_live_response(
        self,
        *,
        pending: PendingApproval,
        status: ResolutionStatus,
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
        claim_released = False
        try:
            await asyncio.sleep(_CANCEL_RESOLUTION_RACE_GRACE_SECONDS)
            with self._live_lock:
                cancelled = pending.card_event_id in self._cancelled_card_event_ids
            if cancelled:
                resolved_status: ApprovalStatus = "expired"
                resolved_reason = _DEFAULT_CANCELLED_REASON
            else:
                resolved_status, resolved_reason = self._normalized_resolution_request(
                    pending,
                    status=status,
                    reason=reason,
                )
            decision = self._new_decision(status=resolved_status, reason=resolved_reason, resolved_by=resolved_by)
            await self._settle_waiter_with_terminal_edit(waiter, decision)
            with self._live_lock:
                self._resolving_card_event_ids.discard(pending.card_event_id)
                self._resolved_card_event_ids.add(pending.card_event_id)
                self._cancelled_card_event_ids.discard(pending.card_event_id)
            claim_released = True
            return ApprovalActionResult(
                consumed=True,
                resolved=True,
                error_reason=_DEFAULT_TRUNCATED_APPROVAL_REASON
                if resolved_reason == _DEFAULT_TRUNCATED_APPROVAL_REASON
                else None,
                thread_id=pending.thread_id,
                card_event_id=pending.card_event_id,
            )
        finally:
            if not claim_released:
                with self._live_lock:
                    self._resolving_card_event_ids.discard(pending.card_event_id)

    async def _deny_matrix_only_card(
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
        claim_released = False
        try:
            delivered = await self._emit_resolution(
                pending,
                status="denied",
                reason=reason,
                resolved_by=resolved_by,
            )
            with self._live_lock:
                self._resolving_card_event_ids.discard(pending.card_event_id)
                if delivered:
                    self._resolved_card_event_ids.add(pending.card_event_id)
            claim_released = True
            return ApprovalActionResult(
                consumed=True,
                resolved=delivered,
                thread_id=pending.thread_id,
                card_event_id=pending.card_event_id,
            )
        finally:
            if not claim_released:
                with self._live_lock:
                    self._resolving_card_event_ids.discard(pending.card_event_id)

    async def _settle_waiter_with_terminal_edit(
        self,
        waiter: _LiveApprovalWaiter,
        decision: ApprovalDecision,
    ) -> bool:
        pending = PendingApproval.from_card_event(waiter.card_event, room_id=waiter.room_id)
        delivered = await self._emit_resolution(
            pending,
            status=decision.status,
            reason=decision.reason,
            resolved_by=decision.resolved_by,
        )
        if delivered:
            self._complete_waiter(waiter.card_event_id, decision)
            return True
        fail_closed_decision = decision
        if decision.status == "approved":
            fail_closed_decision = self._new_decision(
                status="denied",
                reason=_DEFAULT_SEND_FAILURE_REASON,
                resolved_by=decision.resolved_by,
            )
        self._complete_waiter(waiter.card_event_id, fail_closed_decision)
        return False

    async def _emit_resolution(
        self,
        pending: PendingApproval,
        *,
        status: ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> bool:
        if self._edit_event is None:
            return False
        try:
            return await self._edit_event(
                pending.room_id,
                pending.card_event_id,
                self._resolved_event_content(
                    pending,
                    status=status,
                    reason=reason,
                    resolved_by=resolved_by,
                    resolved_at=_utcnow(),
                ),
            )
        except Exception:
            logger.warning(
                "Failed to edit approval Matrix event",
                approval_id=pending.approval_id,
                room_id=pending.room_id,
                event_id=pending.card_event_id,
                exc_info=True,
            )
            return False

    async def _pending_approval_for_card(self, *, room_id: str, card_event_id: str) -> PendingApproval | None:
        card_event, origin = await self._card_event_with_origin(room_id=room_id, card_event_id=card_event_id)
        if card_event is None:
            return None
        try:
            pending = PendingApproval.from_card_event(card_event, room_id=room_id)
        except (TypeError, ValueError):
            return None
        if self._live_waiter_for_card(card_event_id) is not None:
            latest_edit = await self._latest_edit(room_id=room_id, card_event_id=card_event_id)
            terminal_state_known = True
        else:
            latest_edit, terminal_state_known = await self._latest_edit_with_history_fallback(pending, origin=origin)
        if not terminal_state_known:
            return None
        if pending.latest_status(latest_edit) != "pending":
            return None
        return pending

    async def _card_event(self, *, room_id: str, card_event_id: str) -> dict[str, Any] | None:
        card_event, _origin = await self._card_event_with_origin(room_id=room_id, card_event_id=card_event_id)
        return card_event

    async def _card_event_with_origin(
        self,
        *,
        room_id: str,
        card_event_id: str,
    ) -> tuple[dict[str, Any] | None, _CardOrigin]:
        with self._live_lock:
            live = self._pending_by_card_event.get(card_event_id)
            if live is not None:
                return live.card_event, _CardOrigin.CACHE_HIT
        if self._event_cache is not None:
            cached_event = await self._event_cache.get_event(room_id, card_event_id)
            if cached_event is not None:
                return cached_event, _CardOrigin.CACHE_HIT
        if self._event_fetcher is not None:
            return await self._event_fetcher(room_id, card_event_id), _CardOrigin.FETCHED
        return None, _CardOrigin.FETCHED

    async def _latest_edit(self, *, room_id: str, card_event_id: str) -> dict[str, Any] | None:
        if self._event_cache is None:
            return None
        return await self._event_cache.get_latest_edit(room_id, card_event_id)

    async def _latest_edit_with_history_fallback(
        self,
        pending: PendingApproval,
        *,
        origin: _CardOrigin,
    ) -> tuple[dict[str, Any] | None, bool]:
        latest_edit = await self._latest_edit(room_id=pending.room_id, card_event_id=pending.card_event_id)
        if latest_edit is not None:
            if _terminal_edit_matches_card_sender(latest_edit, pending.card_sender_id):
                return latest_edit, True
            latest_edit = None
        if (
            self._event_cache is not None
            and origin == _CardOrigin.CACHE_HIT
            and pending.card_sender_id == self._transport_sender_id()
        ):
            return None, True
        try:
            history_events = await self._scan_room_messages_for_approval_events(
                pending.room_id,
                since_ts_ms=_lookback_cutoff_ms(24),
                limit=500,
            )
        except Exception as exc:
            logger.warning("approval.history_scan_failed", room_id=pending.room_id, error=str(exc))
            return None, False
        return (
            _latest_replacement_edits_by_card_event_id(
                history_events,
                card_senders={pending.card_event_id: pending.card_sender_id},
            ).get(pending.card_event_id),
            True,
        )

    async def _scan_cached_room_cards(
        self,
        room_id: str,
        *,
        since_ts_ms: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        if self._event_cache is None:
            return []
        events = await self._event_cache.get_recent_room_events(
            room_id,
            event_type="io.mindroom.tool_approval",
            since_ts_ms=since_ts_ms,
            limit=limit,
        )
        return [event for event in events if _is_original_approval_card(event)]

    async def _scan_room_messages_for_approval_events(
        self,
        room_id: str,
        *,
        since_ts_ms: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        if self._room_event_scanner is None:
            return []
        return await self._room_event_scanner(room_id, since_ts_ms, limit)

    async def _shutdown(self, *, reason: str) -> None:
        with self._live_lock:
            waiters = list(self._pending_by_card_event.values())
        for waiter in waiters:
            decision = self._new_decision(status="expired", reason=reason, resolved_by=None)
            claimed_waiter = self._claim_live_resolution(waiter.card_event_id)
            if claimed_waiter is None:
                with suppress(Exception):
                    await self._wait_for_competing_terminal_decision(waiter)
                continue
            claim_released = False
            try:
                await self._settle_waiter_with_terminal_edit(claimed_waiter, decision)
                with self._live_lock:
                    self._resolving_card_event_ids.discard(claimed_waiter.card_event_id)
                    self._resolved_card_event_ids.add(claimed_waiter.card_event_id)
                claim_released = True
            finally:
                if not claim_released:
                    with self._live_lock:
                        self._resolving_card_event_ids.discard(claimed_waiter.card_event_id)

    def _abort_pending(self, *, reason: str) -> None:
        with self._live_lock:
            waiters = list(self._pending_by_card_event.values())
            self._pending_by_card_event.clear()
            self._resolving_card_event_ids.clear()
        for waiter in waiters:
            self._complete_waiter_direct(waiter, self._new_decision(status="expired", reason=reason, resolved_by=None))

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

    def _known_in_memory_approval_card(self, card_event_id: str) -> bool:
        with self._live_lock:
            return (
                card_event_id in self._pending_by_card_event
                or card_event_id in self._resolving_card_event_ids
                or card_event_id in self._resolved_card_event_ids
                or card_event_id in self._cancelled_card_event_ids
            )

    async def _wait_for_competing_terminal_decision(self, waiter: _LiveApprovalWaiter) -> ApprovalDecision:
        if waiter.future.done():
            return waiter.future.result()
        return await asyncio.shield(asyncio.wrap_future(waiter.future))

    async def _pending_from_event_if_matching(
        self,
        event: dict[str, Any],
        *,
        room_id: str,
        approval_id: str,
        origin: _CardOrigin,
        latest_edit: dict[str, Any] | None = None,
        latest_edit_known: bool = False,
    ) -> PendingApproval | None:
        try:
            pending = PendingApproval.from_card_event(event, room_id=room_id)
        except (TypeError, ValueError):
            return None
        if pending.approval_id != approval_id:
            return None
        if not latest_edit_known:
            latest_edit, terminal_state_known = await self._latest_edit_with_history_fallback(
                pending,
                origin=origin,
            )
            if not terminal_state_known:
                return None
        if not _terminal_edit_matches_card_sender(latest_edit, pending.card_sender_id):
            latest_edit = None
        if pending.latest_status(latest_edit) != "pending":
            return None
        return pending

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
        room_id: str,
        content: dict[str, Any],
        requested_at: datetime,
    ) -> dict[str, Any]:
        del room_id
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
        room_id: str,
        thread_id: str | None,
        requester_id: str | None,
        approver_user_id: str,
        requested_at: datetime,
        expires_at: datetime,
        status: PendingApprovalStatus,
    ) -> dict[str, Any]:
        del room_id
        content: dict[str, Any] = {
            "msgtype": "io.mindroom.tool_approval",
            "body": ApprovalManager._event_body(tool_name, status),
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
        if arguments_truncated:
            content["arguments_truncated"] = True
        if requester_id is not None:
            content["requester_id"] = requester_id
        return content

    @staticmethod
    def _resolved_event_content(
        pending: PendingApproval,
        *,
        status: ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
        resolved_at: datetime,
    ) -> dict[str, Any]:
        requested_at = _parse_datetime(pending.requested_at) or datetime.fromtimestamp(
            pending.created_at_ms / 1000,
            tz=UTC,
        )
        expires_at = _parse_datetime(pending.expires_at) or requested_at + timedelta(seconds=pending.timeout_seconds)
        content = ApprovalManager._pending_event_content(
            approval_id=pending.approval_id,
            tool_name=pending.tool_name,
            arguments=pending.arguments_preview,
            arguments_truncated=pending.arguments_preview_truncated,
            agent_name=pending.agent_name,
            room_id=pending.room_id,
            thread_id=pending.thread_id,
            requester_id=pending.requester_id or None,
            approver_user_id=pending.approver_user_id,
            requested_at=requested_at,
            expires_at=expires_at,
            status=status,
        )
        content["body"] = ApprovalManager._event_body(pending.tool_name, status)
        content["resolved_at"] = resolved_at.isoformat()
        content["resolved_by"] = resolved_by
        if reason:
            content["resolution_reason"] = reason
        return content

    @staticmethod
    def _event_body(tool_name: str, status: PendingApprovalStatus) -> str:
        if status == "approved":
            return f"Approved: {tool_name}"
        if status == "denied":
            return f"Denied: {tool_name}"
        if status == "expired":
            return f"Expired: {tool_name}"
        return f"🔒 Approval required: {tool_name}"

    @classmethod
    def _normalized_resolution_request(
        cls,
        pending: PendingApproval,
        *,
        status: ResolutionStatus,
        reason: str | None,
    ) -> tuple[ApprovalStatus, str | None]:
        if status == "approved" and pending.arguments_preview_truncated:
            return "denied", _DEFAULT_TRUNCATED_APPROVAL_REASON
        return status, reason

    @staticmethod
    def _new_decision(
        *,
        status: ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> ApprovalDecision:
        return ApprovalDecision(
            status=status,
            reason=reason,
            resolved_by=resolved_by,
            resolved_at=_utcnow(),
        )


def _required_str(event: dict[str, Any], key: str) -> str:
    value = event.get(key)
    if isinstance(value, str) and value:
        return value
    msg = f"Approval card event is missing {key}."
    raise ValueError(msg)


def _content_str(content: dict[str, Any], key: str) -> str | None:
    value = content.get(key)
    return value if isinstance(value, str) and value else None


def _created_at_ms(event: dict[str, Any], requested_at: str | None) -> int:
    parsed = _parse_datetime(requested_at)
    if parsed is not None:
        return int(parsed.timestamp() * 1000)
    timestamp = event.get("origin_server_ts")
    return timestamp if isinstance(timestamp, int) and not isinstance(timestamp, bool) else 0


def _timeout_seconds(requested_at: str | None, expires_at: str | None) -> int:
    requested = _parse_datetime(requested_at)
    expires = _parse_datetime(expires_at)
    if requested is None or expires is None:
        return 0
    return max(0, int((expires - requested).total_seconds()))


def _is_replace_content(content: dict[str, Any]) -> bool:
    relates_to = content.get("m.relates_to")
    return isinstance(relates_to, dict) and relates_to.get("rel_type") == "m.replace"


def _is_original_approval_card(event: dict[str, Any]) -> bool:
    if event.get("type") != "io.mindroom.tool_approval":
        return False
    content = event.get("content")
    return isinstance(content, dict) and not _is_replace_content(content)


def _terminal_edit_matches_card_sender(edit: dict[str, Any] | None, card_sender_id: str) -> bool:
    if edit is None:
        return True
    return edit.get("sender") == card_sender_id


def _card_senders_by_event_id(events: list[dict[str, Any]]) -> dict[str, str]:
    card_senders: dict[str, str] = {}
    for event in events:
        if not _is_original_approval_card(event):
            continue
        event_id = event.get("event_id")
        sender = event.get("sender")
        if isinstance(event_id, str) and isinstance(sender, str):
            card_senders[event_id] = sender
    return card_senders


def _latest_replacement_edits_by_card_event_id(
    events: list[dict[str, Any]],
    *,
    card_senders: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    latest_edits: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") != "io.mindroom.tool_approval":
            continue
        content = event.get("content")
        if not isinstance(content, dict):
            continue
        relates_to = content.get("m.relates_to")
        if not isinstance(relates_to, dict) or relates_to.get("rel_type") != "m.replace":
            continue
        card_event_id = relates_to.get("event_id")
        if not isinstance(card_event_id, str) or not card_event_id:
            continue
        expected_sender = card_senders.get(card_event_id) if card_senders is not None else None
        if expected_sender is not None and event.get("sender") != expected_sender:
            continue
        current = latest_edits.get(card_event_id)
        if current is None or _event_timestamp_ms(event) >= _event_timestamp_ms(current):
            latest_edits[card_event_id] = event
    return latest_edits


def _event_timestamp_ms(event: dict[str, Any]) -> int:
    timestamp = event.get("origin_server_ts")
    return timestamp if isinstance(timestamp, int) and not isinstance(timestamp, bool) else 0


def _lookback_cutoff_ms(lookback_hours: int) -> int:
    return int((time.time() - max(lookback_hours, 0) * 3600) * 1000)


def _purge_legacy_approval_files(storage_root: Path) -> int:
    legacy_dir = storage_root / _APPROVALS_DIRNAME
    if not legacy_dir.exists():
        return 0
    purged = 0
    for json_file in legacy_dir.glob("*.json"):
        try:
            json_file.unlink()
            purged += 1
        except OSError as exc:
            logger.warning("approval.legacy_purge.failed", path=str(json_file), error=str(exc))
    with suppress(OSError):
        legacy_dir.rmdir()
    if purged:
        logger.info("approval.legacy_purge", purged_count=purged)
    return purged


def get_approval_store() -> ApprovalManager | None:
    """Return the module-level approval manager when initialized."""
    return _MANAGER


def initialize_approval_store(
    runtime_paths: RuntimePaths,
    *,
    sender: MatrixEventSender | None = None,
    editor: MatrixEventEditor | None = None,
    event_cache: ConversationEventCache | None = None,
    event_fetcher: MatrixEventFetcher | None = None,
    room_event_scanner: MatrixRoomEventScanner | None = None,
    approval_room_ids: ApprovalRoomProvider | None = None,
    transport_sender: TransportSenderProvider | None = None,
    runtime_loop: asyncio.AbstractEventLoop | None = None,
) -> ApprovalManager:
    """Initialize the module-level approval manager for one runtime context."""
    del runtime_loop
    global _MANAGER

    if _MANAGER is not None and _MANAGER._runtime_storage_root == runtime_paths.storage_root:
        _MANAGER._configure_transport(
            sender=sender,
            editor=editor,
            event_cache=event_cache,
            event_fetcher=event_fetcher,
            room_event_scanner=room_event_scanner,
            approval_room_ids=approval_room_ids,
            transport_sender=transport_sender,
        )
        return _MANAGER

    if _MANAGER is not None:
        _MANAGER._abort_pending(reason=_DEFAULT_REINITIALIZE_REASON)

    _MANAGER = ApprovalManager(
        runtime_paths,
        sender=sender,
        editor=editor,
        event_cache=event_cache,
        event_fetcher=event_fetcher,
        room_event_scanner=room_event_scanner,
        approval_room_ids=approval_room_ids,
        transport_sender=transport_sender,
    )
    return _MANAGER


async def shutdown_approval_manager(reason: str = _DEFAULT_SHUTDOWN_REASON) -> None:
    """Expire pending approvals and drop the module-level manager."""
    global _MANAGER

    manager = _MANAGER
    if manager is not None:
        await manager._shutdown(reason=reason)
        _MANAGER = None
