"""OpenBB tool configuration."""

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
    from agno.tools.openbb import OpenBBTools


@register_tool_with_metadata(
    name="openbb",
    display_name="OpenBB",
    description="Financial data and market analysis tools",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="TrendingUp",
    icon_color="text-green-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="openbb_pat",
            label="OpenBB Personal Access Token",
            type="password",
            required=False,
            placeholder="your_personal_access_token",
            description="OpenBB personal access token for enhanced data access (can also be set via OPENBB_PAT env var)",
        ),
        ConfigField(
            name="provider",
            label="Data Provider",
            type="text",
            required=False,
            default="yfinance",
            placeholder="yfinance",
            description="Data provider to use (benzinga, fmp, intrinio, polygon, tiingo, tmx, yfinance)",
        ),
        # Feature flags grouped by functionality
        # Stock data operations
        ConfigField(
            name="stock_price",
            label="Stock Price",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting current stock prices and quotes",
        ),
        ConfigField(
            name="search_symbols",
            label="Search Symbols",
            type="boolean",
            required=False,
            default=False,
            description="Enable searching for company ticker symbols by name",
        ),
        # Company information
        ConfigField(
            name="company_news",
            label="Company News",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting latest company news and press releases",
        ),
        ConfigField(
            name="company_profile",
            label="Company Profile",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting company profiles and overviews",
        ),
        # Analysis tools
        ConfigField(
            name="price_targets",
            label="Price Targets",
            type="boolean",
            required=False,
            default=False,
            description="Enable getting consensus price targets and analyst recommendations",
        ),
    ],
    dependencies=["openbb"],
    docs_url="https://docs.agno.com/tools/toolkits/others/openbb",
    helper_text="Note: OpenBB has strict dependency requirements. Install with `pip install openbb` in a separate environment if needed.",
)
def openbb_tools() -> type[OpenBBTools]:
    """Return OpenBB financial data and analysis tools."""
    try:
        from agno.tools.openbb import OpenBBTools

        return OpenBBTools
    except ImportError as e:
        msg = "OpenBB package not installed. Please install with: pip install openbb"
        raise ImportError(msg) from e
