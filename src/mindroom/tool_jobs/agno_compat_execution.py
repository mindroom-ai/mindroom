"""SDK schema and approved-call dispatch bindings."""

from __future__ import annotations

from copy import deepcopy
from functools import partial, wraps
from threading import Lock
from typing import TYPE_CHECKING, Any

from agno.models.base import Model
from agno.tools.function import Function

from mindroom.tool_jobs.agno_execution import (
    declares_wait_timeout,
    execute_owned_tool_call,
    wait_mode,
    wrap_tool_execution,
)
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.models.fallback import FallbackConfig
    from agno.tools.function import FunctionCall

    from mindroom.tool_jobs.agno_execution import ToolCallResult


_SDK_BINDINGS_INSTALLED = False
_SDK_BINDINGS_LOCK = Lock()


# AGNO_COMPAT: Own every nested SDK dispatch inside accepted background execution.
# Reason: Embedded agents may bypass MindRoom's model construction; SDK sync dispatch
# offloads a complete hook chain whose thread outlives cancellation of its waiter.
# Upstream issue: No matching public accepted-call completion owner identified.
# Upstream PR: None identified.
# Remove when: SDK dispatch retains synchronous work until its resource owner can close.
# Coverage: tests/test_tool_job_workflows.py::test_workflow_participant_runs_multiple_sync_tools
# Coverage: tests/test_tool_job_workflows.py::test_cancel_composite_job_drains_all_sync_children
def _install_owned_dispatch_binding() -> None:
    global _SDK_BINDINGS_INSTALLED
    with _SDK_BINDINGS_LOCK:
        if _SDK_BINDINGS_INSTALLED:
            return
        original = Model.arun_function_call

        @wraps(original)
        async def execute(model: Model, function_call: FunctionCall) -> ToolCallResult:
            return await execute_owned_tool_call(partial(original, model), function_call)

        type.__setattr__(Model, "arun_function_call", execute)
        _SDK_BINDINGS_INSTALLED = True


# AGNO_COMPAT: Project framework parameters before provider-specific schema conversion.
# Reason: Agno has no shared schema hook that leaves application Function parameters unchanged.
# Upstream issue: No matching public reserved-parameter extension point identified.
# Upstream PR: None identified.
# Remove when: SDK supports per-call framework metadata outside application kwargs.
# Coverage: tests/test_tool_job_wait_timeout.py::test_shared_schema_adds_optional_wait_without_changing_application_schema
# Coverage: tests/test_tool_job_exclusions.py::test_registered_plugin_exclusion_is_pinned_for_every_function
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
            # A declared wait_timeout keeps its authored schema; the collision fails only that call's execution.
            if (
                not isinstance(tool, Function)
                or wait_mode(tool, depth=depth) != "managed"
                or declares_wait_timeout(tool)
            ):
                projected.append(tool)
                continue
            function = tool.model_copy()
            function.parameters = deepcopy(tool.parameters)
            function.parameters.setdefault("properties", {})["wait_timeout"] = {
                "anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}],
                "description": (
                    "Seconds to wait for this call, without cancelling its execution. "
                    "Omit or pass null to wait until completion or a newer message you answer; "
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
# Coverage: tests/test_tool_job_execution.py::test_fast_result_acknowledges_exact_saved_sdk_run
# Coverage: tests/test_tool_job_control_calls.py::test_model_control_preserves_timing_across_human_followup
# Coverage: tests/test_tool_job_sdk_functions.py::test_sdk_generated_functions_run_inline_with_native_schemas
def install_tool_job_execution(
    model: Model,
    fallback_config: FallbackConfig | None = None,
    *,
    depth: int = 0,
) -> None:
    """Bind the approved-call owner to primary and concrete fallback models."""
    _install_owned_dispatch_binding()
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
        namespace["arun_function_call"] = wrap_tool_execution(candidate.arun_function_call, depth=depth)
        namespace["_mindroom_tool_jobs"] = True
