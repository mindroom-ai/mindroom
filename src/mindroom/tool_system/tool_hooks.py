"""Bridge Agno per-function tool hooks into MindRoom's hook registry."""

from __future__ import annotations

import asyncio
import inspect
import time
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from threading import Thread
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4
from weakref import WeakKeyDictionary

from agno.tools.function import FunctionCall

from mindroom.hooks import (
    ToolAfterCallContext,
    ToolBeforeCallContext,
    emit,
    emit_gate,
)
from mindroom.hooks.types import EVENT_TOOL_AFTER_CALL, EVENT_TOOL_BEFORE_CALL
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import get_tool_runtime_context, resolve_tool_runtime_hook_bindings
from mindroom.tool_system.tool_failures import record_tool_failure
from mindroom.tool_system.worker_routing import active_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from agno.tools import Toolkit
    from agno.tools.function import Function

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks.registry import HookRegistry
    from mindroom.hooks.types import HookMessageSender, HookRoomStatePutter, HookRoomStateQuerier
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_DECLINED_RESULT_TEMPLATE = (
    "[TOOL CALL DECLINED]\n"
    "Tool: {tool_name}\n"
    "Reason: {reason}\n\n"
    "Adjust your approach — try a different tool or different arguments."
)
_SYNC_BRIDGES: WeakKeyDictionary[Callable[..., Any], Callable[..., Any]] = WeakKeyDictionary()
ToolHookResult = Any
_ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN_ASYNC = FunctionCall._build_nested_execution_chain_async
_AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED = False
logger = get_logger(__name__)


@dataclass(slots=True)
class _DeferredAsyncToolHookResult:
    """Sentinel used when a sync hook needs async completion on the current loop."""

    awaitable: Awaitable[ToolHookResult]


def _resolved_thread_id(
    default_thread_id: str | None,
    execution_identity: ToolExecutionIdentity | None,
    runtime_context: ToolRuntimeContext | None,
) -> str | None:
    if runtime_context is not None and runtime_context.resolved_thread_id is not None:
        return runtime_context.resolved_thread_id

    if execution_identity is not None and execution_identity.resolved_thread_id is not None:
        return execution_identity.resolved_thread_id

    return default_thread_id


@dataclass(frozen=True, slots=True)
class _ResolvedToolContext:
    agent_name: str
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    session_id: str | None
    channel: str | None
    config: Config | None
    runtime_paths: RuntimePaths | None
    correlation_id: str
    message_sender: HookMessageSender | None
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
            "room_state_querier": self.room_state_querier,
            "room_state_putter": self.room_state_putter,
            "message_received_depth": self.message_received_depth,
        }


def _coalesce(*values: str | None) -> str | None:
    for value in values:
        if value is not None:
            return value
    return None


def _resolve_tool_context(
    *,
    agent_name: str | None,
    room_id: str | None,
    thread_id: str | None,
    requester_id: str | None,
    session_id: str | None,
    execution_identity: ToolExecutionIdentity | None,
    config: Config | None,
    runtime_paths: RuntimePaths | None,
) -> _ResolvedToolContext:
    runtime_context = get_tool_runtime_context()
    resolved_execution_identity = active_tool_execution_identity(execution_identity)
    request_runtime_context = runtime_context if execution_identity is None else None
    bindings = resolve_tool_runtime_hook_bindings(runtime_context) if runtime_context is not None else None
    return _ResolvedToolContext(
        agent_name=(
            _coalesce(
                agent_name,
                runtime_context.agent_name if runtime_context is not None else None,
                resolved_execution_identity.agent_name if resolved_execution_identity is not None else None,
            )
            or ""
        ),
        room_id=_coalesce(
            room_id,
            request_runtime_context.room_id if request_runtime_context is not None else None,
            resolved_execution_identity.room_id if resolved_execution_identity is not None else None,
        ),
        thread_id=_resolved_thread_id(thread_id, resolved_execution_identity, request_runtime_context),
        requester_id=_coalesce(
            requester_id,
            request_runtime_context.requester_id if request_runtime_context is not None else None,
            resolved_execution_identity.requester_id if resolved_execution_identity is not None else None,
        ),
        session_id=_coalesce(
            session_id,
            request_runtime_context.session_id if request_runtime_context is not None else None,
            resolved_execution_identity.session_id if resolved_execution_identity is not None else None,
        ),
        channel=resolved_execution_identity.channel if resolved_execution_identity is not None else None,
        config=runtime_context.config if runtime_context is not None else config,
        runtime_paths=runtime_context.runtime_paths if runtime_context is not None else runtime_paths,
        correlation_id=(
            runtime_context.correlation_id
            if runtime_context is not None and runtime_context.correlation_id
            else "tool-hook:" + uuid4().hex
        ),
        message_sender=bindings.message_sender if bindings is not None else None,
        room_state_querier=bindings.room_state_querier if bindings is not None else None,
        room_state_putter=bindings.room_state_putter if bindings is not None else None,
        message_received_depth=bindings.message_received_depth if bindings is not None else 0,
    )


