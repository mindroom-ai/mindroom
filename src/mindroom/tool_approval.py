"""Tool-call approval evaluation and Matrix-only approval management."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from mindroom.config.main import Config

ApprovalStatus = Literal["approved", "denied", "expired"]
PendingApprovalStatus = Literal["pending", "approved", "denied", "expired"]
MatrixEventSender = Callable[[str, str, str, dict[str, Any]], Awaitable[str | None]]
MatrixEventEditor = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]

_DEFAULT_CANCELLED_REASON = "Tool approval request was cancelled."
_DEFAULT_MISSING_CONTEXT_REASON = "Tool approval requires a Matrix room and thread."
_DEFAULT_REINITIALIZE_REASON = "MindRoom reinitialized before approval completed."
_DEFAULT_SEND_FAILURE_REASON = "Tool approval request could not be delivered to Matrix."
_DEFAULT_SHUTDOWN_REASON = "MindRoom shut down before approval completed."
_DEFAULT_TIMEOUT_REASON = "Tool approval request timed out."
_APPROVE_REACTION_KEYS = frozenset({"✅"})
_MANAGER: ApprovalManager | None = None
_SCRIPT_CACHE: dict[tuple[str, int], ModuleType] = {}
logger = get_logger(__name__)


class ToolApprovalScriptError(RuntimeError):
    """One approval-script load or execution failure."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """One resolved approval outcome."""

    status: ApprovalStatus
    reason: str | None
    resolved_by: str | None
    resolved_at: datetime


@dataclass(slots=True)
class PendingApproval:
    """One live approval request awaiting a human decision."""

    id: str
    tool_name: str
    arguments: dict[str, Any]
    agent_name: str
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    matched_rule: str
    script_path: str | None
    requested_at: datetime
    expires_at: datetime
    future: asyncio.Future[ApprovalDecision] = field(repr=False)
    status: PendingApprovalStatus = "pending"
    resolution_reason: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    event_id: str | None = None


class ApprovalManager:
    """Track live approvals in memory and mirror them into Matrix."""

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        *,
        sender: MatrixEventSender | None = None,
        editor: MatrixEventEditor | None = None,
    ) -> None:
        self._runtime_storage_root = runtime_paths.storage_root
        self._send_event = sender
        self._edit_event = editor
        self._pending_by_id: dict[str, PendingApproval] = {}
        self._approval_id_by_event_id: dict[str, str] = {}

    @property
    def runtime_storage_root(self) -> Path:
        """Return the runtime storage root bound to this manager."""
        return self._runtime_storage_root

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

    def get_request(self, approval_id: str) -> PendingApproval | None:
        """Return one pending approval by ID."""
        return self._pending_by_id.get(approval_id)

    def list_pending(self) -> list[PendingApproval]:
        """Return pending approvals ordered by request time."""
        pending = [approval for approval in self._pending_by_id.values() if approval.status == "pending"]
        return sorted(pending, key=lambda approval: approval.requested_at)

    def approval_id_for_event(self, event_id: str) -> str | None:
        """Return the approval ID for one Matrix approval event."""
        return self._approval_id_by_event_id.get(event_id)

    async def request_approval(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        agent_name: str,
        room_id: str | None,
        thread_id: str | None,
        requester_id: str | None,
        matched_rule: str,
        script_path: str | None,
        timeout_seconds: float,
    ) -> ApprovalDecision:
        """Send one Matrix approval card and wait for a decision."""
        if room_id is None or thread_id is None:
            return self._new_decision(status="denied", reason=_DEFAULT_MISSING_CONTEXT_REASON, resolved_by=None)
        if self._send_event is None:
            return self._new_decision(status="expired", reason=_DEFAULT_SEND_FAILURE_REASON, resolved_by=None)

        requested_at = _utcnow()
        pending = PendingApproval(
            id=uuid4().hex,
            tool_name=tool_name,
            arguments=arguments,
            agent_name=agent_name,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            matched_rule=matched_rule,
            script_path=script_path,
            requested_at=requested_at,
            expires_at=requested_at + timedelta(seconds=max(timeout_seconds, 0.0)),
            future=asyncio.get_running_loop().create_future(),
        )

        try:
            event_id = await self._send_event(
                room_id,
                thread_id,
                agent_name,
                self._pending_event_content(pending),
            )
        except Exception:
            logger.warning(
                "Failed to send approval Matrix event",
                approval_id=pending.id,
                room_id=room_id,
                thread_id=thread_id,
                agent_name=agent_name,
                exc_info=True,
            )
            return self._new_decision(status="expired", reason=_DEFAULT_SEND_FAILURE_REASON, resolved_by=None)

        if not event_id:
            return self._new_decision(status="expired", reason=_DEFAULT_SEND_FAILURE_REASON, resolved_by=None)

        pending.event_id = event_id
        self._pending_by_id[pending.id] = pending
        self._approval_id_by_event_id[event_id] = pending.id

        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout=max(timeout_seconds, 0.0))
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
        return (
            await self._resolve_pending(
                approval_id,
                status=status,
                reason=reason,
                resolved_by=resolved_by,
            )
            is not None
        )

    async def handle_reaction(
        self,
        *,
        approval_event_id: str,
        reaction_key: str,
        resolved_by: str,
    ) -> bool:
        """Approve one request from a reaction on the approval card."""
        if reaction_key not in _APPROVE_REACTION_KEYS:
            return False
        approval_id = self._approval_id_by_event_id.get(approval_event_id)
        if approval_id is None:
            return False
        return await self.handle_approval_resolution(
            approval_id=approval_id,
            status="approved",
            reason=None,
            resolved_by=resolved_by,
        )

    async def handle_reply(
        self,
        *,
        approval_event_id: str,
        reason: str | None,
        resolved_by: str,
    ) -> bool:
        """Deny one request from a reply to the approval card."""
        approval_id = self._approval_id_by_event_id.get(approval_event_id)
        if approval_id is None:
            return False
        trimmed_reason = reason.strip() if isinstance(reason, str) else ""
        return await self.handle_approval_resolution(
            approval_id=approval_id,
            status="denied",
            reason=trimmed_reason or None,
            resolved_by=resolved_by,
        )

    async def approve(
        self,
        approval_id: str,
        *,
        resolved_by: str | None = None,
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
        resolved_by: str | None = None,
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
        for approval_id in list(self._pending_by_id):
            await self._resolve_pending(
                approval_id,
                status="expired",
                reason=reason,
                resolved_by=None,
            )
            self._discard(approval_id)

    def abort_pending(self, *, reason: str) -> None:
        """Expire every live approval without awaiting Matrix edits."""
        for pending in list(self._pending_by_id.values()):
            self._apply_decision(
                pending,
                status="expired",
                reason=reason,
                resolved_by=None,
            )
        self._pending_by_id.clear()
        self._approval_id_by_event_id.clear()

    async def _resolve_for_callsite(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> PendingApproval:
        pending = self._pending_by_id.get(approval_id)
        if pending is None:
            msg = f"Approval request '{approval_id}' was not found."
            raise LookupError(msg)
        if pending.status != "pending":
            msg = f"Approval request '{approval_id}' is already {pending.status}."
            raise ValueError(msg)
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
        pending = self._pending_by_id.get(approval_id)
        if pending is None:
            return None

        decision = self._apply_decision(
            pending,
            status=status,
            reason=reason,
            resolved_by=resolved_by,
        )
        if decision is None:
            return None

        await self._edit_resolved_event(pending, decision)
        return decision

    def _apply_decision(
        self,
        pending: PendingApproval,
        *,
        status: ApprovalStatus,
        reason: str | None,
        resolved_by: str | None,
    ) -> ApprovalDecision | None:
        if pending.status != "pending" or pending.future.done():
            return None

        decision = self._new_decision(status=status, reason=reason, resolved_by=resolved_by)
        pending.status = status
        pending.resolution_reason = reason
        pending.resolved_at = decision.resolved_at
        pending.resolved_by = resolved_by
        pending.future.set_result(decision)
        return decision

    async def _edit_resolved_event(
        self,
        pending: PendingApproval,
        decision: ApprovalDecision,
    ) -> None:
        if self._edit_event is None or pending.room_id is None or pending.event_id is None:
            return
        try:
            await self._edit_event(
                pending.room_id,
                pending.event_id,
                pending.agent_name,
                self._resolved_event_content(pending, decision),
            )
        except Exception:
            logger.warning(
                "Failed to edit approval Matrix event",
                approval_id=pending.id,
                room_id=pending.room_id,
                event_id=pending.event_id,
                agent_name=pending.agent_name,
                exc_info=True,
            )

    def _discard(self, approval_id: str) -> None:
        pending = self._pending_by_id.pop(approval_id, None)
        if pending is None or pending.event_id is None:
            return
        self._approval_id_by_event_id.pop(pending.event_id, None)

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
    def _event_body(tool_name: str, status: PendingApprovalStatus) -> str:
        if status == "approved":
            return f"Approved: {tool_name}"
        if status == "denied":
            return f"Denied: {tool_name}"
        if status == "expired":
            return f"Expired: {tool_name}"
        return f"🔒 Approval required: {tool_name}"

    def _pending_event_content(self, pending: PendingApproval) -> dict[str, Any]:
        content: dict[str, Any] = {
            "msgtype": "io.mindroom.tool_approval",
            "body": self._event_body(pending.tool_name, pending.status),
            "tool_name": pending.tool_name,
            "tool_call_id": pending.id,
            "arguments": pending.arguments,
            "agent_name": pending.agent_name,
            "status": pending.status,
            "approval_id": pending.id,
            "requested_at": pending.requested_at.isoformat(),
            "expires_at": pending.expires_at.isoformat(),
            "thread_id": pending.thread_id,
        }
        if pending.requester_id is not None:
            content["requester_id"] = pending.requester_id
        return content

    def _resolved_event_content(
        self,
        pending: PendingApproval,
        decision: ApprovalDecision,
    ) -> dict[str, Any]:
        content = self._pending_event_content(pending)
        content["body"] = self._event_body(pending.tool_name, pending.status)
        content["status"] = decision.status
        content["resolved_at"] = decision.resolved_at.isoformat()
        content["resolved_by"] = decision.resolved_by
        if decision.reason:
            content["resolution_reason"] = decision.reason
            if decision.status == "denied":
                content["denial_reason"] = decision.reason
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

    stale_keys = [key for key in _SCRIPT_CACHE if key[0] == str(resolved_path) and key != cache_key]
    for stale_key in stale_keys:
        _SCRIPT_CACHE.pop(stale_key, None)
    _SCRIPT_CACHE[cache_key] = module
    return module, resolved_path


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
            msg = f"Approval script '{resolved_path}' failed: {exc!s}"
            raise ToolApprovalScriptError(msg) from exc
        if not isinstance(result, bool):
            msg = f"Approval script '{resolved_path}' returned a non-bool result."
            raise ToolApprovalScriptError(msg)
        return result, matched_rule, script_path, timeout_seconds

    return require_approval, matched_rule, script_path, timeout_seconds


def get_approval_store() -> ApprovalManager | None:
    """Return the module-level approval manager when initialized."""
    return _MANAGER


def initialize_approval_store(
    runtime_paths: RuntimePaths,
    *,
    sender: MatrixEventSender | None = None,
    editor: MatrixEventEditor | None = None,
) -> ApprovalManager:
    """Initialize the module-level approval manager for one runtime context."""
    global _MANAGER

    if _MANAGER is not None and _MANAGER.runtime_storage_root == runtime_paths.storage_root:
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
