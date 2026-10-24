"""MindRoom's tool-call policy around every tool call.

Plugin before and after hooks, the background-script approval gate, job and authority checkpoints, durable call
recording, timing, and failure records. The owned loop, voice calls, scheduled tool calls, script runs, and the MCP
gateway run it as a ``ToolMiddleware`` (``build_tool_hook_middleware``).
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    emit,
    emit_gate,
)
from mindroom.llm.request_context import current_llm_request_log_context
from mindroom.llm.runs import denial_note_for
from mindroom.logging_config import get_logger
from mindroom.oauth.providers import OAuthConnectionRequired, oauth_connection_required_payload
from mindroom.timing import elapsed_ms_since, emit_timing_event
from mindroom.tool_jobs.control import job_checkpoint
from mindroom.tool_jobs.execution_authority import check_current_execution_authority
from mindroom.tool_system.call_record import tool_call_recorder
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
    from collections.abc import Awaitable, Callable, Iterator
    from concurrent.futures import Executor

    from mindroom.approval.tool_approval import BackgroundScriptToolOrigin, ToolApprovalDecision
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import (
        HookMatrixAdmin,
        HookMessageSender,
        HookRegistry,
        HookRegistryState,
        HookRoomStatePutter,
        HookRoomStateQuerier,
    )
    from mindroom.tool_system.invocation import ToolCall, ToolMiddleware, ToolNext
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_DECLINED_RESULT_TEMPLATE = (
    "[TOOL CALL DECLINED]\n"
    "Tool: {tool_name}\n"
    "Reason: {reason}\n\n"
    "Adjust your approach — try a different tool or different arguments."
)
_ToolHookResult = Any


@dataclass(frozen=True, slots=True)
class BackgroundToolApprovalDenied:
    """Typed background result that the broker publishes as a terminal denial."""

    reason: str


# The approval callback between before-call hooks and the tool body: the terminal decision for one automation origin.
type _ToolApprovalGate = Callable[[BackgroundScriptToolOrigin, str, dict[str, Any]], Awaitable[ToolApprovalDecision]]


logger = get_logger(__name__)


@dataclass(slots=True)
class SyncToolCompletionTracker:
    """Expose one context-bound synchronous leaf task to its resource owner.

    ``executor`` runs the entrypoint; ``None`` uses the event loop's default executor.
    An owner that supplies an executor also runs and retains the blocking call an async
    entrypoint offloads, such as a worker proxy request, so its capacity covers that thread.
    """

    executor: Executor | None = None
    task: asyncio.Task[_ToolHookResult] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _cancelled_before_start: bool = field(default=False, init=False, repr=False)

    def track(self, task: asyncio.Task[_ToolHookResult]) -> None:
        """Record the one real synchronous entrypoint started in this call."""
        if self.task is not None:
            msg = "A tool call cannot start more than one synchronous entrypoint."
            raise RuntimeError(msg)
        self.task = task

    def _claim_start(self) -> bool:
        """Atomically claim actual entrypoint start against request cancellation."""
        with self._lock:
            if self._cancelled_before_start:
                return False
            self._started = True
            return True

    def _cancel_before_start(self) -> bool:
        """Return whether cancellation won before the entrypoint began."""
        with self._lock:
            if self._started:
                return False
            self._cancelled_before_start = True
            return True

    def started_task(self) -> asyncio.Task[_ToolHookResult] | None:
        """Return the completion task only after the real entrypoint has begun."""
        with self._lock:
            return self.task if self._started else None


_SYNC_TOOL_COMPLETION_TRACKER: ContextVar[SyncToolCompletionTracker | None] = ContextVar(
    "mindroom_sync_tool_completion_tracker",
    default=None,
)


@contextmanager
def track_sync_tool_completion(tracker: SyncToolCompletionTracker | None) -> Iterator[None]:
    """Bind synchronous leaf completion ownership to one tool call."""
    token = _SYNC_TOOL_COMPLETION_TRACKER.set(tracker)
    try:
        yield
    finally:
        _SYNC_TOOL_COMPLETION_TRACKER.reset(token)


@dataclass(frozen=True, slots=True, kw_only=True)
class _ResolvedToolContext:
    agent_name: str
    room_id: str | None = None
    thread_id: str | None = None
    reply_to_event_id: str | None
    requester_id: str | None = None
    session_id: str | None = None
    channel: str | None = None
    execution_identity: ToolExecutionIdentity | None = None
    config: Config | None
    runtime_paths: RuntimePaths | None
    correlation_id: str
    origin: BackgroundScriptToolOrigin | None
    message_sender: HookMessageSender | None = None
    matrix_admin: HookMatrixAdmin | None = None
    room_state_querier: HookRoomStateQuerier | None = None
    room_state_putter: HookRoomStatePutter | None = None
    message_received_depth: int = 0
    hook_registry_state: HookRegistryState | None = None

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
            "_hook_registry_state": self.hook_registry_state,
        }

    def record_kwargs(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name or None,
            "room_id": self.room_id,
            "thread_id": self.thread_id,
            "reply_to_event_id": self.reply_to_event_id,
            "requester_id": self.requester_id,
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "execution_identity": self.execution_identity,
            "runtime_paths": self.runtime_paths,
            "origin": self.origin,
        }


def _correlation_id_for_runtime_context(
    runtime_context: ToolRuntimeContext | None,
    origin: BackgroundScriptToolOrigin | None,
) -> str:
    if origin is not None:
        return f"background-script:{origin.run_id}:{origin.call_id}"
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
    dispatch_context: ToolDispatchContext | None,
    *,
    agent_name: str | None,
    config: Config | None,
    runtime_paths: RuntimePaths | None,
    origin: BackgroundScriptToolOrigin | None,
) -> _ResolvedToolContext:
    if isinstance(dispatch_context, LiveToolDispatchContext):
        runtime_context = dispatch_context.runtime_context
        identity = dispatch_context.execution_identity
        bindings = resolve_tool_runtime_hook_bindings(runtime_context)
        return _ResolvedToolContext(
            agent_name=agent_name or identity.agent_name,
            room_id=identity.room_id,
            thread_id=identity.resolved_thread_id or identity.thread_id,
            reply_to_event_id=runtime_context.reply_to_event_id,
            requester_id=identity.requester_id,
            session_id=identity.session_id,
            channel=identity.channel,
            execution_identity=identity,
            config=runtime_context.config,
            runtime_paths=runtime_context.runtime_paths,
            correlation_id=_correlation_id_for_runtime_context(runtime_context, origin),
            origin=origin,
            message_sender=bindings.message_sender,
            matrix_admin=bindings.matrix_admin,
            room_state_querier=bindings.room_state_querier,
            room_state_putter=bindings.room_state_putter,
            message_received_depth=bindings.message_received_depth,
            hook_registry_state=runtime_context.hook_registry_state,
        )

    reply_to_event_id = current_llm_request_log_context().get("reply_to_event_id")
    if not isinstance(reply_to_event_id, str):
        reply_to_event_id = None
    correlation_id = _correlation_id_for_runtime_context(None, origin)
    if dispatch_context is None:
        return _ResolvedToolContext(
            agent_name=agent_name or "",
            reply_to_event_id=reply_to_event_id,
            config=config,
            runtime_paths=runtime_paths,
            correlation_id=correlation_id,
            origin=origin,
        )
    identity = dispatch_context.execution_identity
    return _ResolvedToolContext(
        agent_name=agent_name or identity.agent_name,
        room_id=identity.room_id,
        thread_id=identity.resolved_thread_id or identity.thread_id,
        reply_to_event_id=reply_to_event_id,
        requester_id=identity.requester_id,
        session_id=identity.session_id,
        channel=identity.channel,
        execution_identity=identity,
        config=config,
        runtime_paths=runtime_paths,
        correlation_id=correlation_id,
        origin=origin,
    )


def _format_declined_result(tool_name: str, reason: str) -> str:
    return _DECLINED_RESULT_TEMPLATE.format(tool_name=tool_name, reason=reason)


async def run_sync_tool_entrypoint(call: Callable[[], _ToolHookResult]) -> _ToolHookResult:
    """Run one synchronous tool call off the event loop, retained by any completion owner."""
    tracker = _SYNC_TOOL_COMPLETION_TRACKER.get()

    def invoke() -> _ToolHookResult:
        if tracker is not None and not tracker._claim_start():
            raise asyncio.CancelledError
        return call()

    async def offload() -> _ToolHookResult:
        executor = tracker.executor if tracker is not None else None
        return await asyncio.get_running_loop().run_in_executor(executor, copy_context().run, invoke)

    task = asyncio.create_task(offload(), name="sync-tool-entrypoint")
    if tracker is None:
        return await task
    tracker.track(task)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if tracker._cancel_before_start():
            task.cancel()
        raise


async def run_async_entrypoint_blocking_call(
    call: Callable[[], _ToolHookResult],
    *,
    executor: Executor,
) -> _ToolHookResult:
    """Run one blocking call that an async entrypoint offloads to ``executor``.

    A completion owner with its own executor runs and retains the call there like a synchronous
    entrypoint; other callers keep prompt cancellation and leave the thread to finish alone.
    """
    tracker = _SYNC_TOOL_COMPLETION_TRACKER.get()
    if tracker is None or tracker.executor is None:
        return await asyncio.get_running_loop().run_in_executor(executor, copy_context().run, call)
    return await run_sync_tool_entrypoint(call)


# Plugins wrap each tool call by replacing this function, so callers look it up through the module at call time:
# mindroom-ai/agent-vault-bridge-plugin scopes the call's environment around it.
# tests/test_plugin_patched_seams.py pins every private seam a public plugin patches.
async def _call_tool(
    func: Callable[..., Awaitable[_ToolHookResult]],
    args: dict[str, Any],
    *,
    tool_name: str,
    agent_name: str | None,
) -> _ToolHookResult:
    emit_timing_event("Tool hook dispatch timing", phase="tool_entry", tool_name=tool_name, agent_name=agent_name)
    return await func(**args)


async def _call_recorded_tool(
    func: Callable[..., Awaitable[_ToolHookResult]],
    args: dict[str, Any],
    *,
    tool_name: str,
    agent_name: str | None,
) -> _ToolHookResult:
    """Run the tool, first recording the call where the work that makes it keeps its durable state."""
    recorder = tool_call_recorder()
    call_id = None if recorder is None else await recorder.started(tool_name, args)
    if recorder is None or call_id is None:
        return await _call_tool(func, args, tool_name=tool_name, agent_name=agent_name)
    try:
        result = await _call_tool(func, args, tool_name=tool_name, agent_name=agent_name)
    except Exception as exc:
        # It ran and failed. A cancelled call stays started, since it may have taken effect.
        await recorder.finished(call_id, tool_name, args, exc)
        raise
    await recorder.finished(call_id, tool_name, args, result)
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


async def _run_before_hooks(
    *,
    hook_registry: HookRegistry,
    resolved_context: _ResolvedToolContext,
    hook_arguments: dict[str, Any] | None,
    args: dict[str, Any],
    tool_name: str,
) -> str | None:
    """Run the before-call hooks; return the declined result when one of them declines the call."""
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


@dataclass(slots=True)
class _ToolBridgeTiming:
    started_at: float
    before_hooks_ms: float | None = None
    tool_body_ms: float | None = None
    result_ready_ms: float | None = None
    after_hooks_ms: float | None = None

    def record_timing(self) -> ToolCallTiming:
        """Return phases persisted to tool_calls.jsonl; after hooks stay debug-event only."""
        return ToolCallTiming(
            before_hooks_ms=self.before_hooks_ms,
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
            tool_body_ms=self.tool_body_ms,
            result_ready_ms=self.result_ready_ms,
            after_hooks_ms=self.after_hooks_ms,
            total_bridge_ms=elapsed_ms_since(self.started_at, clock=time.perf_counter, ndigits=2),
        )


def _record_success(
    tool_name: str,
    args: dict[str, Any],
    result: _ToolHookResult,
    duration_ms: float,
    timing: _ToolBridgeTiming,
    resolved_context: _ResolvedToolContext,
) -> None:
    """Record a successful call durably when the config logs LLM requests."""
    if not (resolved_context.config and resolved_context.config.debug.log_llm_requests):
        return
    record_tool_success(
        tool_name=tool_name,
        arguments=args,
        result=result,
        duration_ms=duration_ms,
        timing=timing.record_timing(),
        **resolved_context.record_kwargs(),
    )


def _record_failure(
    tool_name: str,
    args: dict[str, Any],
    error: BaseException,
    duration_ms: float,
    timing: _ToolBridgeTiming,
    resolved_context: _ResolvedToolContext,
) -> None:
    """Record and log a failed call; a record that cannot be written never masks the tool's own error."""
    try:
        failure_record = record_tool_failure(
            tool_name=tool_name,
            arguments=args,
            error=error,
            duration_ms=duration_ms,
            timing=timing.record_timing(),
            **resolved_context.record_kwargs(),
        )
    except Exception:
        logger.exception(
            "Failed to record tool failure",
            tool_name=tool_name,
            correlation_id=resolved_context.correlation_id,
        )
        return
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


