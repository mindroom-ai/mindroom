"""Bridge Agno per-function tool hooks into MindRoom's hook registry."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import Callable
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4
from weakref import WeakKeyDictionary

from agno.tools.function import FunctionCall

from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    emit,
    emit_gate,
)
from mindroom.llm_request_logging import current_llm_request_log_context
from mindroom.logging_config import get_logger
from mindroom.oauth.providers import OAuthConnectionRequired, oauth_connection_required_payload
from mindroom.sync_bridge_state import sync_tool_bridge_blocked_loop
from mindroom.timing import elapsed_ms_since, emit_timing_event
from mindroom.tool_approval import (
    ToolApprovalCall,
    ToolApprovalScriptError,
    ToolCallWorkflowOrigin,
    request_tool_approval_for_call,
)
from mindroom.tool_system.approval_bypass import ToolApprovalBypassResult, evaluate_tool_approval_bypass
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    ToolDispatchContext,
    execution_identity_matches_tool_runtime_context,
    get_tool_runtime_context,
    resolve_tool_runtime_hook_bindings,
)
from mindroom.tool_system.tool_calls import ToolCallTiming, record_tool_failure, record_tool_success
from mindroom.tool_system.worker_routing import active_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Coroutine

    from agno.tools import Toolkit
    from agno.tools.function import Function

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import (
        HookMatrixAdmin,
        HookMessageSender,
        HookRegistry,
        HookRoomStatePutter,
        HookRoomStateQuerier,
    )
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_ToolBypassResultTransform = Callable[[Callable[..., Any] | None, object, dict[str, Any]], object]
_DECLINED_RESULT_TEMPLATE = (
    "[TOOL CALL DECLINED]\n"
    "Tool: {tool_name}\n"
    "Reason: {reason}\n\n"
    "Adjust your approach — try a different tool or different arguments."
)
_APPROVAL_POLICY_FAILURE_REASON = "Tool approval policy failed."
_SYNC_BRIDGES: WeakKeyDictionary[Callable[..., Any], Callable[..., Any]] = WeakKeyDictionary()
_BOUND_BRIDGE_HOOK_BASES: WeakKeyDictionary[Callable[..., Any], Callable[..., Any]] = WeakKeyDictionary()
_ToolHookResult = Any
# Agno does not currently expose a hook-chain extension point for unwrapping MindRoom's
# deferred sync-bridge results. Keep these wrappers covered by tests when bumping Agno
# in uv.lock, and drop them once upstream supports this as public API.
_ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN_ASYNC = FunctionCall._build_nested_execution_chain_async
_ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN = FunctionCall._build_nested_execution_chain
_AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED = False
_AGNO_SYNC_TOOL_HOOK_CHAIN_PATCHED = False
logger = get_logger(__name__)


@dataclass(slots=True)
class _DeferredAsyncToolHookResult:
    """Sentinel used when a sync hook needs async completion on the current loop."""

    awaitable: Awaitable[_ToolHookResult]


@dataclass(frozen=True, slots=True)
class _ResolvedToolContext:
    agent_name: str
    room_id: str | None
    thread_id: str | None
    reply_to_event_id: str | None
    requester_id: str | None
    session_id: str | None
    channel: str | None
    config: Config | None
    runtime_paths: RuntimePaths | None
    correlation_id: str
    message_sender: HookMessageSender | None
    matrix_admin: HookMatrixAdmin | None
    room_state_querier: HookRoomStateQuerier | None
    room_state_putter: HookRoomStatePutter | None
    message_received_depth: int

    def hook_context_kwargs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "arguments": arguments,
            "agent_name": self.agent_name,
            "room_id": self.room_id,
            "thread_id": self.thread_id,
            "requester_id": self.requester_id,
            "session_id": self.session_id,
            "config": self.config,
            "runtime_paths": self.runtime_paths,
            "correlation_id": self.correlation_id,
            "message_sender": self.message_sender,
            "matrix_admin": self.matrix_admin,
            "room_state_querier": self.room_state_querier,
            "room_state_putter": self.room_state_putter,
            "message_received_depth": self.message_received_depth,
        }


@dataclass(frozen=True, slots=True)
class _ToolHookBridgeContext:
    """Static hook-bridge inputs that remain valid across live and detached calls."""

    agent_name: str | None
    config: Config | None
    runtime_paths: RuntimePaths | None
    dispatch_context: ToolDispatchContext | None


def _correlation_id_for_runtime_context(runtime_context: ToolRuntimeContext | None) -> str:
    if runtime_context is not None and runtime_context.correlation_id:
        return runtime_context.correlation_id
    request_context = current_llm_request_log_context()
    correlation_id = request_context.get("correlation_id")
    if isinstance(correlation_id, str) and correlation_id:
        return correlation_id
    return "tool-hook:" + uuid4().hex


def _ambient_tool_dispatch_context() -> ToolDispatchContext | None:
    runtime_context = get_tool_runtime_context()
    if runtime_context is not None:
        return LiveToolDispatchContext.from_runtime_context(runtime_context)
    execution_identity = active_tool_execution_identity(None)
    if execution_identity is not None:
        return ToolDispatchContext(execution_identity=execution_identity)
    return None


def _explicit_bridge_dispatch_context(
    dispatch_context: ToolDispatchContext | None,
) -> ToolDispatchContext | None:
    if dispatch_context is None:
        return None
    if isinstance(dispatch_context, LiveToolDispatchContext):
        return dispatch_context
    runtime_context = get_tool_runtime_context()
    if runtime_context is not None and execution_identity_matches_tool_runtime_context(
        dispatch_context.execution_identity,
        runtime_context,
    ):
        return LiveToolDispatchContext.from_runtime_context(runtime_context)
    return dispatch_context


def _resolve_tool_context(
    *,
    bridge_context: _ToolHookBridgeContext,
) -> _ResolvedToolContext:
    dispatch_context = bridge_context.dispatch_context
    if isinstance(dispatch_context, LiveToolDispatchContext):
        runtime_context = dispatch_context.runtime_context
        resolved_runtime_paths = runtime_context.runtime_paths
        bindings = resolve_tool_runtime_hook_bindings(runtime_context)
        return _ResolvedToolContext(
            agent_name=bridge_context.agent_name or dispatch_context.execution_identity.agent_name,
            room_id=dispatch_context.execution_identity.room_id,
            thread_id=dispatch_context.execution_identity.resolved_thread_id
            or dispatch_context.execution_identity.thread_id,
            reply_to_event_id=runtime_context.reply_to_event_id,
            requester_id=dispatch_context.execution_identity.requester_id,
            session_id=dispatch_context.execution_identity.session_id,
            channel=dispatch_context.execution_identity.channel,
            config=runtime_context.config,
            runtime_paths=resolved_runtime_paths,
            correlation_id=_correlation_id_for_runtime_context(runtime_context),
            message_sender=bindings.message_sender,
            matrix_admin=bindings.matrix_admin,
            room_state_querier=bindings.room_state_querier,
            room_state_putter=bindings.room_state_putter,
            message_received_depth=bindings.message_received_depth,
        )

    if dispatch_context is not None:
        resolved_runtime_paths = bridge_context.runtime_paths
        request_context = current_llm_request_log_context()
        reply_to_event_id = request_context.get("reply_to_event_id")
        return _ResolvedToolContext(
            agent_name=bridge_context.agent_name or dispatch_context.execution_identity.agent_name,
            room_id=dispatch_context.execution_identity.room_id,
            thread_id=dispatch_context.execution_identity.resolved_thread_id
            or dispatch_context.execution_identity.thread_id,
            reply_to_event_id=reply_to_event_id if isinstance(reply_to_event_id, str) else None,
            requester_id=dispatch_context.execution_identity.requester_id,
            session_id=dispatch_context.execution_identity.session_id,
            channel=dispatch_context.execution_identity.channel,
            config=bridge_context.config,
            runtime_paths=resolved_runtime_paths,
            correlation_id=_correlation_id_for_runtime_context(None),
            message_sender=None,
            matrix_admin=None,
            room_state_querier=None,
            room_state_putter=None,
            message_received_depth=0,
        )

    request_context = current_llm_request_log_context()
    reply_to_event_id = request_context.get("reply_to_event_id")
    return _ResolvedToolContext(
        agent_name=bridge_context.agent_name or "",
        room_id=None,
        thread_id=None,
        reply_to_event_id=reply_to_event_id if isinstance(reply_to_event_id, str) else None,
        requester_id=None,
        session_id=None,
        channel=None,
        config=bridge_context.config,
        runtime_paths=bridge_context.runtime_paths,
        correlation_id=_correlation_id_for_runtime_context(None),
        message_sender=None,
        matrix_admin=None,
        room_state_querier=None,
        room_state_putter=None,
        message_received_depth=0,
    )


def _should_record_successful_tool_call(resolved_context: _ResolvedToolContext) -> bool:
    """Return whether successful tool calls should be durably logged."""
    return bool(resolved_context.config and resolved_context.config.debug.log_llm_requests)


def _record_debug_tool_success(
    *,
    tool_name: str,
    arguments: dict[str, object],
    result: object,
    duration_ms: float,
    timing: ToolCallTiming | None,
    resolved_context: _ResolvedToolContext,
    dispatch_context: ToolDispatchContext | None,
) -> None:
    if not _should_record_successful_tool_call(resolved_context):
        return
    record_tool_success(
        tool_name=tool_name,
        arguments=arguments,
        result=result,
        duration_ms=duration_ms,
        timing=timing,
        agent_name=resolved_context.agent_name or None,
        room_id=resolved_context.room_id,
        thread_id=resolved_context.thread_id,
        reply_to_event_id=resolved_context.reply_to_event_id,
        requester_id=resolved_context.requester_id,
        session_id=resolved_context.session_id,
        correlation_id=resolved_context.correlation_id,
        execution_identity=dispatch_context.execution_identity if dispatch_context is not None else None,
        runtime_paths=resolved_context.runtime_paths,
    )


def _format_declined_result(tool_name: str, reason: str) -> str:
    return _DECLINED_RESULT_TEMPLATE.format(tool_name=tool_name, reason=reason)


def _approval_status_reason(status: str, reason: str | None) -> str:
    if reason:
        return reason
    if status == "approved":
        return "Tool approval was granted."
    if status == "denied":
        return "Tool approval was denied."
    if status == "expired":
        return "Tool approval request expired."
    return "Tool approval request is pending."


async def _await_result(awaitable: Awaitable[_ToolHookResult]) -> _ToolHookResult:
    return await awaitable


def _run_coroutine_from_sync(coroutine: _ToolHookResult) -> _ToolHookResult:
    if not inspect.isawaitable(coroutine):
        return coroutine
    runner_coroutine = cast("Coroutine[Any, Any, _ToolHookResult]", _await_result(coroutine))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(runner_coroutine)
    return _DeferredAsyncToolHookResult(runner_coroutine)


def _run_deferred_result_from_sync(deferred: _DeferredAsyncToolHookResult) -> _ToolHookResult:
    """Run a deferred async hook result for Agno's synchronous execute() chain."""
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await_result(deferred.awaitable))

    result_box: list[_ToolHookResult] = []
    error_box: list[BaseException] = []
    context = copy_context()

    def runner() -> None:
        try:
            result_box.append(context.run(asyncio.run, _await_result(deferred.awaitable)))
        except BaseException as exc:
            error_box.append(exc)

    with sync_tool_bridge_blocked_loop(running_loop):
        thread = threading.Thread(target=runner, name="mindroom-tool-hook-sync-bridge")
        thread.start()
        thread.join()
    if error_box:
        raise error_box[0]
    return result_box[0]


