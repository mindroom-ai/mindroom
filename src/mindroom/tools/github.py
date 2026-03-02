"""Tools registry for all available Agno tools.

This module provides a centralized registry for all tools that can be used by agents.
Tools are registered by string name and can be instantiated dynamically when loading agents.
"""

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
    from agno.tools.github import GithubTools


@register_tool_with_metadata(
    name="github",
    display_name="GitHub",
    description="Repository and issue management",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiGithub",
    icon_color="text-gray-800",  # GitHub black
    config_fields=[
        ConfigField(
            name="access_token",
            label="Access Token",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default=None,
        ),
    ],
    dependencies=["PyGithub"],
    docs_url="https://docs.agno.com/tools/toolkits/others/github",
)
def github_tools() -> type[GithubTools]:
    """Return GitHub tools for repository management."""
    from agno.tools.github import GithubTools

    return GithubTools
