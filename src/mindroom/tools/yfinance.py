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
        # Basic stock data
        ConfigField(
            name="stock_price",
            label="Stock Price",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting current stock prices",
        ),
        ConfigField(
            name="company_info",
            label="Company Info",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting company information and overview",
        ),
        ConfigField(
            name="stock_fundamentals",
            label="Stock Fundamentals",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting fundamental data for stocks",
        ),
        ConfigField(
            name="historical_prices",
            label="Historical Prices",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting historical stock prices",
        ),
        # Financial statements and analysis
        ConfigField(
            name="income_statements",
            label="Income Statements",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting income statements",
        ),
        ConfigField(
            name="key_financial_ratios",
            label="Key Financial Ratios",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting key financial ratios",
        ),
        # Market data and recommendations
        ConfigField(
            name="analyst_recommendations",
            label="Analyst Recommendations",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting analyst recommendations",
        ),
        ConfigField(
            name="company_news",
            label="Company News",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting company news and press releases",
        ),
        ConfigField(
            name="technical_indicators",
            label="Technical Indicators",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting technical indicators",
        ),
        # Enable all features
        ConfigField(
            name="enable_all",
            label="Enable All",
            type="boolean",
            required=False,
            default=False,
            description="Enable all available Yahoo Finance tools",
        ),
    ],
    dependencies=["yfinance"],
    docs_url="https://docs.agno.com/tools/toolkits/others/yfinance",
)
def yfinance_tools() -> type[YFinanceTools]:
    """Return Yahoo Finance tools for financial data."""
    from agno.tools.yfinance import YFinanceTools

    return YFinanceTools
