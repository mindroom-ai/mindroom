"""Tool-call approval rule evaluation and public approval API."""

from __future__ import annotations

import importlib.util
import inspect
import threading
from copy import deepcopy
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom import approval_manager
from mindroom.approval_manager import (
    DEFAULT_ROUTER_MANAGED_ROOM_REASON,
    DEFAULT_SHUTDOWN_REASON,
    ApprovalActionResult,
    ApprovalCardLocator,
    ApprovalRoomProvider,
    ApprovalStartupSweep,
    ContinuationReadyHandler,
    MatrixEventEditor,
    MatrixEventSender,
    SendingDeviceProvider,
    SentApprovalEvent,
    ToolApprovalTransportError,
    TransportSenderProvider,
)
from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.entity_resolution import is_human_requester_id
from mindroom.logging_config import get_logger
from mindroom.tool_system.approval_exemptions import tool_call_is_approval_exempt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from types import ModuleType

    from mindroom.config.approval import ApprovalRuleConfig
    from mindroom.config.main import Config
    from mindroom.event_journal import ApprovalView

__all__ = [
    "DEFAULT_ROUTER_MANAGED_ROOM_REASON",
    "POLICY_CONFIRMATION_APPROVAL_TYPE",
    "ApprovalActionResult",
    "ApprovalStartupSweep",
    "MatrixApprovalAction",
    "SentApprovalEvent",
    "ToolApprovalCall",
    "ToolApprovalScriptError",
    "ToolApprovalTransportError",
    "evaluate_tool_approval",
    "expire_continuation_approval_cards",
    "expire_orphaned_approval_cards_on_startup",
    "handle_matrix_approval_action",
    "initialize_approval_runtime",
    "is_process_active_approval_card",
    "resolve_tool_approval_approver",
    "send_suspended_tool_approval",
    "shutdown_approval_runtime",
    "tool_may_require_approval",
]

# Agno copies this field onto the paused ToolExecution, preserving whether MindRoom added the confirmation boundary.
POLICY_CONFIRMATION_APPROVAL_TYPE = "mindroom_policy"
_SCRIPT_CACHE: dict[tuple[str, int], ModuleType] = {}
_SCRIPT_CACHE_LOCK = threading.Lock()
logger = get_logger(__name__)


class ToolApprovalScriptError(RuntimeError):
    """One approval-script load or execution failure."""


@dataclass(frozen=True, slots=True)
class ToolApprovalCall:
    """One tool call that may require a Matrix approval card."""

    config: Config
    runtime_paths: RuntimePaths
    tool_name: str
    arguments: dict[str, Any]
    agent_name: str
    room_id: str | None
    thread_id: str | None
    requester_id: str | None


@dataclass(frozen=True, slots=True)
class MatrixApprovalAction:
    """One Matrix approval action emitted by a reaction, reply, or custom event."""

    room_id: str
    sender_id: str
    card_event_id: str | None
    status: Literal["approved", "denied"]
    reason: str | None


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
        msg = f"Approval script '{resolved_path}' failed to import with {type(exc).__name__}"
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


def _clear_script_cache() -> None:
    """Clear the shared approval-script cache under the cache lock."""
    with _SCRIPT_CACHE_LOCK:
        _SCRIPT_CACHE.clear()


def _matching_tool_approval_rule(config: Config, tool_name: str) -> ApprovalRuleConfig | None:
    return next((rule for rule in config.tool_approval.rules if fnmatchcase(tool_name, rule.match)), None)


def tool_may_require_approval(config: Config, tool_name: str) -> bool:
    """Return whether one tool must use Agno's persisted confirmation boundary."""
    rule = _matching_tool_approval_rule(config, tool_name)
    if rule is None:
        return config.tool_approval.default == "require_approval"
    return rule.action != "auto_approve"


def resolve_tool_approval_approver(
    config: Config,
    runtime_paths: RuntimePaths,
    requester_id: str | None,
) -> str | None:
    """Return the human requester allowed to resolve one approval request."""
    if requester_id is None or not requester_id.startswith("@") or ":" not in requester_id:
        return None
    if not is_human_requester_id(requester_id, config, runtime_paths):
        return None
    return requester_id