async def _execute_bridge(  # noqa: C901
    tool_name: str,
    func: Callable[..., Awaitable[_ToolHookResult]],
    args: dict[str, Any],
    *,
    hook_registry: HookRegistry,
    agent_name: str | None,
    dispatch_context: ToolDispatchContext | None,
    config: Config | None,
    runtime_paths: RuntimePaths | None,
    has_before_hooks: bool,
    has_after_hooks: bool,
    origin: BackgroundScriptToolOrigin | None,
    approval_gate: _ToolApprovalGate | None,
) -> _ToolHookResult:
    timing = _ToolBridgeTiming(started_at=time.perf_counter())
    job_checkpoint()
    resolved_context = _resolve_tool_context(
        _explicit_bridge_dispatch_context(dispatch_context) or _ambient_tool_dispatch_context(),
        agent_name=agent_name,
        config=config,
        runtime_paths=runtime_paths,
        origin=origin,
    )
    resolved_agent_name = resolved_context.agent_name or None
    emit_timing_event(
        "Tool hook dispatch timing",
        phase="bridge_entry",
        tool_name=tool_name,
        agent_name=resolved_agent_name,
        has_before_hooks=has_before_hooks,
        has_after_hooks=has_after_hooks,
    )
    hook_arguments = deepcopy(args) if has_before_hooks or has_after_hooks else None

    async def finish(
        outcome: str,
        *,
        duration_ms: float,
        result: _ToolHookResult = None,
        error: BaseException | None = None,
        blocked: bool = False,
    ) -> None:
        if has_after_hooks:
            after_hooks_started_at = time.perf_counter()
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
            timing.after_hooks_ms = elapsed_ms_since(after_hooks_started_at, clock=time.perf_counter, ndigits=2)
        timing.emit_finish(tool_name=tool_name, agent_name=resolved_agent_name, outcome=outcome)

    async def succeed(result: _ToolHookResult, outcome: str) -> _ToolHookResult:
        duration_ms = timing.mark_result_ready()
        _record_success(tool_name, args, result, duration_ms, timing, resolved_context)
        await finish(outcome, duration_ms=duration_ms, result=result)
        return result

    if has_before_hooks:
        before_hooks_started_at = time.perf_counter()
        declined_result = await _run_before_hooks(
            hook_registry=hook_registry,
            resolved_context=resolved_context,
            hook_arguments=hook_arguments,
            args=args,
            tool_name=tool_name,
        )
        timing.before_hooks_ms = elapsed_ms_since(before_hooks_started_at, clock=time.perf_counter, ndigits=2)
        if declined_result is not None:
            duration_ms = timing.mark_result_ready()
            await finish("blocked_before_hooks", duration_ms=duration_ms, result=declined_result, blocked=True)
            return declined_result

    if origin is not None and approval_gate is not None:
        decision = await approval_gate(origin, tool_name, deepcopy(args))
        if not decision.approved:
            reason = denial_note_for(decision.reason)
            declined_result = _format_declined_result(tool_name, reason)
            duration_ms = timing.mark_result_ready()
            await finish("blocked_approval", duration_ms=duration_ms, result=declined_result, blocked=True)
            return BackgroundToolApprovalDenied(reason=reason)

    # Refuse the tool body to work that was stopped, or lost its grant, while its hooks and approval ran.
    try:
        job_checkpoint()
        check_current_execution_authority(arguments=args)
    except BaseException as stopped:
        await finish("blocked_before_body", duration_ms=timing.mark_result_ready(), error=stopped, blocked=True)
        raise

    tool_body_started_at = time.perf_counter()
    try:
        result = await _call_recorded_tool(func, args, tool_name=tool_name, agent_name=resolved_agent_name)
    except OAuthConnectionRequired as exc:
        timing.tool_body_ms = elapsed_ms_since(tool_body_started_at, clock=time.perf_counter, ndigits=2)
        return await succeed(oauth_connection_required_payload(exc), "oauth_connection_required")
    except BaseException as error:
        timing.tool_body_ms = elapsed_ms_since(tool_body_started_at, clock=time.perf_counter, ndigits=2)
        duration_ms = timing.mark_result_ready()
        _record_failure(tool_name, args, error, duration_ms, timing, resolved_context)
        await finish("error", duration_ms=duration_ms, error=error)
        raise
    timing.tool_body_ms = elapsed_ms_since(tool_body_started_at, clock=time.perf_counter, ndigits=2)
    return await succeed(result, "success")


