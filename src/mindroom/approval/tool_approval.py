"""Tool-call approval rule evaluation and public approval API."""

from __future__ import annotations

import importlib.util
import inspect
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from mindroom.approval import manager as approval_manager
from mindroom.approval.auto_approve_windows import auto_approve_expires_at
from mindroom.approval.manager import (
    ApprovalActionResult,
    ToolApprovalTransportError,
)
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.event_journal import DEFAULT_ROUTER_MANAGED_ROOM_REASON
from mindroom.logging_config import get_logger
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_system.approval_exemptions import tool_call_is_approval_exempt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from types import ModuleType

    from mindroom.config.main import Config
    from mindroom.tool_system.declarations import ApprovalAlias
    from mindroom.tool_system.registry import ToolRegistry

__all__ = [
    "DEFAULT_ROUTER_MANAGED_ROOM_REASON",
    "NO_APPROVER_REASON",
    "ApprovalActionResult",
    "BackgroundScriptToolOrigin",
    "MatrixApprovalAction",
    "ToolApprovalDecision",
    "ToolApprovalScriptError",
    "ToolApprovalTransportError",
    "evaluate_tool_approval",
    "handle_matrix_approval_action",
    "human_gate",
    "is_process_active_approval_card",
    "resolve_tool_approval_approver",
    "scheduled_call_offers_any_arguments",
    "shutdown_approval_runtime",
    "tool_may_require_approval",
    "toolkit_approval_alias",
]

NO_APPROVER_REASON = "No approval recipient is configured; the tool was denied safely."
# The terminal card edit carries the reason twice, and nio encrypts it as ASCII-escaped JSON that base64 grows by 4/3.
# Escaping turns one emoji into 12 bytes, so the reply is bounded by escaped size to keep the edit below the event limit.
_MAX_RESOLUTION_REASON_JSON_BYTES = 2000
_SCRIPT_CACHE: dict[tuple[str, int], ModuleType] = {}
_SCRIPT_CACHE_LOCK = threading.Lock()
logger = get_logger(__name__)


class ToolApprovalScriptError(RuntimeError):
    """One approval-script load or execution failure, or one approval alias judge failure."""


@dataclass(frozen=True, slots=True)
class BackgroundScriptToolOrigin:
    """Durable identity for one background-script tool call."""

    run_id: str
    call_id: str
    requester_id: str
    toolkit_name: str
    function_name: str


@dataclass(frozen=True, slots=True)
class ToolApprovalDecision:
    """A gated call's final approval decision."""

    approved: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MatrixApprovalAction:
    """One Matrix approval action emitted by a reaction, reply, or custom event."""

    room_id: str
    sender_id: str
    card_event_id: str | None
    status: Literal["approved", "denied"] | None
    reason: str | None
    auto_approve_seconds: int | None = None
    action: Literal["revoke_auto_approval"] | None = None
    grant_id: str | None = None
    current_binding: str | None = None
    scheduled_scope: str | None = None


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


# The approved-egress switch's tool asks before each network grant unless an authored rule already requires approval.
_APPROVED_EGRESS_RULE = ApprovalRuleConfig(match="request_network_access", action="require_approval")


def _matching_tool_approval_rule(config: Config, tool_name: str) -> ApprovalRuleConfig | None:
    rule = next((rule for rule in config.tool_approval.rules if fnmatchcase(tool_name, rule.match)), None)
    if (
        config.approved_egress_enabled
        and fnmatchcase(tool_name, _APPROVED_EGRESS_RULE.match)
        and (rule is None or rule.action != "require_approval")
    ):
        return _APPROVED_EGRESS_RULE
    return rule


def _rule_outcome(config: Config, rule: ApprovalRuleConfig | None) -> tuple[bool, float]:
    """Return whether ``rule``, or the default when no rule matches, may ask about a call, and how long its card stays open."""
    may_ask = config.tool_approval.default == "require_approval" if rule is None else rule.action != "auto_approve"
    timeout_days = config.tool_approval.timeout_days if rule is None or rule.timeout_days is None else rule.timeout_days
    return may_ask, timeout_days * 24 * 60 * 60


def _rules_may_ask(config: Config, tool_name: str) -> tuple[bool, float]:
    """Return whether the rules matching ``tool_name`` may ask about a call of it, and how long its card stays open."""
    return _rule_outcome(config, _matching_tool_approval_rule(config, tool_name))


