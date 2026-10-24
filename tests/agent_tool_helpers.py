"""Find and run the tool functions an agent build offers, the way the owned loop runs them."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system.declarations import tool_schema_source
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call

if TYPE_CHECKING:
    from mindroom.agent_loop.build import AgentBuild
    from mindroom.tool_system.invocation import ToolOutcome
    from mindroom.tool_system.tool_function import ToolFunction


def offered_function(build: AgentBuild, name: str) -> ToolFunction:
    """Return the function of ``name`` a run of ``build`` offers the model."""
    function = prepare_run_tools(build.spec).functions.get(name)
    if function is None:
        pytest.fail(f"The agent does not offer {name}")
    return function


async def call_entrypoint(function: ToolFunction, **arguments: Any) -> object:  # noqa: ANN401
    """Call a function's entrypoint directly, without the framework arguments or middleware of a loop call."""
    result = function.entrypoint(**arguments)
    return await result if inspect.isawaitable(result) else result


async def run_offered(
    build: AgentBuild,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    context: ToolCallContext | None = None,
) -> ToolOutcome:
    """Run one call of an offered function through the build's tool middleware, as a reply runs it."""
    return await execute_tool_call(
        offered_function(build, name),
        arguments or {},
        context or ToolCallContext(run_id="run", session_id="session"),
        middleware=agent_tool_middleware(build),
    )


def source_toolkit(function: ToolFunction) -> object:
    """Return the toolkit object whose bound method an adapted function finally calls."""
    source = tool_schema_source(inspect.unwrap(function.entrypoint))
    owner = getattr(source, "__self__", None)
    if owner is None:
        pytest.fail(f"{function.name} does not call a toolkit method")
    return owner
