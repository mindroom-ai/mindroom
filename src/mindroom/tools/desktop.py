"""Matrix desktop-device tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolStatus
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
    runtime_config_required=True,
    icon="MonitorUp",
    icon_color="text-cyan-500",
    config_fields=[
        ConfigField(
            name="device_user_id",
            label="Desktop Matrix User ID",
            type="text",
            description="Dedicated Matrix account used by the local desktop bridge.",
        ),
        ConfigField(
            name="device_id",
            label="Desktop Matrix Device ID",
            type="text",
            description="Exact device ID printed by 'mindroom desktop login'.",
        ),
        ConfigField(
            name="device_ed25519",
            label="Desktop Device Fingerprint",
            type="text",
            description="Exact Ed25519 fingerprint printed by 'mindroom desktop login'.",
        ),
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
    function_names=("desktop",),
)
def desktop_tools() -> type[DesktopTools]:
    """Return the Matrix desktop toolkit."""
    from mindroom.custom_tools.desktop import DesktopTools

    return DesktopTools