def tool_may_require_approval(config: Config, tool_name: str, alias: ApprovalAlias | None = None) -> bool:
    """Return whether one tool must use the persisted confirmation boundary.

    A function with an approval ``alias`` must also when the function it is judged as must.
    """
    return _rules_may_ask(config, tool_name)[0] or (alias is not None and _rules_may_ask(config, alias.judged_as)[0])


def toolkit_approval_alias(
    config: Config,
    tools: ToolRegistry,
    toolkit: str | None,
    function_name: str,
) -> ApprovalAlias | None:
    """Return the approval alias that the tool ``toolkit`` names declares for its function ``function_name``.

    ``toolkit`` is the registered tool the function comes from, or the authored toolkit a call names, whose preset or
    implied tools each count; None, for a function generated for the agent, declares none.
    """
    if toolkit is None:
        return None
    for name in config.expand_tool_names([toolkit]):
        metadata = tools.get(name)
        if metadata is not None and function_name in metadata.approval_aliases:
            return metadata.approval_aliases[function_name]
    return None


def resolve_tool_approval_approver(
    config: Config,
    runtime_paths: RuntimePaths,
    requester_id: str | None,
) -> str | None:
    """Return the human requester allowed to resolve one approval request."""
    if requester_id is None or not requester_id.startswith("@") or ":" not in requester_id:
        return None
    canonical_requester_id = resolve_human_requester_alias(requester_id, config, runtime_paths)
    if not is_human_requester_id(canonical_requester_id, config, runtime_paths):
        return None
    return canonical_requester_id


def human_gate(
    runtime_paths: RuntimePaths,
    *,
    approver_id: str | None,
    room_id: str | None,
    thread_id: str | None,
) -> ToolApprovalDecision | None:
    """Decide a gated call that no person needs to or can answer, or return None to ask its approver.

    The approver's auto-approve window in the conversation approves it. A call no one may approve, or one in a room
    without the router, is denied with a reason; the router's tells the agent how to invite it.
    """
    if (
        auto_approve_expires_at(runtime_paths, room_id=room_id, thread_id=thread_id, requester_id=approver_id)
        is not None
    ):
        return ToolApprovalDecision(approved=True)
    if approver_id is None:
        return ToolApprovalDecision(approved=False, reason=NO_APPROVER_REASON)
    manager = approval_manager.get_approval_store()
    if room_id is not None and manager is not None and manager.transport.router_missing(room_id):
        return ToolApprovalDecision(approved=False, reason=DEFAULT_ROUTER_MANAGED_ROOM_REASON)
    return None


async def evaluate_tool_approval(
    config: Config,
    runtime_paths: RuntimePaths,
    tool_name: str,
    arguments: dict[str, Any],
    agent_name: str,
    alias: ApprovalAlias | None,
) -> tuple[bool, float]:
    """Return the approval decision for one tool call, and how long its card stays open.

    A call of a function with an approval ``alias`` also needs approval when a call of the function it is judged as,
    one its ``judge`` names, would under that function's own rules; when its arguments cannot tell, whenever those
    rules may ask.
    """
    requires_approval, timeout_seconds = await _evaluate_rules(config, runtime_paths, tool_name, arguments, agent_name)
    if requires_approval or alias is None:
        return requires_approval, timeout_seconds
    judged_calls = _judged_calls(alias, tool_name, arguments)
    if judged_calls is None:
        judged = _rules_may_ask(config, alias.judged_as)
        return judged if judged[0] else (False, timeout_seconds)
    for judged_arguments in judged_calls:
        judged = await _evaluate_rules(config, runtime_paths, alias.judged_as, dict(judged_arguments), agent_name)
        if judged[0]:
            return judged
    return False, timeout_seconds


def _judged_calls(
    alias: ApprovalAlias,
    tool_name: str,
    arguments: dict[str, Any],
) -> Sequence[Mapping[str, object]] | None:
    """Return the calls ``alias``'s judge says one call of ``tool_name`` amounts to, failing as an approval script does."""
    try:
        judged_calls = alias.judge(arguments)
    except Exception as exc:
        logger.warning("Approval alias judge raised", tool_name=tool_name, exc_info=True)
        msg = f"Approval alias judge of '{tool_name}' failed with {type(exc).__name__}"
        raise ToolApprovalScriptError(msg) from exc
    if judged_calls is None:
        return None
    # Text is a sequence too, but never of calls: an empty one would otherwise approve the call.
    if (
        not isinstance(judged_calls, Sequence)
        or isinstance(judged_calls, str | bytes)
        or not all(isinstance(call, Mapping) for call in judged_calls)
    ):
        msg = f"Approval alias judge of '{tool_name}' returned neither a sequence of argument mappings nor None."
        raise ToolApprovalScriptError(msg)
    return judged_calls


