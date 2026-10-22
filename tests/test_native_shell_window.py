"""A standard agent's native shell command runs inside its CLI window only when the shell tool offers it ungated."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from mindroom.agent_cli.cli_shell_agent import wrap_native_shell_window
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.protocol import ToolCallOperation
from mindroom.agent_cli.session import CliTurnOwner, TurnToolRegistry
from mindroom.agent_cli.shell_contract import AgentCliShellEnv, current_agent_cli_shell_env
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.minimal_agent_fixtures import cli_window, settled_receipt
from tests.test_agent_tool_calls import _catalog, _toolkit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.tool_system.tool_access import ToolKey

# The commands here never dial the CLI routes; they call the owner directly.
_SHELL_ENV = AgentCliShellEnv("http://127.0.0.1:9", "test-grant")


async def _run_shell_command(command: str) -> str:
    return f"ran {command}"


def _function(name: str, *, requires_confirmation: bool | None = None) -> ToolFunction:
    return ToolFunction(
        name=name,
        description=None,
        parameters={"type": "object", "properties": {}, "required": []},
        requires_confirmation=requires_confirmation,
        entrypoint=_run_shell_command,
    )


def _toolkits(*, requires_confirmation: bool | None = None) -> tuple[AdaptedToolkit, ...]:
    return (
        AdaptedToolkit(name="calculator", functions=(_function("add"),), instructions=None),
        AdaptedToolkit(
            name="shell",
            functions=(_function("run_shell_command", requires_confirmation=requires_confirmation),),
            instructions=None,
        ),
    )


@pytest.mark.asyncio
async def test_only_the_shell_command_is_windowed_and_runs_as_before_without_a_cli() -> None:
    """The offered shell command gets the window wrapper, which calls the command directly outside a CLI."""
    toolkits = _toolkits()

    windowed = wrap_native_shell_window(toolkits, {"add": "calculator", "run_shell_command": "shell"})

    assert windowed is not None
    assert windowed[0] == toolkits[0]
    [shell] = windowed[1].functions
    assert shell.entrypoint is not _run_shell_command
    assert shell == replace(toolkits[1].functions[0], entrypoint=shell.entrypoint)
    assert await shell.entrypoint(command="ls") == "ran ls"


def test_a_gated_or_foreign_shell_command_stays_unwindowed() -> None:
    """An approved command resumes without this response's CLI, and only the shell tool's own command is windowed."""
    assert wrap_native_shell_window(_toolkits(requires_confirmation=True), {"run_shell_command": "shell"}) is None
    assert wrap_native_shell_window(_toolkits(), {"run_shell_command": "plugin"}) is None
    assert wrap_native_shell_window(_toolkits()[:1], {"add": "calculator"}) is None


async def _allow(_key: ToolKey, _arguments: dict[str, object]) -> None:
    return None


async def _windowed_shell(
    tmp_path: Path,
    run_shell_command: Callable[..., Awaitable[str]],
) -> tuple[ToolFunction, LiveTurnTools]:
    """Return the windowed shell command and a response CLI owner whose catalog offers that command."""
    windowed = wrap_native_shell_window((_toolkit("shell", run_shell_command),), {"run_shell_command": "shell"})
    assert windowed is not None
    catalog = await _catalog(tmp_path, windowed)
    catalog.store = create_state_storage("helper", tmp_path, session_table="sessions")
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run"),
        catalog=catalog,
        authorize=_allow,
    )
    owner.shell_env = _SHELL_ENV
    [shell] = windowed[0].functions
    return shell, owner


@pytest.mark.asyncio
async def test_with_the_response_cli_bound_the_shell_command_runs_in_its_own_window(tmp_path: Path) -> None:
    """The command runs in a window of its own, with the response's CLI environment exported to it."""
    seen: list[AgentCliShellEnv | None] = []

    async def run_shell_command(args: str) -> str:
        seen.append(current_agent_cli_shell_env())
        return f"ran {args}"

    shell, owner = await _windowed_shell(tmp_path, run_shell_command)
    assert shell.entrypoint is not None
    async with response_cli_lifetime() as lifetime:
        lifetime.register(owner, TurnToolRegistry())
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            assert await shell.entrypoint(args="ls") == "ran ls"

    [env] = seen
    assert env is not None
    assert env.window is not None
    assert env.window.startswith("native-")
    assert env == replace(_SHELL_ENV, window=env.window)
    assert current_agent_cli_shell_env() is None


@pytest.mark.asyncio
async def test_a_shell_command_the_cli_dispatches_keeps_its_parent_command_s_window(tmp_path: Path) -> None:
    """A shell command called through the CLI from inside a windowed command opens no window of its own."""
    windows: dict[str, str | None] = {}

    async def run_shell_command(args: str) -> str:
        windows[args] = cli_window()
        if args == "outer":
            queued = await owner.operation(
                window=cli_window(),
                operation=ToolCallOperation(
                    operation="tools.call",
                    call_id=uuid4(),
                    toolkit="shell",
                    function="run_shell_command",
                    arguments={"args": "nested"},
                ),
            )
            receipt = await settled_receipt(owner, str(queued["call_id"]))
            assert receipt["status"] == "completed", receipt
        return f"ran {args}"

    shell, owner = await _windowed_shell(tmp_path, run_shell_command)
    assert shell.entrypoint is not None
    async with response_cli_lifetime() as lifetime:
        lifetime.register(owner, TurnToolRegistry())
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            assert await shell.entrypoint(args="outer") == "ran outer"

    assert windows["outer"] is not None
    assert windows["outer"].startswith("native-")
    assert windows["nested"] == windows["outer"]
