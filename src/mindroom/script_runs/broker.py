"""Direct execution broker for governed background-script tool calls."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from agno.tools.function import Function, FunctionCall, FunctionExecutionResult

from mindroom.script_runs.models import (
    ScriptCallRecord,
    ScriptCallState,
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
)
from mindroom.script_runs.policy import effective_script_grants, resolve_current_script_tool_surface
from mindroom.script_runs.store import (
    ScriptCallNotFoundError,
    ScriptCapabilityError,
    ScriptRunNotFoundError,
    ScriptRunStore,
)
from mindroom.tool_approval import (
    AutomationToolOrigin,
    BackgroundScriptToolOrigin,
    ToolApprovalDecision,
    evaluate_tool_approval,
)
from mindroom.tool_system.automation_approval import build_automation_approval_config
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.tool_calls import sanitize_failure_text
from mindroom.tool_system.tool_hooks import (
    SyncToolCompletionTracker,
    build_tool_hook_bridge,
    prepend_tool_hook_bridge,
    track_sync_tool_completion,
)
from mindroom.tool_system.worker_proxy_client import to_json_compatible
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    ToolExecutionIdentity,
    build_agent_toolkit_worker_target,
    parse_tool_execution_identity_payload,
    run_with_tool_execution_identity,
)

if TYPE_CHECKING:
    from agno.tools import Toolkit

    from mindroom.config.main import Config

__all__ = [
    "ScriptBrokerAuthenticationError",
    "ScriptCallPreparationPendingError",
    "ScriptCallReceipt",
    "ScriptRuntimeResolver",
    "ScriptRuntimeWorkerAuthority",
    "ScriptToolBroker",
    "ScriptToolCallRequest",
    "digest_arguments",
]

_INDETERMINATE_ERROR = {
    "kind": "indeterminate",
    "message": "The call was accepted, but its terminal result cannot be determined safely.",
    "retryable": False,
}
_REVOKED_GRANT_ERROR = {
    "kind": "capability_revoked",
    "message": "The requested tool is no longer available to this script run.",
    "retryable": False,
}
_INVALID_RESULT_ERROR = {
    "kind": "invalid_tool_result",
    "message": "The tool returned a result that cannot be represented as strict JSON.",
    "retryable": False,
}
_MAX_MATERIALIZED_RESULT_BYTES = 64 * 1024
_MAX_MATERIALIZED_RESULT_ITEMS = 1_000
_NEVER_PREAPPROVE_TOOLKITS = frozenset({"claude_agent", "config_manager", "scheduler", "subagents"})
_ACTIVE_RUN_STATES = frozenset({ScriptRunState.STARTING, ScriptRunState.RUNNING})


class ScriptBrokerAuthenticationError(ValueError):
    """Raised when a gateway capability cannot be authenticated safely."""


class ScriptCallPreparationPendingError(RuntimeError):
    """Raised when a call is still authenticating and has no durable claim yet."""


class ScriptRuntimeResolver(Protocol):
    """Resolve live runtime and approval services for one durable script owner."""

    def resolve(self, run: ScriptRunRecord, *, correlation_id: str) -> ToolRuntimeContext:
        """Rebuild the live context for the durable run owner."""
        ...

    def resolve_worker_authority(
        self,
        run: ScriptRunRecord,
        *,
        context: ToolRuntimeContext,
    ) -> ScriptRuntimeWorkerAuthority:
        """Resolve the live worker allocation and context-derived routing authority."""
        ...

    async def request_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        context: ToolRuntimeContext,
        grant: ScriptToolGrant,
        arguments: dict[str, object],
        timeout_seconds: float,
    ) -> ToolApprovalDecision:
        """Await the bound requester's normal approval decision."""
        ...

    async def settle_approval(self, origin: BackgroundScriptToolOrigin, *, reason: str) -> None:
        """Settle an exact approval whose broker ownership ended indeterminately."""
        ...

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        """Settle only pending approvals after the run's broker ownership ends."""
        ...


class _BackgroundApprovalGate(Protocol):
    async def __call__(
        self,
        origin: AutomationToolOrigin,
        tool_name: str,
        arguments: dict[str, object],
    ) -> ToolApprovalDecision: ...