async def evaluate_tool_approval(
    config: Config,
    runtime_paths: RuntimePaths,
    tool_name: str,
    arguments: dict[str, Any],
    agent_name: str,
) -> tuple[bool, float]:
    """Return the approval decision for one tool call."""
    approval_config = config.tool_approval
    require_approval = approval_config.default == "require_approval"
    timeout_seconds = approval_config.timeout_days * 24 * 60 * 60

    if tool_call_is_approval_exempt(tool_name, arguments):
        return False, timeout_seconds

    rule = _matching_tool_approval_rule(config, tool_name)
    if rule is None:
        return require_approval, timeout_seconds
    if rule.timeout_days is not None:
        timeout_seconds = rule.timeout_days * 24 * 60 * 60
    if rule.action is not None:
        return rule.action == "require_approval", timeout_seconds

    assert rule.script is not None
    module, resolved_path = _load_script_module(rule.script, runtime_paths)
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
    return result, timeout_seconds


async def send_suspended_tool_approval(
    call: ToolApprovalCall,
    *,
    approval_id: str,
    continuation_id: str,
    continuation_generation: int,
    tool_call_id: str,
    expires_at_ns: int,
) -> SentApprovalEvent | None:
    """Send a durable card for a paused Agno run without retaining a waiter."""
    manager = approval_manager.get_approval_store()
    approver = resolve_tool_approval_approver(call.config, call.runtime_paths, call.requester_id)
    if manager is None or call.room_id is None or call.requester_id is None or approver is None:
        return None
    return await manager.create_detached_approval(
        approval_id=approval_id,
        continuation_id=continuation_id,
        continuation_generation=continuation_generation,
        tool_call_id=tool_call_id,
        tool_name=call.tool_name,
        arguments=deepcopy(call.arguments),
        room_id=call.room_id,
        requester_id=call.requester_id,
        approver_user_id=approver,
        expires_at_ns=expires_at_ns,
        agent_name=call.agent_name,
        thread_id=call.thread_id,
    )


async def handle_matrix_approval_action(
    action: MatrixApprovalAction,
    *,
    before_consume: Callable[[], Awaitable[None]] | None = None,
) -> ApprovalActionResult:
    """Resolve a durable continuation card anchored to its Matrix event."""
    manager = approval_manager.get_approval_store()
    if manager is None:
        return ApprovalActionResult(consumed=False, resolved=False)
    sanitized_reason = action.reason.strip() if isinstance(action.reason, str) and action.reason.strip() else None
    if action.card_event_id is None:
        return ApprovalActionResult(consumed=False, resolved=False)
    return await manager.handle_card_response(
        room_id=action.room_id,
        sender_id=action.sender_id,
        card_event_id=action.card_event_id,
        status=action.status,
        reason=sanitized_reason,
        before_consume=before_consume,
    )


def is_process_active_approval_card(card_event_id: str) -> bool:
    """Return whether one approval card is being settled in this process."""
    manager = approval_manager.get_approval_store()
    return manager is not None and manager.has_active_in_memory_approval_card(card_event_id)


def initialize_approval_runtime(
    runtime_paths: RuntimePaths,
    *,
    sender: MatrixEventSender,
    editor: MatrixEventEditor,
    cards: ApprovalView | None,
    approval_room_ids: ApprovalRoomProvider,
    transport_sender: TransportSenderProvider,
    sending_device: SendingDeviceProvider,
    locate_card: ApprovalCardLocator,
    continuation_ready: ContinuationReadyHandler | None = None,
) -> None:
    """Initialize the approval runtime behind the public approval seam."""
    approval_manager.initialize_approval_store(
        runtime_paths,
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=approval_room_ids,
        transport_sender=transport_sender,
        sending_device=sending_device,
        locate_card=locate_card,
        continuation_ready=continuation_ready,
    )


async def expire_continuation_approval_cards(continuation_id: str) -> bool:
    """Expire every unresolved Matrix card for one continuation."""
    manager = approval_manager.get_approval_store()
    return False if manager is None else await manager.expire_continuation_cards(continuation_id)


async def expire_orphaned_approval_cards_on_startup() -> ApprovalStartupSweep:
    """Settle legacy and orphaned approval cards without executing their tools."""
    manager = approval_manager.get_approval_store()
    if manager is None:
        return ApprovalStartupSweep(discarded=0, failed=0)
    return await manager.discard_pending_on_startup()


async def shutdown_approval_runtime(reason: str = DEFAULT_SHUTDOWN_REASON) -> None:
    """Stop approval transport work, drop runtime state, and clear script state."""
    try:
        await approval_manager.shutdown_approval_manager(reason=reason)
    finally:
        _clear_script_cache()
