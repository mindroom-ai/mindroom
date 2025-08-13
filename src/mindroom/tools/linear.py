"""Linear tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
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
    icon="FaLinear",
    icon_color="text-purple-600",
    config_fields=[
        # User operations
        ConfigField(
            name="get_user_details",
            label="Get User Details",
            type="boolean",
            required=False,
            default=True,
            description="Enable fetching authenticated user details",
        ),
        ConfigField(
            name="get_teams_details",
            label="Get Teams Details",
            type="boolean",
            required=False,
            default=True,
            description="Enable fetching team details for the authenticated user",
        ),
        # Issue operations
        ConfigField(
            name="get_issue_details",
            label="Get Issue Details",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving details of specific issues by ID",
        ),
        ConfigField(
            name="create_issue",
            label="Create Issue",
            type="boolean",
            required=False,
            default=True,
            description="Enable creating new issues within teams and projects",
        ),
        ConfigField(
            name="update_issue",
            label="Update Issue",
            type="boolean",
            required=False,
            default=True,
            description="Enable updating issue titles and states",
        ),
        ConfigField(
            name="get_user_assigned_issues",
            label="Get User Assigned Issues",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving issues assigned to specific users",
        ),
        ConfigField(
            name="get_workflow_issues",
            label="Get Workflow Issues",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving issues within specific workflow states",
        ),
        ConfigField(
            name="get_high_priority_issues",
            label="Get High Priority Issues",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving issues with high priority (priority <= 2)",
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/others/linear",
)
def linear_tools() -> type[LinearTools]:
    """Return Linear tools for issue tracking and project management."""
    from agno.tools.linear import LinearTools

    return LinearTools