@dataclass(frozen=True, slots=True)
class ScriptRuntimeWorkerAuthority:
    """Live worker authority independently resolved for a durable script run."""

    worker_id: str | None
    local_unsafe: bool
    worker_target: ResolvedWorkerTarget


@dataclass(frozen=True, slots=True)
class _PreparedScriptCall:
    run: ScriptRunRecord
    call: ScriptCallRecord
    arguments: dict[str, object]
    created: bool


@dataclass(frozen=True, slots=True)
class _PreparedExecution:
    context: ToolRuntimeContext
    execution_identity: ToolExecutionIdentity
    toolkit: Toolkit
    function: Function
    approval_config: Config


@dataclass(frozen=True, slots=True)
class _AcceptedScriptCall:
    receipt: ScriptCallReceipt
    execution_task: asyncio.Task[ScriptCallReceipt] | None = None


class _CurrentGrantRevokedError(ValueError):
    """Raised when a launch grant is absent from the current live surface."""


class _InvalidToolResultError(ValueError):
    """Raised when a successful tool result is not strict JSON data."""


@dataclass(frozen=True, slots=True)
class ScriptToolCallRequest:
    """One token-free request for a stable logical tool call."""

    run_id: str
    call_id: str
    grant: ScriptToolGrant
    arguments: dict[str, object]

    @property
    def arguments_digest(self) -> str:
        """Return the canonical immutable digest claimed before execution."""
        return digest_arguments(self.arguments)


@dataclass(frozen=True, slots=True)
class ScriptCallReceipt:
    """JSON-wire representation of one durable script call receipt."""

    run_id: str
    call_id: str
    grant: ScriptToolGrant
    arguments_digest: str
    state: ScriptCallState
    created_at: str
    updated_at: str
    result: object | None = None
    error: object | None = None


def digest_arguments(arguments: Mapping[str, object]) -> str:
    """Hash one canonical JSON-wire argument object."""
    normalized = to_json_compatible(arguments)
    encoded = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _receipt_from_record(record: ScriptCallRecord) -> ScriptCallReceipt:
    return ScriptCallReceipt(
        run_id=record.run_id,
        call_id=record.call_id,
        grant=record.grant,
        arguments_digest=record.arguments_digest,
        state=record.state,
        created_at=record.created_at,
        updated_at=record.updated_at,
        result=record.result,
        error=record.error,
    )


def _background_origin(run: ScriptRunRecord, call: ScriptCallRecord) -> BackgroundScriptToolOrigin:
    return BackgroundScriptToolOrigin(
        run_id=run.run_id,
        call_id=call.call_id,
        requester_id=run.owner_user_id,
        toolkit_name=call.grant.toolkit_name,
        function_name=call.grant.function_name,
    )


