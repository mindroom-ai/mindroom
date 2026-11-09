"""OpenBB tool configuration."""

from __future__ import annotations

import importlib
from importlib import util as importlib_util
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.file_locks import advisory_file_lock
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.openbb import OpenBBTools


def _load_openbb_tools() -> type[OpenBBTools]:
    """Import OpenBB tools, letting one process at a time run OpenBB's package build on first import.

    OpenBB builds its command package into its own install on first import and fails the import
    of any other process that starts while the build holds its non-blocking lock, so every process
    using that install waits on a lock beside it instead.
    """
    spec = importlib_util.find_spec("openbb")
    if spec is None or spec.origin is None:
        msg = "No module named 'openbb'"
        raise ModuleNotFoundError(msg, name="openbb")
    with advisory_file_lock(Path(spec.origin).parent / ".mindroom-build.lock"):
        module = importlib.import_module("agno.tools.openbb")
    return cast("type[OpenBBTools]", module.OpenBBTools)


@register_tool_with_metadata(
    name="openbb",
    file_access=ToolFileAccess.NONE,
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
            env=("OPENBB_PAT",),
            label="Personal Access Token",
            type="password",
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
    dependencies=["openbb", "polars", "pyarrow"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#openbb",
    function_names=(
        "get_company_news",
        "get_company_profile",
        "get_price_targets",
        "get_stock_price",
        "search_company_symbol",
    ),
)
def openbb_tools() -> type[OpenBBTools]:
    """Return OpenBB tools for financial data."""
    return _load_openbb_tools()