async def _evaluate_rules(
    config: Config,
    runtime_paths: RuntimePaths,
    tool_name: str,
    arguments: dict[str, Any],
    agent_name: str,
) -> tuple[bool, float]:
    """Return the decision of the rules that match ``tool_name`` itself for one call of it."""
    if tool_call_is_approval_exempt(tool_name, arguments, runtime_paths):
        return False, config.tool_approval.timeout_days * 24 * 60 * 60

    rule = _matching_tool_approval_rule(config, tool_name)
    may_ask, timeout_seconds = _rule_outcome(config, rule)
    if rule is None or rule.script is None:
        return may_ask, timeout_seconds

    module, resolved_path = _load_script_module(rule.script, runtime_paths)
    check = getattr(module, "check", None)
    if not callable(check):
        msg = f"Approval script '{resolved_path}' must define callable check(tool_name, arguments, agent_name)."
        raise ToolApprovalScriptError(msg)
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


def _bounded_resolution_reason(reason: str) -> str:
    """Return the longest prefix whose ASCII-escaped JSON string body fits the reason budget."""
    escaped_bytes = 0
    for index, character in enumerate(reason):
        escaped_bytes += len(json.dumps(character)) - 2
        if escaped_bytes > _MAX_RESOLUTION_REASON_JSON_BYTES:
            return reason[:index]
    return reason


async def handle_matrix_approval_action(
    action: MatrixApprovalAction,
    *,
    authorize_responder: Callable[[str], bool],
    before_consume: Callable[[], Awaitable[None]] | None = None,
) -> ApprovalActionResult:
    """Resolve a durable continuation card anchored to its Matrix event."""
    manager = approval_manager.get_approval_store()
    if manager is None:
        return ApprovalActionResult(consumed=False)
    sanitized_reason = (
        _bounded_resolution_reason(action.reason.strip())
        if isinstance(action.reason, str) and action.reason.strip()
        else None
    )
    if action.card_event_id is None:
        return ApprovalActionResult(consumed=False)
    if (
        action.action == "revoke_auto_approval"
        and action.grant_id is not None
        and action.status is None
        and action.auto_approve_seconds is None
        and action.scheduled_scope is None
    ):
        return await manager.handle_grant_revocation(
            room_id=action.room_id,
            sender_id=action.sender_id,
            card_event_id=action.card_event_id,
            grant_id=action.grant_id,
            authorize_responder=authorize_responder,
            before_consume=before_consume,
        )
    if (
        action.status is None
        or action.action is not None
        or action.grant_id is not None
        or (action.scheduled_scope is not None and action.auto_approve_seconds is not None)
    ):
        return ApprovalActionResult(consumed=False)
    return await manager.handle_card_response(
        room_id=action.room_id,
        sender_id=action.sender_id,
        card_event_id=action.card_event_id,
        status=action.status,
        reason=sanitized_reason,
        before_consume=before_consume,
        authorize_responder=authorize_responder,
        auto_approve_seconds=action.auto_approve_seconds,
        current_binding=action.current_binding,
        scheduled_scope=action.scheduled_scope,
    )


def scheduled_call_offers_any_arguments(
    config: Config,
    tool_name: str,
    arguments: dict[str, object],
    *,
    requester_id: str,
    approver_id: str,
    authored_confirmation: bool,
) -> bool:
    """Return whether a scheduled call's card may offer approving any arguments.

    Like a timed approval, the broader scope needs requesters who approve their own
    calls. A tool that asks for its own confirmation is approved only for the exact
    call shown. Generic MCP dispatch names its remote tool in the arguments, so
    approving any arguments there would approve every tool on the server.
    """
    if not config.tool_approval.scheduled_any_arguments or approver_id != requester_id or authored_confirmation:
        return False
    operation = grant_operation(config, tool_name, arguments)
    return operation is not None and operation.mcp_server_id is None


def is_process_active_approval_card(card_event_id: str) -> bool:
    """Return whether one approval card is being settled in this process."""
    manager = approval_manager.get_approval_store()
    return manager is not None and manager.has_active_in_memory_approval_card(card_event_id)


async def shutdown_approval_runtime() -> None:
    """Stop approval transport work, drop runtime state, and clear script state."""
    try:
        await approval_manager.shutdown_approval_manager()
    finally:
        with _SCRIPT_CACHE_LOCK:
            _SCRIPT_CACHE.clear()
