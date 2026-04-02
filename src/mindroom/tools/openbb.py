"""OpenBB tool configuration."""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, cast

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.openbb import OpenBBTools


def _load_openbb_tools() -> type[OpenBBTools]:
    """Import OpenBB tools without triggering OpenBB's package auto-build in this process."""
    previous_auto_build = os.environ.get("OPENBB_AUTO_BUILD")
    os.environ["OPENBB_AUTO_BUILD"] = "false"
    try:
        module = importlib.import_module("agno.tools.openbb")
    finally:
        if previous_auto_build is None:
            os.environ.pop("OPENBB_AUTO_BUILD", None)
        else:
            os.environ["OPENBB_AUTO_BUILD"] = previous_auto_build
    return cast("type[OpenBBTools]", module.OpenBBTools)


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
            name="obb",
            label="OpenBB Instance",
            type="text",
            required=False,
            default=None,
            description="Optional pre-configured OpenBB instance (advanced usage)",
        ),
        ConfigField(
            name="openbb_pat",
            label="Personal Access Token",
            type="text",
            required=False,
            default=None,
            description="OpenBB PAT for premium data providers. Optional - works without it using yfinance.",
        ),
        ConfigField(
            name="provider",
            label="Data Provider",
            type="text",
            required=False,
            default="yfinance",
            description="Data provider: yfinance, benzinga, fmp, intrinio, polygon, tiingo, or tmx",
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
    return _load_openbb_tools()
