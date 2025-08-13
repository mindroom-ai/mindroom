"""Pandas tools configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.pandas import PandasTools


@register_tool_with_metadata(
    name="pandas",
    display_name="Pandas",
    description="Advanced data manipulation and analysis",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Database",
    icon_color="text-blue-600",
    config_fields=[],
    dependencies=["pandas"],
    docs_url="https://docs.agno.com/tools/toolkits/database/pandas",
)
def pandas_tools() -> type[PandasTools]:
    """Return Pandas tools for data manipulation and analysis."""
    from agno.tools.pandas import PandasTools

    return PandasTools
