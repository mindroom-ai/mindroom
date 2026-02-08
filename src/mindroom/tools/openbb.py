"""OpenBB tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.openbb import OpenBBTools


@register_tool_with_metadata(
    name="openbb",
    display_name="OpenBB",
    description="Get stock prices, company news, price targets, and company profiles from OpenBB financial platform",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaChartArea",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="openbb_pat",
            label="Personal Access Token",
            type="password",
            required=False,
            default=None,
            description="OpenBB PAT for premium data providers (falls back to OPENBB_PAT env var). Optional - works without it using yfinance.",
        ),
        ConfigField(
            name="provider",
            label="Data Provider",
            type="select",
            required=False,
            default="yfinance",
            options=[
                {"label": "Yahoo Finance", "value": "yfinance"},
                {"label": "Benzinga", "value": "benzinga"},
                {"label": "FMP", "value": "fmp"},
                {"label": "Intrinio", "value": "intrinio"},
                {"label": "Polygon", "value": "polygon"},
                {"label": "Tiingo", "value": "tiingo"},
                {"label": "TMX", "value": "tmx"},
            ],
        ),
        ConfigField(
            name="enable_get_stock_price",
            label="Enable Get Stock Price",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_company_symbol",
            label="Enable Search Company Symbol",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_get_company_news",
            label="Enable Get Company News",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_get_company_profile",
            label="Enable Get Company Profile",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_get_price_targets",
            label="Enable Get Price Targets",
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
    ],
    dependencies=["openbb"],
    docs_url="https://docs.agno.com/tools/toolkits/others/openbb",
)
def openbb_tools() -> type[OpenBBTools]:
    """Return OpenBB tools for financial data."""
    from agno.tools.openbb import OpenBBTools

    return OpenBBTools
