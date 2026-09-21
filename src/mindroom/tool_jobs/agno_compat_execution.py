"""SDK schema, approved-call dispatch, and saved wait-metadata bindings."""

from __future__ import annotations

from copy import deepcopy
from functools import partial, wraps
from threading import Lock
from typing import TYPE_CHECKING, Any

from agno.agent import _tools as agent_tools
from agno.models.base import Model
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.team import _tools as team_tools
from agno.tools.function import Function

from mindroom.custom_tools.job import is_job_function, project_native_job_wait
from mindroom.tool_jobs.agno_compat_resources import install_execution_resource_bindings
from mindroom.tool_jobs.agno_execution import (
    call_wait_mode,
    execute_owned_tool_call,
    is_background_job_excluded,
    is_framework_function,
    validate_wait_timeout_parameter,
    wrap_tool_execution,
)
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_jobs.wait_timeout import bind_tool_wait_modes
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from agno.models.fallback import FallbackConfig
    from agno.tools.function import FunctionCall

    from mindroom.tool_jobs.agno_execution import ToolCallResult


_SDK_BINDINGS_INSTALLED = False
_SDK_BINDINGS_LOCK = Lock()


def _wrap_wait_metadata[**P, R](original: Callable[P, R]) -> Callable[P, R]:
    def prepare(*args: P.args, **kwargs: P.kwargs) -> R:
        context = get_tool_runtime_context()
        run, run_context = kwargs.get("run_response"), kwargs.get("run_context")
        if (
            context is not None
            and get_background_runtime(context.runtime_paths) is not None
            and isinstance(run, RunOutput | TeamRunOutput)
            and isinstance(run_context, RunContext)
        ):
            if run.metadata is None:
                run.metadata = {}
            if run_context.metadata is None:
                run_context.metadata = {}
            bind_tool_wait_modes(run.metadata, run_context.metadata, run_context.run_id)
        return original(*args, **kwargs)

    return prepare


# AGNO_COMPAT: Bind exact saved wait modes before continued Functions receive their run context.
# Reason: Agno rebuilds continuation metadata, forwarding parent metadata into team members,
# and does not share later run-context mutations with the saved output.
# Upstream issue: No matching public persisted per-call metadata extension point identified.
# Upstream PR: None identified.
# Remove when: SDK preserves owner-controlled per-call metadata through approval continuation.
# Coverage: tests/test_tool_job_approval_modes.py, tests/test_tool_job_restart_integration.py.
def _install_sdk_bindings() -> None:
    global _SDK_BINDINGS_INSTALLED
    with _SDK_BINDINGS_LOCK:
        if not _SDK_BINDINGS_INSTALLED:
            vars(agent_tools)["determine_tools_for_model"] = _wrap_wait_metadata(agent_tools.determine_tools_for_model)
            vars(team_tools)["_determine_tools_for_model"] = _wrap_wait_metadata(team_tools._determine_tools_for_model)
            _install_owned_dispatch_binding()
            _SDK_BINDINGS_INSTALLED = True


# AGNO_COMPAT: Own every nested SDK dispatch inside accepted background execution.
# Reason: Embedded agents may bypass MindRoom's model construction; SDK sync dispatch
# offloads a complete hook chain whose thread outlives cancellation of its waiter.
# Upstream issue: No matching public accepted-call completion owner identified.
# Upstream PR: None identified.
# Remove when: SDK dispatch retains synchronous work until its resource owner can close.
# Coverage: tests/test_tool_job_workflows.py, tests/test_tool_job_execution.py.
def _install_owned_dispatch_binding() -> None:
    original = Model.arun_function_call

    @wraps(original)
    async def execute(model: Model, function_call: FunctionCall) -> ToolCallResult:
        return await execute_owned_tool_call(partial(original, model), function_call)

    type.__setattr__(Model, "arun_function_call", execute)