@dataclass(slots=True)
class ScriptToolBroker:
    """Execute stable script calls through the ordinary registered-tool path."""

    store: ScriptRunStore
    runtime_resolver: ScriptRuntimeResolver
    _tasks: dict[tuple[str, str], asyncio.Task[ScriptCallReceipt]] = field(default_factory=dict, init=False)
    _preparing: dict[tuple[str, str], int] = field(default_factory=dict, init=False)
    _preparation_changed: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _run_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)
    _cleanup_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    def _prepare_call(self, request: ScriptToolCallRequest, token: str) -> _PreparedScriptCall:
        run = self.store.require_active_capability(request.run_id, token)
        claim = self.store.claim_call(
            run_id=run.run_id,
            call_id=request.call_id,
            grant=request.grant,
            arguments_digest=request.arguments_digest,
        )
        return _PreparedScriptCall(
            run=run,
            call=claim.call,
            arguments=request.arguments,
            created=claim.created,
        )

    def _prepare_authenticated_call(
        self,
        request: ScriptToolCallRequest,
        authorization: str | None,
    ) -> _PreparedScriptCall:
        token = self.authenticate(request.run_id, authorization)
        return self._prepare_call(request, token)

    async def _accept_prepared_call(
        self,
        request: ScriptToolCallRequest,
        *,
        authorization: str | None,
    ) -> _AcceptedScriptCall:
        key = (request.run_id, request.call_id)
        self._preparing[key] = self._preparing.get(key, 0) + 1
        preparation_finished = False
        try:
            prepared = await asyncio.to_thread(self._prepare_authenticated_call, request, authorization)

            if not prepared.created:
                owned_elsewhere = self._call_is_owned(key, exclude_current_preparation=True)
                self._finish_preparation(key)
                preparation_finished = True
                if prepared.call.state is ScriptCallState.PENDING and not owned_elsewhere:
                    return _AcceptedScriptCall(
                        receipt=await self._publish_async(
                            prepared.call,
                            state=ScriptCallState.INDETERMINATE,
                            error=_INDETERMINATE_ERROR,
                        ),
                    )
                return _AcceptedScriptCall(
                    receipt=_receipt_from_record(prepared.call),
                    execution_task=self._tasks.get(key),
                )

            task = asyncio.create_task(
                self._execute_claimed_call(prepared.run, prepared.call, prepared.arguments),
                name=f"script-tool:{prepared.run.run_id}:{prepared.call.call_id}",
            )
            self._tasks[key] = task
            self._finish_preparation(key)
            preparation_finished = True

            def forget_completed_task(completed: asyncio.Task[ScriptCallReceipt]) -> None:
                if self._tasks.get(key) is completed:
                    self._tasks.pop(key, None)
                if not any(active_key[0] == prepared.run.run_id for active_key in self._tasks):
                    self._run_locks.pop(prepared.run.run_id, None)

            task.add_done_callback(forget_completed_task)
            return _AcceptedScriptCall(
                receipt=_receipt_from_record(prepared.call),
                execution_task=task,
            )
        finally:
            if not preparation_finished:
                self._finish_preparation(key)

    def _finish_preparation(self, key: tuple[str, str]) -> None:
        remaining = self._preparing.get(key, 0) - 1
        if remaining > 0:
            self._preparing[key] = remaining
        else:
            self._preparing.pop(key, None)
        self._preparation_changed.set()

    def _call_is_owned(
        self,
        key: tuple[str, str],
        *,
        exclude_current_preparation: bool = False,
    ) -> bool:
        task = self._tasks.get(key)
        if task is not None and not task.done():
            return True
        preparation_count = self._preparing.get(key, 0)
        if exclude_current_preparation:
            preparation_count -= 1
        return preparation_count > 0

    def get_call(self, run_id: str, call_id: str) -> ScriptCallReceipt:
        """Return one stable durable call receipt."""
        key = (run_id, call_id)
        try:
            record = self.store.get_call(run_id, call_id)
        except ScriptCallNotFoundError:
            if self._call_is_owned(key):
                msg = "Background script call acceptance is not yet determined."
                raise ScriptCallPreparationPendingError(msg) from None
            raise
        if record.state is ScriptCallState.PENDING and not self._call_is_owned(key):
            record = self.store.publish_call_result(
                run_id=run_id,
                call_id=call_id,
                state=ScriptCallState.INDETERMINATE,
                error=_INDETERMINATE_ERROR,
            )
        return _receipt_from_record(record)

    def authenticate(self, run_id: str, authorization: str | None) -> str:
        """Authenticate a bearer capability with one constant-time comparison path."""
        token = _bearer_token(authorization)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        run: ScriptRunRecord | None
        try:
            run = self.store.get_run(run_id)
        except ScriptRunNotFoundError:
            run = None
        expected_hash = run.token_hash if run is not None else "0" * len(token_hash)
        matches = hmac.compare_digest(expected_hash, token_hash)
        if run is None or not matches or run.cancel_requested_at is not None or run.state not in _ACTIVE_RUN_STATES:
            msg = "Background script call is unavailable."
            raise ScriptBrokerAuthenticationError(msg)
        return token

    async def cancel_run(self, run_id: str) -> None:
        """Cancel this broker's work for a run whose capability is already revoked."""
        while any(key[0] == run_id for key in self._preparing):
            self._preparation_changed.clear()
            if any(key[0] == run_id for key in self._preparing):
                await self._preparation_changed.wait()
        active = [(key, task) for key, task in self._tasks.items() if key[0] == run_id and not task.done()]
        for _key, task in active:
            task.cancel()
        if active:
            await asyncio.gather(*(task for _key, task in active), return_exceptions=True)
        for (_claimed_run_id, call_id), _task in active:
            record = await asyncio.to_thread(self.store.get_call, run_id, call_id)
            if record.state is ScriptCallState.PENDING:
                await asyncio.to_thread(
                    self.store.publish_call_result,
                    run_id=run_id,
                    call_id=call_id,
                    state=ScriptCallState.INDETERMINATE,
                    error=_INDETERMINATE_ERROR,
                )
        pending = await asyncio.to_thread(self.store.pending_calls, run_id)
        for record in pending:
            await asyncio.to_thread(
                self.store.publish_call_result,
                run_id=run_id,
                call_id=record.call_id,
                state=ScriptCallState.INDETERMINATE,
                error=_INDETERMINATE_ERROR,
            )
        await self.runtime_resolver.settle_run_approvals(
            run_id,
            reason="Background script ownership was cancelled.",
        )

    async def accept_authenticated(
        self,
        request: ScriptToolCallRequest,
        authorization: str | None,
    ) -> ScriptCallReceipt:
        """Authenticate and durably claim one gateway call before acknowledging it."""
        accepted = await self._accept_prepared_call(request, authorization=authorization)
        return accepted.receipt

    async def get_authenticated(
        self,
        run_id: str,
        call_id: str,
        authorization: str | None,
    ) -> ScriptCallReceipt:
        """Authenticate a receipt and settle approval debt discovered as orphaned."""
        self.authenticate(run_id, authorization)
        receipt = await asyncio.to_thread(self.get_call, run_id, call_id)
        if receipt.state is ScriptCallState.INDETERMINATE:
            run, call = await asyncio.gather(
                asyncio.to_thread(self.store.get_run, run_id),
                asyncio.to_thread(self.store.get_call, run_id, call_id),
            )
            await self.runtime_resolver.settle_approval(
                _background_origin(run, call),
                reason="Background script call ownership was orphaned after restart.",
            )
        return receipt

    async def _execute_claimed_call(
        self,
        run: ScriptRunRecord,
        call: ScriptCallRecord,
        arguments: dict[str, object],
    ) -> ScriptCallReceipt:
        run_lock = self._run_locks.setdefault(run.run_id, asyncio.Lock())
        async with run_lock:
            try:
                durable_run = await asyncio.to_thread(self.store.require_call_dispatch_allowed, run.run_id)
            except ScriptCapabilityError:
                return await self._publish_async(
                    call,
                    state=ScriptCallState.FAILED,
                    error=_REVOKED_GRANT_ERROR,
                )
            return await self._execute_claimed_call_serialized(durable_run, call, arguments)

    async def _execute_claimed_call_serialized(
        self,
        run: ScriptRunRecord,
        call: ScriptCallRecord,
        arguments: dict[str, object],
    ) -> ScriptCallReceipt:
        origin = _background_origin(run, call)
        correlation_id = f"background-script:{run.run_id}:{call.call_id}"
        execution_started = False
        try:
            prepared = await asyncio.to_thread(
                self._prepare_execution,
                run,
                call,
                correlation_id,
            )
            toolkit = prepared.toolkit
            await _connect_toolkit(toolkit)
            execution_started = True
            execution, materialized = await self._run_prepared_execution(
                prepared,
                run=run,
                call=call,
                origin=origin,
                arguments=arguments,
            )

            if execution.status != "success":
                return await self._publish_async(
                    call,
                    state=ScriptCallState.FAILED,
                    error={
                        "kind": "tool_failure",
                        "message": sanitize_failure_text(execution.error or "Tool execution failed."),
                        "retryable": False,
                    },
                )
            result = _strict_json_result(materialized)
            return await self._publish_async(call, state=ScriptCallState.COMPLETED, result=result)
        except asyncio.CancelledError:
            raise
        except _CurrentGrantRevokedError:
            return await self._publish_async(call, state=ScriptCallState.FAILED, error=_REVOKED_GRANT_ERROR)
        except _InvalidToolResultError:
            return await self._publish_async(call, state=ScriptCallState.FAILED, error=_INVALID_RESULT_ERROR)
        except (ScriptCapabilityError, TypeError, ValueError) as exc:
            return await self._publish_async(
                call,
                state=ScriptCallState.FAILED,
                error={"kind": "call_rejected", "message": sanitize_failure_text(str(exc)), "retryable": False},
            )
        except BaseException as exc:
            state = ScriptCallState.INDETERMINATE if execution_started else ScriptCallState.FAILED
            kind = "indeterminate" if execution_started else "runtime_failure"
            error = (
                _INDETERMINATE_ERROR
                if execution_started
                else {"kind": kind, "message": sanitize_failure_text(str(exc)), "retryable": True}
            )
            return await self._publish_async(call, state=state, error=error)

    async def _run_prepared_execution(
        self,
        prepared: _PreparedExecution,
        *,
        run: ScriptRunRecord,
        call: ScriptCallRecord,
        origin: BackgroundScriptToolOrigin,
        arguments: dict[str, object],
    ) -> tuple[FunctionExecutionResult, object]:
        context = prepared.context
        toolkit = prepared.toolkit
        function = prepared.function
        completion_tracker = SyncToolCompletionTracker()
        cleanup_transferred = False

        async def execute_function() -> tuple[FunctionExecutionResult, object]:
            with tool_runtime_context(context), track_sync_tool_completion(completion_tracker):
                authored_decision = await _request_authored_confirmation(
                    runtime_resolver=self.runtime_resolver,
                    origin=origin,
                    context=context,
                    run=run,
                    call=call,
                    arguments=arguments,
                    approval_config=prepared.approval_config,
                    required=function.requires_confirmation is True,
                )
                approval_gate = _build_background_approval_gate(
                    runtime_resolver=self.runtime_resolver,
                    context=context,
                    run=run,
                    call=call,
                    approval_config=prepared.approval_config,
                    authored_decision=authored_decision,
                )
                bridge = build_tool_hook_bridge(
                    context.hook_registry,
                    agent_name=run.agent_name,
                    dispatch_context=LiveToolDispatchContext(
                        execution_identity=prepared.execution_identity,
                        runtime_context=context,
                    ),
                    config=prepared.approval_config,
                    runtime_paths=context.runtime_paths,
                    origin=origin,
                    approval_gate=approval_gate,
                )
                prepend_tool_hook_bridge(toolkit, bridge)
                if authored_decision is not None:
                    function.cache_results = function.cache_results and authored_decision.approved
                execution_result = await FunctionCall(
                    function=function,
                    arguments=arguments,
                    call_id=call.call_id,
                ).aexecute()
                materialized_result = await _materialize_successful_result(execution_result)
            return execution_result, materialized_result

        try:
            return await run_with_tool_execution_identity(
                prepared.execution_identity,
                operation=execute_function,
            )
        except asyncio.CancelledError:
            completion_task = completion_tracker.started_task()
            if completion_task is not None and not completion_task.done():
                self._retain_toolkit_cleanup(completion_task, toolkit)
                cleanup_transferred = True
            raise
        finally:
            if not cleanup_transferred:
                await _close_toolkit(toolkit)

    def _retain_toolkit_cleanup(
        self,
        completion_task: asyncio.Task[object],
        toolkit: Toolkit,
    ) -> None:
        async def cleanup() -> None:
            try:
                await completion_task
            finally:
                await _close_toolkit(toolkit)

        cleanup_task = asyncio.create_task(cleanup(), name="script-toolkit-cleanup")
        self._cleanup_tasks.add(cleanup_task)

        def forget_cleanup_task(completed: asyncio.Task[None]) -> None:
            self._cleanup_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        cleanup_task.add_done_callback(forget_cleanup_task)

    def _prepare_execution(
        self,
        run: ScriptRunRecord,
        call: ScriptCallRecord,
        correlation_id: str,
    ) -> _PreparedExecution:
        context = self.runtime_resolver.resolve(run, correlation_id=correlation_id)
        current_surface = resolve_current_script_tool_surface(context)
        if call.grant not in effective_script_grants(run.grants, current_surface.grants):
            raise _CurrentGrantRevokedError
        worker_authority = self.runtime_resolver.resolve_worker_authority(run, context=context)
        execution_identity = _validate_resolved_authority(run, context, worker_authority)
        toolkit = current_surface.toolkits_by_name.get(call.grant.toolkit_name)
        if toolkit is None:
            raise _CurrentGrantRevokedError
        function = _toolkit_function(toolkit, call.grant.function_name)
        return _PreparedExecution(
            context=context,
            execution_identity=execution_identity,
            toolkit=toolkit,
            function=function,
            approval_config=_background_approval_config(context, current_surface.toolkits_by_name),
        )

    async def _publish_async(
        self,
        call: ScriptCallRecord,
        *,
        state: ScriptCallState,
        result: object | None = None,
        error: object | None = None,
    ) -> ScriptCallReceipt:
        return await asyncio.to_thread(self._publish, call, state=state, result=result, error=error)

    def _publish(
        self,
        call: ScriptCallRecord,
        *,
        state: ScriptCallState,
        result: object | None = None,
        error: object | None = None,
    ) -> ScriptCallReceipt:
        try:
            stored = self.store.publish_call_result(
                run_id=call.run_id,
                call_id=call.call_id,
                state=state,
                result=result,
                error=error,
            )
        except BaseException:
            if state is not ScriptCallState.INDETERMINATE:
                try:
                    stored = self.store.publish_call_result(
                        run_id=call.run_id,
                        call_id=call.call_id,
                        state=ScriptCallState.INDETERMINATE,
                        error=_INDETERMINATE_ERROR,
                    )
                except BaseException:
                    return _receipt_from_record(
                        replace(call, state=ScriptCallState.INDETERMINATE, error=_INDETERMINATE_ERROR),
                    )
            else:
                return _receipt_from_record(replace(call, state=state, result=result, error=error))
        return _receipt_from_record(stored)


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        return ""
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def _validate_resolved_authority(
    run: ScriptRunRecord,
    context: ToolRuntimeContext,
    worker_authority: ScriptRuntimeWorkerAuthority,
) -> ToolExecutionIdentity:
    durable_identity = parse_tool_execution_identity_payload(
        run.execution_identity,
        strict=True,
        error_prefix="Background script execution_identity",
    )
    if durable_identity is None:
        msg = "Background script execution identity is unavailable."
        raise ValueError(msg)
    live_identity = build_execution_identity_from_runtime_context(context)
    live_config = context.current_config
    expected_process_worker_target = build_agent_toolkit_worker_target(
        "user_agent",
        context.agent_name,
        is_private=live_config.get_agent(context.agent_name).private is not None,
        execution_identity=durable_identity,
        runtime_paths=context.runtime_paths,
    )
    expected_tool_worker_target = build_agent_toolkit_worker_target(
        live_config.resolve_entity(context.agent_name).execution_scope,
        context.agent_name,
        is_private=live_config.get_agent(context.agent_name).private is not None,
        execution_identity=durable_identity,
        runtime_paths=context.runtime_paths,
    )
    expected_durable_worker_key = None if run.local_unsafe else expected_process_worker_target.worker_key
    if (
        durable_identity != live_identity
        or durable_identity.agent_name != run.agent_name
        or durable_identity.requester_id != run.owner_user_id
        or durable_identity.room_id != run.room_id
        or durable_identity.resolved_thread_id != run.thread_root_event_id
        or run.worker_key != expected_durable_worker_key
        or worker_authority.worker_id != run.worker_id
        or worker_authority.local_unsafe != run.local_unsafe
        or worker_authority.worker_target != expected_tool_worker_target
    ):
        msg = "Live script runtime context does not match the durable run owner."
        raise ValueError(msg)
    return durable_identity


