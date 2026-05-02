"""Tool-call approval rule evaluation and public approval API."""

from __future__ import annotations

import importlib.util
import inspect
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom import approval_manager
from mindroom.approval_manager import (
    DEFAULT_ROUTER_MANAGED_ROOM_REASON,
    DEFAULT_SHUTDOWN_REASON,
    ApprovalActionResult,
    ApprovalDecision,
    ApprovalRoomProvider,
    MatrixEventEditor,
    MatrixEventSender,
    SentApprovalEvent,
    ToolApprovalTransportError,
    TransportSenderProvider,
)
from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import is_agent_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from types import ModuleType

    from mindroom.config.main import Config
    from mindroom.matrix.cache.event_cache import ConversationEventCache

__all__ = [
    "DEFAULT_ROUTER_MANAGED_ROOM_REASON",
    "ApprovalActionResult",
    "ApprovalDecision",
    "MatrixApprovalAction",
    "SentApprovalEvent",
    "ToolApprovalCall",
    "ToolApprovalScriptError",
    "ToolApprovalTransportError",
    "evaluate_tool_approval",
    "expire_orphaned_approval_cards_on_startup",
    "handle_matrix_approval_action",
    "initialize_approval_runtime",
    "is_process_active_approval_card",
    "is_process_approval_card",
    "request_tool_approval_for_call",
    "resolve_tool_approval_approver",
    "shutdown_approval_runtime",
    "tool_requires_approval_for_openai_compat",
]

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
    approval_id: str | None
    status: Literal["approved", "denied"]
    reason: str | None


def _terminal_decision(status: Literal["denied", "expired"], reason: str) -> ApprovalDecision:
    return ApprovalDecision(
        status=status,
        reason=reason,
        resolved_by=None,
        resolved_at=datetime.now(UTC),
    )


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


def tool_requires_approval_for_openai_compat(
    config: Config,
    tool_name: str,
) -> bool:
    """Return whether one `/v1` tool must be hidden because approval may be required."""
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
) -> tuple[bool, float]:
    """Return the approval decision for one tool call."""
    approval_config = config.tool_approval
    require_approval = approval_config.default == "require_approval"
    timeout_seconds = approval_config.timeout_days * 24 * 60 * 60

    for rule in approval_config.rules:
        if not fnmatchcase(tool_name, rule.match):
            continue
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

    return require_approval, timeout_seconds


async def request_tool_approval_for_call(call: ToolApprovalCall) -> ApprovalDecision | None:
    """Return a terminal decision when one tool call is denied, or None when it may proceed."""
    policy_arguments = deepcopy(call.arguments)
    requires_approval, timeout_seconds = await evaluate_tool_approval(
        call.config,
        call.runtime_paths,
        call.tool_name,
        policy_arguments,
        call.agent_name,
    )
    if not requires_approval:
        return None

    manager = approval_manager.get_approval_store()
    if manager is None:
        return _terminal_decision(
            "expired",
            "Tool approval is required but the approval store is not initialized.",
        )

    return await manager.request_approval(
        tool_name=call.tool_name,
        arguments=deepcopy(call.arguments),
        agent_name=call.agent_name,
        room_id=call.room_id,
        thread_id=call.thread_id,
        requester_id=call.requester_id,
        approver_user_id=resolve_tool_approval_approver(
            call.config,
            call.runtime_paths,
            call.requester_id,
        ),
        timeout_seconds=timeout_seconds,
    )


def is_process_approval_card(card_event_id: str) -> bool:
    """Return whether the current process has seen one approval card id."""
    manager = approval_manager.get_approval_store()
    return manager is not None and manager.knows_in_memory_approval_card(card_event_id)


def is_process_active_approval_card(card_event_id: str) -> bool:
    """Return whether one approval card still has an active in-process waiter."""
    manager = approval_manager.get_approval_store()
    return manager is not None and manager.has_active_in_memory_approval_card(card_event_id)


async def handle_matrix_approval_action(action: MatrixApprovalAction) -> ApprovalActionResult:
    """Resolve one Matrix approval action against live in-process approval state."""
    manager = approval_manager.get_approval_store()
    if manager is None:
        return ApprovalActionResult(consumed=False, resolved=False)
    sanitized_reason = action.reason.strip() if isinstance(action.reason, str) and action.reason.strip() else None
    card_result: ApprovalActionResult | None = None
    if action.card_event_id is not None:
        card_result = await manager.handle_card_response(
            room_id=action.room_id,
            sender_id=action.sender_id,
            card_event_id=action.card_event_id,
            status=action.status,
            reason=sanitized_reason,
        )
        if card_result.resolved or action.approval_id is None:
            return card_result
    if action.approval_id is None:
        return ApprovalActionResult(consumed=False, resolved=False)
    approval_id_result = await manager.handle_live_approval_id_response(
        room_id=action.room_id,
        sender_id=action.sender_id,
        approval_id=action.approval_id,
        status=action.status,
        reason=sanitized_reason,
    )
    if approval_id_result.consumed or card_result is None:
        return approval_id_result
    return card_result


def initialize_approval_runtime(
    runtime_paths: RuntimePaths,
    *,
    sender: MatrixEventSender,
    editor: MatrixEventEditor,
    event_cache: ConversationEventCache,
    approval_room_ids: ApprovalRoomProvider,
    transport_sender: TransportSenderProvider,
) -> None:
    """Initialize the approval runtime behind the public approval seam."""
    approval_manager.initialize_approval_store(
        runtime_paths,
        sender=sender,
        editor=editor,
        event_cache=event_cache,
        approval_room_ids=approval_room_ids,
        transport_sender=transport_sender,
    )


async def expire_orphaned_approval_cards_on_startup(*, lookback_hours: int) -> int:
    """Expire router-authored approval cards that can no longer have live waiters."""
    manager = approval_manager.get_approval_store()
    if manager is None:
        return 0
    return await manager.discard_pending_on_startup(lookback_hours=lookback_hours)


async def shutdown_approval_runtime(reason: str = DEFAULT_SHUTDOWN_REASON) -> None:
    """Expire live approvals, drop runtime state, and clear approval script state."""
    await shutdown_approval_store(reason=reason)


async def shutdown_approval_store(reason: str = DEFAULT_SHUTDOWN_REASON) -> None:
    """Expire pending approvals, drop the manager, and clear script state."""
    await approval_manager.shutdown_approval_manager(reason=reason)
    _clear_script_cache()
