"""Persistent terminal tool registration, with the approval aliases that let the shell's rules judge its typing.

🔒 What the agent types into a terminal runs as shell commands, so ``run_shell_command``'s approval rules judge it as
the call that would run the same text: ``terminal_open`` as a shell no argument describes, so it asks whenever those
rules may; ``terminal_run`` as its command, which runs only on an empty line at the shell's prompt; ``terminal_send``
as what its Enters run, joined to the line the terminal held before (``entered_command``). That earlier line comes
from the call's ``line`` argument, which only the toolkit's trusted preparation fills before the call's approval
(``TerminalTools.offered_function``), and the call types only while the terminal's line is still that one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.terminal.lines import entered_command, keys_to_text
from mindroom.tool_system.declarations import (
    ApprovalAlias,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.custom_tools.terminal import TerminalTools

_SHELL_FUNCTION = "run_shell_command"


def run_text(command: str) -> str:
    """Return what ``terminal_run`` types for ``command``: the command line and Enter."""
    return command.strip("\n") + "\r"


def _judge_open(_arguments: Mapping[str, object]) -> None:
    """Opening a terminal starts a shell, whose commands no argument names."""


def _judge_run(arguments: Mapping[str, object]) -> list[dict[str, object]] | None:
    command = arguments.get("command")
    return entered_command("", run_text(command)) if isinstance(command, str) else None


def _judge_send(arguments: Mapping[str, object]) -> list[dict[str, object]] | None:
    keys = arguments.get("keys")
    if not isinstance(keys, str):
        return None
    match arguments.get("line"):
        case {"typed": str() as typed}:
            return entered_command(typed, keys_to_text(keys))
        case _:
            return entered_command(None, keys_to_text(keys))


@register_tool_with_metadata(
    name="terminal",
    display_name="Terminal",
    description="Persistent terminals in the agent's workspace, which people in the room can watch and type in",
    category=ToolCategory.DEVELOPMENT,
    # It reaches files only through the shell it types into, in the agent's worker.
    file_access=ToolFileAccess.NONE,
    executes_code=True,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.PRIMARY,
    requires_primary_runtime=True,
    icon="SquareTerminal",
    icon_color="text-green-500",
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.RUNTIME_CONFIG,
        ToolManagedInitArg.WORKER_TARGET,
    ),
    docs_url="https://docs.mindroom.chat/tools/execution-and-coding/#terminal",
    function_names=("terminal_open", "terminal_run", "terminal_send", "terminal_read", "terminal_list"),
    approval_aliases={
        "terminal_open": ApprovalAlias(judged_as=_SHELL_FUNCTION, judge=_judge_open),
        "terminal_run": ApprovalAlias(judged_as=_SHELL_FUNCTION, judge=_judge_run),
        "terminal_send": ApprovalAlias(judged_as=_SHELL_FUNCTION, judge=_judge_send),
    },
)
def terminal_tools() -> type[TerminalTools]:
    """Return the persistent terminal toolkit."""
    from mindroom.custom_tools.terminal import TerminalTools

    return TerminalTools
