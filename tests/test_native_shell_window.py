"""A standard agent's native shell command runs inside its CLI window only when the shell tool offers it ungated."""

from __future__ import annotations

from dataclasses import replace

import pytest

from mindroom.agent_cli.cli_shell_agent import wrap_native_shell_window
from mindroom.tool_system.agno_toolkits import AdaptedToolkit
from mindroom.tool_system.tool_function import ToolFunction


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