async def dispatch_external_tool_hooks(
    *,
    hook_registry: HookRegistry,
    execution_identity: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    tool_name: str,
    arguments: dict[str, Any],
    before: bool,
    result: object = None,
    error: BaseException | None = None,
    blocked: bool = False,
    duration_ms: float = 0,
) -> str | None:
    """Dispatch one phase of a durable external tool through the ordinary hook policy."""
    dispatch = ToolDispatchContext(execution_identity=execution_identity)
    runtime_context = get_tool_runtime_context()
    if runtime_context is not None and execution_identity_matches_tool_runtime_context(
        replace(execution_identity, agent_name=runtime_context.agent_name),
        runtime_context,
    ):
        # Team members share the live turn's bindings but retain their own hook identity.
        dispatch = LiveToolDispatchContext.from_runtime_context(runtime_context)
    resolved = _resolve_tool_context(
        dispatch,
        agent_name=execution_identity.agent_name,
        config=config,
        runtime_paths=runtime_paths,
        origin=None,
    )
    if before:
        if not hook_registry.has_hooks(EVENT_TOOL_BEFORE_CALL):
            return None
        return await _run_before_hooks(
            hook_registry=hook_registry,
            resolved_context=resolved,
            hook_arguments=None,
            args=arguments,
            tool_name=tool_name,
        )
    if hook_registry.has_hooks(EVENT_TOOL_AFTER_CALL):
        await _emit_after_call(
            hook_registry=hook_registry,
            resolved_context=resolved,
            hook_arguments=None,
            args=arguments,
            tool_name=tool_name,
            result=result,
            error=error,
            blocked=blocked,
            duration_ms=duration_ms,
        )
    return None


