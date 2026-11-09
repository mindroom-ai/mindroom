"""Persistent terminal tool registration; ``TerminalTools`` says which agents may type."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess, ToolManagedInitArg
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.terminal import TerminalTools


@register_tool_with_metadata(
    name="terminal",
    display_name="Terminal",
    description="Persistent terminals in the agent's workspace, which people in the room can watch and type in",
    category=ToolCategory.DEVELOPMENT,
    # It reaches files only through the shell it types into, in the agent's worker.
    file_access=ToolFileAccess.NONE,
    executes_code=True,
    requires_primary_runtime=True,
    icon="SquareTerminal",
    icon_color="text-green-500",
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    docs_url="https://docs.mindroom.chat/tools/execution-and-coding/#terminal",
    function_names=("terminal_open", "terminal_run", "terminal_send", "terminal_read", "terminal_list"),
)
def terminal_tools() -> type[TerminalTools]:
    """Return the persistent terminal toolkit."""
    from mindroom.custom_tools.terminal import TerminalTools

    return TerminalTools
