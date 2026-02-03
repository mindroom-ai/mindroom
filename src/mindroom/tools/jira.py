"""Jira tool configuration."""

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
    from agno.tools.jira import JiraTools


@register_tool_with_metadata(
    name="jira",
    display_name="Jira",
    description="Issue tracking and project management",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaTicketAlt",
    icon_color="text-blue-600",  # Jira blue
    config_fields=[
        ConfigField(
            name="server_url",
            label="Server URL",
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="username",
            label="Username",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="token",
            label="Token",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_get_issue",
            label="Enable Get Issue",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_create_issue",
            label="Enable Create Issue",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_issues",
            label="Enable Search Issues",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_add_comment",
            label="Enable Add Comment",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_add_worklog",
            label="Enable Add Worklog",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["jira"],
    docs_url="https://docs.agno.com/tools/toolkits/others/jira",
)
def jira_tools() -> type[JiraTools]:
    """Return Jira tools for issue tracking and project management."""
    from agno.tools.jira import JiraTools

    return JiraTools
