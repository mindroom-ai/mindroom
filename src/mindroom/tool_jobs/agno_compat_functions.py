"""SDK function bindings retained by accepted application calls and their receipts."""

from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

from agno.tools.function import FunctionCall

from mindroom.tool_jobs.provenance import callable_origin

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.run import RunContext
    from agno.team import Team
    from agno.tools.function import Function


# AGNO_COMPAT: Functions retain their invoking actor and run in private SDK fields.
# Reason: Detached execution needs an isolated context, and consumption must identify its exact SDK owner.
# Upstream issue: No public function-context accessor or context-copy extension point identified.
# Upstream PR: None identified.
# Remove when: Agno exposes these bindings and its asynchronous dispatch classification publicly.
# Coverage: tests/test_tool_job_execution.py::test_fast_result_acknowledges_exact_saved_sdk_run
# Coverage: tests/test_tool_job_team_authorization.py::test_team_member_tool_uses_actual_actor_and_current_requester_grants
def function_agent(function: Function) -> Agent | None:
    """Return the concrete member when the SDK call belongs to an agent."""
    return function._agent


def function_actor(function: Function) -> Agent | Team | None:
    """Return the concrete SDK execution owner, including a team-owned function."""
    return function._agent or function._team


def function_run_context(function: Function) -> RunContext | None:
    """Read the SDK-bound run, never the session metadata of a different attempt."""
    return function._run_context


def isolated_function_call(call: FunctionCall) -> FunctionCall:
    """Retain actor identity while isolating the detached run's mutable state."""
    function = call.function.model_copy()
    context = function._run_context
    if context is not None:
        function._run_context = replace(
            context,
            session_state=deepcopy(context.session_state),
            metadata=deepcopy(context.metadata),
            messages=deepcopy(context.messages),
            dependencies=dict(context.dependencies) if context.dependencies is not None else None,
        )
    return FunctionCall(function=function, arguments=deepcopy(call.arguments), call_id=call.call_id)


def is_framework_function(function: Function) -> bool:
    """Recognize SDK-owned calls that have no independent application execution."""
    if function_actor(function) is None:
        return True
    origin = callable_origin(function)
    return (
        origin["module"] == "agno.team._default_tools"
        and origin["qualname"] is not None
        and "get_delegate_task" in origin["qualname"]
    )


def uses_sdk_async_dispatch(function: Function) -> bool:
    """Match Agno's dispatch decision so synchronous leaves keep their thread owner."""
    return (
        inspect.iscoroutinefunction(function.entrypoint)
        or inspect.isasyncgenfunction(function.entrypoint)
        or inspect.iscoroutine(function.entrypoint)
        or any(inspect.iscoroutinefunction(hook) for hook in function.tool_hooks or [])
    )
