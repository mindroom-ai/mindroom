"""Yahoo Finance tool configuration."""

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
            name="enable_stock_price",
            label="Enable Stock Price",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_company_info",
            label="Enable Company Info",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_stock_fundamentals",
            label="Enable Stock Fundamentals",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_income_statements",
            label="Enable Income Statements",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_key_financial_ratios",
            label="Enable Key Financial Ratios",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_analyst_recommendations",
            label="Enable Analyst Recommendations",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_company_news",
            label="Enable Company News",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_technical_indicators",
            label="Enable Technical Indicators",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_historical_prices",
            label="Enable Historical Prices",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
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
