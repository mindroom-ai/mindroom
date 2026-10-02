"""Canonical shell behavior for one minimal-mode response, in a worker or the primary."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.tool_system.output_files import ToolOutputFilePolicy, wrap_toolkit_for_output_files
from mindroom.tool_system.tool_access import function_schema, validate_tool_arguments
from mindroom.tools.shell import AgentCliShellBinding, shell_tools

if TYPE_CHECKING:
    from agno.tools.toolkit import Toolkit

    from mindroom.agent_cli.session import CliGrant, TurnToolBridge
    from mindroom.agent_cli.worker_protocol import CliShellSettings
    from mindroom.constants import RuntimePaths


class _CliShellHandle(Protocol):
    """Identify where one response's minimal shell runs."""

    @property
    def worker_id(self) -> str:
        """Return the worker or local shell identity bound into the response owner."""
        ...


class CliShell(Protocol):
    """One response's minimal Bash, in a dedicated worker or in the primary."""

    @property
    def handle(self) -> _CliShellHandle:
        """Return the identity bound into the response owner."""
        ...

    async def install_grant(self, bridge: TurnToolBridge, grant: CliGrant, *, shell: CliShellSettings) -> None:
        """Bind the response grant before any command runs."""
        ...

    async def invoke_shell(self, function_name: str, arguments: dict[str, object]) -> object:
        """Run one already authorized canonical shell function."""
        ...


class InvalidShellArgumentsError(ValueError):
    """The canonical shell function would not accept these arguments."""


def build_agent_cli_shell(
    shell: CliShellSettings,
    *,
    runtime_paths: RuntimePaths,
    binding: AgentCliShellBinding,
) -> Toolkit:
    """Build the agent's effective shell with the response's CLI environment and output policy."""
    toolkit = shell_tools()(
        base_dir=shell.workspace,
        shell_path_prepend=shell.shell_path_prepend,
        runtime_paths=runtime_paths,
        agent_cli_binding=binding,
    )
    wrap_toolkit_for_output_files(
        toolkit,
        ToolOutputFilePolicy(
            Path(shell.workspace),
            max_bytes=shell.output_max_bytes,
            auto_save_threshold_bytes=shell.output_auto_save_threshold_bytes,
        ),
    )
    return toolkit


async def invoke_agent_cli_shell(toolkit: Toolkit, function_name: str, arguments: dict[str, object]) -> object:
    """Validate arguments against the canonical schema, then run the shell function to completion."""
    function = toolkit.async_functions.get(function_name) or toolkit.functions[function_name]
    entrypoint = function.entrypoint
    assert entrypoint is not None
    function.process_entrypoint()
    try:
        validate_tool_arguments(function_schema(function), arguments)
    except ValueError as exc:
        raise InvalidShellArgumentsError(str(exc)) from exc
    if inspect.iscoroutinefunction(entrypoint):
        return await entrypoint(**arguments)
    # Sync calls must finish before the owning operation's lifetime ends.
    task = asyncio.create_task(asyncio.to_thread(entrypoint, **arguments))
    return await wait_for_future_until_complete(task)
