"""Tool-call approval evaluation and Matrix-only approval management.

ApprovalManager is accessed from multiple event loops and threads.
All reads and writes touching the in-memory approval indexes must go through
``self._state_lock`` so approval resolution stays consistent across runtimes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import tempfile
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom.constants import RuntimePaths, resolve_config_relative_path, safe_replace
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import is_agent_id
from mindroom.tool_system.tool_failures import sanitize_failure_text, sanitize_failure_value

if TYPE_CHECKING:
    from types import ModuleType

    from mindroom.config.main import Config

ApprovalStatus = Literal["approved", "denied", "expired"]
PendingApprovalStatus = Literal["pending", "approved", "denied", "expired"]
MatrixEventSender = Callable[[str, str | None, str, dict[str, Any]], Awaitable["SentApprovalEvent | None"]]
MatrixEventEditor = Callable[[str, str, str, dict[str, Any]], Awaitable[bool]]

_APPROVALS_DIRNAME = "approvals"
_DEFAULT_CANCELLED_REASON = "Tool approval request was cancelled."
_DEFAULT_MISSING_CONTEXT_REASON = "Tool approval requires a Matrix room."
_DEFAULT_MISSING_REQUESTER_REASON = "Tool approval requires a human requester."
_DEFAULT_RESTART_REASON = "MindRoom restarted before approval completed."
_DEFAULT_REINITIALIZE_REASON = "MindRoom reinitialized before approval completed."
_DEFAULT_SEND_FAILURE_REASON = "Tool approval request could not be delivered to Matrix."
_DEFAULT_SHUTDOWN_REASON = "MindRoom shut down before approval completed."
_DEFAULT_TIMEOUT_REASON = "Tool approval request timed out."
_DEFAULT_UNDELIVERED_RESTART_REASON = "MindRoom restarted before approval request could be delivered to Matrix."
_DEFAULT_TRUNCATED_APPROVAL_REASON = (
    "Cannot approve: the displayed arguments are truncated. "
    "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
)
_APPROVE_REACTION_KEYS = frozenset({"✅"})
_MAX_ARGUMENTS_PREVIEW_CHARS = 1200
_MANAGER: ApprovalManager | None = None
_SCRIPT_CACHE: dict[tuple[str, int], ModuleType] = {}
_SCRIPT_CACHE_LOCK = threading.Lock()
logger = get_logger(__name__)


class ToolApprovalScriptError(RuntimeError):
    """One approval-script load or execution failure."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _compact_preview_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _json_preview_length(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _build_arguments_preview(arguments: dict[str, Any]) -> tuple[object, bool]:
    sanitized = sanitize_failure_value(arguments)
    preview_text = _compact_preview_text(sanitized)
    if len(preview_text) <= _MAX_ARGUMENTS_PREVIEW_CHARS:
        return sanitized, False
    preview = sanitize_failure_text(preview_text, max_length=_MAX_ARGUMENTS_PREVIEW_CHARS)
    while len(json.dumps(preview, ensure_ascii=False, sort_keys=True)) > _MAX_ARGUMENTS_PREVIEW_CHARS:
        overflow = len(json.dumps(preview, ensure_ascii=False, sort_keys=True)) - _MAX_ARGUMENTS_PREVIEW_CHARS
        next_max_length = max(len(preview) - overflow - 8, 1)
        next_preview = sanitize_failure_text(preview_text, max_length=next_max_length)
        if next_preview == preview:
            break
        preview = next_preview
    return preview, True


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


def _event_arguments_payload(pending: PendingApproval) -> tuple[dict[str, Any], bool]:
    return pending.event_arguments_payload, pending.event_arguments_truncated


def _load_event_arguments_payload(
    payload: dict[str, Any],
    *,
    arguments_preview_payload: object,
    arguments_preview_truncated: bool,
) -> tuple[dict[str, Any], bool]:
    event_arguments_payload = payload.get("event_arguments_payload")
    if isinstance(event_arguments_payload, dict):
        return cast("dict[str, Any]", event_arguments_payload), bool(payload.get("event_arguments_truncated"))

    legacy_arguments = payload.get("arguments")
    if isinstance(legacy_arguments, dict):
        return _build_event_arguments_preview(cast("dict[str, Any]", legacy_arguments))
    if isinstance(arguments_preview_payload, dict):
        return cast("dict[str, Any]", arguments_preview_payload), arguments_preview_truncated
    return {"value": arguments_preview_payload}, True


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """One resolved approval outcome."""

    status: ApprovalStatus
    reason: str | None
    resolved_by: str | None
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class SentApprovalEvent:
    """One delivered approval event plus the Matrix user that sent it."""

    event_id: str
    sender_user_id: str


@dataclass(frozen=True, slots=True)
class AnchoredApprovalActionResult:
    """One anchored approval-action outcome."""

    handled: bool
    error_reason: str | None = None
    thread_id: str | None = None
    notice_sender_user_id: str | None = None


