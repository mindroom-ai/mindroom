"""Instance-scoped binding at Agno's approved FunctionCall dispatch boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.custom_tools.job import project_native_job_wait
from mindroom.tool_jobs.agno_compat_resources import install_execution_resource_bindings
from mindroom.tool_jobs.agno_execution import wrap_tool_execution
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from agno.models.base import Model
    from agno.models.fallback import FallbackConfig
    from agno.tools.function import FunctionCall


# AGNO_COMPAT: Bind after the SDK driver has admitted approvals and external calls.
# Reason: Agno exposes no public hook around the complete approved call executor.
# Upstream issue: No matching public accepted-operation extension point identified.
# Upstream PR: None identified.
# Remove when: SDK exposes public approved-call execution ownership.
# Coverage: tests/test_tool_job_execution.py.
def install_tool_job_execution(model: Model, fallback_config: FallbackConfig | None = None, *, depth: int = 0) -> None:
    """Bind the approved-call owner to primary and concrete fallback models."""
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
        namespace["arun_function_calls"] = _wrap_job_wait_dispatch(candidate.arun_function_calls, depth=depth)
        namespace["arun_function_call"] = wrap_tool_execution(candidate.arun_function_call, depth=depth)
        namespace["_mindroom_tool_jobs"] = True


# AGNO_COMPAT: Native approval projection depends on the exact management call arguments.
# Reason: Agno only exposes static Function external-execution flags before admission.
# Upstream issue: No matching argument-sensitive public extension point identified.
# Upstream PR: None identified.
# Remove when: SDK supports per-call external approval requirements.
# Coverage: tests/test_job_tools.py, tests/test_background_delegation.py.
def _wrap_job_wait_dispatch(
    original: Callable[..., AsyncIterator[Any]],
    *,
    depth: int,
) -> Callable[..., AsyncIterator[Any]]:
    async def dispatch(function_calls: list[FunctionCall], *args: object, **kwargs: object) -> AsyncIterator[Any]:
        if not kwargs.get("skip_pause_check", False):
            for call in function_calls:
                await project_native_job_wait(call, depth=depth)
        stream = original(function_calls, *args, **kwargs)
        async with closing_async_stream(stream):
            async for event in stream:
                yield event

    return dispatch