def _resolve_deferred_sync_result(result: _ToolHookResult) -> _ToolHookResult:
    while isinstance(result, _DeferredAsyncToolHookResult):
        result = _run_deferred_result_from_sync(result)
    return result


def _patch_agno_sync_tool_hook_chain() -> None:
    """Teach Agno's sync tool hook chain to unwrap deferred async bridge results."""
    global _AGNO_SYNC_TOOL_HOOK_CHAIN_PATCHED

    if _AGNO_SYNC_TOOL_HOOK_CHAIN_PATCHED:
        return

    @wraps(_ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN)
    def _patched_build_nested_execution_chain(
        self: FunctionCall,
        entrypoint_args: dict[str, Any],
    ) -> Callable[..., _ToolHookResult]:
        execution_chain = _ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN(self, entrypoint_args)

        def _wrapped_execution_chain(name: str, func: Callable[..., Any], args: dict[str, Any]) -> _ToolHookResult:
            return _resolve_deferred_sync_result(execution_chain(name, func, args))

        return _wrapped_execution_chain

    type.__setattr__(FunctionCall, "_build_nested_execution_chain", _patched_build_nested_execution_chain)
    _AGNO_SYNC_TOOL_HOOK_CHAIN_PATCHED = True


def _patch_agno_async_tool_hook_chain() -> None:
    """Teach Agno's async tool hook chain to unwrap deferred sync-hook awaitables."""
    global _AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED

    if _AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED:
        return

    @wraps(_ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN_ASYNC)
    async def _patched_build_nested_execution_chain_async(
        self: FunctionCall,
        entrypoint_args: dict[str, Any],
    ) -> Callable[..., Awaitable[_ToolHookResult]]:
        execution_chain = await _ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN_ASYNC(self, entrypoint_args)

        async def _wrapped_execution_chain(
            name: str,
            func: Callable[..., Any],
            args: dict[str, Any],
        ) -> _ToolHookResult:
            result = await execution_chain(name, func, args)
            while isinstance(result, _DeferredAsyncToolHookResult):
                result = await result.awaitable
            return result

        return _wrapped_execution_chain

    type.__setattr__(FunctionCall, "_build_nested_execution_chain_async", _patched_build_nested_execution_chain_async)
    _AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED = True


