"""Visualization tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.visualization import VisualizationTools


@register_tool_with_metadata(
    name="visualization",
    file_access=ToolFileAccess.NONE,
    display_name="Visualization",
    description="Create bar charts, line charts, pie charts, scatter plots, and histograms using matplotlib",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaChartBar",
    icon_color="text-indigo-500",
    config_fields=[
        ConfigField(
            name="output_dir",
            label="Output Directory",
            type="text",
            required=False,
            default="charts",
            description="Directory inside the agent workspace where charts are saved",
        ),
        bool_config_field("enable_create_bar_chart", "Enable Bar Chart"),
        bool_config_field("enable_create_line_chart", "Enable Line Chart"),
        bool_config_field("enable_create_pie_chart", "Enable Pie Chart"),
        bool_config_field("enable_create_scatter_plot", "Enable Scatter Plot"),
        bool_config_field("enable_create_histogram", "Enable Histogram"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["matplotlib"],
    docs_url="https://docs.mindroom.chat/tools/execution-and-coding/#visualization",
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,),
    function_names=("create_chart",),
)
def visualization_tools() -> type[VisualizationTools]:
    """Return Visualization tools that save charts only inside the agent workspace."""
    from mindroom.custom_tools.visualization import VisualizationTools

    return VisualizationTools