@dataclass(slots=True)
class PendingApproval:
    """One approval request plus any live wait state."""

    id: str
    tool_name: str
    arguments: dict[str, Any]
    arguments_preview: object
    arguments_preview_truncated: bool
    event_arguments_payload: dict[str, Any]
    event_arguments_truncated: bool
    agent_name: str
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    approver_user_id: str
    original_event_sender_user_id: str | None
    matched_rule: str
    script_path: str | None
    requested_at: datetime
    expires_at: datetime
    future: Future[ApprovalDecision] | None = field(default=None, repr=False)
    status: PendingApprovalStatus = "pending"
    resolution_reason: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    event_id: str | None = None
    resolution_synced_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the persisted approval payload."""
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "arguments_preview": self.arguments_preview,
            "arguments_preview_truncated": self.arguments_preview_truncated,
            "event_arguments_payload": self.event_arguments_payload,
            "event_arguments_truncated": self.event_arguments_truncated,
            "agent_name": self.agent_name,
            "room_id": self.room_id,
            "thread_id": self.thread_id,
            "requester_id": self.requester_id,
            "approver_user_id": self.approver_user_id,
            "original_event_sender_user_id": self.original_event_sender_user_id,
            "matched_rule": self.matched_rule,
            "script_path": self.script_path,
            "requested_at": self.requested_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status,
            "resolution_reason": self.resolution_reason,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at is not None else None,
            "resolved_by": self.resolved_by,
            "event_id": self.event_id,
            "resolution_synced_at": (
                self.resolution_synced_at.isoformat() if self.resolution_synced_at is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PendingApproval:
        """Rebuild one approval payload from disk."""
        arguments_preview_payload = payload.get("arguments_preview")
        if arguments_preview_payload is None:
            legacy_arguments = payload.get("arguments")
            if isinstance(legacy_arguments, dict):
                arguments_preview_payload, arguments_preview_truncated = _build_arguments_preview(
                    cast("dict[str, Any]", legacy_arguments),
                )
            else:
                arguments_preview_payload = sanitize_failure_text(str(legacy_arguments or ""))
                arguments_preview_truncated = False
        else:
            arguments_preview_truncated = bool(payload.get("arguments_preview_truncated"))
        event_arguments_payload, event_arguments_truncated = _load_event_arguments_payload(
            payload,
            arguments_preview_payload=arguments_preview_payload,
            arguments_preview_truncated=arguments_preview_truncated,
        )
        event_id = cast("str | None", payload.get("event_id"))
        original_event_sender_user_id = cast("str | None", payload.get("original_event_sender_user_id"))
        if event_id is None and (
            not isinstance(original_event_sender_user_id, str) or not original_event_sender_user_id.strip()
        ):
            original_event_sender_user_id = None
        elif not isinstance(original_event_sender_user_id, str) or not original_event_sender_user_id.strip():
            msg = f"Persisted approval request '{payload.get('id')}' is missing original_event_sender_user_id."
            raise ValueError(msg)
        return cls(
            id=cast("str", payload["id"]),
            tool_name=cast("str", payload["tool_name"]),
            arguments={},
            arguments_preview=arguments_preview_payload,
            arguments_preview_truncated=arguments_preview_truncated,
            event_arguments_payload=event_arguments_payload,
            event_arguments_truncated=event_arguments_truncated,
            agent_name=cast("str", payload["agent_name"]),
            room_id=cast("str | None", payload.get("room_id")),
            thread_id=cast("str | None", payload.get("thread_id")),
            requester_id=cast("str | None", payload.get("requester_id")),
            approver_user_id=cast("str", payload["approver_user_id"]),
            original_event_sender_user_id=original_event_sender_user_id,
            matched_rule=cast("str", payload["matched_rule"]),
            script_path=cast("str | None", payload.get("script_path")),
            requested_at=cast("datetime", _parse_datetime(cast("str", payload["requested_at"]))),
            expires_at=cast("datetime", _parse_datetime(cast("str", payload["expires_at"]))),
            status=cast("PendingApprovalStatus", payload["status"]),
            resolution_reason=cast("str | None", payload.get("resolution_reason")),
            resolved_at=_parse_datetime(cast("str | None", payload.get("resolved_at"))),
            resolved_by=cast("str | None", payload.get("resolved_by")),
            event_id=event_id,
            resolution_synced_at=_parse_datetime(cast("str | None", payload.get("resolution_synced_at"))),
        )


class ApprovalManager:
    """Track live approvals, persist them, and reconcile Matrix cards."""

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        *,
        sender: MatrixEventSender | None = None,
        editor: MatrixEventEditor | None = None,
    ) -> None:
        self._runtime_storage_root = runtime_paths.storage_root
        self._storage_dir = runtime_paths.storage_root / _APPROVALS_DIRNAME
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._send_event = sender
        self._edit_event = editor
        self._state_lock = threading.Lock()
        self._requests_by_id: dict[str, PendingApproval] = {}
        self._pending_by_id: dict[str, PendingApproval] = {}
        self._approval_id_by_event_id: dict[str, str] = {}
        self._replay_in_progress: set[str] = set()
        self._load_existing()

    @property
    def runtime_storage_root(self) -> Path:
        """Return the runtime storage root bound to this manager."""
        return self._runtime_storage_root

    @property
    def storage_dir(self) -> Path:
        """Return the approvals persistence directory."""
        return self._storage_dir

    def configure_transport(
        self,
        *,
        sender: MatrixEventSender | None = None,
        editor: MatrixEventEditor | None = None,
    ) -> None:
        """Update the Matrix transport callbacks."""
        if sender is not None:
            self._send_event = sender
        if editor is not None:
            self._edit_event = editor

    def _request_path(self, approval_id: str) -> Path:
        return self._storage_dir / f"{approval_id}.json"

    def _persist_request(self, pending: PendingApproval) -> None:
        target_path = self._request_path(pending.id)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self._storage_dir,
            prefix=f"{pending.id}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(pending.to_dict(), handle, sort_keys=True)
            handle.write("\n")
            tmp_path = Path(handle.name)
        safe_replace(tmp_path, target_path)

    def _delete_request_file(self, approval_id: str) -> None:
        self._request_path(approval_id).unlink(missing_ok=True)

    def _store_request(self, pending: PendingApproval) -> None:
        with self._state_lock:
            self._requests_by_id[pending.id] = pending
            if pending.status == "pending":
                self._pending_by_id[pending.id] = pending
            if pending.event_id is not None:
                self._approval_id_by_event_id[pending.event_id] = pending.id

    def _pending_request(self, approval_id: str) -> PendingApproval | None:
        with self._state_lock:
            return self._pending_by_id.get(approval_id)

    def anchored_request_for_event(
        self,
        *,
        approval_event_id: str,
        room_id: str,
    ) -> PendingApproval | None:
        """Return one approval card anchored to the given room event."""
        return self._anchored_request(approval_event_id=approval_event_id, room_id=room_id)

    def _anchored_request(
        self,
        *,
        approval_event_id: str,
        room_id: str,
    ) -> PendingApproval | None:
        with self._state_lock:
            approval_id = self._approval_id_by_event_id.get(approval_event_id)
            if approval_id is None:
                return None
            pending = self._requests_by_id.get(approval_id)
            if pending is None or pending.event_id != approval_event_id or pending.room_id != room_id:
                return None
            if pending.status != "pending" and pending.resolution_synced_at is not None:
                return None
            return pending

    def _pending_ids_snapshot(self) -> list[str]:
        with self._state_lock:
            return list(self._pending_by_id)

    def _pending_requests_snapshot(self) -> list[PendingApproval]:
        with self._state_lock:
            return list(self._pending_by_id.values())

    def _set_event_delivery(self, approval_id: str, event_id: str, sender_user_id: str) -> None:
        with self._state_lock:
            pending = self._requests_by_id.get(approval_id)
            if pending is None:
                return
            if pending.event_id is not None:
                self._approval_id_by_event_id.pop(pending.event_id, None)
            pending.event_id = event_id
            pending.original_event_sender_user_id = sender_user_id
            self._approval_id_by_event_id[event_id] = approval_id

    def _load_existing(self) -> None:
        loaded_requests: dict[str, PendingApproval] = {}
        for request_path in sorted(self._storage_dir.glob("*.json")):
            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
                pending = PendingApproval.from_dict(cast("dict[str, Any]", payload))
            except Exception:
                logger.exception("Failed to load persisted approval request", path=str(request_path))
                continue
            loaded_requests[pending.id] = pending

        for pending in loaded_requests.values():
            if pending.status == "pending":
                pending.status = "expired"
                pending.resolution_reason = (
                    _DEFAULT_RESTART_REASON if pending.event_id is not None else _DEFAULT_UNDELIVERED_RESTART_REASON
                )
                pending.resolved_at = _utcnow()
                pending.resolved_by = None
                pending.resolution_synced_at = None
                if pending.event_id is None:
                    self._delete_request_file(pending.id)
                else:
                    self._persist_request(pending)

        with self._state_lock:
            self._requests_by_id = loaded_requests
            self._pending_by_id = {
                approval_id: pending for approval_id, pending in loaded_requests.items() if pending.status == "pending"
            }
            self._approval_id_by_event_id = {
                pending.event_id: pending.id for pending in loaded_requests.values() if pending.event_id is not None
            }

    def get_request(self, approval_id: str) -> PendingApproval | None:
        """Return one pending approval by ID."""
        return self._pending_request(approval_id)

    def list_pending(self) -> list[PendingApproval]:
        """Return pending approvals ordered by request time."""
        with self._state_lock:
            pending = [approval for approval in self._pending_by_id.values() if approval.status == "pending"]
        return sorted(pending, key=lambda approval: approval.requested_at)

    def list_unsynced_resolved(self) -> list[PendingApproval]:
        """Return resolved approvals whose Matrix cards still need one edit."""
        with self._state_lock:
            requests = [
                approval
                for approval in self._requests_by_id.values()
                if (
                    approval.status != "pending"
                    and approval.room_id is not None
                    and approval.event_id is not None
                    and approval.resolution_synced_at is None
                )
            ]
        return sorted(requests, key=lambda approval: approval.resolved_at or approval.requested_at)

    def _claim_unsynced_resolved_replay(self, approval_id: str) -> PendingApproval | None:
        with self._state_lock:
            pending = self._requests_by_id.get(approval_id)
            if (
                pending is None
                or pending.status == "pending"
                or pending.event_id is None
                or pending.resolution_synced_at is not None
                or approval_id in self._replay_in_progress
            ):
                return None
            self._replay_in_progress.add(approval_id)
            return pending

    def _finish_unsynced_resolved_replay(self, approval_id: str) -> None:
        with self._state_lock:
            self._replay_in_progress.discard(approval_id)

    def _preflight_request_decision(
        self,
        *,
        room_id: str | None,
        thread_id: str | None,
        approver_user_id: str | None,
    ) -> ApprovalDecision | None:
        del thread_id
        if room_id is None:
            return self._new_decision(status="denied", reason=_DEFAULT_MISSING_CONTEXT_REASON, resolved_by=None)
        if approver_user_id is None:
            return self._new_decision(status="denied", reason=_DEFAULT_MISSING_REQUESTER_REASON, resolved_by=None)
        if self._send_event is None:
            return self._new_decision(status="expired", reason=_DEFAULT_SEND_FAILURE_REASON, resolved_by=None)
        return None

    async def request_approval(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        agent_name: str,
        transport_agent_name: str,
        room_id: str | None,
        thread_id: str | None,
        requester_id: str | None,
        approver_user_id: str | None,
        matched_rule: str,
        script_path: str | None,
        timeout_seconds: float,
    ) -> ApprovalDecision:
        """Send one Matrix approval card and wait for a decision."""
        preflight_decision = self._preflight_request_decision(
            room_id=room_id,
            thread_id=thread_id,
            approver_user_id=approver_user_id,
        )
        if preflight_decision is not None:
            return preflight_decision
        assert approver_user_id is not None
        assert room_id is not None
        assert self._send_event is not None

        requested_at = _utcnow()
        arguments_preview, arguments_preview_truncated = _build_arguments_preview(arguments)
        event_arguments_payload, event_arguments_truncated = _build_event_arguments_preview(arguments)
        pending = PendingApproval(
            id=uuid4().hex,
            tool_name=tool_name,
            arguments=arguments,
            arguments_preview=arguments_preview,
            arguments_preview_truncated=arguments_preview_truncated,
            event_arguments_payload=event_arguments_payload,
            event_arguments_truncated=event_arguments_truncated,
            agent_name=agent_name,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            approver_user_id=approver_user_id,
            original_event_sender_user_id=None,
            matched_rule=matched_rule,
            script_path=script_path,
            requested_at=requested_at,
            expires_at=requested_at + timedelta(seconds=max(timeout_seconds, 0.0)),
            future=Future(),
        )
        self._store_request(pending)
        self._persist_request(pending)

        sent_event: SentApprovalEvent | None = None
        try:
            try:
                sent_event = await self._send_event(
                    room_id,
                    thread_id,
                    transport_agent_name,
                    self._pending_event_content(pending),
                )
            except asyncio.CancelledError:
                applied_decision = self._apply_decision(
                    pending.id,
                    status="expired",
                    reason=_DEFAULT_CANCELLED_REASON,
                    resolved_by=None,
                )
                if applied_decision is not None:
                    self._persist_request(applied_decision[0])
                self._discard(pending.id)
                raise
        except Exception:
            logger.warning(
                "Failed to send approval Matrix event",
                approval_id=pending.id,
                room_id=room_id,
                thread_id=thread_id,
                agent_name=transport_agent_name,
                exc_info=True,
            )
        if sent_event is None:
            logger.warning(
                "Approval Matrix event was not delivered",
                approval_id=pending.id,
                room_id=room_id,
                thread_id=thread_id,
                agent_name=transport_agent_name,
            )
            decision = await self._resolve_pending(
                pending.id,
                status="expired",
                reason=_DEFAULT_SEND_FAILURE_REASON,
                resolved_by=None,
            )
            self._discard(pending.id)
            return decision or self._decision_from_pending(pending)

        self._set_event_delivery(pending.id, sent_event.event_id, sent_event.sender_user_id)
        self._persist_request(pending)
        if pending.status != "pending" and pending.resolution_synced_at is None:
            await self._edit_resolved_event(pending)

        try:
            return await self._await_approval_decision(pending)
        finally:
            self._discard(pending.id)

    async def handle_approval_resolution(
        self,
        *,
        approval_id: str,
        status: Literal["approved", "denied"],
        reason: str | None,
        resolved_by: str,
    ) -> bool:
        """Resolve one approval from a Matrix reaction, reply, or custom event."""
        pending = self._pending_request(approval_id)
        if pending is None or pending.approver_user_id != resolved_by:
            return False
        return (
            await self._resolve_pending(
                approval_id,
                status=status,
                reason=reason,
                resolved_by=resolved_by,
            )
            is not None
        )

    async def _handle_anchored_resolution(
        self,
        *,
        approval_event_id: str,
        room_id: str,
        status: Literal["approved", "denied"],
        reason: str | None,
        resolved_by: str,
        handled_on_truncated_approval: bool = True,
    ) -> AnchoredApprovalActionResult:
        """Resolve one Matrix-anchored approval action against the original approval card."""
        pending = self._anchored_request(
            approval_event_id=approval_event_id,
            room_id=room_id,
        )
        if pending is None:
            return AnchoredApprovalActionResult(handled=False)
        if pending.status != "pending":
            return AnchoredApprovalActionResult(
                handled=pending.resolution_synced_at is None and pending.approver_user_id == resolved_by,
            )
        if pending.approver_user_id != resolved_by:
            return AnchoredApprovalActionResult(handled=False)
        if status == "approved" and pending.event_arguments_truncated:
            return AnchoredApprovalActionResult(
                handled=handled_on_truncated_approval,
                error_reason=_DEFAULT_TRUNCATED_APPROVAL_REASON,
                thread_id=pending.thread_id,
                notice_sender_user_id=pending.original_event_sender_user_id,
            )

        if (
            await self._resolve_pending(
                pending.id,
                status=status,
                reason=reason,
                resolved_by=resolved_by,
            )
            is not None
        ):
            return AnchoredApprovalActionResult(handled=True)
        refreshed = self._anchored_request(
            approval_event_id=approval_event_id,
            room_id=room_id,
        )
        return AnchoredApprovalActionResult(
            handled=refreshed is not None and refreshed.status != "pending" and refreshed.resolution_synced_at is None,
        )

    async def handle_reaction(
        self,
        *,
        approval_event_id: str,
        room_id: str,
        reaction_key: str,
        resolved_by: str,
    ) -> AnchoredApprovalActionResult:
        """Approve one request from a reaction on the approval card."""
        if reaction_key not in _APPROVE_REACTION_KEYS:
            return AnchoredApprovalActionResult(handled=False)
        return await self._handle_anchored_resolution(
            approval_event_id=approval_event_id,
            room_id=room_id,
            status="approved",
            reason=None,
            resolved_by=resolved_by,
        )

    async def handle_reply(
        self,
        *,
        approval_event_id: str,
        room_id: str,
        reason: str | None,
        resolved_by: str,
    ) -> AnchoredApprovalActionResult:
        """Deny one request from a reply to the approval card."""
        trimmed_reason = reason.strip() if isinstance(reason, str) else ""
        return await self._handle_anchored_resolution(
            approval_event_id=approval_event_id,
            room_id=room_id,
            status="denied",
            reason=trimmed_reason or None,
            resolved_by=resolved_by,
        )

    async def handle_custom_response(
        self,
        *,
        approval_event_id: str,
        room_id: str,
        status: Literal["approved", "denied"],
        reason: str | None,
        resolved_by: str,
    ) -> AnchoredApprovalActionResult:
        """Resolve one custom approval response anchored to the original approval card."""
        trimmed_reason = reason.strip() if isinstance(reason, str) else ""
        return await self._handle_anchored_resolution(
            approval_event_id=approval_event_id,
            room_id=room_id,
            status=status,
            reason=trimmed_reason or None,
            resolved_by=resolved_by,
            handled_on_truncated_approval=False,
        )

    async def approve(
        self,
        approval_id: str,
        *,
        resolved_by: str,
    ) -> PendingApproval:
        """Approve one pending request directly."""
        return await self._resolve_for_callsite(
            approval_id,
            status="approved",
            reason=None,
            resolved_by=resolved_by,
        )

    async def deny(
        self,
        approval_id: str,
        *,
        reason: str | None = None,
        resolved_by: str,
    ) -> PendingApproval:
        """Deny one pending request directly."""
        return await self._resolve_for_callsite(
            approval_id,
            status="denied",
            reason=reason,
            resolved_by=resolved_by,
        )

    async def expire(
        self,
        approval_id: str,
        *,
        reason: str | None = None,
    ) -> PendingApproval:
        """Expire one pending request directly."""
        return await self._resolve_for_callsite(
            approval_id,
            status="expired",
            reason=reason,
            resolved_by=None,
        )

    async def shutdown(self, *, reason: str = _DEFAULT_SHUTDOWN_REASON) -> None:
        """Expire every live approval and update the corresponding Matrix cards."""
        for approval_id in self._pending_ids_snapshot():
            await self._resolve_pending(
                approval_id,
                status="expired",
                reason=reason,
                resolved_by=None,
            )
            self._discard(approval_id)

    def abort_pending(self, *, reason: str) -> None:
        """Expire every live approval without awaiting Matrix edits."""
        for pending in self._pending_requests_snapshot():
            decision = self._apply_decision(
                pending.id,
                status="expired",
                reason=reason,
                resolved_by=None,
            )
            if decision is not None:
                self._persist_request(pending)
            self._discard(pending.id)

    async def _resolve_for_callsite(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> PendingApproval:
        pending = self._pending_request(approval_id)
        if pending is None:
            msg = f"Approval request '{approval_id}' was not found."
            raise LookupError(msg)
        if pending.status != "pending":
            msg = f"Approval request '{approval_id}' is already {pending.status}."
            raise ValueError(msg)
        if status in {"approved", "denied"}:
            if not resolved_by:
                msg = f"Approval request '{approval_id}' requires the original requester to resolve it."
                raise PermissionError(msg)
            if resolved_by != pending.approver_user_id:
                msg = f"Approval request '{approval_id}' can only be resolved by the original requester."
                raise PermissionError(msg)
        await self._resolve_pending(
            approval_id,
            status=status,
            reason=reason,
            resolved_by=resolved_by,
        )
        return pending

    async def _resolve_pending(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> ApprovalDecision | None:
        applied_decision = self._apply_decision(
            approval_id,
            status=status,
            reason=reason,
            resolved_by=resolved_by,
        )
        if applied_decision is None:
            return None
        pending, decision = applied_decision

        self._persist_request(pending)
        await self._edit_resolved_event(pending)
        return decision

    def _apply_decision(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> tuple[PendingApproval, ApprovalDecision] | None:
        with self._state_lock:
            pending = self._pending_by_id.get(approval_id)
            if pending is None:
                return None
            future = pending.future
            if pending.status != "pending" or (future is not None and future.done()):
                return None

            decision = self._new_decision(status=status, reason=reason, resolved_by=resolved_by)
            if future is not None:
                try:
                    future.set_result(decision)
                except InvalidStateError:
                    return None
            pending.status = status
            pending.resolution_reason = reason
            pending.resolved_at = decision.resolved_at
            pending.resolved_by = resolved_by
            pending.resolution_synced_at = None
            pending.arguments = {}
            return pending, decision

    async def _edit_resolved_event(
        self,
        pending: PendingApproval,
    ) -> None:
        if (
            self._edit_event is None
            or pending.room_id is None
            or pending.event_id is None
            or not isinstance(pending.original_event_sender_user_id, str)
            or not pending.original_event_sender_user_id
        ):
            return
        try:
            delivered = await self._edit_event(
                pending.room_id,
                pending.event_id,
                pending.original_event_sender_user_id,
                self._resolved_event_content(pending),
            )
        except Exception:
            logger.warning(
                "Failed to edit approval Matrix event",
                approval_id=pending.id,
                room_id=pending.room_id,
                event_id=pending.event_id,
                exc_info=True,
            )
            return
        if not delivered:
            return
        pending.resolution_synced_at = _utcnow()
        self._persist_request(pending)
        self._discard(pending.id)

    async def _await_approval_decision(self, pending: PendingApproval) -> ApprovalDecision:
        """Wait for one approval result using the already-advertised absolute expiry."""
        try:
            assert pending.future is not None
            if pending.future.done():
                return pending.future.result()
            remaining_seconds = self._remaining_timeout_seconds(pending)
            if remaining_seconds <= 0:
                decision = await self._resolve_pending(
                    pending.id,
                    status="expired",
                    reason=_DEFAULT_TIMEOUT_REASON,
                    resolved_by=None,
                )
                return decision or self._decision_from_pending(pending)
            wrapped_future = asyncio.wrap_future(pending.future)
            return await asyncio.wait_for(asyncio.shield(wrapped_future), timeout=remaining_seconds)
        except TimeoutError:
            decision = await self._resolve_pending(
                pending.id,
                status="expired",
                reason=_DEFAULT_TIMEOUT_REASON,
                resolved_by=None,
            )
            return decision or self._decision_from_pending(pending)
        except asyncio.CancelledError:
            await self._resolve_pending(
                pending.id,
                status="expired",
                reason=_DEFAULT_CANCELLED_REASON,
                resolved_by=None,
            )
            raise

    def _discard(self, approval_id: str) -> None:
        delete_request_file = False
        with self._state_lock:
            pending = self._pending_by_id.pop(approval_id, None)
            if pending is None:
                pending = self._requests_by_id.get(approval_id)
            if pending is None:
                return
            if pending.status == "pending":
                self._pending_by_id[approval_id] = pending
                if pending.event_id is not None:
                    self._approval_id_by_event_id[pending.event_id] = approval_id
                return
            if pending.event_id is None:
                self._requests_by_id.pop(approval_id, None)
                delete_request_file = True
            elif pending.resolution_synced_at is None:
                self._approval_id_by_event_id[pending.event_id] = approval_id
                return
            else:
                self._approval_id_by_event_id.pop(pending.event_id, None)
                self._requests_by_id.pop(approval_id, None)
                delete_request_file = True
        if delete_request_file:
            self._delete_request_file(approval_id)

    async def sync_unsynced_resolved(self) -> list[PendingApproval]:
        """Replay any resolved approval cards that were never edited in Matrix."""
        synced_requests: list[PendingApproval] = []
        for pending in self.list_unsynced_resolved():
            claimed_pending = self._claim_unsynced_resolved_replay(pending.id)
            if claimed_pending is None:
                continue
            try:
                previous_synced_at = claimed_pending.resolution_synced_at
                await self._edit_resolved_event(claimed_pending)
                if claimed_pending.resolution_synced_at != previous_synced_at:
                    synced_requests.append(claimed_pending)
            finally:
                self._finish_unsynced_resolved_replay(pending.id)
        return synced_requests

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

    @staticmethod
    def _decision_from_pending(pending: PendingApproval) -> ApprovalDecision:
        resolved_at = pending.resolved_at or _utcnow()
        status: ApprovalStatus = pending.status if pending.status != "pending" else "expired"
        return ApprovalDecision(
            status=status,
            reason=pending.resolution_reason,
            resolved_by=pending.resolved_by,
            resolved_at=resolved_at,
        )

    @staticmethod
    def _remaining_timeout_seconds(pending: PendingApproval) -> float:
        """Return the remaining time before the advertised approval deadline."""
        return max(0.0, (pending.expires_at - _utcnow()).total_seconds())

    @staticmethod
    def _event_body(tool_name: str, status: PendingApprovalStatus) -> str:
        if status == "approved":
            return f"Approved: {tool_name}"
        if status == "denied":
            return f"Denied: {tool_name}"
        if status == "expired":
            return f"Expired: {tool_name}"
        return f"🔒 Approval required: {tool_name}"

    def _pending_event_content(self, pending: PendingApproval) -> dict[str, Any]:
        event_arguments, arguments_truncated = _event_arguments_payload(pending)
        content: dict[str, Any] = {
            "msgtype": "io.mindroom.tool_approval",
            "body": self._event_body(pending.tool_name, pending.status),
            "tool_name": pending.tool_name,
            "tool_call_id": pending.id,
            "arguments": event_arguments,
            "agent_name": pending.agent_name,
            "status": pending.status,
            "approval_id": pending.id,
            "requested_at": pending.requested_at.isoformat(),
            "expires_at": pending.expires_at.isoformat(),
            "thread_id": pending.thread_id,
        }
        if arguments_truncated:
            content["arguments_truncated"] = True
        if pending.requester_id is not None:
            content["requester_id"] = pending.requester_id
        return content

    def _resolved_event_content(
        self,
        pending: PendingApproval,
    ) -> dict[str, Any]:
        content = self._pending_event_content(pending)
        content["body"] = self._event_body(pending.tool_name, pending.status)
        content["status"] = pending.status
        content["resolved_at"] = pending.resolved_at.isoformat() if pending.resolved_at is not None else None
        content["resolved_by"] = pending.resolved_by
        if pending.resolution_reason:
            content["resolution_reason"] = pending.resolution_reason
        else:
            content.pop("resolution_reason", None)
        return content


def _check_callable_from_module(
    module: ModuleType,
    resolved_path: Path,
) -> Callable[[str, dict[str, Any], str], bool] | Callable[[str, dict[str, Any], str], Awaitable[bool]]:
    check = getattr(module, "check", None)
    if not callable(check):
        msg = f"Approval script '{resolved_path}' must define callable check(tool_name, arguments, agent_name)."
        raise ToolApprovalScriptError(msg)
    return cast(
        "Callable[[str, dict[str, Any], str], bool] | Callable[[str, dict[str, Any], str], Awaitable[bool]]",
        check,
    )


def _load_script_module(
    script: str,
    runtime_paths: RuntimePaths,
) -> tuple[ModuleType, Path]:
    resolved_path = resolve_config_relative_path(script, runtime_paths)
    if not resolved_path.is_file():
        msg = f"Approval script '{resolved_path}' was not found."
        raise ToolApprovalScriptError(msg)

    mtime_ns = resolved_path.stat().st_mtime_ns
    cache_key = (str(resolved_path), mtime_ns)
    with _SCRIPT_CACHE_LOCK:
        cached_module = _SCRIPT_CACHE.get(cache_key)
    if cached_module is not None:
        return cached_module, resolved_path

    spec = importlib.util.spec_from_file_location(f"mindroom_tool_approval_{uuid4().hex}", resolved_path)
    if spec is None or spec.loader is None:
        msg = f"Approval script '{resolved_path}' could not be loaded."
        raise ToolApprovalScriptError(msg)

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        msg = f"Approval script '{resolved_path}' failed to import: {exc!s}"
        raise ToolApprovalScriptError(msg) from exc

    with _SCRIPT_CACHE_LOCK:
        cached_module = _SCRIPT_CACHE.get(cache_key)
        if cached_module is not None:
            return cached_module, resolved_path
        stale_keys = [key for key in _SCRIPT_CACHE if key[0] == str(resolved_path) and key != cache_key]
        for stale_key in stale_keys:
            _SCRIPT_CACHE.pop(stale_key, None)
        _SCRIPT_CACHE[cache_key] = module
    return module, resolved_path


def tool_requires_approval_for_openai_compat(
    config: Config,
    tool_name: str,
) -> bool:
    """Return whether one `/v1` tool must be hidden because approval may be required.

    This is a conservative static check used while constructing OpenAI-compatible
    agent tool schemas. Script-based rules are treated as requiring approval
    because `/v1` has no Matrix approval transport and cannot safely defer that
    decision to request-time arguments.
    """
    approval_config = config.tool_approval
    require_approval = approval_config.default == "require_approval"

    for rule in approval_config.rules:
        if not fnmatchcase(tool_name, rule.match):
            continue
        if rule.action is not None:
            return rule.action == "require_approval"
        return True

    return require_approval


def resolve_tool_approval_approver(
    config: Config,
    runtime_paths: RuntimePaths,
    requester_id: str | None,
) -> str | None:
    """Return the human requester allowed to resolve one approval request."""
    if requester_id is None or not requester_id.startswith("@") or ":" not in requester_id:
        return None
    if is_agent_id(requester_id, config, runtime_paths):
        return None
    if requester_id in config.bot_accounts:
        return None
    if requester_id == config.get_mindroom_user_id(runtime_paths):
        return None
    return requester_id


async def evaluate_tool_approval(
    config: Config,
    runtime_paths: RuntimePaths,
    tool_name: str,
    arguments: dict[str, Any],
    agent_name: str,
) -> tuple[bool, str, str | None, float]:
    """Return the approval decision for one tool call."""
    approval_config = config.tool_approval
    require_approval = approval_config.default == "require_approval"
    matched_rule = "<default>"
    script_path: str | None = None
    timeout_seconds = approval_config.timeout_days * 24 * 60 * 60

    for rule in approval_config.rules:
        if not fnmatchcase(tool_name, rule.match):
            continue
        matched_rule = rule.match
        if rule.timeout_days is not None:
            timeout_seconds = rule.timeout_days * 24 * 60 * 60
        if rule.action is not None:
            return rule.action == "require_approval", matched_rule, None, timeout_seconds

        assert rule.script is not None
        module, resolved_path = _load_script_module(rule.script, runtime_paths)
        script_path = str(resolved_path)
        check = _check_callable_from_module(module, resolved_path)
        try:
            result = check(tool_name, arguments, agent_name)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            logger.warning("Approval script raised", script_path=str(resolved_path), exc_info=True)
            msg = f"Approval script '{resolved_path}' failed with {type(exc).__name__}"
            raise ToolApprovalScriptError(msg) from exc
        if not isinstance(result, bool):
            msg = f"Approval script '{resolved_path}' returned a non-bool result."
            raise ToolApprovalScriptError(msg)
        return result, matched_rule, script_path, timeout_seconds

    return require_approval, matched_rule, script_path, timeout_seconds


def get_approval_store() -> ApprovalManager | None:
    """Return the module-level approval manager when initialized."""
    return _MANAGER


async def sync_unsynced_approval_event_resolutions() -> list[PendingApproval]:
    """Replay any resolved approval cards that were not edited before restart."""
    manager = get_approval_store()
    if manager is None:
        return []
    return await manager.sync_unsynced_resolved()


def initialize_approval_store(
    runtime_paths: RuntimePaths,
    *,
    sender: MatrixEventSender | None = None,
    editor: MatrixEventEditor | None = None,
) -> ApprovalManager:
    """Initialize the module-level approval manager for one runtime context."""
    global _MANAGER

    storage_dir = runtime_paths.storage_root / _APPROVALS_DIRNAME
    if _MANAGER is not None and _MANAGER.storage_dir == storage_dir:
        _MANAGER.configure_transport(sender=sender, editor=editor)
        return _MANAGER

    if _MANAGER is not None:
        _MANAGER.abort_pending(reason=_DEFAULT_REINITIALIZE_REASON)

    _MANAGER = ApprovalManager(runtime_paths, sender=sender, editor=editor)
    return _MANAGER


async def shutdown_approval_store(
    reason: str = _DEFAULT_SHUTDOWN_REASON,
) -> None:
    """Expire pending approvals and drop the module-level manager."""
    global _MANAGER

    manager = _MANAGER
    if manager is None:
        _SCRIPT_CACHE.clear()
        return

    await manager.shutdown(reason=reason)
    _MANAGER = None
    _SCRIPT_CACHE.clear()
