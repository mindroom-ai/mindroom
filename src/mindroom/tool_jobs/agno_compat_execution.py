"""Instance-scoped binding at Agno's approved FunctionCall dispatch boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_jobs.agno_compat_resources import install_execution_resource_bindings
from mindroom.tool_jobs.agno_execution import wrap_tool_execution

if TYPE_CHECKING:
    from agno.models.base import Model
    from agno.models.fallback import FallbackConfig


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
        namespace["arun_function_call"] = wrap_tool_execution(candidate.arun_function_call, depth=depth)
        namespace["_mindroom_tool_jobs"] = True