# AGNO_COMPAT: Project framework parameters before provider-specific schema conversion.
# Reason: Agno has no shared schema hook that leaves application Function parameters unchanged.
# Upstream issue: No matching public reserved-parameter extension point identified.
# Upstream PR: None identified.
# Remove when: SDK supports per-call framework metadata outside application kwargs.
# Coverage: tests/test_tool_job_wait_timeout.py, tests/test_tool_job_control_calls.py, tests/test_tool_job_exclusions.py.
def _wrap_tool_schemas(
    original: Callable[..., list[dict[str, Any]]],
    *,
    depth: int,
) -> Callable[..., list[dict[str, Any]]]:
    def format_tools(tools: list[Function | dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        context = get_tool_runtime_context()
        if context is None or get_background_runtime(context.runtime_paths) is None:
            return original(tools)
        projected: list[Function | dict[str, Any]] = []
        for tool in tools or []:
            if not isinstance(tool, Function) or is_framework_function(tool) or is_background_job_excluded(tool):
                projected.append(tool)
                continue
            validate_wait_timeout_parameter(tool)
            if tool.stop_after_tool_call or ((job_owns_execution() or depth > 0) and not is_job_function(tool)):
                projected.append(tool)
                continue
            function = tool.model_copy()
            function.parameters = deepcopy(tool.parameters)
            function.parameters.setdefault("properties", {})["wait_timeout"] = {
                "anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}],
                "description": (
                    "Seconds to wait for this call, without cancelling its execution. "
                    "Omit or pass null to wait until completion or a human follow-up; "
                    "zero returns a job handle immediately."
                ),
            }
            if function.strict:
                required = function.parameters.setdefault("required", [])
                if "wait_timeout" not in required:
                    required.append("wait_timeout")
            projected.append(function)
        return original(projected)

    return format_tools


# AGNO_COMPAT: Bind after the SDK driver has admitted approvals and external calls.
# Reason: Agno exposes no public hook around the complete approved call executor.
# Upstream issue: No matching public accepted-operation extension point identified.
# Upstream PR: None identified.
# Remove when: SDK exposes public approved-call execution ownership.
# Coverage: tests/test_tool_job_execution.py, tests/test_tool_job_control_calls.py, tests/test_tool_job_learning.py.
def install_tool_job_execution(
    model: Model,
    fallback_config: FallbackConfig | None = None,
    *,
    depth: int = 0,
) -> None:
    """Bind the approved-call owner to primary and concrete fallback models."""
    _install_sdk_bindings()
    install_execution_resource_bindings()
    models = [model]
    if fallback_config is not None:
        models.extend(
            item
            for item in (
                *fallback_config.on_error,
                *fallback_config.on_rate_limit,
                *fallback_config.on_context_overflow,
            )
            if not isinstance(item, str)
        )
    for candidate in models:
        namespace = vars(candidate)
        if namespace.get("_mindroom_tool_jobs"):
            continue
        namespace["_format_tools"] = _wrap_tool_schemas(candidate._format_tools, depth=depth)
        namespace["arun_function_calls"] = _wrap_job_wait_dispatch(candidate.arun_function_calls, depth=depth)
        namespace["arun_function_call"] = wrap_tool_execution(candidate.arun_function_call, depth=depth)
        namespace["_mindroom_tool_jobs"] = True


# AGNO_COMPAT: Capture wait semantics and project native approvals before SDK admission.
# Reason: Agno has no public per-call metadata hook before a confirmation pause.
# Upstream issue: No matching argument-sensitive public extension point identified.
# Upstream PR: None identified.
# Remove when: SDK supports per-call external approval requirements.
# Coverage: tests/test_job_tools.py, tests/test_background_delegation.py, tests/test_tool_job_approval_modes.py.
def _wrap_job_wait_dispatch(
    original: Callable[..., AsyncIterator[Any]],
    *,
    depth: int,
) -> Callable[..., AsyncIterator[Any]]:
    async def dispatch(function_calls: list[FunctionCall], *args: object, **kwargs: object) -> AsyncIterator[Any]:
        context = get_tool_runtime_context()
        if context is not None and get_background_runtime(context.runtime_paths) is not None:
            for call in function_calls:
                call_wait_mode(call, depth=depth)
        if not kwargs.get("skip_pause_check", False):
            for call in function_calls:
                await project_native_job_wait(call, depth=depth)
        stream = original(function_calls, *args, **kwargs)
        async with closing_async_stream(stream):
            async for event in stream:
                yield event

    return dispatch
