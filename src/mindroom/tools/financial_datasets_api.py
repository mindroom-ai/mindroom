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
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
    ],
    dependencies=["requests"],  # Only standard dependency needed
    docs_url="https://docs.agno.com/tools/toolkits/others/financial_datasets",
)
def financial_datasets_api_tools() -> type[FinancialDatasetsTools]:
    """Return Financial Datasets API tools for comprehensive financial data access."""
    from agno.tools.financial_datasets import FinancialDatasetsTools

    return FinancialDatasetsTools
