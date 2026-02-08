"""ClickUp tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.clickup import ClickUpTools


@register_tool_with_metadata(
    name="clickup",
    display_name="ClickUp",
    description="Manage tasks, spaces, and lists in ClickUp project management",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiClickup",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=True,
            placeholder="ClickUp API key",
            description="API key from ClickUp (falls back to CLICKUP_API_KEY env var)",
        ),
        ConfigField(
            name="master_space_id",
            label="Master Space ID",
            type="text",
            required=True,
            placeholder="ClickUp space ID",
            description="ID of the master space to work with (falls back to MASTER_SPACE_ID env var)",
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/others/clickup",
    helper_text="Get your API key from [ClickUp Settings > Apps](https://app.clickup.com/settings/apps)",
)
def clickup_tools() -> type[ClickUpTools]:
    """Return ClickUp tools for project management."""
    from agno.tools.clickup import ClickUpTools

    return ClickUpTools
