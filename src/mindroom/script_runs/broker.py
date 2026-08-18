"""Direct execution broker for governed background-script tool calls."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from agno.tools import Toolkit
from agno.tools.function import Function, FunctionCall, FunctionExecutionResult

from mindroom.script_runs.models import (
    ScriptCallRecord,
    ScriptCallState,
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
)
from mindroom.script_runs.policy import effective_script_grants, resolve_current_script_grants
from mindroom.script_runs.store import ScriptCapabilityError, ScriptRunNotFoundError, ScriptRunStore
from mindroom.tool_approval import (
    AutomationToolOrigin,
    BackgroundScriptToolOrigin,
    ToolApprovalDecision,
    evaluate_tool_approval,
)
from mindroom.tool_system.automation_approval import build_automation_approval_config
from mindroom.tool_system.catalog import ensure_tool_registry_loaded
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.tool_calls import sanitize_failure_text
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge
from mindroom.tool_system.worker_proxy_client import to_json_compatible
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity

if TYPE_CHECKING:
    from mindroom.config.main import Config

__all__ = [
    "ScriptBrokerAuthenticationError",
    "ScriptCallReceipt",
    "ScriptRuntimeResolver",
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
_MAX_MATERIALIZED_RESULT_BYTES = 64 * 1024
_MAX_MATERIALIZED_RESULT_ITEMS = 1_000
_NEVER_PREAPPROVE_TOOLKITS = frozenset({"claude_agent", "config_manager", "scheduler", "subagents"})
_ACTIVE_RUN_STATES = frozenset({ScriptRunState.STARTING, ScriptRunState.RUNNING})


class ScriptBrokerAuthenticationError(ValueError):
    """Raised when a gateway capability cannot be authenticated safely."""


class ScriptRuntimeResolver(Protocol):
    """Resolve live runtime and approval services for one durable script owner."""

    def resolve(self, run: ScriptRunRecord, *, correlation_id: str) -> ToolRuntimeContext:
        """Rebuild the live context for the durable run owner."""
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


class _BackgroundApprovalGate(Protocol):
    async def __call__(
        self,
        origin: AutomationToolOrigin,
        tool_name: str,
        arguments: dict[str, object],
    ) -> ToolApprovalDecision: ...


@dataclass(frozen=True, slots=True)
class ScriptToolCallRequest:
    """One capability-bearing request for a stable logical tool call."""

    run_id: str
    call_id: str
    grant: ScriptToolGrant
    arguments: dict[str, object]
    token: str = ""

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
        state=record.state,
        created_at=record.created_at,
        updated_at=record.updated_at,
        result=record.result,
        error=record.error,
    )


@dataclass(slots=True)
class ScriptToolBroker:
    """Execute stable script calls through the ordinary registered-tool path."""

    store: ScriptRunStore
    runtime_resolver: ScriptRuntimeResolver
    _tasks: dict[tuple[str, str], asyncio.Task[ScriptCallReceipt]] = field(default_factory=dict, init=False)

    async def submit_call(self, request: ScriptToolCallRequest) -> ScriptCallReceipt:
        """Claim and execute one call, or replay its existing durable receipt."""
        run = self.store.require_active_capability(request.run_id, request.token)
        claim = self.store.claim_call(
            run_id=run.run_id,
            call_id=request.call_id,
            grant=request.grant,
            arguments_digest=request.arguments_digest,
        )
        key = (run.run_id, request.call_id)
        if not claim.created:
            existing_task = self._tasks.get(key)
            if claim.call.state is ScriptCallState.PENDING and existing_task is None:
                record = self.store.publish_call_result(
                    run_id=run.run_id,
                    call_id=request.call_id,
                    state=ScriptCallState.INDETERMINATE,
                    error=_INDETERMINATE_ERROR,
                )
                return _receipt_from_record(record)
            return _receipt_from_record(claim.call)

        task = asyncio.create_task(
            self._execute_claimed_call(run, claim.call, request.arguments),
            name=f"script-tool:{run.run_id}:{request.call_id}",
        )
        self._tasks[key] = task

        def forget_completed_task(completed: asyncio.Task[ScriptCallReceipt]) -> None:
            if self._tasks.get(key) is completed:
                self._tasks.pop(key, None)

        task.add_done_callback(forget_completed_task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._tasks.pop(key, None)

    def get_call(self, run_id: str, call_id: str) -> ScriptCallReceipt:
        """Return one stable durable call receipt."""
        return _receipt_from_record(self.store.get_call(run_id, call_id))

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

    async def submit_authenticated(
        self,
        request: ScriptToolCallRequest,
        authorization: str | None,
    ) -> ScriptCallReceipt:
        """Authenticate and submit one gateway call without trusting body identity fields."""
        token = self.authenticate(request.run_id, authorization)
        return await self.submit_call(replace(request, token=token))

    def get_authenticated(
        self,
        run_id: str,
        call_id: str,
        authorization: str | None,
    ) -> ScriptCallReceipt:
        """Authenticate and retrieve one gateway receipt."""
        self.authenticate(run_id, authorization)
        return self.get_call(run_id, call_id)

    async def _execute_claimed_call(
        self,
        run: ScriptRunRecord,
        call: ScriptCallRecord,
        arguments: dict[str, object],
    ) -> ScriptCallReceipt:
        origin = BackgroundScriptToolOrigin(
            run_id=run.run_id,
            call_id=call.call_id,
            requester_id=run.owner_user_id,
            toolkit_name=call.grant.toolkit_name,
            function_name=call.grant.function_name,
        )
        correlation_id = f"background-script:{run.run_id}:{call.call_id}"
        execution_started = False
        try:
            context = self.runtime_resolver.resolve(run, correlation_id=correlation_id)
            _validate_resolved_context(run, context)
            current_grants = resolve_current_script_grants(context)
            if call.grant not in effective_script_grants(run.grants, current_grants):
                return self._publish(call, state=ScriptCallState.FAILED, error=_REVOKED_GRANT_ERROR)

            toolkit = _build_current_toolkit(context, call.grant)
            approval_config = _background_approval_config(context, current_grants, toolkit)
            approval_gate, approval_denials = _build_background_approval_gate(
                runtime_resolver=self.runtime_resolver,
                context=context,
                run=run,
                call=call,
                approval_config=approval_config,
            )

            bridge = build_tool_hook_bridge(
                context.hook_registry,
                agent_name=run.agent_name,
                dispatch_context=LiveToolDispatchContext.from_runtime_context(context),
                config=approval_config,
                runtime_paths=context.runtime_paths,
                origin=origin,
                approval_gate=approval_gate,
            )
            prepend_tool_hook_bridge(toolkit, bridge)
            function = _toolkit_function(toolkit, call.grant.function_name)
            execution_identity = build_execution_identity_from_runtime_context(context)
            materialized: object = None
            await _connect_toolkit(toolkit)
            try:
                execution_started = True

                async def execute_function() -> tuple[FunctionExecutionResult, object]:
                    with tool_runtime_context(context):
                        execution_result = await FunctionCall(
                            function=function,
                            arguments=arguments,
                            call_id=call.call_id,
                        ).aexecute()
                        materialized_result = await _materialize_successful_result(
                            execution_result,
                            _approval_denial(approval_denials),
                        )
                    return execution_result, materialized_result

                execution, materialized = await run_with_tool_execution_identity(
                    execution_identity,
                    operation=execute_function,
                )
            finally:
                await _close_toolkit(toolkit)

            approval_denial = _approval_denial(approval_denials)
            if approval_denial is not None:
                return self._publish(
                    call,
                    state=ScriptCallState.DECLINED,
                    error={
                        "kind": "approval_declined",
                        "message": sanitize_failure_text(approval_denial.reason or "The tool call was declined."),
                        "retryable": False,
                    },
                )
            if execution.status != "success":
                return self._publish(
                    call,
                    state=ScriptCallState.FAILED,
                    error={
                        "kind": "tool_failure",
                        "message": sanitize_failure_text(execution.error or "Tool execution failed."),
                        "retryable": False,
                    },
                )
            return self._publish(call, state=ScriptCallState.COMPLETED, result=to_json_compatible(materialized))
        except asyncio.CancelledError:
            raise
        except (ScriptCapabilityError, ValueError) as exc:
            return self._publish(
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
            return self._publish(call, state=state, error=error)

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


def _validate_resolved_context(run: ScriptRunRecord, context: ToolRuntimeContext) -> None:
    if (
        context.agent_name != run.agent_name
        or context.requester_id != run.owner_user_id
        or context.room_id != run.room_id
        or (run.thread_root_event_id is not None and context.resolved_thread_id != run.thread_root_event_id)
    ):
        msg = "Live script runtime context does not match the durable run owner."
        raise ValueError(msg)


def _build_background_approval_gate(
    *,
    runtime_resolver: ScriptRuntimeResolver,
    context: ToolRuntimeContext,
    run: ScriptRunRecord,
    call: ScriptCallRecord,
    approval_config: Config,
) -> tuple[
    _BackgroundApprovalGate,
    list[ToolApprovalDecision],
]:
    approval_denials: list[ToolApprovalDecision] = []

    async def approval_gate(
        origin: AutomationToolOrigin,
        tool_name: str,
        arguments: dict[str, object],
    ) -> ToolApprovalDecision:
        assert tool_name == call.grant.function_name
        requires_approval, timeout_seconds = await evaluate_tool_approval(
            approval_config,
            context.runtime_paths,
            call.grant.function_name,
            arguments,
            run.agent_name,
        )
        if not requires_approval:
            return ToolApprovalDecision(approved=True)
        assert isinstance(origin, BackgroundScriptToolOrigin)
        decision = await runtime_resolver.request_approval(
            origin=origin,
            context=context,
            grant=call.grant,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
        )
        if not decision.approved:
            approval_denials.append(decision)
        return decision

    return approval_gate, approval_denials


def _approval_denial(decisions: list[ToolApprovalDecision]) -> ToolApprovalDecision | None:
    return decisions[-1] if decisions else None


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
    current_grants: frozenset[ScriptToolGrant],
    selected_toolkit: Toolkit,
) -> Config:
    config = context.current_config
    toolkits: dict[str, Toolkit] = {}
    for grant in current_grants:
        toolkit = toolkits.setdefault(grant.toolkit_name, Toolkit(name=grant.toolkit_name))
        toolkit.functions[grant.function_name] = selected_toolkit.functions.get(
            grant.function_name,
            selected_toolkit.async_functions.get(grant.function_name, Function(name=grant.function_name)),
        )
    return build_automation_approval_config(
        config,
        toolkits_by_name=toolkits,
        preapproved_toolkits=_script_allowed_toolkits(config, context.agent_name),
        never_preapprove_toolkits=_NEVER_PREAPPROVE_TOOLKITS,
    )


def _build_current_toolkit(context: ToolRuntimeContext, grant: ScriptToolGrant) -> Toolkit:
    config = context.current_config
    ensure_tool_registry_loaded(context.runtime_paths, config)
    entity_view = config.resolve_entity(context.agent_name)
    all_deferred_tools = [entry.name for entry in entity_view.authored_deferred_tool_configs]
    surface = visible_tool_surface(
        agent_name=context.agent_name,
        config=config,
        loaded_tools=all_deferred_tools,
        enable_dynamic_tools_manager=False,
    )
    tool_entry = next((entry for entry in surface.runtime_tool_configs if entry.name == grant.toolkit_name), None)
    if tool_entry is None:
        msg = "The requested tool is no longer available to this script run."
        raise ScriptCapabilityError(msg)

    from mindroom.agents import build_agent_toolkit, resolve_runtime_worker_tools  # noqa: PLC0415
    from mindroom.runtime_resolution import resolve_agent_runtime  # noqa: PLC0415

    execution_identity = build_execution_identity_from_runtime_context(context)
    agent_runtime = resolve_agent_runtime(
        context.agent_name,
        config,
        context.runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    worker_tools = resolve_runtime_worker_tools(
        context.agent_name,
        config,
        context.runtime_paths,
        [grant.toolkit_name],
        tool_registry_preloaded=True,
    )
    toolkit = build_agent_toolkit(
        grant.toolkit_name,
        agent_name=context.agent_name,
        config=config,
        runtime_paths=context.runtime_paths,
        worker_tools=worker_tools,
        runtime_overrides=entity_view.tool_runtime_overrides(grant.toolkit_name),
        agent_runtime=agent_runtime,
        tool_config_overrides=tool_entry.tool_config_overrides,
        execution_identity=execution_identity,
        session_id=context.session_id,
    )
    if toolkit is None:
        msg = "The requested tool is no longer available to this script run."
        raise ScriptCapabilityError(msg)
    return toolkit


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
        await _maybe_await(toolkit.connect())


async def _close_toolkit(toolkit: Toolkit) -> None:
    if toolkit.requires_connect:
        await _maybe_await(toolkit.close())


async def _materialize_result(result: object) -> object:
    if inspect.isasyncgen(result) or isinstance(result, AsyncIterator):
        items: list[object] = []
        async for item in result:
            _append_bounded_result(items, item)
        return items
    if inspect.isgenerator(result) or isinstance(result, Iterator):
        items = []
        for item in result:
            _append_bounded_result(items, item)
        return items
    return result


async def _materialize_successful_result(
    execution: FunctionExecutionResult,
    approval_denial: ToolApprovalDecision | None,
) -> object:
    if approval_denial is not None or execution.status != "success":
        return None
    return await _materialize_result(execution.result)


def _append_bounded_result(items: list[object], item: object) -> None:
    if len(items) >= _MAX_MATERIALIZED_RESULT_ITEMS:
        msg = "Tool stream exceeded the bounded result item limit."
        raise ValueError(msg)
    items.append(to_json_compatible(item))
    encoded = json.dumps(items, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > _MAX_MATERIALIZED_RESULT_BYTES:
        msg = "Tool stream exceeded the bounded result byte limit."
        raise ValueError(msg)
