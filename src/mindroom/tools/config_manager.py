"""Config Manager tool configuration."""

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
    from mindroom.custom_tools.config_manager import ConfigManagerTools


@register_tool_with_metadata(
    name="config_manager",
    display_name="Config Manager",
    description="Build and manage MindRoom agents with expert knowledge of the system",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Settings",
    icon_color="text-purple-500",
    config_fields=[
        ConfigField(
            name="config_path",
            label="Configuration Path",
            type="text",
            required=False,
            description="Path to the configuration file (uses default if not specified)",
        ),
    ],
    dependencies=["agno", "pydantic", "yaml"],
    docs_url="https://github.com/mindroom-ai/mindroom",
)
def config_manager_tools() -> type[ConfigManagerTools]:
    """Return config manager tools for agent building."""
    from mindroom.custom_tools.config_manager import ConfigManagerTools

    return ConfigManagerTools