def _build_background_approval_gate(
    *,
    runtime_resolver: ScriptRuntimeResolver,
    context: ToolRuntimeContext,
    run: ScriptRunRecord,
    call: ScriptCallRecord,
    approval_config: Config,
    authored_decision: ToolApprovalDecision | None,
) -> _BackgroundApprovalGate:

    async def approval_gate(
        origin: AutomationToolOrigin,
        tool_name: str,
        arguments: dict[str, object],
    ) -> ToolApprovalDecision:
        assert tool_name == call.grant.function_name
        if authored_decision is not None:
            return authored_decision
        policy_requires_approval, timeout_seconds = await evaluate_tool_approval(
            approval_config,
            context.runtime_paths,
            call.grant.function_name,
            arguments,
            run.agent_name,
        )
        if not policy_requires_approval:
            return ToolApprovalDecision(approved=True)
        assert isinstance(origin, BackgroundScriptToolOrigin)
        return await runtime_resolver.request_approval(
            origin=origin,
            context=context,
            grant=call.grant,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
        )

    return approval_gate


async def _request_authored_confirmation(
    *,
    runtime_resolver: ScriptRuntimeResolver,
    origin: BackgroundScriptToolOrigin,
    context: ToolRuntimeContext,
    run: ScriptRunRecord,
    call: ScriptCallRecord,
    arguments: dict[str, object],
    approval_config: Config,
    required: bool,
) -> ToolApprovalDecision | None:
    """Resolve function-authored confirmation before Agno can return a cached value."""
    if not required:
        return None
    _, timeout_seconds = await evaluate_tool_approval(
        approval_config,
        context.runtime_paths,
        call.grant.function_name,
        arguments,
        run.agent_name,
    )
    return await runtime_resolver.request_approval(
        origin=origin,
        context=context,
        grant=call.grant,
        arguments=arguments,
        timeout_seconds=timeout_seconds,
    )


