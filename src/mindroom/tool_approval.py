"""Tool-call approval rule evaluation and public approval API."""

from __future__ import annotations

import importlib.util
import inspect
import threading
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from mindroom.approval_manager import (
    _DEFAULT_ROUTER_MANAGED_ROOM_REASON,
    _DEFAULT_SHUTDOWN_REASON,
    AnchoredApprovalActionResult,
    ApprovalDecision,
    ApprovalManager,
    PendingApproval,
    SentApprovalEvent,
    ToolApprovalTransportError,
    get_approval_store,
    initialize_approval_store,
    shutdown_approval_manager,
)
from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import is_agent_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from types import ModuleType

    from mindroom.config.main import Config

__all__ = [
    "_DEFAULT_ROUTER_MANAGED_ROOM_REASON",
    "AnchoredApprovalActionResult",
    "ApprovalDecision",
    "ApprovalManager",
    "PendingApproval",
    "SentApprovalEvent",
    "ToolApprovalScriptError",
    "ToolApprovalTransportError",
    "evaluate_tool_approval",
    "get_approval_store",
    "initialize_approval_store",
    "resolve_tool_approval_approver",
    "shutdown_approval_store",
    "tool_requires_approval_for_openai_compat",
]

_SCRIPT_CACHE: dict[tuple[str, int], ModuleType] = {}
_SCRIPT_CACHE_LOCK = threading.Lock()
logger = get_logger(__name__)


class ToolApprovalScriptError(RuntimeError):
    """One approval-script load or execution failure."""


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


async def shutdown_approval_store(reason: str = _DEFAULT_SHUTDOWN_REASON) -> None:
    """Expire pending approvals, drop the manager, and clear script state."""
    await shutdown_approval_manager(reason=reason)
    _clear_script_cache()
