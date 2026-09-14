"""Persist plugin hook phases across subagent approval continuations."""

from __future__ import annotations

import time
from copy import deepcopy
from typing import TYPE_CHECKING

from mindroom.delegation_state import DelegationHookState
from mindroom.hooks import HookRegistry
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_hooks import dispatch_external_tool_hooks
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload, serialize_tool_execution_identity

if TYPE_CHECKING:
    from typing import Any

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _registry(config: Config, runtime_paths: RuntimePaths) -> HookRegistry:
    context = get_tool_runtime_context()
    if context is not None:
        return context.hook_registry
    # Recovery runs outside a Matrix turn, so compile the current plugin snapshot.
    from mindroom.tool_system.plugins import load_plugins  # noqa: PLC0415

    return HookRegistry.from_plugins(load_plugins(config, runtime_paths))


async def before_delegation(
    *,
    execution_identity: ToolExecutionIdentity,
    arguments: dict[str, Any],
    config: Config,
    runtime_paths: RuntimePaths,
) -> DelegationHookState:
    """Run the plugin gate before child execution and retain its isolated inputs."""
    state = DelegationHookState(
        execution_identity=serialize_tool_execution_identity(execution_identity),
        arguments=deepcopy(arguments),
        started_at=time.time(),
    )
    state.blocked_result = await dispatch_external_tool_hooks(
        hook_registry=_registry(config, runtime_paths),
        execution_identity=execution_identity,
        config=config,
        runtime_paths=runtime_paths,
        tool_name="run_subagent",
        arguments=state.arguments,
        before=True,
    )
    return state


async def after_delegation(
    state: DelegationHookState,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    result: str | None,
    error: BaseException | None = None,
) -> None:
    """Emit the terminal tool result once per retained lifecycle, never on pause."""
    if state.after_called:
        return
    identity = parse_tool_execution_identity_payload(state.execution_identity, strict=True)
    if identity is None:
        msg = "Delegation hook is missing its caller identity"
        raise ValueError(msg)
    await dispatch_external_tool_hooks(
        hook_registry=_registry(config, runtime_paths),
        execution_identity=identity,
        config=config,
        runtime_paths=runtime_paths,
        tool_name="run_subagent",
        arguments=state.arguments,
        before=False,
        result=state.blocked_result or result,
        error=error,
        blocked=state.blocked_result is not None,
        duration_ms=max(0, (time.time() - state.started_at) * 1000),
    )
    state.after_called = True