_patch_agno_sync_tool_hook_chain()
_patch_agno_async_tool_hook_chain()


async def _call_tool(
    func: Callable[..., Any],
    args: dict[str, Any],
    *,
    tool_name: str,
    agent_name: str | None,
) -> _ToolHookResult:
    async_entrypoint = inspect.iscoroutinefunction(func)
    emit_timing_event(
        "Tool hook dispatch timing",
        phase="tool_entry",
        tool_name=tool_name,
        agent_name=agent_name,
        async_entrypoint=async_entrypoint,
    )
    if async_entrypoint:
        result = await func(**args)
    else:
        result = await asyncio.to_thread(func, **args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _emit_after_call(
    *,
    hook_registry: HookRegistry,
    resolved_context: _ResolvedToolContext,
    hook_arguments: dict[str, Any] | None,
    args: dict[str, Any],
    tool_name: str,
    result: _ToolHookResult,
    error: BaseException | None,
    blocked: bool,
    duration_ms: float,
) -> None:
    after_context = ToolAfterCallContext(
        **resolved_context.hook_context_kwargs(hook_arguments if hook_arguments is not None else deepcopy(args)),
        tool_name=tool_name,
        result=result,
        error=error,
        blocked=blocked,
        duration_ms=duration_ms,
    )
    await emit(hook_registry, EVENT_TOOL_AFTER_CALL, after_context)


async def _resolve_tool_approval_gate(
    *,
    resolved_context: _ResolvedToolContext,
    func: Callable[..., Any],
    approval_entrypoint: Callable[..., Any] | None,
    args: dict[str, Any],
    tool_name: str,
    workflow_origin: ToolCallWorkflowOrigin | None,
) -> str | ToolApprovalBypassResult | None:
    bypass_entrypoint = approval_entrypoint if approval_entrypoint is not None else func
    bypass_decision = evaluate_tool_approval_bypass(tool_name, bypass_entrypoint, args)
    if isinstance(bypass_decision, ToolApprovalBypassResult):
        return bypass_decision
    if bypass_decision:
        return None
    if resolved_context.config is None or resolved_context.runtime_paths is None:
        return None

    try:
        approval_decision = await request_tool_approval_for_call(
            ToolApprovalCall(
                config=resolved_context.config,
                runtime_paths=resolved_context.runtime_paths,
                tool_name=tool_name,
                arguments=args,
                agent_name=resolved_context.agent_name,
                room_id=resolved_context.room_id,
                thread_id=resolved_context.thread_id,
                requester_id=resolved_context.requester_id,
                workflow_origin=workflow_origin,
            ),
        )
    except ToolApprovalScriptError:
        logger.warning("Tool approval policy failed", exc_info=True)
        return _format_declined_result(tool_name, _APPROVAL_POLICY_FAILURE_REASON)

    if approval_decision is None or approval_decision.status == "approved":
        return None

    return _format_declined_result(
        tool_name,
        _approval_status_reason(approval_decision.status, approval_decision.reason),
    )


async def _maybe_block_for_before_hooks(
    *,
    hook_registry: HookRegistry,
    resolved_context: _ResolvedToolContext,
    hook_arguments: dict[str, Any] | None,
    args: dict[str, Any],
    tool_name: str,
    has_before_hooks: bool,
) -> str | None:
    if not has_before_hooks:
        return None

    before_context = ToolBeforeCallContext(
        **resolved_context.hook_context_kwargs(hook_arguments if hook_arguments is not None else deepcopy(args)),
        tool_name=tool_name,
    )
    before_hooks_started_at = time.perf_counter()
    emit_timing_event(
        "Tool hook dispatch timing",
        phase="before_hooks_start",
        tool_name=tool_name,
        agent_name=resolved_context.agent_name or None,
    )
    await emit_gate(hook_registry, EVENT_TOOL_BEFORE_CALL, before_context)
    emit_timing_event(
        "Tool hook dispatch timing",
        phase="before_hooks_finish",
        tool_name=tool_name,
        agent_name=resolved_context.agent_name or None,
        declined=before_context.declined,
        duration_ms=elapsed_ms_since(before_hooks_started_at, clock=time.perf_counter, ndigits=2),
    )
    if not before_context.declined:
        return None

    return _format_declined_result(tool_name, before_context.decline_reason)


async def _finish_blocked_tool_call(
    *,
    timing: _ToolBridgeTiming,
    hook_registry: HookRegistry,
    resolved_context: _ResolvedToolContext,
    hook_arguments: dict[str, Any] | None,
    args: dict[str, Any],
    tool_name: str,
    blocked_result: str,
    has_after_hooks: bool,
    outcome: str,
) -> str:
    duration_ms = timing.mark_result_ready()
    await _maybe_emit_after_call_timed(
        has_after_hooks=has_after_hooks,
        timing=timing,
        hook_registry=hook_registry,
        resolved_context=resolved_context,
        hook_arguments=hook_arguments,
        args=args,
        tool_name=tool_name,
        result=blocked_result,
        error=None,
        blocked=True,
        duration_ms=duration_ms,
    )
    timing.emit_finish(
        tool_name=tool_name,
        agent_name=resolved_context.agent_name or None,
        outcome=outcome,
    )
    return blocked_result


@dataclass(slots=True)
class _ToolBridgeTiming:
    started_at: float
    before_hooks_ms: float | None = None
    approval_ms: float | None = None
    tool_body_ms: float | None = None
    result_ready_ms: float | None = None
    after_hooks_ms: float | None = None

    def record_timing(self) -> ToolCallTiming:
        """Return phases persisted to tool_calls.jsonl; after hooks stay debug-event only."""
        return ToolCallTiming(
            before_hooks_ms=self.before_hooks_ms,
            approval_ms=self.approval_ms,
            tool_body_ms=self.tool_body_ms,
            result_ready_ms=self.result_ready_ms,
        )

    def mark_result_ready(self) -> float:
        duration_ms = elapsed_ms_since(self.started_at, clock=time.perf_counter, ndigits=2)
        self.result_ready_ms = duration_ms
        return duration_ms

    def emit_finish(self, *, tool_name: str, agent_name: str | None, outcome: str) -> None:
        emit_timing_event(
            "Tool hook dispatch timing",
            phase="bridge_finish",
            tool_name=tool_name,
            agent_name=agent_name,
            outcome=outcome,
            before_hooks_ms=self.before_hooks_ms,
            approval_ms=self.approval_ms,
            tool_body_ms=self.tool_body_ms,
            result_ready_ms=self.result_ready_ms,
            after_hooks_ms=self.after_hooks_ms,
            total_bridge_ms=elapsed_ms_since(self.started_at, clock=time.perf_counter, ndigits=2),
        )


async def _emit_after_call_timed(
    *,
    hook_registry: HookRegistry,
    resolved_context: _ResolvedToolContext,
    hook_arguments: dict[str, Any] | None,
    args: dict[str, Any],
    tool_name: str,
    result: _ToolHookResult,
    error: BaseException | None,
    blocked: bool,
    duration_ms: float,
) -> float:
    started_at = time.perf_counter()
    await _emit_after_call(
        hook_registry=hook_registry,
        resolved_context=resolved_context,
        hook_arguments=hook_arguments,
        args=args,
        tool_name=tool_name,
        result=result,
        error=error,
        blocked=blocked,
        duration_ms=duration_ms,
    )
    return elapsed_ms_since(started_at, clock=time.perf_counter, ndigits=2)


async def _maybe_emit_after_call_timed(
    *,
    has_after_hooks: bool,
    timing: _ToolBridgeTiming,
    hook_registry: HookRegistry,
    resolved_context: _ResolvedToolContext,
    hook_arguments: dict[str, Any] | None,
    args: dict[str, Any],
    tool_name: str,
    result: _ToolHookResult,
    error: BaseException | None,
    blocked: bool,
    duration_ms: float,
) -> None:
    if not has_after_hooks:
        return
    timing.after_hooks_ms = await _emit_after_call_timed(
        hook_registry=hook_registry,
        resolved_context=resolved_context,
        hook_arguments=hook_arguments,
        args=args,
        tool_name=tool_name,
        result=result,
        error=error,
        blocked=blocked,
        duration_ms=duration_ms,
    )


async def _finish_successful_tool_call(
    *,
    timing: _ToolBridgeTiming,
    hook_registry: HookRegistry,
    resolved_context: _ResolvedToolContext,
    dispatch_context: ToolDispatchContext | None,
    hook_arguments: dict[str, Any] | None,
    args: dict[str, Any],
    tool_name: str,
    result: _ToolHookResult,
    has_after_hooks: bool,
    outcome: str,
) -> _ToolHookResult:
    duration_ms = timing.mark_result_ready()
    _record_debug_tool_success(
        tool_name=tool_name,
        arguments=args,
        result=result,
        duration_ms=duration_ms,
        timing=timing.record_timing(),
        resolved_context=resolved_context,
        dispatch_context=dispatch_context,
    )
    await _maybe_emit_after_call_timed(
        has_after_hooks=has_after_hooks,
        timing=timing,
        hook_registry=hook_registry,
        resolved_context=resolved_context,
        hook_arguments=hook_arguments,
        args=args,
        tool_name=tool_name,
        result=result,
        error=None,
        blocked=False,
        duration_ms=duration_ms,
    )
    timing.emit_finish(
        tool_name=tool_name,
        agent_name=resolved_context.agent_name or None,
        outcome=outcome,
    )
    return result


async def _execute_bridge(
    *,
    hook_registry: HookRegistry,
    tool_name: str,
    func: Callable[..., Any],
    approval_entrypoint: Callable[..., Any] | None,
    args: dict[str, Any],
    agent_name: str | None,
    dispatch_context: ToolDispatchContext | None,
    config: Config | None,
    runtime_paths: RuntimePaths | None,
    has_before_hooks: bool,
    has_after_hooks: bool,
    workflow_origin: ToolCallWorkflowOrigin | None,
    bypass_result_transform: _ToolBypassResultTransform | None,
) -> _ToolHookResult:
    started_at = time.perf_counter()
    timing = _ToolBridgeTiming(started_at=started_at)
    effective_dispatch_context = _explicit_bridge_dispatch_context(dispatch_context) or _ambient_tool_dispatch_context()
    bridge_context = _ToolHookBridgeContext(
        agent_name=agent_name,
        config=config,
        runtime_paths=runtime_paths,
        dispatch_context=effective_dispatch_context,
    )
    resolved_context = _resolve_tool_context(
        bridge_context=bridge_context,
    )
    emit_timing_event(
        "Tool hook dispatch timing",
        phase="bridge_entry",
        tool_name=tool_name,
        agent_name=resolved_context.agent_name or None,
        has_before_hooks=has_before_hooks,
        has_after_hooks=has_after_hooks,
    )
    hook_arguments = deepcopy(args) if has_before_hooks or has_after_hooks else None
    before_hooks_started_at = time.perf_counter()
    blocked_result = await _maybe_block_for_before_hooks(
        hook_registry=hook_registry,
        resolved_context=resolved_context,
        hook_arguments=hook_arguments,
        args=args,
        tool_name=tool_name,
        has_before_hooks=has_before_hooks,
    )
    if has_before_hooks:
        timing.before_hooks_ms = elapsed_ms_since(before_hooks_started_at, clock=time.perf_counter, ndigits=2)
    if blocked_result is not None:
        return await _finish_blocked_tool_call(
            timing=timing,
            hook_registry=hook_registry,
            resolved_context=resolved_context,
            hook_arguments=hook_arguments,
            args=args,
            tool_name=tool_name,
            blocked_result=blocked_result,
            has_after_hooks=has_after_hooks,
            outcome="blocked_before_hooks",
        )

    approval_started_at = time.perf_counter()
    approval_gate_result = await _resolve_tool_approval_gate(
        resolved_context=resolved_context,
        func=func,
        approval_entrypoint=approval_entrypoint,
        args=args,
        tool_name=tool_name,
        workflow_origin=workflow_origin,
    )
    timing.approval_ms = elapsed_ms_since(approval_started_at, clock=time.perf_counter, ndigits=2)
    if isinstance(approval_gate_result, ToolApprovalBypassResult):
        bypass_entrypoint = approval_entrypoint if approval_entrypoint is not None else func
        result = approval_gate_result.result
        if bypass_result_transform is not None:
            result = bypass_result_transform(bypass_entrypoint, result, args)
        return await _finish_successful_tool_call(
            timing=timing,
            hook_registry=hook_registry,
            resolved_context=resolved_context,
            dispatch_context=effective_dispatch_context,
            hook_arguments=hook_arguments,
            args=args,
            tool_name=tool_name,
            result=result,
            has_after_hooks=has_after_hooks,
            outcome="approval_bypass_result",
        )
    if approval_gate_result is not None:
        return await _finish_blocked_tool_call(
            timing=timing,
            hook_registry=hook_registry,
            resolved_context=resolved_context,
            hook_arguments=hook_arguments,
            args=args,
            tool_name=tool_name,
            blocked_result=approval_gate_result,
            has_after_hooks=has_after_hooks,
            outcome="blocked_approval",
        )

    result: _ToolHookResult = None
    error: BaseException | None = None
    tool_body_started_at = time.perf_counter()
    try:
        result = await _call_tool(
            func,
            args,
            tool_name=tool_name,
            agent_name=resolved_context.agent_name or None,
        )
        timing.tool_body_ms = elapsed_ms_since(tool_body_started_at, clock=time.perf_counter, ndigits=2)
    except OAuthConnectionRequired as exc:
        timing.tool_body_ms = elapsed_ms_since(tool_body_started_at, clock=time.perf_counter, ndigits=2)
        result = oauth_connection_required_payload(exc)
        duration_ms = timing.mark_result_ready()
        _record_debug_tool_success(
            tool_name=tool_name,
            arguments=args,
            result=result,
            duration_ms=duration_ms,
            timing=timing.record_timing(),
            resolved_context=resolved_context,
            dispatch_context=effective_dispatch_context,
        )
        await _maybe_emit_after_call_timed(
            has_after_hooks=has_after_hooks,
            timing=timing,
            hook_registry=hook_registry,
            resolved_context=resolved_context,
            hook_arguments=hook_arguments,
            args=args,
            tool_name=tool_name,
            result=result,
            error=None,
            blocked=False,
            duration_ms=duration_ms,
        )
        timing.emit_finish(
            tool_name=tool_name,
            agent_name=resolved_context.agent_name or None,
            outcome="oauth_connection_required",
        )
        return result
    except BaseException as exc:
        error = exc
        timing.tool_body_ms = elapsed_ms_since(tool_body_started_at, clock=time.perf_counter, ndigits=2)
        duration_ms = timing.mark_result_ready()
        try:
            failure_record = record_tool_failure(
                tool_name=tool_name,
                arguments=args,
                error=error,
                duration_ms=duration_ms,
                timing=timing.record_timing(),
                agent_name=resolved_context.agent_name or None,
                room_id=resolved_context.room_id,
                thread_id=resolved_context.thread_id,
                reply_to_event_id=resolved_context.reply_to_event_id,
                requester_id=resolved_context.requester_id,
                session_id=resolved_context.session_id,
                correlation_id=resolved_context.correlation_id,
                execution_identity=(
                    effective_dispatch_context.execution_identity if effective_dispatch_context is not None else None
                ),
                runtime_paths=resolved_context.runtime_paths,
            )
        except Exception:
            logger.exception(
                "Failed to record tool failure",
                tool_name=tool_name,
                correlation_id=resolved_context.correlation_id,
            )
        else:
            logger.warning(
                "Tool call failed",
                tool_name=tool_name,
                agent_name=resolved_context.agent_name or None,
                error_type=failure_record.error_type,
                error_message=failure_record.error_message,
                duration_ms=failure_record.duration_ms,
                correlation_id=resolved_context.correlation_id,
                channel=resolved_context.channel,
            )
        await _maybe_emit_after_call_timed(
            has_after_hooks=has_after_hooks,
            timing=timing,
            hook_registry=hook_registry,
            resolved_context=resolved_context,
            hook_arguments=hook_arguments,
            args=args,
            tool_name=tool_name,
            result=None,
            error=error,
            blocked=False,
            duration_ms=duration_ms,
        )
        timing.emit_finish(
            tool_name=tool_name,
            agent_name=resolved_context.agent_name or None,
            outcome="error",
        )
        raise

    return await _finish_successful_tool_call(
        timing=timing,
        hook_registry=hook_registry,
        resolved_context=resolved_context,
        dispatch_context=effective_dispatch_context,
        hook_arguments=hook_arguments,
        args=args,
        tool_name=tool_name,
        result=result,
        has_after_hooks=has_after_hooks,
        outcome="success",
    )


def build_tool_hook_bridge(
    hook_registry: HookRegistry,
    agent_name: str | None,
    dispatch_context: ToolDispatchContext | None = None,
    config: Config | None = None,
    runtime_paths: RuntimePaths | None = None,
    workflow_origin: ToolCallWorkflowOrigin | None = None,
    bypass_result_transform: _ToolBypassResultTransform | None = None,
) -> Callable[..., Any]:
    """Return one Agno-compatible tool hook bridge."""
    has_before_hooks = hook_registry.has_hooks(EVENT_TOOL_BEFORE_CALL)
    has_after_hooks = hook_registry.has_hooks(EVENT_TOOL_AFTER_CALL)

    async def bridge(
        name: str,
        func: Callable[..., Any],
        args: dict[str, Any],
        approval_entrypoint: Callable[..., Any] | None = None,
    ) -> _ToolHookResult:
        return await _execute_bridge(
            hook_registry=hook_registry,
            tool_name=name,
            func=func,
            approval_entrypoint=approval_entrypoint,
            args=args,
            agent_name=agent_name,
            dispatch_context=dispatch_context,
            config=config,
            runtime_paths=runtime_paths,
            has_before_hooks=has_before_hooks,
            has_after_hooks=has_after_hooks,
            workflow_origin=workflow_origin,
            bypass_result_transform=bypass_result_transform,
        )

    def sync_bridge(
        name: str,
        func: Callable[..., Any],
        args: dict[str, Any],
        approval_entrypoint: Callable[..., Any] | None = None,
    ) -> _ToolHookResult:
        if inspect.iscoroutinefunction(func):
            return _DeferredAsyncToolHookResult(
                _execute_bridge(
                    hook_registry=hook_registry,
                    tool_name=name,
                    func=func,
                    approval_entrypoint=approval_entrypoint,
                    args=args,
                    agent_name=agent_name,
                    dispatch_context=dispatch_context,
                    config=config,
                    runtime_paths=runtime_paths,
                    has_before_hooks=has_before_hooks,
                    has_after_hooks=has_after_hooks,
                    workflow_origin=workflow_origin,
                    bypass_result_transform=bypass_result_transform,
                ),
            )
        return _run_coroutine_from_sync(
            _execute_bridge(
                hook_registry=hook_registry,
                tool_name=name,
                func=func,
                approval_entrypoint=approval_entrypoint,
                args=args,
                agent_name=agent_name,
                dispatch_context=dispatch_context,
                config=config,
                runtime_paths=runtime_paths,
                has_before_hooks=has_before_hooks,
                has_after_hooks=has_after_hooks,
                workflow_origin=workflow_origin,
                bypass_result_transform=bypass_result_transform,
            ),
        )

    _SYNC_BRIDGES[bridge] = sync_bridge
    return bridge


def prepend_tool_hook_bridge(
    toolkit: Toolkit,
    bridge: Callable[..., Any] | None,
) -> Toolkit:
    """Prepend one bridge hook to every function in a toolkit, preserving existing hooks."""
    if bridge is None:
        return toolkit

    seen_functions: set[int] = set()
    for function in (*toolkit.functions.values(), *toolkit.async_functions.values()):
        if id(function) in seen_functions:
            continue
        seen_functions.add(id(function))
        _prepend_function_tool_hook(function, bridge)
    return toolkit


def _prepend_function_tool_hook(function: Function, bridge: Callable[..., Any]) -> None:
    sync_bridge = _SYNC_BRIDGES.get(bridge)
    bridge_hooks = [sync_bridge if sync_bridge is not None else bridge]
    bridge_hook_ids = {id(hook) for hook in bridge_hooks}

    existing_hooks = [
        hook for hook in list(function.tool_hooks or []) if not _is_bridge_hook_for_bases(hook, bridge_hook_ids)
    ]
    function.tool_hooks = [
        *[_bind_bridge_hook_to_entrypoint(hook, function.entrypoint) for hook in bridge_hooks],
        *existing_hooks,
    ]


def _bind_bridge_hook_to_entrypoint(
    hook: Callable[..., Any],
    approval_entrypoint: Callable[..., Any] | None,
) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(hook):

        @wraps(hook)
        async def async_bound_bridge_hook(name: str, func: Callable[..., Any], args: dict[str, Any]) -> _ToolHookResult:
            return await hook(name, func, args, approval_entrypoint=approval_entrypoint)

        _BOUND_BRIDGE_HOOK_BASES[async_bound_bridge_hook] = hook
        return async_bound_bridge_hook

    @wraps(hook)
    def bound_bridge_hook(name: str, func: Callable[..., Any], args: dict[str, Any]) -> _ToolHookResult:
        return hook(name, func, args, approval_entrypoint=approval_entrypoint)

    _BOUND_BRIDGE_HOOK_BASES[bound_bridge_hook] = hook
    return bound_bridge_hook


def _is_bridge_hook_for_bases(hook: Callable[..., Any], bridge_hook_ids: set[int]) -> bool:
    if id(hook) in bridge_hook_ids:
        return True
    try:
        base_hook = _BOUND_BRIDGE_HOOK_BASES.get(hook)
    except TypeError:
        return False
    return base_hook is not None and id(base_hook) in bridge_hook_ids
