"""Find and run the tool functions an agent build offers, and single calls through the tool-call policy."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import HookBindings
from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system.declarations import tool_schema_source
from mindroom.tool_system.invocation import ToolCall, ToolCallContext, execute_tool_call
from mindroom.tool_system.tool_function import ToolFunction
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookRegistry
    from mindroom.tool_system.invocation import ToolMiddleware
    from mindroom.tool_system.tool_function import ToolOutcome

# Paths for a policy whose test writes nothing: its calls log no requests and its hooks read no state root.
_UNWRITTEN_RUNTIME_PATHS = resolve_runtime_paths(
    config_path=Path("/nonexistent/mindroom-test/config.yaml"),
    process_env={},
)


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
    entrypoint = function.entrypoint
    while (owner := getattr(entrypoint, "__self__", None)) is None:
        source = tool_schema_source(inspect.unwrap(entrypoint))
        if source is entrypoint:
            pytest.fail(f"{function.name} does not call a toolkit method")
        entrypoint = source
    return owner


def detached_policy(
    hook_registry: HookRegistry,
    *,
    config: Config | None = None,
    runtime_paths: RuntimePaths | None = None,
    **policy: Any,  # noqa: ANN401 - forwarded to build_tool_hook_middleware
) -> ToolMiddleware:
    """Return the tool-call policy a runner without a Matrix conversation builds around ``hook_registry``'s hooks.

    ``config`` and ``runtime_paths`` default to an empty config and paths nothing is written under; a call inside a
    test's conversation uses that conversation's instead. ``policy`` names the origin and approval gate a test sets.
    """
    return build_tool_hook_middleware(
        HookBindings(registry=hook_registry),
        config if config is not None else Config(),
        runtime_paths if runtime_paths is not None else _UNWRITTEN_RUNTIME_PATHS,
        **policy,
    )


def tool_policy(
    hook_registry: HookRegistry,
    **policy: Any,  # noqa: ANN401 - forwarded to detached_policy
) -> Callable[[str, Callable[..., Awaitable[object]], dict[str, Any]], Awaitable[object]]:
    """Return the tool-call policy as a call around a named async function and its keyword arguments.

    The policy is ``detached_policy``'s, run on its own outside any loop or run, as the current identity.
    """
    middleware = detached_policy(hook_registry, **policy)

    async def call(name: str, func: Callable[..., Awaitable[object]], arguments: dict[str, Any]) -> object:
        function = ToolFunction(name=name, description=None, parameters={}, entrypoint=func)
        context = ToolCallContext(run_id="run", session_id="session", user_id=None)
        return await middleware(
            ToolCall(function=function, arguments=arguments, context=context),
            lambda forwarded: func(**forwarded),
        )

    return call
