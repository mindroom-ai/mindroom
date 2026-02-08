"""Bitbucket tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.bitbucket import BitbucketTools


@register_tool_with_metadata(
    name="bitbucket",
    display_name="Bitbucket",
    description="Manage Bitbucket repositories, pull requests, commits, and issues",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="FaBitbucket",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="username",
            label="Username",
            type="text",
            required=True,
            placeholder="Bitbucket username",
            description="Bitbucket username (falls back to BITBUCKET_USERNAME env var)",
        ),
        ConfigField(
            name="password",
            label="App Password",
            type="password",
            required=False,
            description="App password (falls back to BITBUCKET_PASSWORD env var). Use either this or token.",
        ),
        ConfigField(
            name="token",
            label="Token",
            type="password",
            required=False,
            description="Access token (falls back to BITBUCKET_TOKEN env var). Use either this or password.",
        ),
        ConfigField(
            name="workspace",
            label="Workspace",
            type="text",
            required=True,
            placeholder="my-workspace",
        ),
        ConfigField(
            name="repo_slug",
            label="Repository Slug",
            type="text",
            required=True,
            placeholder="my-repo",
        ),
        ConfigField(
            name="server_url",
            label="Server URL",
            type="url",
            required=False,
            default="api.bitbucket.org",
        ),
        ConfigField(
            name="api_version",
            label="API Version",
            type="text",
            required=False,
            default="2.0",
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/others/bitbucket",
    helper_text="Create an app password at [Bitbucket Settings](https://bitbucket.org/account/settings/app-passwords/)",
)
def bitbucket_tools() -> type[BitbucketTools]:
    """Return Bitbucket tools for repository management."""
    from agno.tools.bitbucket import BitbucketTools

    return BitbucketTools
