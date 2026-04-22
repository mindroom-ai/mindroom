"""Linear tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.linear import LinearTools


@register_tool_with_metadata(
    name="linear",
    display_name="Linear",
    description="Issue tracking and project management",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiLinear",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/others/linear",
    function_names=(
        "create_issue",
        "get_high_priority_issues",
        "get_issue_details",
        "get_teams_details",
        "get_user_assigned_issues",
        "get_user_details",
        "get_workflow_issues",
        "update_issue",
    ),
)
def linear_tools() -> type[LinearTools]:
    """Return Linear tools for issue tracking and project management."""
    from agno.tools.linear import LinearTools

    return LinearTools
