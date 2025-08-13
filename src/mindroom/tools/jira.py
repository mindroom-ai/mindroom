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
        # Authentication
        ConfigField(
            name="server_url",
            label="Server URL",
            type="url",
            required=True,
            placeholder="https://your-domain.atlassian.net",
            description="The URL of the JIRA server (can also be set via JIRA_SERVER_URL env var)",
        ),
        ConfigField(
            name="username",
            label="Username",
            type="text",
            required=False,
            placeholder="your.email@company.com",
            description="The JIRA username for authentication (can also be set via JIRA_USERNAME env var)",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            placeholder="your-password",
            description="The JIRA password for authentication (can also be set via JIRA_PASSWORD env var)",
        ),
        ConfigField(
            name="token",
            label="API Token",
            type="password",
            required=False,
            placeholder="your-api-token",
            description="The JIRA API token for authentication (can also be set via JIRA_TOKEN env var)",
        ),
    ],
    dependencies=["jira"],
    docs_url="https://docs.agno.com/tools/toolkits/others/jira",
)
def jira_tools() -> type[JiraTools]:
    """Return Jira tools for issue tracking and project management."""
    from agno.tools.jira import JiraTools

    return JiraTools
