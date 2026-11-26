"""Matrix desktop tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.shell.execution import shell_args_to_command
from mindroom.tool_system.declarations import (
    ApprovalAlias,
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.custom_tools.desktop import DesktopTools


def _judge_desktop_shell_approval(arguments: Mapping[str, object]) -> list[dict[str, object]] | None:
    """Map ``run_desktop_shell`` calls to existing ``desktop(action="run_shell", ...)`` approval rules."""
    try:
        command = shell_args_to_command(arguments.get("args"))
    except ValueError:
        return None
    cwd = arguments.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return None
    return [{"action": "run_shell", "command": command, **({"cwd": cwd} if isinstance(cwd, str) else {})}]


@register_tool_with_metadata(
    name="desktop",
    file_access=ToolFileAccess.NONE,
    display_name="Matrix Desktop",
    description="Operate the user's paired local computer: control allowlisted apps, read selected folders, and run locally approved shell commands",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    requires_primary_runtime=True,
    requires_room_context=True,
    icon="MonitorUp",
    icon_color="text-cyan-500",
    config_fields=[
        ConfigField(
            name="timeout_seconds",
            label="Command Timeout Seconds",
            type="number",
            required=False,
            default=30,
            description=(
                "Short-lived command and response timeout, from 1 to 120 seconds. Shell command starts always "
                "wait up to 120 seconds for approval on the computer."
            ),
        ),
    ],
    docs_url="https://docs.mindroom.chat/tools/desktop/",
    helper_text=(
        "Ask the requester to send `/desktop setup` in a private Matrix room containing only them and one "
        "Desktop-enabled agent."
    ),
    function_names=("desktop", "run_desktop_shell"),
    approval_aliases={"run_desktop_shell": ApprovalAlias(judged_as="desktop", judge=_judge_desktop_shell_approval)},
    managed_init_args=(ToolManagedInitArg.CREDENTIALS_MANAGER, ToolManagedInitArg.WORKER_TARGET),
)
def desktop_tools() -> type[DesktopTools]:
    """Return the Matrix desktop toolkit."""
    from mindroom.custom_tools.desktop import DesktopTools

    return DesktopTools
