"""Usage statistics toolkit registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.usage_stats import UsageStatsTools


@register_tool_with_metadata(
    name="usage_stats",
    display_name="Usage Statistics",
    description="Inspect retained token usage without modifying session storage",
    category=ToolCategory.INFORMATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.PRIMARY,
    icon="FaChartBar",
    icon_color="text-cyan-500",
    dependencies=["agno"],
    function_names=("get_my_usage", "get_all_usage"),
    managed_init_args=(ToolManagedInitArg.AGENT_NAME,),
    agent_override_fields=[
        ConfigField(
            name="admin_scope",
            label="Enable Admin Scope",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
)
def usage_stats_tools() -> type[UsageStatsTools]:
    """Return retained usage statistics tools."""
    from mindroom.custom_tools.usage_stats import UsageStatsTools

    return UsageStatsTools