def _format_declined_result(tool_name: str, reason: str) -> str:
    return _DECLINED_RESULT_TEMPLATE.format(tool_name=tool_name, reason=reason)


async def _await_result(awaitable: Awaitable[ToolHookResult]) -> ToolHookResult:
    return await awaitable


def _run_coroutine_from_sync(coroutine: ToolHookResult) -> ToolHookResult:
    if not inspect.isawaitable(coroutine):
        return coroutine
    runner_coroutine = cast("Coroutine[Any, Any, ToolHookResult]", _await_result(coroutine))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(runner_coroutine)

    result: ToolHookResult = None
    error: BaseException | None = None
    context = copy_context()

    def runner() -> None:
        nonlocal error, result
        try:
            result = context.run(asyncio.run, runner_coroutine)
        except BaseException as exc:  # pragma: no cover - re-raised in caller thread
            error = exc

    thread = Thread(target=runner)
    thread.start()
    thread.join()
    if error is not None:
        raise error
    return result


def _patch_agno_async_tool_hook_chain() -> None:
    """Teach Agno's async tool hook chain to unwrap deferred sync-hook awaitables."""
    global _AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED

    if _AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED:
        return

    @wraps(_ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN_ASYNC)
    async def _patched_build_nested_execution_chain_async(
        self: FunctionCall,
        entrypoint_args: dict[str, Any],
    ) -> Callable[..., Awaitable[ToolHookResult]]:
        execution_chain = await _ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN_ASYNC(self, entrypoint_args)

        async def _wrapped_execution_chain(name: str, func: Callable[..., Any], args: dict[str, Any]) -> ToolHookResult:
            result = await execution_chain(name, func, args)
            while isinstance(result, _DeferredAsyncToolHookResult):
                result = await result.awaitable
            return result

        return _wrapped_execution_chain

    type.__setattr__(FunctionCall, "_build_nested_execution_chain_async", _patched_build_nested_execution_chain_async)
    _AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED = True


_patch_agno_async_tool_hook_chain()


