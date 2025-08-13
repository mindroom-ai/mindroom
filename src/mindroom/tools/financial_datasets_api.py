"""Financial Datasets API tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.financial_datasets import FinancialDatasetsTools


@register_tool_with_metadata(
    name="financial_datasets_api",
    display_name="Financial Datasets API",
    description="Comprehensive financial data API for stocks, financial statements, SEC filings, and cryptocurrency",
    category=ToolCategory.DEVELOPMENT,  # From /tools/toolkits/others/ path
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API key
    setup_type=SetupType.API_KEY,  # Uses API key authentication
    icon="TrendingUp",  # Financial/trending icon
    icon_color="text-green-600",  # Financial green color
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="your_api_key_here",
            description="Financial Datasets API key (can also be set via FINANCIAL_DATASETS_API_KEY env var)",
        ),
        # Financial Statements
        ConfigField(
            name="enable_financial_statements",
            label="Enable Financial Statements",
            type="boolean",
            required=False,
            default=True,
            description="Enable financial statement related functions (income statements, balance sheets, cash flow)",
        ),
        # Company Information
        ConfigField(
            name="enable_company_info",
            label="Enable Company Info",
            type="boolean",
            required=False,
            default=True,
            description="Enable company information related functions",
        ),
        # Market Data
        ConfigField(
            name="enable_market_data",
            label="Enable Market Data",
            type="boolean",
            required=False,
            default=True,
            description="Enable market data related functions (stock prices, earnings, metrics)",
        ),
        # Ownership Data
        ConfigField(
            name="enable_ownership_data",
            label="Enable Ownership Data",
            type="boolean",
            required=False,
            default=True,
            description="Enable ownership data related functions (insider trades, institutional ownership)",
        ),
        # News
        ConfigField(
            name="enable_news",
            label="Enable News",
            type="boolean",
            required=False,
            default=True,
            description="Enable news related functions",
        ),
        # SEC Filings
        ConfigField(
            name="enable_sec_filings",
            label="Enable SEC Filings",
            type="boolean",
            required=False,
            default=True,
            description="Enable SEC filings related functions",
        ),
        # Cryptocurrency
        ConfigField(
            name="enable_crypto",
            label="Enable Crypto",
            type="boolean",
            required=False,
            default=True,
            description="Enable cryptocurrency related functions",
        ),
        # Search
        ConfigField(
            name="enable_search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=True,
            description="Enable search related functions",
        ),
    ],
    dependencies=["requests"],  # Only standard dependency needed
    docs_url="https://docs.agno.com/tools/toolkits/others/financial_datasets",
)
def financial_datasets_api_tools() -> type[FinancialDatasetsTools]:
    """Return Financial Datasets API tools for comprehensive financial data access."""
    from agno.tools.financial_datasets import FinancialDatasetsTools

    return FinancialDatasetsTools