def _script_allowed_toolkits(config: Config, agent_name: str) -> frozenset[str]:
    for entry in config.resolve_entity(agent_name).tool_configs:
        if entry.name != "script":
            continue
        raw_allowed = entry.tool_config_overrides.get("allowed_tools")
        if isinstance(raw_allowed, str):
            return frozenset({raw_allowed.strip()}) if raw_allowed.strip() else frozenset()
        if isinstance(raw_allowed, list):
            return frozenset(item.strip() for item in raw_allowed if isinstance(item, str) and item.strip())
    return frozenset()


def _background_approval_config(
    context: ToolRuntimeContext,
    toolkits_by_name: Mapping[str, Toolkit],
) -> Config:
    config = context.current_config
    return build_automation_approval_config(
        config,
        toolkits_by_name=toolkits_by_name,
        preapproved_toolkits=_script_allowed_toolkits(config, context.agent_name),
        never_preapprove_toolkits=_NEVER_PREAPPROVE_TOOLKITS,
    )


def _toolkit_function(toolkit: Toolkit, function_name: str) -> Function:
    function = toolkit.async_functions.get(function_name) or toolkit.functions.get(function_name)
    if function is None:
        msg = "The requested tool is no longer available to this script run."
        raise ScriptCapabilityError(msg)
    return function


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


