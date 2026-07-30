"""Matrix desktop-device tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolManagedInitArg, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.desktop import DesktopTools


@register_tool_with_metadata(
    name="desktop",
    display_name="Matrix Desktop",
    description="Operate exact locally allowlisted applications through accessibility state over Matrix encryption",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
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
            description="Short-lived command and response timeout, from 1 to 120 seconds.",
        ),
    ],
    docs_url="https://docs.mindroom.chat/tools/desktop/",
    helper_text=(
        "Ask the requester to send `!desktop setup` in a private Matrix room containing only them and one "
        "Desktop-enabled agent, plus the router when it serves the command."
    ),
    function_names=("desktop",),
    managed_init_args=(ToolManagedInitArg.CREDENTIALS_MANAGER, ToolManagedInitArg.WORKER_TARGET),
)
def desktop_tools() -> type[DesktopTools]:
    """Return the Matrix desktop toolkit."""
    from mindroom.custom_tools.desktop import DesktopTools

    return DesktopTools
