"""Yahoo Finance tool configuration."""

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
    from agno.tools.yfinance import YFinanceTools


@register_tool_with_metadata(
    name="yfinance",
    display_name="Yahoo Finance",
    description="Get financial data and stock information from Yahoo Finance",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaChartLine",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="session",
            label="Session",
            type="text",
            required=False,
            default=None,
        ),
    ],
    dependencies=["yfinance"],
    docs_url="https://docs.agno.com/tools/toolkits/others/yfinance",
)
def yfinance_tools() -> type[YFinanceTools]:
    """Return Yahoo Finance tools for financial data."""
    from agno.tools.yfinance import YFinanceTools

    return YFinanceTools
