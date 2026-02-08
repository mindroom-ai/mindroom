"""Visualization tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.visualization import VisualizationTools


@register_tool_with_metadata(
    name="visualization",
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
)
def visualization_tools() -> type[VisualizationTools]:
    """Return Visualization tools for creating charts and plots."""
    from agno.tools.visualization import VisualizationTools

    return VisualizationTools
