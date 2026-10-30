"""Find and run the tool functions an agent build offers, and single calls through the tool-call policy."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system.agno_toolkits import Toolkit, toolkit_function
from mindroom.tool_system.invocation import ToolCall, ToolCallContext, execute_tool_call
from mindroom.tool_system.tool_function import ToolFunction
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.hooks import HookRegistry
    from mindroom.tool_system.agno_toolkits import AnyToolkit, Function
    from mindroom.tool_system.tool_function import ToolOutcome


def toolkit_offers(toolkit: AnyToolkit, name: str) -> ToolFunction:
    """Return the function of ``name`` ``toolkit`` offers, as an agent's run offers it."""
    function = toolkit_function(toolkit, name)
    if function is None:
        pytest.fail(f"The toolkit does not offer {name}")
    return function


def blocking_entrypoint(toolkit: AnyToolkit, name: str) -> Callable[..., Any]:
    """Return the entrypoint ``toolkit`` offers for ``name`` as a blocking call, which runs an async one to its end."""
    entrypoint = toolkit_offers(toolkit, name).entrypoint

    def call(*args: object, **kwargs: object) -> object:
        result = entrypoint(*args, **kwargs)
        return asyncio.run(result) if inspect.iscoroutine(result) else result

    return call


def offered_agno_function(function: Function) -> ToolFunction:
    """Return ``function``, an Agno function built alone, as a toolkit holding it offers it."""
    toolkit = Toolkit(name="test")
    toolkit.functions[function.name] = function
    return toolkit_offers(toolkit, function.name)


def offered_function(build: AgentBuild, name: str) -> ToolFunction:
    """Return the function of ``name`` a run of ``build`` offers the model."""
    function = build.spec.functions.get(name)
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
        context or ToolCallContext(run_id="run", session_id="session", user_id=None),
        middleware=agent_tool_middleware(build),
    )


def source_toolkit(function: ToolFunction) -> object:
    """Return the toolkit object whose bound method an adapted function finally calls."""
    source = inspect.unwrap(function.entrypoint)
    owner = getattr(source, "__self__", None)
    if owner is None:
        pytest.fail(f"{function.name} does not call a toolkit method")
    return owner


def tool_policy(
    hook_registry: HookRegistry,
    agent_name: str | None,
    **policy: Any,  # noqa: ANN401 - forwarded to build_tool_hook_middleware
) -> Callable[[str, Callable[..., Awaitable[object]], dict[str, Any]], Awaitable[object]]:
    """Return the tool-call policy as a call around a named async function and its keyword arguments.

    The policy is ``build_tool_hook_middleware``'s, run on its own outside any loop or run; ``policy`` names what a
    test sets, and the rest is a policy with no dispatch, config, or runtime paths.
    """
    middleware = build_tool_hook_middleware(
        hook_registry,
        agent_name,
        **({"dispatch_context": None, "config": None, "runtime_paths": None} | policy),
    )

    async def call(name: str, func: Callable[..., Awaitable[object]], arguments: dict[str, Any]) -> object:
        function = ToolFunction(name=name, description=None, parameters={}, entrypoint=func)
        context = ToolCallContext(run_id="run", session_id="session", user_id=None)
        return await middleware(
            ToolCall(function=function, arguments=arguments, context=context),
            lambda forwarded: func(**forwarded),
        )

    return call
