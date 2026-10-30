"""Persist plugin hook phases across subagent approval continuations."""

from __future__ import annotations

import time
from copy import deepcopy
from typing import TYPE_CHECKING

from mindroom.delegation.state import DelegationHookState
from mindroom.hooks import HookBindings, HookRegistry
from mindroom.tool_system.tool_hooks import dispatch_external_tool_hooks
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload, serialize_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _detached_hooks(config: Config, runtime_paths: RuntimePaths) -> Callable[[], HookBindings]:
    """Return the hooks a phase without a Matrix conversation runs, such as recovery: the current plugin snapshot's."""

    def compile_plugins() -> HookBindings:
        from mindroom.tool_system.plugins import load_plugins  # noqa: PLC0415

        return HookBindings(registry=HookRegistry.from_plugins(load_plugins(config, runtime_paths)))

    return compile_plugins


async def before_delegation(
    *,
    execution_identity: ToolExecutionIdentity,
    arguments: dict[str, Any],
    tool_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> DelegationHookState:
    """Run the plugin gate before child execution and retain its isolated inputs."""
    state = DelegationHookState(
        execution_identity=serialize_tool_execution_identity(execution_identity),
        arguments=deepcopy(arguments),
        started_at=time.time(),
        tool_name=tool_name,
    )
    state.blocked_result = await dispatch_external_tool_hooks(
        detached_hooks=_detached_hooks(config, runtime_paths),
        execution_identity=execution_identity,
        config=config,
        runtime_paths=runtime_paths,
        tool_name=state.tool_name,
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
    await dispatch_external_tool_hooks(
        detached_hooks=_detached_hooks(config, runtime_paths),
        execution_identity=parse_tool_execution_identity_payload(state.execution_identity),
        config=config,
        runtime_paths=runtime_paths,
        tool_name=state.tool_name,
        arguments=state.arguments,
        before=False,
        result=state.blocked_result or result,
        error=error,
        blocked=state.blocked_result is not None,
        duration_ms=max(0, (time.time() - state.started_at) * 1000),
    )
    state.after_called = True
