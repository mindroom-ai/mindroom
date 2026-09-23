"""Report Publishing tool metadata registration."""

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
)
from mindroom.tool_system.registration import register_builtin_tool_metadata

register_builtin_tool_metadata(
    ToolMetadata(
        name="report_publishing",
        display_name="Report Publishing",
        description="Share reports through public links that you can revoke",
        category=ToolCategory.PRODUCTIVITY,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        requires_room_context=True,
        consumes_workspace_paths=True,
        icon="Share2",
        icon_color="text-emerald-500",
        config_fields=[],
        dependencies=[],
        function_names=(
            "publish_report",
            "revoke_public_report",
        ),
    ),
)