def build_tool_hook_bridge(
    hook_registry: HookRegistry,
    agent_name: str | None,
    dispatch_context: ToolDispatchContext | None = None,
    config: Config | None = None,
    runtime_paths: RuntimePaths | None = None,
    origin: BackgroundScriptToolOrigin | None = None,
    approval_gate: _ToolApprovalGate | None = None,
) -> Callable[..., Any]:
    """Return the tool-call policy as one ``(name, func, args)`` hook around ``func``, which the middleware wraps.

    ``func`` is the middleware's async continuation, which runs the rest of the call.
    """
    return partial(
        _execute_bridge,
        hook_registry=hook_registry,
        agent_name=agent_name,
        dispatch_context=dispatch_context,
        config=config,
        runtime_paths=runtime_paths,
        has_before_hooks=hook_registry.has_hooks(EVENT_TOOL_BEFORE_CALL),
        has_after_hooks=hook_registry.has_hooks(EVENT_TOOL_AFTER_CALL),
        origin=origin,
        approval_gate=approval_gate,
    )


def build_tool_hook_middleware(
    hook_registry: HookRegistry,
    agent_name: str | None,
    dispatch_context: ToolDispatchContext | None = None,
    config: Config | None = None,
    runtime_paths: RuntimePaths | None = None,
    origin: BackgroundScriptToolOrigin | None = None,
    approval_gate: _ToolApprovalGate | None = None,
) -> ToolMiddleware:
    """Return the tool-call policy as the middleware around every tool call, in the loop and outside it.

    The call reaches its entrypoint only through ``call_next``, so a plugin before-hook that declines it, a
    revoked grant, or a Stop committed while the call was being recorded keeps the tool from running. A background
    script's call passes its ``origin`` and the ``approval_gate`` that asks its requester; a declined call then
    returns ``BackgroundToolApprovalDenied`` without running.
    """
    bridge = build_tool_hook_bridge(
        hook_registry,
        agent_name,
        dispatch_context,
        config,
        runtime_paths,
        origin=origin,
        approval_gate=approval_gate,
    )

    async def middleware(call: ToolCall, call_next: ToolNext) -> object:
        async def run(**arguments: Any) -> object:  # noqa: ANN401 - the hook chain passes arguments as kwargs
            return await call_next(arguments)

        return await bridge(call.function.name, run, call.arguments)

    return middleware