async def _call_tool(func: Callable[..., Any], args: dict[str, Any]) -> ToolHookResult:
    result = func(**args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _execute_bridge(
    *,
    hook_registry: HookRegistry,
    tool_name: str,
    func: Callable[..., Any],
    args: dict[str, Any],
    agent_name: str | None,
    room_id: str | None,
    thread_id: str | None,
    requester_id: str | None,
    session_id: str | None,
    execution_identity: ToolExecutionIdentity | None,
    config: Config | None,
    runtime_paths: RuntimePaths | None,
    has_before_hooks: bool,
    has_after_hooks: bool,
) -> ToolHookResult:
    started_at = time.perf_counter()
    resolved_context = _resolve_tool_context(
        agent_name=agent_name,
        room_id=room_id,
        thread_id=thread_id,
        requester_id=requester_id,
        session_id=session_id,
        execution_identity=execution_identity,
        config=config,
        runtime_paths=runtime_paths,
    )
    hook_arguments = deepcopy(args) if has_before_hooks or has_after_hooks else None

    if has_before_hooks:
        before_context = ToolBeforeCallContext(
            **resolved_context.hook_context_kwargs(hook_arguments if hook_arguments is not None else deepcopy(args)),
            tool_name=tool_name,
        )
        await emit_gate(hook_registry, EVENT_TOOL_BEFORE_CALL, before_context)
        if before_context.declined:
            result = _format_declined_result(tool_name, before_context.decline_reason)
            if has_after_hooks:
                after_context = ToolAfterCallContext(
                    **resolved_context.hook_context_kwargs(
                        hook_arguments if hook_arguments is not None else deepcopy(args),
                    ),
                    tool_name=tool_name,
                    result=result,
                    error=None,
                    blocked=True,
                    duration_ms=(time.perf_counter() - started_at) * 1000,
                )
                await emit(hook_registry, EVENT_TOOL_AFTER_CALL, after_context)
            return result

    result: ToolHookResult = None
    error: BaseException | None = None
    try:
        result = await _call_tool(func, args)
    except BaseException as exc:
        error = exc
        duration_ms = (time.perf_counter() - started_at) * 1000
        try:
            failure_record = record_tool_failure(
                tool_name=tool_name,
                arguments=args,
                error=error,
                duration_ms=duration_ms,
                agent_name=resolved_context.agent_name or None,
                room_id=resolved_context.room_id,
                thread_id=resolved_context.thread_id,
                requester_id=resolved_context.requester_id,
                session_id=resolved_context.session_id,
                correlation_id=resolved_context.correlation_id,
                execution_identity=active_tool_execution_identity(execution_identity),
                runtime_paths=resolved_context.runtime_paths,
            )
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
        except Exception:
            logger.exception(
                "Failed to record tool failure",
                tool_name=tool_name,
                correlation_id=resolved_context.correlation_id,
            )
        if has_after_hooks:
            after_context = ToolAfterCallContext(
                **resolved_context.hook_context_kwargs(
                    hook_arguments if hook_arguments is not None else deepcopy(args),
                ),
                tool_name=tool_name,
                result=None,
                error=error,
                blocked=False,
                duration_ms=duration_ms,
            )
            await emit(hook_registry, EVENT_TOOL_AFTER_CALL, after_context)
        raise

    if has_after_hooks:
        after_context = ToolAfterCallContext(
            **resolved_context.hook_context_kwargs(hook_arguments if hook_arguments is not None else deepcopy(args)),
            tool_name=tool_name,
            result=result,
            error=error,
            blocked=False,
            duration_ms=(time.perf_counter() - started_at) * 1000,
        )
        await emit(hook_registry, EVENT_TOOL_AFTER_CALL, after_context)
    return result


def build_tool_hook_bridge(
    hook_registry: HookRegistry,
    agent_name: str | None,
    room_id: str | None = None,
    thread_id: str | None = None,
    requester_id: str | None = None,
    session_id: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    config: Config | None = None,
    runtime_paths: RuntimePaths | None = None,
) -> Callable[..., Any]:
    """Return one Agno-compatible tool hook bridge."""
    has_before_hooks = hook_registry.has_hooks(EVENT_TOOL_BEFORE_CALL)
    has_after_hooks = hook_registry.has_hooks(EVENT_TOOL_AFTER_CALL)

    async def bridge(name: str, func: Callable[..., Any], args: dict[str, Any]) -> ToolHookResult:
        return await _execute_bridge(
            hook_registry=hook_registry,
            tool_name=name,
            func=func,
            args=args,
            agent_name=agent_name,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            session_id=session_id,
            execution_identity=execution_identity,
            config=config,
            runtime_paths=runtime_paths,
            has_before_hooks=has_before_hooks,
            has_after_hooks=has_after_hooks,
        )

    def sync_bridge(name: str, func: Callable[..., Any], args: dict[str, Any]) -> ToolHookResult:
        if inspect.iscoroutinefunction(func):
            return _DeferredAsyncToolHookResult(
                _execute_bridge(
                    hook_registry=hook_registry,
                    tool_name=name,
                    func=func,
                    args=args,
                    agent_name=agent_name,
                    room_id=room_id,
                    thread_id=thread_id,
                    requester_id=requester_id,
                    session_id=session_id,
                    execution_identity=execution_identity,
                    config=config,
                    runtime_paths=runtime_paths,
                    has_before_hooks=has_before_hooks,
                    has_after_hooks=has_after_hooks,
                ),
            )
        return _run_coroutine_from_sync(
            _execute_bridge(
                hook_registry=hook_registry,
                tool_name=name,
                func=func,
                args=args,
                agent_name=agent_name,
                room_id=room_id,
                thread_id=thread_id,
                requester_id=requester_id,
                session_id=session_id,
                execution_identity=execution_identity,
                config=config,
                runtime_paths=runtime_paths,
                has_before_hooks=has_before_hooks,
                has_after_hooks=has_after_hooks,
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
    is_async_entrypoint = inspect.iscoroutinefunction(function.entrypoint) or inspect.isasyncgenfunction(
        function.entrypoint,
    )
    bridge_hooks = [bridge] if is_async_entrypoint or sync_bridge is None else [sync_bridge]

    existing_hooks = [hook for hook in list(function.tool_hooks or []) if hook not in bridge_hooks]
    function.tool_hooks = [*bridge_hooks, *existing_hooks]
