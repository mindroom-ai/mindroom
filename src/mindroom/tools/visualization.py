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
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.tools.agno_compat_visualization import WorkspaceVisualizationTools


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
        ConfigField(
            name="enable_create_bar_chart",
            label="Enable Bar Chart",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_create_line_chart",
            label="Enable Line Chart",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_create_pie_chart",
            label="Enable Pie Chart",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_create_scatter_plot",
            label="Enable Scatter Plot",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_create_histogram",
            label="Enable Histogram",
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
    dependencies=["matplotlib"],
    docs_url="https://docs.agno.com/tools/toolkits/others/visualization",
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,),
    function_names=(
        "create_bar_chart",
        "create_histogram",
        "create_line_chart",
        "create_pie_chart",
        "create_scatter_plot",
    ),
)
def visualization_tools() -> type[WorkspaceVisualizationTools]:
    """Return Visualization tools that save charts only inside the agent workspace."""
    from mindroom.tools.agno_compat_visualization import WorkspaceVisualizationTools

    return WorkspaceVisualizationTools
