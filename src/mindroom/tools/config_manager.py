"""Config Manager tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.config_manager import ConfigManagerTools


@register_tool_with_metadata(
    name="config_manager",
    display_name="Config Manager",
    description="Inspect and control the full MindRoom configuration, including agents, plugins, and authorization",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Settings",
    icon_color="text-purple-500",
    config_fields=[],
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    dependencies=["agno", "pydantic", "pyyaml"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=("get_info", "manage_config", "manage_agent", "manage_team"),
)
def config_manager_tools() -> type[ConfigManagerTools]:
    """Return full configuration-management tools."""
    from mindroom.custom_tools.config_manager import ConfigManagerTools

    return ConfigManagerTools
