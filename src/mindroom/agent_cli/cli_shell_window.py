"""Build-time shell window bindings for the standard agent CLI."""

from __future__ import annotations

import functools
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mindroom.agent_cli.lifetime import current_cli_lifetime
from mindroom.agent_cli.shell_access import minimal_shell_problems
from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.tool_system.agent_tool_calls import cli_dispatch_active
from mindroom.tool_system.tool_function import CONFIRMATION_GATES

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import AdaptedToolkit
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = ["STANDARD_CLI_NOTE", "standard_cli_eligible", "wrap_native_shell_window"]

STANDARD_CLI_NOTE = (
    "Inside shell commands, `mindroom-agent` calls your other tools by name, so one script can combine many calls; "
    "run `mindroom-agent --help` for usage."
)


def standard_cli_eligible(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    execution_identity: ToolExecutionIdentity | None,
) -> bool:
    """Return whether this standard agent's shell can reach MindRoom's CLI routes in a Matrix conversation."""
    return (
        execution_identity is not None
        and execution_identity.channel == "matrix"
        and not minimal_shell_problems(config, runtime_paths, agent_name)
    )


def wrap_native_shell_window(toolkits: Sequence[AdaptedToolkit]) -> tuple[AdaptedToolkit, ...] | None:
    """Return ``toolkits`` with the native `run_shell_command` running inside its own CLI window; None without one.

    ``toolkits`` are a run's, which offer each function name once; only the shell tool's own command is windowed,
    whether the agent lists `shell` or a preset brings it in.
    """
    for index, toolkit in enumerate(toolkits):
        for position, function in enumerate(toolkit.functions):
            if function.name != SHELL_FUNCTION:
                continue
            # An approved command resumes in a later run without this response's CLI, like a minimal subagent's.
            if function.registered_toolkit != "shell" or function.gate in CONFIRMATION_GATES:
                return None
            functions = list(toolkit.functions)
            functions[position] = replace(function, entrypoint=_windowed(function.entrypoint))
            return (*toolkits[:index], replace(toolkit, functions=tuple(functions)), *toolkits[index + 1 :])
    return None


def _windowed(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def windowed(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        lifetime = current_cli_lifetime()
        owner = lifetime.owner if lifetime is not None else None
        # Nested CLI shell calls already run inside their parent's window.
        if owner is None or owner.shell_env is None or cli_dispatch_active():
            return await original(*args, **kwargs)
        return await owner.run_native_shell(functools.partial(original, *args, **kwargs))

    return windowed
