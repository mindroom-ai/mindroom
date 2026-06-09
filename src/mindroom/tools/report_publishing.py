"""Report Publishing tool metadata registration."""

from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
    register_builtin_tool_metadata,
)

register_builtin_tool_metadata(
    ToolMetadata(
        name="report_publishing",
        display_name="Report Publishing",
        description="Publish authorized report artifacts through revocable public links",
        category=ToolCategory.PRODUCTIVITY,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
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
