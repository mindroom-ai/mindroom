"""Atlassian Cloud tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.atlassian import AtlassianTools


@register_tool_with_metadata(
    name="atlassian",
    file_access=ToolFileAccess.NONE,
    display_name="Atlassian",
    description="Search and update Jira issues and Confluence pages as the connected Atlassian user",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="atlassian",
    icon="SiAtlassian",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="site_url",
            label="Site URL",
            type="url",
            required=False,
            default=None,
            placeholder="https://example.atlassian.net",
            description="Atlassian Cloud site to use; needed when the connected account can reach several sites.",
        ),
        ConfigField(
            name="cloud_id",
            label="Cloud ID",
            type="text",
            required=False,
            default=None,
            description="Atlassian cloud ID of the site; takes precedence over the site URL.",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    docs_url="https://docs.mindroom.chat/tools/atlassian/",
    helper_text=(
        "Each user connects their own Atlassian account. "
        "Store the Atlassian OAuth 2.0 (3LO) app client ID and secret in the atlassian_oauth_client credential service."
    ),
    function_names=(
        "jira_search_issues",
        "jira_get_issue",
        "jira_create_issue",
        "jira_update_issue",
        "jira_add_comment",
        "jira_transition_issue",
        "confluence_search",
        "confluence_get_page",
        "confluence_list_attachments",
        "confluence_download_attachment",
        "confluence_create_page",
        "confluence_update_page",
        "confluence_add_comment",
    ),
)
def atlassian_tools() -> type[AtlassianTools]:
    """Return Jira and Confluence Cloud tools for the default Atlassian connection."""
    from mindroom.custom_tools.atlassian import AtlassianTools

    return AtlassianTools