async def _connect_toolkit(toolkit: Toolkit) -> None:
    if toolkit.requires_connect:
        await _run_toolkit_lifecycle(toolkit.connect)


async def _close_toolkit(toolkit: Toolkit) -> None:
    if toolkit.requires_connect:
        await _run_toolkit_lifecycle(toolkit.close)


async def _run_toolkit_lifecycle(operation: Callable[[], object]) -> None:
    if inspect.iscoroutinefunction(operation):
        await operation()
        return
    result = await asyncio.to_thread(operation)
    await _maybe_await(result)


async def _materialize_result(result: object) -> object:
    if inspect.isasyncgen(result) or isinstance(result, AsyncIterator):
        items: list[object] = []
        encoded_bytes = 2
        async for item in result:
            encoded_bytes = _append_bounded_result(items, item, encoded_bytes)
        return items
    if inspect.isgenerator(result) or isinstance(result, Iterator):
        items = []
        encoded_bytes = 2
        for item in result:
            encoded_bytes = _append_bounded_result(items, item, encoded_bytes)
        return items
    return result


async def _materialize_successful_result(
    execution: FunctionExecutionResult,
) -> object:
    if execution.status != "success":
        return None
    return await _materialize_result(execution.result)


def _strict_json_result(result: object) -> object:
    normalized = to_json_compatible(result)
    try:
        json.dumps(normalized, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise _InvalidToolResultError from exc
    return normalized


def _append_bounded_result(items: list[object], item: object, encoded_bytes: int) -> int:
    if len(items) >= _MAX_MATERIALIZED_RESULT_ITEMS:
        msg = "Tool stream exceeded the bounded result item limit."
        raise ValueError(msg)
    normalized = to_json_compatible(item)
    item_bytes = len(
        json.dumps(normalized, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode(),
    )
    next_encoded_bytes = encoded_bytes + item_bytes + int(bool(items))
    if next_encoded_bytes > _MAX_MATERIALIZED_RESULT_BYTES:
        msg = "Tool stream exceeded the bounded result byte limit."
        raise ValueError(msg)
    items.append(normalized)
    return next_encoded_bytes
